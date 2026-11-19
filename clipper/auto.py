"""Autopilot: post one fully-clean clip per run, no human review.

    python -m clipper.auto            # what GitHub Actions runs every 2 hours
    python -m clipper.auto --dry-run  # process + show what would be posted, post nothing

Rules
- Only clips whose verdict is "pass" (no warnings at all) on a platform get posted there.
- Instagram + Facebook: one Reel per run.  YouTube: at most once every YOUTUBE_EVERY_HOURS (default 4h).
- The clip history (clipper.db) lives in your R2 bucket (state/clipper.db) so runs never repost.
"""
import argparse
import json
import os
import sys
import time

from . import config, publish
from .__main__ import candidates, process_clip
from .db import DB

STATE_KEY = "state/clipper.db"
MAX_TRIES = int(os.getenv("AUTO_MAX_TRIES", "8"))  # clips to try per run before giving up


def _kv(db: DB):
    db.conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    get = lambda k: (db.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone() or [None])[0]

    def put(k, v):
        db.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, str(v)))
        db.conn.commit()
    return get, put


def _sync_state(cfg, direction: str) -> None:
    s3, bucket = publish._r2(), publish._env("R2_BUCKET")
    if direction == "down":
        try:
            s3.download_file(bucket, STATE_KEY, str(cfg.db_path))
            print("state: loaded clip history from R2")
        except Exception:
            print("state: no history in R2 yet, starting fresh")
    else:
        s3.upload_file(str(cfg.db_path), bucket, STATE_KEY)
        print("state: saved clip history to R2")


def _tokens_from_env() -> None:
    """In CI the OAuth tokens come from GitHub Secrets instead of the tokens/ folder."""
    for env, name in (("META_TOKENS_JSON", "meta.json"), ("YOUTUBE_TOKENS_JSON", "youtube.json")):
        if os.getenv(env) and not (publish.TOKENS / name).exists():
            publish._save(name, json.loads(os.environ[env]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--min-views", type=int, default=50)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--local-state", action="store_true", help="use local clipper.db instead of R2")
    a = ap.parse_args()

    cfg = config.load()
    _tokens_from_env()
    if not a.local_state:
        _sync_state(cfg, "down")
    db = DB(cfg.db_path)
    get, put = _kv(db)
    yt_every = float(os.getenv("YOUTUBE_EVERY_HOURS", "4")) * 3600
    yt_due = publish.connections()["youtube"]["connected"] and time.time() - float(get("last_youtube") or 0) >= yt_every
    conn = publish.connections()
    meta_ok = conn["meta"]["connected"]
    print(f"targets: instagram={meta_ok and bool(conn['meta']['instagram'])} facebook={meta_ok} youtube={yt_due}")

    posted_any = False
    try:
        for c in candidates(cfg, db, a.hours, a.min_views, a.lang)[:MAX_TRIES]:
            out = process_clip(c, cfg, db)
            if not out:
                continue
            meta = json.loads(out.with_suffix(".json").read_text())
            P = meta["platforms"]
            targets = [p for p in ("instagram", "facebook") if meta_ok and P[p]["verdict"] == "pass"]
            if "instagram" in targets and not conn["meta"]["instagram"]:
                targets.remove("instagram")
            if yt_due and P["youtube"]["verdict"] == "pass":
                targets.append("youtube")
            if not targets:
                warns = {r for p in P.values() for r in p["reasons"]}
                print(f"  not clean enough to auto-post ({'; '.join(sorted(warns))[:200]}), trying next clip")
                continue

            print(f"  posting to: {', '.join(targets)}" + (" (dry run, nothing sent)" if a.dry_run else ""))
            if a.dry_run:
                posted_any = True
                break
            for p in targets:
                try:
                    if p == "instagram":
                        url = publish.post_instagram(out, P[p]["caption"])
                    elif p == "facebook":
                        url = publish.post_facebook(out, P[p]["caption"])
                    else:
                        url = publish.post_youtube(out, P[p]["title"], P[p]["caption"])
                        put("last_youtube", time.time())
                    P[p].update(status="approved", publish={"state": "published", "url": url, "at": int(time.time()), "auto": True})
                    print(f"  ✓ {p}: {url}")
                    posted_any = True
                except Exception as e:
                    P[p].update(publish={"state": "failed", "error": str(e)[:500], "auto": True})
                    print(f"  ✗ {p}: {e}")
            out.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
            db.record(c, "posted" if posted_any else "rendered", str(out))
            break
        else:
            print("No clean clip found this run. Will try again next run.")
    finally:
        if not a.local_state and not a.dry_run:
            _sync_state(cfg, "up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
