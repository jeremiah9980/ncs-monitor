#!/usr/bin/env python3
"""
Export GameChanger leaderboards from gc_stats.db.

Creates reports/gc-player-leaders.json for gc-leaderboard.html.
Leaderboards are grouped by GameChanger season and by calendar year.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "gc_stats.db"
OUT_JSON = ROOT / "reports" / "gc-player-leaders.json"
SNAPSHOT = ROOT / "snapshots" / "latest.json"

SEASON_RE = re.compile(r"^(20\d{2})\s+Fastpitch", re.I)
GC_SEASON_RE = re.compile(r"\b(Winter|Spring|Summer|Fall)\s+(20\d{2})\b", re.I)
SEASON_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}

BAT_INT_COLS = ("AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB")
PIT_INT_COLS = ("H", "R", "ER", "BB", "SO", "HR")

BAT_METRICS = [
    {"key": "HR", "label": "Home Runs", "type": "count", "abbr": "HR"},
    {"key": "3B", "label": "Triples", "type": "count", "abbr": "3B"},
    {"key": "2B", "label": "Doubles", "type": "count", "abbr": "2B"},
    {"key": "H", "label": "Hits", "type": "count", "abbr": "H"},
    {"key": "RBI", "label": "Runs Batted In", "type": "count", "abbr": "RBI"},
    {"key": "R", "label": "Runs", "type": "count", "abbr": "R"},
    {"key": "SB", "label": "Stolen Bases", "type": "count", "abbr": "SB"},
    {"key": "BB", "label": "Walks", "type": "count", "abbr": "BB"},
    {"key": "AVG", "label": "Batting Average", "type": "rate", "abbr": "AVG"},
]
PIT_METRICS = [
    {"key": "SO", "label": "Strikeouts", "type": "count", "abbr": "K"},
    {"key": "IP", "label": "Innings Pitched", "type": "innings", "abbr": "IP"},
    {"key": "ER", "label": "Earned Runs Allowed", "type": "low", "abbr": "ER"},
    {"key": "BB", "label": "Walks Allowed", "type": "low", "abbr": "BB"},
]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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


def avg(hits: int, at_bats: int) -> float:
    return round(hits / at_bats, 3) if at_bats else 0.0


def season_sort_key(label: str) -> tuple[int, int, str]:
    m = GC_SEASON_RE.search(label or "")
    if not m:
        return (0, 0, label or "")
    return (int(m.group(2)), SEASON_ORDER.get(m.group(1).lower(), 0), label)


def season_year(season: str) -> int:
    m = SEASON_RE.match(season or "")
    return int(m.group(1)) if m else 0


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS players (
        player_id   TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        roster_team TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS teams (
        gc_url    TEXT PRIMARY KEY,
        gc_name   TEXT DEFAULT '',
        season    TEXT DEFAULT '',
        ncs_teams TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS games (
        game_id    TEXT PRIMARY KEY,
        gc_url     TEXT NOT NULL,
        date       TEXT NOT NULL,
        opponent   TEXT DEFAULT '',
        home_away  TEXT DEFAULT '',
        source_url TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS stat_lines (
        game_id   TEXT NOT NULL,
        player_id TEXT NOT NULL,
        section   TEXT NOT NULL,
        stats     TEXT NOT NULL,
        positions TEXT DEFAULT '',
        PRIMARY KEY (game_id, player_id, section)
    );
    """)


def load_snapshot_players() -> dict[str, dict[str, Any]]:
    if not SNAPSHOT.exists():
        return {}
    snap = json.loads(SNAPSHOT.read_text())
    details = snap.get("player_details", {})
    players: dict[str, dict[str, Any]] = {}

    for _team_key, team in snap.get("teams", {}).items():
        roster_team = (team.get("team_name") or "").strip()
        for p in team.get("players", []):
            pid = str(p.get("player_id") or "").strip()
            if not pid:
                continue
            rec = players.setdefault(pid, {
                "name": p.get("name") or "Unknown player",
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


def decode_team_list(value: str) -> list[str]:
    try:
        data = json.loads(value or "[]")
        return [str(x) for x in data if x]
    except json.JSONDecodeError:
        return []


def new_bucket(player_id: str, name: str, roster_team: str, current_teams: list[str], last_season_teams: list[str]) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "name": name,
        "roster_team": roster_team,
        "current_teams": current_teams,
        "last_season_teams": last_season_teams,
        "team_labels": set(),
        "games": set(),
        "batting": {col: 0 for col in BAT_INT_COLS},
        "pitching": {col: 0 for col in PIT_INT_COLS},
        "ip_thirds": 0,
        "pitching_games": set(),
    }


def materialize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    batting = dict(bucket["batting"])
    batting["GP"] = len(bucket["games"])
    batting["AVG"] = avg(batting.get("H", 0), batting.get("AB", 0))

    pitching = dict(bucket["pitching"])
    pitching["GP"] = len(bucket["pitching_games"])
    pitching["IP"] = thirds_to_ip(bucket["ip_thirds"])
    pitching["IP_thirds"] = bucket["ip_thirds"]

    return {
        "player_id": bucket["player_id"],
        "name": bucket["name"],
        "roster_team": bucket["roster_team"],
        "current_teams": bucket["current_teams"],
        "last_season_teams": bucket["last_season_teams"],
        "team_labels": sorted(bucket["team_labels"]),
        "batting": batting,
        "pitching": pitching,
    }


def add_to_period(periods: dict[str, dict[str, dict[str, Any]]], period: str, base: dict[str, Any], row: dict[str, Any]) -> None:
    pid = base["player_id"]
    bucket = periods.setdefault(period or "Unknown", {}).setdefault(
        pid,
        new_bucket(pid, base["name"], base["roster_team"], base["current_teams"], base["last_season_teams"]),
    )
    if base.get("team_label"):
        bucket["team_labels"].add(base["team_label"])

    stats = row["stats"]
    if row["section"] == "batting":
        bucket["games"].add(row["game_id"])
        for col in BAT_INT_COLS:
            bucket["batting"][col] += _int0(stats.get(col))
    elif row["section"] == "pitching":
        bucket["pitching_games"].add(row["game_id"])
        for col in PIT_INT_COLS:
            bucket["pitching"][col] += _int0(stats.get(col))
        bucket["ip_thirds"] += ip_to_thirds(stats.get("IP"))


def ranked(entries: list[dict[str, Any]], section: str, metric: dict[str, str], limit: int, min_ab: int, min_ip_outs: int) -> list[dict[str, Any]]:
    key = metric["key"]
    typ = metric["type"]
    eligible = []
    for entry in entries:
        stats = entry[section]
        if section == "batting" and key == "AVG" and stats.get("AB", 0) < min_ab:
            continue
        if section == "pitching" and typ in ("low", "innings") and stats.get("IP_thirds", 0) < min_ip_outs:
            continue
        value = stats.get(key)
        if key == "IP":
            sort_value = stats.get("IP_thirds", 0)
        else:
            sort_value = value if isinstance(value, (int, float)) else _int0(value)
        if sort_value == 0 and typ != "low":
            continue
        eligible.append((sort_value, entry))

    reverse = typ != "low"
    eligible.sort(key=lambda pair: (pair[0], pair[1]["name"]), reverse=reverse)
    out = []
    last_value = object()
    rank = 0
    seen = 0
    for sort_value, entry in eligible[:limit]:
        seen += 1
        if sort_value != last_value:
            rank = seen
            last_value = sort_value
        stats = entry[section]
        out.append({
            "rank": rank,
            "player_id": entry["player_id"],
            "name": entry["name"],
            "roster_team": entry["roster_team"],
            "team_labels": entry["team_labels"],
            "value": stats.get(key),
            "sort_value": sort_value,
            "GP": stats.get("GP", 0),
            "AB": stats.get("AB", 0) if section == "batting" else None,
            "H": stats.get("H", 0),
            "RBI": stats.get("RBI", 0) if section == "batting" else None,
            "AVG": stats.get("AVG") if section == "batting" else None,
            "IP": stats.get("IP") if section == "pitching" else None,
        })
    return out


def build_leaderboards(period_buckets: dict[str, dict[str, dict[str, Any]]], limit: int, min_ab: int, min_ip_outs: int) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for period, by_player in period_buckets.items():
        entries = [materialize_bucket(bucket) for bucket in by_player.values()]
        output[period] = {
            "players": len(entries),
            "batting": {m["key"]: ranked(entries, "batting", m, limit, min_ab, min_ip_outs) for m in BAT_METRICS},
            "pitching": {m["key"]: ranked(entries, "pitching", m, limit, min_ab, min_ip_outs) for m in PIT_METRICS},
        }
    return dict(sorted(output.items(), key=lambda kv: season_sort_key(kv[0]) if not kv[0].isdigit() else (int(kv[0]), 9, kv[0]), reverse=True))


def build_report(db_path: Path, out_path: Path, limit: int, min_ab: int, min_ip_outs: int) -> dict[str, Any]:
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}\nRun ./run_gc_stats.sh first, then rerun this exporter.")

    snapshot_players = load_snapshot_players()
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)

    players = {}
    for pid, name, roster_team in conn.execute("SELECT player_id, name, roster_team FROM players").fetchall():
        snap = snapshot_players.get(pid, {})
        players[pid] = {
            "player_id": pid,
            "name": snap.get("name") or name,
            "roster_team": snap.get("roster_team") or roster_team or "",
            "current_teams": snap.get("current_teams", []),
            "last_season_teams": snap.get("last_season_teams", []),
        }

    seasons: dict[str, dict[str, dict[str, Any]]] = {}
    years: dict[str, dict[str, dict[str, Any]]] = {}
    overall: dict[str, dict[str, Any]] = {}

    rows = conn.execute(
        "SELECT l.player_id, l.section, l.stats, g.game_id, g.date, "
        "COALESCE(t.season, ''), COALESCE(t.gc_name, ''), COALESCE(t.ncs_teams, '') "
        "FROM stat_lines l "
        "JOIN games g ON g.game_id = l.game_id "
        "LEFT JOIN teams t ON t.gc_url = g.gc_url"
    ).fetchall()

    for pid, section, stats_json, game_id, game_date, season, gc_name, ncs_teams_json in rows:
        try:
            stats = json.loads(stats_json or "{}")
        except json.JSONDecodeError:
            continue
        player = players.get(pid, {"player_id": pid, "name": pid, "roster_team": gc_name or "", "current_teams": [], "last_season_teams": []})
        ncs_teams = decode_team_list(ncs_teams_json)
        player_teams = player.get("current_teams", []) + player.get("last_season_teams", [])
        team_label = next((name for name in ncs_teams if name in player_teams), None) or (ncs_teams[0] if ncs_teams else gc_name)
        base = dict(player)
        base["team_label"] = team_label
        row = {"section": section, "stats": stats, "game_id": game_id}
        year = str(game_date or "")[:4] if game_date else "Unknown"

        add_to_period(seasons, season or "Unknown Season", base, row)
        add_to_period(years, year or "Unknown", base, row)
        add_to_period({"Overall": overall}, "Overall", base, row)

    conn.close()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": db_path.name,
        "limit": limit,
        "minimums": {"AVG_min_AB": min_ab, "pitching_min_outs": min_ip_outs},
        "metrics": {"batting": BAT_METRICS, "pitching": PIT_METRICS},
        "seasons": build_leaderboards(seasons, limit, min_ab, min_ip_outs),
        "years": build_leaderboards(years, limit, min_ab, min_ip_outs),
        "overall": build_leaderboards({"Overall": overall}, limit, min_ab, min_ip_outs).get("Overall", {"batting": {}, "pitching": {}}),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Export GameChanger leaderboards from gc_stats.db")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite DB path. Default: gc_stats.db")
    parser.add_argument("--out", default=str(OUT_JSON), help="Output JSON path. Default: reports/gc-player-leaders.json")
    parser.add_argument("--limit", type=int, default=25, help="Players per leaderboard. Default: 25")
    parser.add_argument("--min-ab", type=int, default=10, help="Minimum AB for AVG leaderboard. Default: 10")
    parser.add_argument("--min-ip-outs", type=int, default=3, help="Minimum pitching outs for low/IP leaderboards. Default: 3")
    args = parser.parse_args()

    report = build_report(Path(args.db), Path(args.out), args.limit, args.min_ab, args.min_ip_outs)
    log(f"Exported leaders: {len(report['seasons'])} season group(s), {len(report['years'])} year group(s) -> {args.out}")
    log("Publish: git add reports/gc-player-leaders.json && git commit -m 'Update GC leaderboards' && git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
