#!/bin/bash
# Collect GameChanger stats for the manually curated top-teams list.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv-gc ]; then
  echo "Creating venv (.venv-gc) and installing deps..."
  python3 -m venv .venv-gc
  ./.venv-gc/bin/pip install -q -r requirements-gc.txt
fi

./.venv-gc/bin/python gc_top_teams.py "$@"

echo
echo "View locally:"
echo "  python3 -m http.server 8123"
echo "  open http://localhost:8123/gc-player-stats.html"
echo "  open http://localhost:8123/gc-leaderboard.html"
echo
echo "Publish generated top-team stats and leaderboards:"
echo "  git add reports/gc-player-stats.json reports/gc-player-leaders.json reports/gc-top-teams-run.json"
echo "  git commit -m 'Update GC top team stats'"
echo "  git push"
