#!/usr/bin/env python3
"""
Export batting and pitching reports for specific players from local gc_stats.db.

Creates reports/gc-target-player-stats.json for gc-target-players.html.
Default target players:
- Jordyn Haynes
- Brooklyn Franco
- Maisy Finlestein
- Abigail Holland
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "gc_stats.db"
OUT_JSON = ROOT / "reports" / "gc-target-player-stats.json"
SNAPSHOT = ROOT / "snapshots" / "latest.json"

DEFAULT_PLAYERS = ["Jordyn Haynes", "Brooklyn Franco", "Maisy Finlestein", "Abigail Holland"]
BAT_COLS = ("AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB")
PIT_COLS = ("H", "R", "ER", "BB", "SO", "HR")
SEASON_RE = re.compile(r"^(20\d{2})\s+Fastpitch", re.I)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _int0(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def ip_to_thirds(value: Any) -> int:
    try:
        whole, _, frac = str(value or "0").partition(".")
        return int(whole or 0) * 3 + min(_int0(frac), 2)
    except ValueError:
        return 0


def thirds_to_ip(thirds: int) -> str:
    return f"{thirds // 3}.{thirds % 3}"


def batting_totals(lines: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {col: 0 for col in BAT_COLS}
    games = set()
    for line in lines:
        games.add(line["game_id"])
        stats = line.get("stats") or {}
        for col in BAT_COLS:
            totals[col] += _int0(stats.get(col))
    totals["GP"] = len(games)
    totals["AVG"] = round(totals["H"] / totals["AB"], 3) if totals["AB"] else 0.0
    return totals


def pitching_totals(lines: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {col: 0 for col in PIT_COLS}
    games = set()
    outs = 0
    for line in lines:
        games.add(line["game_id"])
        stats = line.get("stats") or {}
        for col in PIT_COLS:
            totals[col] += _int0(stats.get(col))
        outs += ip_to_thirds(stats.get("IP"))
    totals["GP"] = len(games)
    totals["IP"] = thirds_to_ip(outs)
    totals["IP_thirds"] = outs
    return totals


def season_year(season: str) -> int:
    m = SEASON_RE.match(season or "")
    return int(m.group(1)) if m else 0


def load_snapshot_players() -> dict[str, dict[str, Any]]:
    if not SNAPSHOT.exists():
        return {}
    snap = json.loads(SNAPSHOT.read_text())
    details = snap.get("player_details", {})
    players: dict[str, dict[str, Any]] = {}
    for _team_key, team in snap.get("teams", {}).items():
        roster_team = (team.get("team_name") or "").strip()
        for player in team.get("players", []):
            pid = str(player.get("player_id") or "").strip()
            if not pid:
                continue
            rec = players.setdefault(pid, {
                "name": player.get("name") or "Unknown player",
                "roster_team": roster_team,
                "current_teams": [],
                "last_season_teams": [],
            })
            hist = details.get(pid, {}).get("team_history", [])
            years = sorted({season_year(h.get("season", "")) for h in hist if season_year(h.get("season", ""))}, reverse=True)
            cur_year = years[0] if years else 0
            last_year = years[1] if len(years) > 1 else 0
            for h in hist:
                year = season_year(h.get("season", ""))
                name = (h.get("team") or "").strip()
                status = (h.get("status") or "").lower()
                if not name:
                    continue
                if year == cur_year and status in ("active", "guest") and name not in rec["current_teams"]:
                    rec["current_teams"].append(name)
                elif year == last_year and status in ("past", "active", "guest", "removed") and name not in rec["last_season_teams"]:
                    rec["last_season_teams"].append(name)
            if roster_team and roster_team not in rec["current_teams"]:
                rec["current_teams"].append(roster_team)
    return players


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS players (
        player_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        roster_team TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS teams (
        gc_url TEXT PRIMARY KEY,
        gc_name TEXT DEFAULT '',
        season TEXT DEFAULT '',
        ncs_teams TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS games (
        game_id TEXT PRIMARY KEY,
        gc_url TEXT NOT NULL,
        date TEXT NOT NULL,
        opponent TEXT DEFAULT '',
        home_away TEXT DEFAULT '',
        source_url TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS stat_lines (
        game_id TEXT NOT NULL,
        player_id TEXT NOT NULL,
        section TEXT NOT NULL,
        stats TEXT NOT NULL,
        positions TEXT DEFAULT '',
        PRIMARY KEY (game_id, player_id, section)
    );
    """)


def parse_targets(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_PLAYERS
    return [p.strip() for p in re.split(r"[,\n;]+", raw) if p.strip()]


def load_matching_players(conn: sqlite3.Connection, targets: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    snapshot = load_snapshot_players()
    db_players = conn.execute("SELECT player_id, name, roster_team FROM players").fetchall()
    by_norm = defaultdict(list)
    for pid, name, roster_team in db_players:
        snap = snapshot.get(pid, {})
        display_name = snap.get("name") or name
        by_norm[norm_name(display_name)].append({
            "player_id": pid,
            "name": display_name,
            "roster_team": snap.get("roster_team") or roster_team or "",
            "current_teams": snap.get("current_teams", []),
            "last_season_teams": snap.get("last_season_teams", []),
        })

    matched: dict[str, dict[str, Any]] = {}
    missing = []
    for target in targets:
        key = norm_name(target)
        exact = by_norm.get(key, [])
        if not exact:
            # fallback: allow partial target or DB spelling differences
            exact = [p for names in by_norm.values() for p in names if key in norm_name(p["name"]) or norm_name(p["name"]) in key]
        if not exact:
            missing.append(target)
            continue
        for player in exact:
            matched[player["player_id"]] = player
    return matched, missing


def decode_stats(value: str) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def team_list(value: str) -> list[str]:
    try:
        return [str(x) for x in json.loads(value or "[]") if x]
    except json.JSONDecodeError:
        return []


def build_report(db_path: Path, out_path: Path, targets: list[str], max_games: int) -> dict[str, Any]:
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}\nRun ./run_gc_stats.sh first, then run this exporter.")

    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    players, missing = load_matching_players(conn, targets)

    report_players = {pid: {**player, "games": [], "season_totals": {}, "year_totals": {}, "overall": {}} for pid, player in players.items()}
    if players:
        placeholders = ",".join("?" for _ in players)
        rows = conn.execute(
            f"SELECT l.player_id, l.section, l.stats, l.positions, "
            f"g.game_id, g.date, g.opponent, g.home_away, g.source_url, "
            f"COALESCE(t.gc_name, ''), COALESCE(t.season, ''), COALESCE(t.ncs_teams, '') "
            f"FROM stat_lines l "
            f"JOIN games g ON g.game_id = l.game_id "
            f"LEFT JOIN teams t ON t.gc_url = g.gc_url "
            f"WHERE l.player_id IN ({placeholders}) "
            f"ORDER BY g.date DESC",
            tuple(players.keys()),
        ).fetchall()
    else:
        rows = []

    raw_lines: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"batting": [], "pitching": []})
    by_season: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(lambda: defaultdict(lambda: {"batting": [], "pitching": []}))
    by_year: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(lambda: defaultdict(lambda: {"batting": [], "pitching": []}))

    for pid, section, stats_json, positions, game_id, date, opponent, home_away, source_url, gc_name, season, ncs_teams_json in rows:
        stats = decode_stats(stats_json)
        player = report_players[pid]
        ncs_teams = team_list(ncs_teams_json)
        player_team_names = player.get("current_teams", []) + player.get("last_season_teams", [])
        team_label = next((n for n in ncs_teams if n in player_team_names), None) or (ncs_teams[0] if ncs_teams else gc_name)
        game = next((g for g in player["games"] if g["game_id"] == game_id), None)
        if game is None:
            game = {
                "game_id": game_id,
                "date": date,
                "team": team_label,
                "gc_team": gc_name,
                "season": season or "Unknown Season",
                "year": str(date or "")[:4] or "Unknown",
                "opponent": opponent,
                "home_away": home_away,
                "source_url": source_url,
            }
            player["games"].append(game)
        game[section] = stats
        if positions:
            game["positions"] = positions

        line = {"game_id": game_id, "date": date, "stats": stats}
        raw_lines[pid][section].append(line)
        by_season[pid][season or "Unknown Season"][section].append(line)
        by_year[pid][str(date or "")[:4] or "Unknown"][section].append(line)

    for pid, player in report_players.items():
        player["games"].sort(key=lambda g: g.get("date", ""), reverse=True)
        player["games"] = player["games"][:max_games]
        player["overall"] = {
            "batting": batting_totals(raw_lines[pid]["batting"]),
            "pitching": pitching_totals(raw_lines[pid]["pitching"]),
        }
        player["season_totals"] = {
            season: {
                "batting": batting_totals(lines["batting"]),
                "pitching": pitching_totals(lines["pitching"]),
            }
            for season, lines in sorted(by_season[pid].items(), reverse=True)
        }
        player["year_totals"] = {
            year: {
                "batting": batting_totals(lines["batting"]),
                "pitching": pitching_totals(lines["pitching"]),
            }
            for year, lines in sorted(by_year[pid].items(), reverse=True)
        }

    conn.close()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": db_path.name,
        "targets": targets,
        "missing": missing,
        "max_games": max_games,
        "players": report_players,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Export specific GameChanger players from gc_stats.db")
    parser.add_argument("--players", help="Comma, semicolon, or newline separated player names")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite DB path. Default: gc_stats.db")
    parser.add_argument("--out", default=str(OUT_JSON), help="Output JSON path. Default: reports/gc-target-player-stats.json")
    parser.add_argument("--max-games", type=int, default=20, help="Recent games to include per player. Default: 20")
    args = parser.parse_args()

    targets = parse_targets(args.players)
    report = build_report(Path(args.db), Path(args.out), targets, args.max_games)
    found = len(report["players"])
    log(f"Exported target players: {found} found, {len(report['missing'])} missing -> {args.out}")
    if report["missing"]:
        log("Missing: " + ", ".join(report["missing"]))
    log("Publish: git add reports/gc-target-player-stats.json && git commit -m 'Update target player stats' && git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
