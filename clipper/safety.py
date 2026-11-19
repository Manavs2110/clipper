"""Content-safety scan + per-platform rules.

scan()      -> raw findings for one clip (nudity, slurs, profanity, music risk, ...)
evaluate()  -> {"verdict": "pass"|"warn"|"block", "reasons": [...]} for one platform

"""
import os
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .wordlists import PROFANITY, SLURS, SUGGESTIVE

PLATFORMS = ("youtube", "instagram", "facebook", "twitch")
EXPOSED = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
           "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
COVERED = {"FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED"}
Word = Tuple[float, float, str]


def _rx(words: List[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


RX_SLUR, RX_PROF, RX_SUGG = _rx(SLURS), _rx(PROFANITY), _rx(SUGGESTIVE)


@lru_cache(maxsize=1)
def _detector():
    from nudenet import NudeDetector

    return NudeDetector()


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return float(out.strip() or 0)


def mean_volume(path: Path) -> Optional[float]:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else None


def scan_nudity(path: Path, fps: float = 2.0) -> Dict:
    """Sample frames and run NudeNet. Exposed = explicit nudity, covered = suggestive."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf", f"fps={fps},scale=640:-2",
                        "-q:v", "3", f"{tmp}/f_%04d.jpg"], check=True)
        frames = sorted(Path(tmp).glob("*.jpg"))
        exposed, covered, top = [], 0, 0.0
        for i, f in enumerate(frames):
            hits = _detector().detect(str(f))
            ex = [h["score"] for h in hits if h["class"] in EXPOSED]
            if ex:
                top = max(top, max(ex))
                if max(ex) >= 0.35:
                    exposed.append(round(i / fps, 1))
            if any(h["class"] in COVERED and h["score"] >= 0.5 for h in hits):
                covered += 1
    return {"frames": len(frames), "exposed_at": exposed, "exposed_max": round(top, 2), "covered_frames": covered}


def scan(video: Path, words: List[Word], title: str, censor: Dict, nudity: Optional[Dict] = None) -> Dict:
    """Scan the *censored* video: anything found here got past the blur/beep step."""
    text = " ".join(w[2] for w in words)
    dur = probe_duration(video)
    speech = sum(e - s for s, e, _ in words)
    return {
        "duration": round(dur, 1),
        "censor": censor,
        "nudity_after_blur": nudity or scan_nudity(video, fps=4),
        "title_slurs": sorted({m.group(0).lower() for m in RX_SLUR.finditer(title)}),
        "early_profanity": [w[2] for w in words if w[0] < 7 and RX_PROF.search(w[2])],
        "suggestive_keywords": sorted({m.group(0).lower() for m in RX_SUGG.finditer(f"{title} {text}")}),
        "speech_ratio": round(speech / dur, 2) if dur else 0,
        "mean_volume_db": mean_volume(video),
    }


def allow_blurred_nudity() -> bool:
    from dotenv import dotenv_values
    from .config import ROOT

    v = dotenv_values(ROOT / ".env").get("ALLOW_BLURRED_NUDITY") or os.getenv("ALLOW_BLURRED_NUDITY") or "false"
    return v.strip().lower() == "true"


def evaluate(r: Dict, platform: str) -> Dict:
    block, warn = [], []
    n, c = r["nudity_after_blur"], r["censor"]
    leaked = n["exposed_max"] >= 0.5 or len(n["exposed_at"]) >= 2
    blurred = c["blurred_seconds"] > 0
    suggestive = blurred or n["covered_frames"] >= 3 or bool(r["suggestive_keywords"])
    music = r["speech_ratio"] < 0.25 and (r["mean_volume_db"] or -99) > -28

    if leaked:
        block.append(f"Nudity still visible after blurring (at {', '.join(map(str, n['exposed_at'][:4]))}s). "
                     "Blur missed it, so this can't go out")
    if r["title_slurs"]:
        block.append(f"Slur in the title: {', '.join(r['title_slurs'])}. Edit the title")
    if platform in ("youtube", "instagram") and r["duration"] > 180:
        block.append("Longer than 3 min: too long for Shorts/Reels")
    if platform == "facebook" and not 3 <= r["duration"] <= 90:
        block.append("Facebook Reels must be 3-90 seconds")

    if blurred and not allow_blurred_nudity():
        block.append(f"Nudity detected ({c['blurred_seconds']}s, blurred). Nudity clips are switched off "
                     "(ALLOW_BLURRED_NUDITY=false in .env)")
    elif blurred:
        risk = {"youtube": "Blurred sexual content in a Short can still be removed or get a strike",
                "instagram": "Meta may still limit reach or remove blurred sexual content",
                "facebook": "Meta may still limit reach or remove blurred sexual content",
                "twitch": "Twitch restricts sexual-focus content even when blurred"}[platform]
        warn.append(f"Nudity blurred ({c['blurred_seconds']}s). {risk}")
    elif suggestive:
        warn.append(f"Sexually suggestive ({', '.join(r['suggestive_keywords']) or 'revealing clothing'})")
    if c["beeped_slurs"]:
        warn.append(f"{c['beeped_slurs']} slur(s) beeped and masked in captions. Listen to check")
    if platform == "youtube" and r["early_profanity"]:
        warn.append(f"Swearing in first 7s ({', '.join(r['early_profanity'][:3])}): limited ads")
    if music:
        warn.append({"youtube": "Probably music: Content ID claim likely",
                     "instagram": "Probably music: Meta may mute the audio or block the Reel",
                     "facebook": "Probably music: Meta may mute the audio or block the Reel",
                     "twitch": "Probably music: DMCA strikes lead to bans. Only upload if sure"}[platform])

    verdict = "block" if block else "warn" if warn else "pass"
    return {"verdict": verdict, "reasons": block + warn}


def platform_meta(title: str, credit: str, verdicts: Dict[str, Dict]) -> Dict:
    tags = "#gta6 #gtavi #gta #gaming"
    base = {
        "youtube": {"title": f"{title} | GTA 6 #shorts"[:100],
                    "caption": f"{title}\n\n🎥 Credit: {credit}\n{tags} #shorts"},
        "instagram": {"title": title, "caption": f"{title}\n\n🎥 Credit: {credit}\n{tags} #reels #twitch"},
        "facebook": {"title": title, "caption": f"{title}\n\n🎥 Credit: {credit}\n{tags} #reels"},
        "twitch": {"title": f"{title} (GTA 6) | clip from {credit}"[:140], "caption": ""},
    }
    return {p: {**base[p], **verdicts[p],
                "status": "blocked" if verdicts[p]["verdict"] == "block" else "pending_review"}
            for p in PLATFORMS}
