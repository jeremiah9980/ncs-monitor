#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "gc_stats.db"
NAMES_FILE = ROOT / "specific_players.txt"
OUT_JSON = ROOT / "reports" / "gc-specific-player-stats.json"

BAT_COLS = ("AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB")
PIT_COLS = ("IP", "H", "R", "ER", "BB", "SO", "HR")


def clean_name(value: str) -> str:
    return " ".join((value or "").lower().split())


def read_names(path: Path, cli_names: list[str] | None) -> list[str]:
    values = []
    if path.exists():
        values.extend(x.strip() for x in path.read_text().splitlines() if x.strip() and not x.strip().startswith("#"))
    for raw in cli_names or []:
        values.extend(x.strip() for x in raw.split(",") if x.strip())
    out, seen = [], set()
    for value in values:
        key = clean_name(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def int0(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def ip_to_thirds(value: Any) -> int:
    try:
        whole, _, frac = str(value or "0").partition(".")
        return int(whole or 0) * 3 + min(int0(frac), 2)
    except ValueError:
        return 0


def thirds_to_ip(value: int) -> str:
    return f"{value // 3}.{value % 3}"


def totals_for(games: list[dict[str, Any]]) -> dict[str, Any]:
    batting_rows = [g["batting"] for g in games if g.get("batting")]
    pitching_rows = [g["pitching"] for g in games if g.get("pitching")]
    batting = {col: sum(int0(row.get(col)) for row in batting_rows) for col in BAT_COLS}
    batting["GP"] = len({g["game_id"] for g in games if g.get("batting")})
    batting["AVG"] = round(batting["H"] / batting["AB"], 3) if batting["AB"] else 0.0
    pitching = {col: sum(int0(row.get(col)) for row in pitching_rows) for col in PIT_COLS if col != "IP"}
    pitching["GP"] = len({g["game_id"] for g in games if g.get("pitching")})
    pitching["IP"] = thirds_to_ip(sum(ip_to_thirds(row.get("IP")) for row in pitching_rows))
    return {"batting": batting, "pitching": pitching}


def build_report(db_path: Path, out_path: Path, names: list[str]) -> dict[str, Any]:
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}. Run gc_player_stats.py first.")
    wanted = {clean_name(name): name for name in names}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT p.player_id, p.name, p.roster_team, l.section, l.stats, l.positions, "
        "g.game_id, g.date, g.opponent, g.home_away, g.source_url, "
        "COALESCE(t.gc_name, '') AS gc_name, COALESCE(t.ncs_teams, '[]') AS ncs_teams "
        "FROM players p "
        "LEFT JOIN stat_lines l ON l.player_id = p.player_id "
        "LEFT JOIN games g ON g.game_id = l.game_id "
        "LEFT JOIN teams t ON t.gc_url = g.gc_url "
        "WHERE lower(p.name) IN ({}) "
        "ORDER BY p.name, g.date DESC".format(",".join("?" for _ in wanted))
        , list(wanted.keys())
    ).fetchall()
    conn.close()

    players: dict[str, dict[str, Any]] = {}
    found = set()
    for row in rows:
        pid = row["player_id"]
        found.add(clean_name(row["name"]))
        player = players.setdefault(pid, {
            "player_id": pid,
            "name": row["name"],
            "roster_team": row["roster_team"] or "",
            "games": [],
        })
        if not row["game_id"]:
            continue
        game = next((g for g in player["games"] if g["game_id"] == row["game_id"]), None)
        if game is None:
            try:
                ncs_teams = json.loads(row["ncs_teams"] or "[]")
            except json.JSONDecodeError:
                ncs_teams = []
            game = {
                "game_id": row["game_id"],
                "date": row["date"],
                "team": ncs_teams[0] if ncs_teams else row["gc_name"],
                "gc_team": row["gc_name"],
                "opponent": row["opponent"],
                "home_away": row["home_away"],
                "source_url": row["source_url"],
            }
            player["games"].append(game)
        try:
            stats = json.loads(row["stats"] or "{}")
        except json.JSONDecodeError:
            stats = {}
        if row["section"] in ("batting", "pitching"):
            game[row["section"]] = stats
        if row["section"] == "batting" and row["positions"]:
            game["positions"] = row["positions"]

    for player in players.values():
        player["games"].sort(key=lambda g: g.get("date") or "", reverse=True)
        player["totals"] = totals_for(player["games"])

    missing = [original for key, original in wanted.items() if key not in found]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": db_path.name,
        "selected_names": names,
        "missing_names": missing,
        "players": players,
    }
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Export batting and pitching stats for selected players from gc_stats.db")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--out", default=str(OUT_JSON))
    parser.add_argument("--players-file", default=str(NAMES_FILE))
    parser.add_argument("--player", action="append")
    args = parser.parse_args()
    names = read_names(Path(args.players_file), args.player)
    if not names:
        raise SystemExit("No names supplied.")
    report = build_report(Path(args.db), Path(args.out), names)
    with_games = sum(1 for p in report["players"].values() if p.get("games"))
    print(f"Exported {with_games}/{len(report['players'])} selected players -> {args.out}")
    if report["missing_names"]:
        print("Missing from DB: " + ", ".join(report["missing_names"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
