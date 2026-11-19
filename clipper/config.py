import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Config:
    twitch_client_id: str
    twitch_client_secret: str
    game_name: str
    whisper_model: str
    font_path: str
    beep_profanity: bool
    work_dir: Path
    review_dir: Path
    db_path: Path


def load(require_twitch: bool = True) -> Config:
    load_dotenv(ROOT / ".env", override=True)  # pick up keys added while the UI is running
    cid = os.getenv("TWITCH_CLIENT_ID", "").strip()
    secret = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
    if require_twitch and (not cid or not secret):
        raise SystemExit("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env (see .env.example)")
    cfg = Config(
        twitch_client_id=cid,
        twitch_client_secret=secret,
        game_name=os.getenv("GAME_NAME", "Grand Theft Auto VI"),
        whisper_model=os.getenv("WHISPER_MODEL", "small"),
        font_path=os.getenv("FONT_PATH", "/System/Library/Fonts/Supplemental/Impact.ttf"),
        beep_profanity=os.getenv("BEEP_PROFANITY", "true").lower() != "false",
        work_dir=ROOT / "work",
        review_dir=ROOT / "output" / "review",
        db_path=ROOT / "clipper.db",
    )
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    cfg.review_dir.mkdir(parents=True, exist_ok=True)
    return cfg
