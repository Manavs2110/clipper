"""Download, transcribe, caption and render a clip to 1080x1920."""
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
# Mac: hardware encoder. Linux (GitHub Actions): x264.
VIDEO_ENC = (["-c:v", "h264_videotoolbox", "-b:v", "10M", "-maxrate", "14M"] if sys.platform == "darwin"
             else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"])
Word = Tuple[float, float, str]


def download(url: str, dest_dir: Path, clip_id: str) -> Path:
    from yt_dlp import YoutubeDL

    opts = {
        "outtmpl": str(dest_dir / f"{clip_id}.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "noprogress": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


@lru_cache(maxsize=1)
def _whisper(model_size: str):
    from faster_whisper import WhisperModel

    import os

    return WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)


def transcribe(path: Path, model_size: str) -> List[Word]:
    segments, _ = _whisper(model_size).transcribe(str(path), word_timestamps=True, vad_filter=True)
    return [(w.start, w.end, w.word.strip()) for s in segments for w in (s.words or []) if w.word.strip()]


def chunk_words(words: List[Word], max_words: int = 3, max_span: float = 1.4) -> List[Word]:
    """Group words into short punchy caption chunks."""
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w[1] - cur[0][0] > max_span):
            chunks.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
            cur = []
        cur.append(w)
    if cur:
        chunks.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
    return chunks


def text_png(text: str, path: Path, font_path: str, size: int, fill=(255, 255, 255)) -> Path:
    """Render centered, stroked text onto a transparent full-width PNG, wrapping as needed."""
    font = ImageFont.truetype(font_path, size)
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    max_w = W - 120
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)

    stroke = max(4, size // 12)
    line_h = int(size * 1.15)
    img = Image.new("RGBA", (W, line_h * len(lines) + stroke * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        x = (W - d.textlength(ln, font=font)) / 2
        d.text((x, stroke + i * line_h), ln, font=font, fill=fill,
               stroke_width=stroke, stroke_fill=(0, 0, 0))
    img.save(path)
    return path


def render(src: Path, out: Path, words: List[Word], hook: str, credit: str,
           font_path: str, work: Path) -> Path:
    """Blurred-fill background, gameplay centered, hook on top, word captions below."""
    work.mkdir(parents=True, exist_ok=True)
    overlays = []  # (png, y, start, end or None)
    overlays.append((text_png(hook.upper(), work / "hook.png", font_path, 78), 360, 0.0, None))
    overlays.append((text_png(credit, work / "credit.png", font_path, 40, fill=(230, 230, 230)), 560, 0.0, None))
    for i, (s, e, t) in enumerate(chunk_words(words)):
        overlays.append((text_png(t.upper(), work / f"cap{i:03d}.png", font_path, 92,
                                  fill=(255, 225, 40)), 1330, s, e))

    inputs = ["-i", str(src)]
    for png, *_ in overlays:
        inputs += ["-i", str(png)]

    graph = [
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:5,eq=brightness=-0.15[bg]",
        f"[0:v]scale={W}:-2[fg]",
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]",
    ]
    for i, (_, y, s, e) in enumerate(overlays, start=1):
        enable = "" if e is None else f":enable='between(t,{s:.2f},{e:.2f})'"
        graph.append(f"[v{i-1}][{i}:v]overlay=0:{y}{enable}[v{i}]")

    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
           "-filter_complex", ";".join(graph),
           "-map", f"[v{len(overlays)}]", "-map", "0:a?",
           *VIDEO_ENC, "-r", "30", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", "60", str(out)]
    subprocess.run(cmd, check=True)
    return out
