#!/usr/bin/env python3
"""
Pull GameChanger batting and pitching stats for a small selected player list.

This uses the same local GameChanger workflow as gc_player_stats.py, but filters
first so only the named players' current teams and last-season teams are scraped.
The DB is still gc_stats.db, and the selected report is written to:

  reports/gc-specific-player-stats.json

Default player list:

  specific_players.txt
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import gc_player_stats as gc

ROOT = Path(__file__).resolve().parent
PLAYERS_FILE = ROOT / "specific_players.txt"
OUT_JSON = ROOT / "reports" / "gc-specific-player-stats.json"


def load_requested_names(players_file: Path, inline_names: list[str] | None) -> list[str]:
    names: list[str] = []
    if players_file.exists():
        names.extend(line.strip() for line in players_file.read_text().splitlines() if line.strip() and not line.strip().startswith("#"))
    if inline_names:
        for raw in inline_names:
            names.extend(part.strip() for part in raw.split(",") if part.strip())
    # preserve order but remove duplicates case-insensitively
    seen = set()
    out = []
    for name in names:
        key = " ".join(name.lower().split())
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def norm_name(name: str) -> str:
    return " ".join((name or "").lower().split())


def filter_players(players: dict, team_names: dict, wanted_names: list[str]) -> tuple[dict, dict, list[str]]:
    wanted = {norm_name(name): name for name in wanted_names}
    matched = {pid: p for pid, p in players.items() if norm_name(p.get("name", "")) in wanted}
    found_names = {norm_name(p.get("name", "")) for p in matched.values()}
    missing = [original for key, original in wanted.items() if key not in found_names]

    keep_team_names = set()
    for p in matched.values():
        keep_team_names.update(p.get("current_teams", []))
        keep_team_names.update(p.get("last_season_teams", []))
    filtered_teams = {
        team: {"players": [pid for pid in info.get("players", []) if pid in matched], "kind": info.get("kind", "current")}
        for team, info in team_names.items()
        if team in keep_team_names
    }
    # If a team has the selected player but the original team list did not include
    # it for some reason, add it so the mapper still tries to find it on GC.
    for team in keep_team_names:
        filtered_teams.setdefault(team, {"players": [], "kind": "current"})
    return matched, filtered_teams, missing


def collect_selected(args: argparse.Namespace) -> dict:
    wanted_names = load_requested_names(Path(args.players_file), args.player)
    if not wanted_names:
        raise SystemExit("No selected players. Add names to specific_players.txt or pass --player \"First Last\".")

    players, team_names = gc.load_players_and_teams(args.current_only)
    players, team_names, missing = filter_players(players, team_names, wanted_names)
    gc.log(f"Selected players requested: {len(wanted_names)}")
    gc.log(f"Selected players found in NCS snapshot: {len(players)}")
    if missing:
        gc.log("Missing from snapshots/latest.json: " + ", ".join(missing))
    if not players:
        raise SystemExit("None of the selected players were found in snapshots/latest.json. Run ncs_monitor.py first or check spelling.")
    gc.log(f"Teams to scrape for selected players: {len(team_names)}")

    profile = gc.clone_profile(Path(args.profile))
    driver = gc.make_driver(profile, headless=not args.headful)
    try:
        team_map = gc.build_team_map(driver, team_names, gc.load_team_map(), args.min_score)
        if args.map_only:
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "selected_names": wanted_names,
                "missing_names": missing,
                "players": {},
                "teams_scraped": {},
            }

        box_scores: dict[str, dict] = {}
        mapped = [(name, t) for name, t in team_map["teams"].items() if t.get("gc_url") and name in team_names]
        gc.log(f"Scraping {len(mapped)} mapped selected-player team(s)...")
        for i, (ncs_name, t) in enumerate(mapped, 1):
            if t["gc_url"] in box_scores:
                box_scores[t["gc_url"]]["ncs_teams"].append(ncs_name)
                gc.log(f"[{i}/{len(mapped)}] {ncs_name} -> already scraped ({t.get('gc_name') or t['gc_url']})")
                continue
            gc.log(f"[{i}/{len(mapped)}] {ncs_name} -> {t.get('gc_name') or t['gc_url']}")
            if not args.skip_follow and gc.follow_team(driver, t["gc_url"]):
                gc.log("    followed team on GC")
            want = 10_000 if args.full_season else args.max_games
            games = gc.last_completed_games(driver, t["gc_url"], want)
            gc.log(f"    {len(games)} completed game(s) in {t.get('season') or 'current season'}")
            for prior in t.get("gc_prior_urls", []):
                if not args.full_season and len(games) >= args.max_games:
                    break
                more = gc.last_completed_games(driver, prior["gc_url"], want if args.full_season else args.max_games - len(games))
                gc.log(f"    +{len(more)} from {prior.get('season') or 'prior season'}")
                games.extend(more)
            parsed = []
            for game in games:
                try:
                    parsed.append(gc.scrape_box_score(driver, game))
                except RuntimeError as exc:
                    raise SystemExit(str(exc)) from exc
                except Exception as exc:
                    gc.log(f"    box score failed ({game['game_id'][:8]}): {exc}")
                time.sleep(0.5)
            box_scores[t["gc_url"]] = {"ncs_teams": [ncs_name], "gc_name": t.get("gc_name", ""), "games": [p for p in parsed if p]}
    finally:
        driver.quit()

    gc.store_db(players, box_scores, team_map)
    report = gc.build_report(players, team_map, box_scores, args.max_games)
    report["selected_names"] = wanted_names
    report["missing_names"] = missing
    report["generated_for"] = "specific_players"
    for pid, rows in gc.season_totals_for_report(report["players"].keys()).items():
        report["players"][pid]["season"] = rows
    OUT_JSON.parent.mkdir(exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull GameChanger batting/pitching stats for selected players only")
    parser.add_argument("--players-file", default=str(PLAYERS_FILE), help="Text file with one player name per line. Default: specific_players.txt")
    parser.add_argument("--player", action="append", help="Add one player name or a comma-separated list. Can be repeated.")
    parser.add_argument("--profile", default=str(gc.DEFAULT_PROFILE), help="Logged-in Chrome profile path")
    parser.add_argument("--headful", action="store_true", help="Show the browser")
    parser.add_argument("--current-only", action="store_true", help="Skip last-season teams")
    parser.add_argument("--map-only", action="store_true", help="Only build/refresh gc_team_map.json")
    parser.add_argument("--skip-follow", action="store_true", help="Do not click Follow on team pages")
    parser.add_argument("--max-games", type=int, default=10, help="Recent games per player in the report. Default: 10")
    parser.add_argument("--full-season", action="store_true", help="Scrape every completed game for mapped current/prior seasons")
    parser.add_argument("--min-score", type=float, default=0.34, help="Minimum team-map score to auto-accept")
    args = parser.parse_args()

    report = collect_selected(args)
    with_stats = sum(1 for p in report.get("players", {}).values() if p.get("games"))
    gc.log(f"Selected report saved: {with_stats}/{len(report.get('players', {}))} player(s) with stats -> {OUT_JSON}")
    gc.log("View it: python3 -m http.server 8123, then open gc-specific-player-stats.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
