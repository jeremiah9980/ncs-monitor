#!/bin/bash
# Export the local SQLite stats database into the static JSON file that
# gc-player-stats.html and GitHub Pages load.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv-gc ]; then
  echo "Creating venv (.venv-gc) and installing deps..."
  python3 -m venv .venv-gc
  ./.venv-gc/bin/pip install -q -r requirements-gc.txt
fi

./.venv-gc/bin/python gc_db_report.py "$@"

echo
echo "View locally:"
echo "  python3 -m http.server 8123"
echo "  open http://localhost:8123/gc-player-stats.html"
echo
echo "Publish the generated JSON to GitHub Pages:"
echo "  git add reports/gc-player-stats.json"
echo "  git commit -m 'Update GC player stats report'"
echo "  git push"
