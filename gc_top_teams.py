#!/usr/bin/env python3
"""
Run the GameChanger stats collector for a manually curated top-teams list.

Default behavior:
1. Read team names from top_teams.txt, one team per line.
2. If top_teams.txt is missing or empty, fall back to config.yaml -> special_watch.
3. Run gc_player_stats.py once per team using --teams "<name>".
4. Regenerate reports/gc-player-stats.json and reports/gc-player-leaders.json from gc_stats.db.
5. Write reports/gc-top-teams-run.json as a run audit.

This intentionally does not auto-rank or guess teams. The list is yours.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent
DEFAULT_LIST = ROOT / "top_teams.txt"
CONFIG = ROOT / "config.yaml"
REPORT = ROOT / "reports" / "gc-top-teams-run.json"


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def read_list_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    names: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    return dedupe(names)


def read_special_watch() -> list[str]:
    if yaml is None or not CONFIG.exists():
        return []
    data = yaml.safe_load(CONFIG.read_text()) or {}
    return dedupe(str(x).strip() for x in data.get("special_watch", []) if str(x).strip())


def dedupe(items) -> list[str]:
    seen = set()
    out: list[str] = []
    for item in items:
        key = " ".join(str(item).lower().split())
        if key and key not in seen:
            seen.add(key)
            out.append(str(item).strip())
    return out


def run_cmd(cmd: list[str], dry_run: bool = False) -> dict[str, Any]:
    log("$ " + " ".join(cmd))
    if dry_run:
        return {"cmd": cmd, "returncode": 0, "dry_run": True}
    proc = subprocess.run(cmd, cwd=ROOT)
    return {"cmd": cmd, "returncode": proc.returncode, "dry_run": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect GameChanger stats for manually listed top teams")
    parser.add_argument("--list-file", default=str(DEFAULT_LIST), help="One team name per line. Default: top_teams.txt")
    parser.add_argument("--no-full-season", action="store_true", help="Only scrape recent games instead of every completed game")
    parser.add_argument("--headful", action="store_true", help="Show the Chrome browser")
    parser.add_argument("--current-only", action="store_true", help="Skip last-season teams")
    parser.add_argument("--skip-follow", action="store_true", help="Do not click Follow on team pages")
    parser.add_argument("--max-games", type=int, default=10, help="Recent games per player for report output. Default: 10")
    parser.add_argument("--min-score", type=float, default=0.34, help="Minimum GC team-map score. Default: 0.34")
    parser.add_argument("--profile", help="Logged-in Chrome profile path")
    parser.add_argument("--limit", type=int, help="Only run the first N teams from the list")
    parser.add_argument("--map-only", action="store_true", help="Only build/refresh gc_team_map.json for the list")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them")
    args = parser.parse_args()

    list_file = Path(args.list_file)
    if not list_file.is_absolute():
        list_file = ROOT / list_file

    teams = read_list_file(list_file)
    source = str(list_file.name)
    if not teams:
        teams = read_special_watch()
        source = "config.yaml:special_watch"

    if args.limit:
        teams = teams[: args.limit]

    if not teams:
        raise SystemExit("No top teams found. Add names to top_teams.txt or config.yaml special_watch.")

    log(f"Top team source: {source}")
    log(f"Top teams to process: {len(teams)}")
    for idx, team in enumerate(teams, 1):
        log(f"  {idx}. {team}")

    results: list[dict[str, Any]] = []
    for idx, team in enumerate(teams, 1):
        cmd = [sys.executable, "gc_player_stats.py", "--teams", team, "--max-games", str(args.max_games), "--min-score", str(args.min_score)]
        if not args.no_full_season:
            cmd.append("--full-season")
        if args.headful:
            cmd.append("--headful")
        if args.current_only:
            cmd.append("--current-only")
        if args.skip_follow:
            cmd.append("--skip-follow")
        if args.map_only:
            cmd.append("--map-only")
        if args.profile:
            cmd.extend(["--profile", args.profile])

        log(f"Top team {idx}/{len(teams)}: {team}")
        result = run_cmd(cmd, args.dry_run)
        result["team"] = team
        results.append(result)
        if result["returncode"] != 0:
            log(f"Stopping because {team} returned {result['returncode']}")
            break

    export_results: list[dict[str, Any]] = []
    if not args.map_only and all(r["returncode"] == 0 for r in results):
        export_results.append(run_cmd([sys.executable, "gc_db_report.py", "--max-games", str(args.max_games)], args.dry_run))
        export_results.append(run_cmd([sys.executable, "gc_leaders_report.py", "--limit", "25", "--min-ab", "10"], args.dry_run))

    REPORT.parent.mkdir(exist_ok=True)
    audit = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "teams": teams,
        "map_only": args.map_only,
        "full_season": not args.no_full_season,
        "max_games": args.max_games,
        "results": results,
        "exports": export_results,
    }
    REPORT.write_text(json.dumps(audit, indent=2))
    log(f"Run audit saved -> {REPORT}")

    failed = [r for r in results + export_results if r.get("returncode")]
    if failed:
        return int(failed[0]["returncode"])

    log("Done. View: gc-player-stats.html and gc-leaderboard.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
