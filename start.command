#!/bin/zsh
# Double-click in Finder (or run ./start.command) to launch the Clipper review UI.
cd "$(dirname "$0")"
lsof -t -iTCP:5050 -sTCP:LISTEN | xargs kill 2>/dev/null   # stop any old copy so code changes load
(sleep 2 && open http://127.0.0.1:5050) &
exec .venv/bin/python -m clipper.ui
