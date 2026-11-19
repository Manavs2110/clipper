"""Blur nudity frame-by-frame and beep slurs / profanity.

Video: ffmpeg decodes raw frames -> NudeNet runs ~10x/sec -> detected regions are pixelated
(boxes padded and held for a short while so brief misses don't flash through) -> ffmpeg re-encodes.
Audio: flagged words are muted and replaced with a 1 kHz tone.
"""
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np

from .safety import EXPOSED, RX_PROF, RX_SLUR, _detector

Word = Tuple[float, float, str]
DETECT_PER_SEC = 15
HOLD_SEC = 1.0     # keep blurring a region this long after the last detection
PAD = 0.4          # grow each box by 40% on every side
MIN_SCORE = 0.25   # blur is cheap, so err toward blurring


def _probe(path: Path) -> Tuple[int, int, float, bool]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip().split(",")
    num, den = out[2].split("/")
    has_audio = bool(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0",
         str(path)], capture_output=True, text=True).stdout.strip())
    return int(out[0]), int(out[1]), float(num) / float(den or 1), has_audio


def _pixelate(frame: np.ndarray, box, W: int, H: int) -> None:
    import cv2

    x, y, w, h = box
    x0, y0 = max(0, int(x - w * PAD)), max(0, int(y - h * PAD))
    x1, y1 = min(W, int(x + w * (1 + PAD))), min(H, int(y + h * (1 + PAD)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    region = frame[y0:y1, x0:x1]
    small = cv2.resize(region, (max(1, (x1 - x0) // 28), max(1, (y1 - y0) // 28)), interpolation=cv2.INTER_AREA)
    frame[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)


def mask_word(w: str) -> str:
    core = re.sub(r"[^\w']", "", w)
    return w.replace(core, core[0] + "*" * (len(core) - 1)) if len(core) > 1 else w


def censor(src: Path, words: List[Word], out: Path, beep_profanity: bool = True,
           full_blur: Sequence[Tuple[float, float]] = (), blur: bool = True) -> Tuple[Dict, List[Word]]:
    """full_blur: time windows where the whole frame is pixelated (fallback for spots the detector missed)."""
    W, H, fps, has_audio = _probe(src)
    step = max(1, round(fps / DETECT_PER_SEC))
    hold_frames = int(HOLD_SEC * fps)

    # --- audio: which words to beep
    beeps, masked = [], []
    for s, e, t in words:
        if RX_SLUR.search(t) or (beep_profanity and RX_PROF.search(t)):
            beeps.append((max(0.0, s - 0.05), e + 0.05, mask_word(t)))
            masked.append((s, e, mask_word(t)))
        else:
            masked.append((s, e, t))

    if has_audio and beeps:
        cond = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e, _ in beeps)
        afilter = (f"[1:a]aresample=48000,volume=0:enable='{cond}'[m];"
                   f"sine=frequency=1000:sample_rate=48000,volume=0.25,volume=0:enable='not({cond})'[b];"
                   f"[m][b]amix=inputs=2:duration=first:normalize=0[a]")
        amap = ["-filter_complex", afilter, "-map", "[a]"]
    else:
        amap = ["-map", "1:a?"]

    if not blur:  # audio-only: copy the video stream untouched
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-i", str(src), "-map", "0:v", *amap,
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)]
        subprocess.run(cmd, check=True)
        return {"blurred_seconds": 0.0, "beeped": [{"t": round(s, 1), "word": m} for s, _, m in beeps],
                "beeped_slurs": sum(1 for _, _, t in words if RX_SLUR.search(t)),
                "beeped_profanity": beep_profanity, "full_blur_seconds": 0.0}, masked

    dec = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", str(src), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error",
                            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", f"{fps:.3f}", "-i", "-",
                            "-i", str(src), "-map", "0:v", *amap,
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)], stdin=subprocess.PIPE)

    frame_bytes = W * H * 3
    active: List[Tuple[list, int]] = []  # (box, last frame seen)
    blurred, idx = 0, 0
    try:
        while True:
            buf = dec.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
            if idx % step == 0:
                for h in _detector().detect(frame):
                    if h["class"] in EXPOSED and h["score"] >= MIN_SCORE:
                        active.append((h["box"], idx))
            active = [(b, seen) for b, seen in active if idx - seen <= hold_frames]
            t = idx / fps
            if any(a <= t <= b for a, b in full_blur):
                _pixelate(frame, (0, 0, W, H), W, H)
                blurred += 1
            else:
                for b, _ in active:
                    _pixelate(frame, b, W, H)
                blurred += bool(active)
            enc.stdin.write(frame.tobytes())
            idx += 1
    finally:
        enc.stdin.close()
        dec.stdout.close()
        dec.wait()
        if enc.wait() != 0:
            raise RuntimeError("ffmpeg failed while writing censored video")

    stats = {
        "blurred_seconds": round(blurred / fps, 1),
        "beeped": [{"t": round(s, 1), "word": m} for s, _, m in beeps],
        "beeped_slurs": sum(1 for _, _, t in words if RX_SLUR.search(t)),
        "beeped_profanity": beep_profanity,
        "full_blur_seconds": round(sum(b - a for a, b in full_blur), 1),
    }
    return stats, masked
