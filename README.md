# Clipper

Turns the top GTA 6 Twitch clips into 9:16 Shorts/Reels with captions, ready for review.

## Setup
    /opt/homebrew/bin/python3.11 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    cp .env.example .env   # add Twitch app credentials

## Run
    .venv/bin/python -m clipper --hours 24 --limit 10 --min-views 50 --lang en

Each clip lands in `output/review/` as an `.mp4` (1080x1920) plus a `.json` holding the
suggested title, caption with streamer credit, and transcript. Processed clips are tracked
in `clipper.db`, so reruns skip them.

## Roadmap
1. [x] Twitch top clips -> 9:16 + word captions -> review folder
2. [ ] Uploaders: YouTube Shorts (Data API v3, OAuth) + Instagram Reels (Graph API, business account)
3. [ ] Live stream recording + chat-spike highlight detection
4. [ ] Claude scoring of candidate moments + hook titles
5. [ ] Overnight scheduler

## Autopilot (GitHub Actions)
`.github/workflows/autopilot.yml` runs `python -m clipper.auto` every 2 hours. Each run posts **one** clip
that passed every safety check with zero warnings: Instagram + Facebook Reels each run, YouTube at most every
4 hours (quota). Clip history is kept in R2 (`state/clipper.db`) so nothing is posted twice. Keys and tokens are
GitHub Secrets. Run it by hand from the Actions tab (tick "dry run" to test without posting).
