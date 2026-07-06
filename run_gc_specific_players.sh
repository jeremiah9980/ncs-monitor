#!/bin/bash
# Pull batting and pitching stats for players listed in specific_players.txt.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv-gc ]; then
  echo "Creating venv (.venv-gc) and installing deps..."
  python3 -m venv .venv-gc
  ./.venv-gc/bin/pip install -q -r requirements-gc.txt
fi

./.venv-gc/bin/python gc_specific_players.py "$@"

echo
echo "View locally:"
echo "  python3 -m http.server 8123"
echo "  open http://localhost:8123/gc-specific-player-stats.html"
echo
echo "Publish selected-player report to GitHub Pages:"
echo "  git add reports/gc-specific-player-stats.json"
echo "  git commit -m 'Update selected GC player stats'"
echo "  git push"
