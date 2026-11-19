"""python -m clipper [--hours 24] [--limit 10] [--min-views 50] [--lang en]"""
import argparse
import json
import re
import shutil
import traceback
from datetime import date
from pathlib import Path
from typing import List, Optional

from . import censor, config, media, safety
from .db import DB
from .twitch import Twitch


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def merge_windows(ws):
    out = []
    for a, b in sorted(ws):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((max(0.0, a), b))
    return out


def candidates(cfg, db: DB, hours: int, min_views: int, lang: str, log=print) -> List[dict]:
    """New Twitch clips, most-viewed first, minus ones already seen or with sexual titles."""
    tw = Twitch(cfg.twitch_client_id, cfg.twitch_client_secret)
    game = tw.game_id(cfg.game_name)
    clips = [c for c in tw.top_clips(game, hours=hours, first=100)
             if c["view_count"] >= min_views
             and (not lang or c.get("language") == lang)
             and not db.seen(c["id"])]
    if not safety.allow_blurred_nudity():  # skip obviously sexual titles without downloading them
        skip = [c for c in clips if safety.RX_SUGG.search(c["title"])]
        for c in skip:
            db.record(c, "blocked", error="sexual title")
        if skip:
            log(f"Skipped {len(skip)} clip(s) by title: " + ", ".join(repr(c["title"][:30]) for c in skip[:5]))
        clips = [c for c in clips if c not in skip]
    log(f"{len(clips)} new clips for {cfg.game_name!r}")
    return clips


def process_clip(c: dict, cfg, db: DB, log=print) -> Optional[Path]:
    """Download -> safety -> censor -> render. Returns the rendered mp4 (with .json beside it) or None."""
    allow_nudity = safety.allow_blurred_nudity()
    work = cfg.work_dir / c["id"]
    name = f"{date.today()}_{slug(c['broadcaster_name'])}_{slug(c['title'])}"
    out = cfg.review_dir / f"{name}.mp4"
    log(f"→ [{c['view_count']} views] {c['broadcaster_name']}: {c['title']}")
    try:
        work.mkdir(parents=True, exist_ok=True)
        pre = None
        src = media.download(c["url"], work, c["id"])
        if not allow_nudity:  # cheap check before the slow transcription
            pre = safety.scan_nudity(src, fps=4)
            if pre["exposed_at"]:
                log(f"  ⛔ nudity at {pre['exposed_at'][0]}s, skipped")
                db.record(c, "blocked", error=f"Nudity at {pre['exposed_at'][:5]}s")
                return None
        words = media.transcribe(src, cfg.whisper_model)
        credit = f"twitch.tv/{c['broadcaster_name']}"

        clean = work / "censored.mp4"
        full: list = []
        nud = None
        for _ in range(3 if allow_nudity else 1):  # censor -> verify -> full-frame blur where it leaked
            cstats, masked = censor.censor(src, words, clean, cfg.beep_profanity, full_blur=full, blur=allow_nudity)
            if not allow_nudity:
                break  # pre-scan at 4 fps already verified this clip
            nud = safety.scan_nudity(clean, fps=4)
            if not nud["exposed_at"]:
                break
            log(f"  blur missed {len(nud['exposed_at'])} spot(s); full-frame blur there and redo")
            full = merge_windows(full + [(t - 1.0, t + 1.0) for t in nud["exposed_at"]])
        words = masked
        log(f"  censor: blurred {cstats['blurred_seconds']}s, beeped {len(cstats['beeped'])} word(s)")
        report = safety.scan(clean, words, c["title"], cstats, nudity=nud or pre)
        verdicts = {p: safety.evaluate(report, p) for p in safety.PLATFORMS}
        log("  safety: " + "  ".join(f"{p}={v['verdict']}" for p, v in verdicts.items()))
        if all(v["verdict"] == "block" for v in verdicts.values()):
            reasons = list(dict.fromkeys(r for v in verdicts.values() for r in v["reasons"]))
            log(f"  ⛔ blocked everywhere, skipped: {reasons[0]}")
            db.record(c, "blocked", error="; ".join(reasons))
            return None

        media.render(clean, out, words, c["title"], credit, cfg.font_path, work)
        meta = {
            "id": c["id"],
            "title": c["title"],
            "credit": credit,
            "source_url": c["url"],
            "views_on_twitch": c["view_count"],
            "transcript": " ".join(w[2] for w in words),
            "safety": report,
            "platforms": safety.platform_meta(c["title"], credit, verdicts),
        }
        out.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        db.record(c, "rendered", str(out))
        log(f"  ✓ {out.name}")
        return out
    except Exception as e:  # keep going on a bad clip
        db.record(c, "failed", error=str(e))
        log(f"  ✗ failed: {e}")
        traceback.print_exc()
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run(hours: int = 24, limit: int = 10, min_views: int = 50, lang: str = "en", log=print,
        redo_blocked: bool = False) -> None:
    cfg = config.load()
    db = DB(cfg.db_path)
    if redo_blocked:
        log(f"Retrying {db.forget('blocked')} previously blocked clip(s)")
    clips = candidates(cfg, db, hours, min_views, lang, log)
    log(f"Rendering up to {limit}")
    for c in clips[:limit]:
        process_clip(c, cfg, db, log)


def main() -> None:
    p = argparse.ArgumentParser(description="Turn top Twitch clips into 9:16 Shorts/Reels for review")
    p.add_argument("--hours", type=int, default=24, help="look-back window")
    p.add_argument("--limit", type=int, default=10, help="max clips to render this run")
    p.add_argument("--min-views", type=int, default=50)
    p.add_argument("--lang", default="en", help="clip language filter, '' for any")
    p.add_argument("--redo-blocked", action="store_true", help="retry clips skipped by the safety filter")
    a = p.parse_args()
    run(a.hours, a.limit, a.min_views, a.lang, redo_blocked=a.redo_blocked)


if __name__ == "__main__":
    main()
