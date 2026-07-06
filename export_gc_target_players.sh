#!/bin/bash
# Export batting and pitching stats for selected players from local gc_stats.db.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv-gc ]; then
  echo "Creating venv (.venv-gc) and installing deps..."
  python3 -m venv .venv-gc
  ./.venv-gc/bin/pip install -q -r requirements-gc.txt
fi

DEFAULT_PLAYERS="Jordyn Haynes,Brooklyn Franco,Maisy Finlestein,Abigail Holland"

if [ "$#" -eq 0 ]; then
  ./.venv-gc/bin/python gc_target_players_report.py --players "$DEFAULT_PLAYERS"
else
  ./.venv-gc/bin/python gc_target_players_report.py "$@"
fi

echo
echo "View locally:"
echo "  python3 -m http.server 8123"
echo "  open http://localhost:8123/gc-target-players.html"
echo
echo "Publish the generated target-player JSON to GitHub Pages:"
echo "  git add reports/gc-target-player-stats.json"
echo "  git commit -m 'Update target player stats'"
echo "  git push"
