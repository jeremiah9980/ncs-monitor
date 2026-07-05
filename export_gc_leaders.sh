#!/bin/bash
# Export local SQLite stats into the static leaderboard JSON used by gc-leaderboard.html.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv-gc ]; then
  echo "Creating venv (.venv-gc) and installing deps..."
  python3 -m venv .venv-gc
  ./.venv-gc/bin/pip install -q -r requirements-gc.txt
fi

./.venv-gc/bin/python gc_leaders_report.py "$@"

echo
echo "View locally:"
echo "  python3 -m http.server 8123"
echo "  open http://localhost:8123/gc-leaderboard.html"
echo
echo "Publish the generated leaderboard JSON to GitHub Pages:"
echo "  git add reports/gc-player-leaders.json"
echo "  git commit -m 'Update GC player leaderboards'"
echo "  git push"
