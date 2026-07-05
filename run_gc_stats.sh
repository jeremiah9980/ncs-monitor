#!/bin/bash
# GC Player Stats workflow — NCS players -> GameChanger last-10-game stats.
#
#   ./run_gc_stats.sh                 # full run, all tracked teams
#   ./run_gc_stats.sh --teams Venom   # any gc_player_stats.py flag passes through
#
# Requires Chrome logged in to GameChanger. The profile path defaults to the
# standard Chrome "Default" profile for your OS (macOS/Windows/Linux); if you
# use a different profile, pass --profile "/path/to/Chrome/ProfileDir"
# (find yours at chrome://version -> Profile Path).
set -e
cd "$(dirname "$0")"

if [ ! -d .venv-gc ]; then
  echo "Creating venv (.venv-gc) and installing deps..."
  python3 -m venv .venv-gc
  ./.venv-gc/bin/pip install -q -r requirements-gc.txt
fi

./.venv-gc/bin/python gc_player_stats.py "$@"

echo
echo "Done. View the stats:"
echo "  python3 -m http.server 8123   # then open http://localhost:8123/gc-player-stats.html"
