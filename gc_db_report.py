#!/usr/bin/env python3
"""
Export gc_stats.db into reports/gc-player-stats.json for gc-player-stats.html.

Use this after gc_player_stats.py has populated the local SQLite database, or any
later time you want to regenerate the static JSON for GitHub Pages without
opening GameChanger again.
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
SNAPSHOT = ROOT / "snapshots" / "latest.json"
DB_PATH = ROOT / "gc_stats.db"
OUT_JSON = ROOT / "reports" / "gc-player-stats.json"

SEASON_RE = re.compile(r"^(20\d{2})\s+Fastpitch", re.I)
GC_SEASON_RE = re.compile(r"\b(Winter|Spring|Summer|Fall)\s+(20\d{2})\b", re.I)
SEASON_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}

BAT_SUM_COLS = ("AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB")
PIT_SUM_COLS = ("H", "R", "ER", "BB", "SO", "HR")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _int0(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def ip_to_thirds(v: Any) -> int:
    """GameChanger-style IP: 2.1 means two innings plus one out."""
    try:
        whole, _, frac = str(v or "0").partition(".")
        return int(whole or 0) * 3 + min(_int0(frac), 2)
    except ValueError:
        return 0


def thirds_to_ip(t: int) -> str:
    return f"{t // 3}.{t % 3}"


def season_year(season: str) -> int:
    m = SEASON_RE.match(season or "")
    return int(m.group(1)) if m else 0


def season_sort_key(season_text: str) -> tuple[int, int]:
    m = GC_SEASON_RE.search(season_text or "")
    if not m:
        return (0, 0)
    return (int(m.group(2)), SEASON_ORDER.get(m.group(1).lower(), 0))


def load_snapshot_players(current_only: bool = False) -> dict[str, dict[str, Any]]:
    """Load current/previous NCS teams so DB stat lines can be labeled nicely."""
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
            if roster_team and not rec.get("roster_team"):
                rec["roster_team"] = roster_team
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
                if year == cur_year and status in ("active", "guest"):
                    if name not in rec["current_teams"]:
                        rec["current_teams"].append(name)
                elif not current_only and year == last_year and status in ("past", "active", "guest", "removed"):
                    if name not in rec["last_season_teams"]:
                        rec["last_season_teams"].append(name)
            if roster_team and roster_team not in rec["current_teams"]:
                rec["current_teams"].append(roster_team)
    return players


def ensure_db_schema(conn: sqlite3.Connection) -> None:
    """Create report-side tables/indexes if a partial DB exists."""
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
    CREATE TABLE IF NOT EXISTS season_totals (
        player_id  TEXT NOT NULL,
        gc_url     TEXT NOT NULL,
        season     TEXT DEFAULT '',
        section    TEXT NOT NULL,
        games      INTEGER NOT NULL,
        stats      TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (player_id, gc_url, section)
    );
    CREATE INDEX IF NOT EXISTS idx_lines_player ON stat_lines (player_id);
    CREATE INDEX IF NOT EXISTS idx_games_team ON games (gc_url);
    """)


def recompute_season_totals(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT l.player_id, g.gc_url, t.season, l.section, l.stats "
        "FROM stat_lines l "
        "JOIN games g ON g.game_id = l.game_id "
        "LEFT JOIN teams t ON t.gc_url = g.gc_url"
    ).fetchall()

    agg: dict[tuple[str, str, str], dict[str, Any]] = {}
    for pid, gc_url, season, section, stats_json in rows:
        key = (pid, gc_url, section)
        entry = agg.setdefault(key, {"season": season or "", "games": 0, "sums": {}, "ip_thirds": 0})
        try:
            stats = json.loads(stats_json or "{}")
        except json.JSONDecodeError:
            continue
        entry["games"] += 1
        cols = BAT_SUM_COLS if section == "batting" else PIT_SUM_COLS
        for col in cols:
            entry["sums"][col] = entry["sums"].get(col, 0) + _int0(stats.get(col))
        if section == "pitching":
            entry["ip_thirds"] += ip_to_thirds(stats.get("IP"))

    with conn:
        conn.execute("DELETE FROM season_totals")
        for (pid, gc_url, section), entry in agg.items():
            sums = dict(entry["sums"])
            if section == "batting":
                sums["AVG"] = round(sums.get("H", 0) / sums["AB"], 3) if sums.get("AB") else 0.0
            else:
                sums["IP"] = thirds_to_ip(entry["ip_thirds"])
            conn.execute(
                "INSERT OR REPLACE INTO season_totals VALUES (?,?,?,?,?,?,?)",
                (pid, gc_url, entry["season"], section, entry["games"], json.dumps(sums), now),
            )


def json_list(value: str) -> list[str]:
    try:
        loaded = json.loads(value or "[]")
        return [str(x) for x in loaded if x]
    except json.JSONDecodeError:
        return []


def pick_team_label(player: dict[str, Any], ncs_teams: list[str], gc_name: str) -> str:
    player_teams = player.get("current_teams", []) + player.get("last_season_teams", [])
    return next((name for name in ncs_teams if name in player_teams), None) or \
        (ncs_teams[0] if ncs_teams else gc_name or player.get("roster_team") or "GameChanger")


def finalize_player(player: dict[str, Any], max_games: int) -> None:
    player["games"].sort(key=lambda g: g.get("date", ""), reverse=True)
    player["games"] = player["games"][:max_games]
    bat = [g["batting"] for g in player["games"] if g.get("batting")]
    totals = {col: sum(_int0(row.get(col)) for row in bat) for col in ("AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB")}
    totals["GP"] = len(player["games"])
    totals["AVG"] = round(totals["H"] / totals["AB"], 3) if totals["AB"] else 0.0
    player["totals"] = totals


def attach_season_totals(conn: sqlite3.Connection, report_players: dict[str, dict[str, Any]]) -> None:
    rows = conn.execute("SELECT player_id, season, section, games, stats FROM season_totals").fetchall()
    latest: dict[str, tuple[int, int]] = {}
    for pid, season, _section, _games, _stats_json in rows:
        if pid not in report_players:
            continue
        key = season_sort_key(season or "")
        if key > latest.get(pid, (-1, -1)):
            latest[pid] = key

    out: dict[str, dict[str, Any]] = {}
    for pid, season, section, games, stats_json in rows:
        if pid not in report_players or season_sort_key(season or "") != latest.get(pid):
            continue
        try:
            stats = json.loads(stats_json or "{}")
        except json.JSONDecodeError:
            continue
        line = out.setdefault(pid, {"label": season or "", "batting": None, "pitching": None})
        cur = line.get(section)
        if cur is None:
            stats["GP"] = games
            line[section] = stats
        elif section == "batting":
            cur["GP"] = cur.get("GP", 0) + games
            for col in BAT_SUM_COLS:
                cur[col] = _int0(cur.get(col)) + _int0(stats.get(col))
            cur["AVG"] = round(cur["H"] / cur["AB"], 3) if cur.get("AB") else 0.0
        elif section == "pitching":
            cur["GP"] = cur.get("GP", 0) + games
            for col in PIT_SUM_COLS:
                cur[col] = _int0(cur.get(col)) + _int0(stats.get(col))
            cur["IP"] = thirds_to_ip(ip_to_thirds(cur.get("IP")) + ip_to_thirds(stats.get("IP")))

    for pid, season in out.items():
        report_players[pid]["season"] = season


def build_report(db_path: Path, out_path: Path, max_games: int, current_only: bool) -> dict[str, Any]:
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}\nRun: ./run_gc_stats.sh --teams \"Venom\" first.")

    conn = sqlite3.connect(db_path)
    ensure_db_schema(conn)
    recompute_season_totals(conn)

    snapshot_players = load_snapshot_players(current_only=current_only)
    report_players: dict[str, dict[str, Any]] = {}

    for pid, name, roster_team in conn.execute("SELECT player_id, name, roster_team FROM players").fetchall():
        base = snapshot_players.get(pid, {})
        report_players[pid] = {
            "name": base.get("name") or name,
            "roster_team": base.get("roster_team") or roster_team or "",
            "current_teams": base.get("current_teams") or ([roster_team] if roster_team else []),
            "last_season_teams": [] if current_only else base.get("last_season_teams", []),
            "games": [],
        }

    rows = conn.execute(
        "SELECT l.player_id, l.section, l.stats, l.positions, "
        "g.game_id, g.date, g.opponent, g.home_away, g.source_url, g.gc_url, "
        "COALESCE(t.gc_name, ''), COALESCE(t.ncs_teams, '') "
        "FROM stat_lines l "
        "JOIN games g ON g.game_id = l.game_id "
        "LEFT JOIN teams t ON t.gc_url = g.gc_url "
        "ORDER BY g.date DESC"
    ).fetchall()

    for pid, section, stats_json, positions, game_id, date, opponent, home_away, source_url, _gc_url, gc_name, ncs_teams_json in rows:
        if pid not in report_players:
            report_players[pid] = {
                "name": pid,
                "roster_team": gc_name or "GameChanger",
                "current_teams": [gc_name] if gc_name else [],
                "last_season_teams": [],
                "games": [],
            }
        try:
            stats = json.loads(stats_json or "{}")
        except json.JSONDecodeError:
            continue
        player = report_players[pid]
        ncs_teams = json_list(ncs_teams_json)
        entry = next((g for g in player["games"] if g["game_id"] == game_id), None)
        if entry is None:
            entry = {
                "game_id": game_id,
                "date": date,
                "team": pick_team_label(player, ncs_teams, gc_name),
                "gc_team": gc_name,
                "opponent": opponent,
                "home_away": home_away,
                "source_url": source_url,
            }
            player["games"].append(entry)
        entry[section] = stats
        if section == "batting" and positions:
            entry["positions"] = positions

    for player in report_players.values():
        finalize_player(player, max_games)

    attach_season_totals(conn, report_players)

    teams_scraped = {}
    for gc_url, gc_name, ncs_teams, games in conn.execute(
        "SELECT t.gc_url, t.gc_name, t.ncs_teams, COUNT(DISTINCT g.game_id) "
        "FROM teams t LEFT JOIN games g ON g.gc_url = t.gc_url "
        "GROUP BY t.gc_url, t.gc_name, t.ncs_teams"
    ).fetchall():
        teams_scraped[gc_url] = {
            "ncs_teams": json_list(ncs_teams),
            "gc_name": gc_name,
            "games": games,
        }

    conn.close()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_games": max_games,
        "source": str(db_path.name),
        "teams_scraped": teams_scraped,
        "players": report_players,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Export gc_stats.db to reports/gc-player-stats.json")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite DB path. Default: gc_stats.db")
    parser.add_argument("--out", default=str(OUT_JSON), help="Output JSON path. Default: reports/gc-player-stats.json")
    parser.add_argument("--max-games", type=int, default=10, help="Recent games per player to publish. Default: 10")
    parser.add_argument("--current-only", action="store_true", help="Do not label/attach last-season NCS teams from snapshots/latest.json")
    args = parser.parse_args()

    report = build_report(Path(args.db), Path(args.out), args.max_games, args.current_only)
    with_stats = sum(1 for p in report["players"].values() if p.get("games"))
    log(f"Exported {with_stats}/{len(report['players'])} player(s) with stats -> {args.out}")
    log("To publish on GitHub Pages: git add reports/gc-player-stats.json && git commit -m 'Update GC player stats' && git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
