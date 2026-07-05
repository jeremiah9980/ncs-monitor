#!/usr/bin/env python3
"""
NCS stats collector
===================
Discovers NCS fastpitch teams in a set of central-TX cities (reusing the
existing ncs_monitor scraper), then pulls each team's season stats, per-class
records, per-tournament results, full game log, and roster into a SQLite DB
(ncs_stats.db). It then walks every rostered player's page for their bio (age,
location) and roster history, linking each player to the teams/seasons they've
played on.

NCS does NOT publish per-player stat lines, so this DB is team-level stats plus
a player -> team/season linkage. See docs/STATS.md.

All table parsing is done by classifying tables on their header/label TEXT
(not positional index), so a brand-new team whose stat tables are present but
zeroed -- and has no tournament/game rows -- parses cleanly to empty lists.

Usage:
  python collect_stats.py                          # discover + collect (six default cities)
  python collect_stats.py --teams "Cortinas"       # collect by team NAME (city filter off)
  python collect_stats.py --limit 3                # cap to 3 teams (quick test)
  python collect_stats.py --dry-run                # parse + print, no DB writes
  python collect_stats.py --input team.html --team-id <url>   # offline single team
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
from pathlib import Path

from bs4 import BeautifulSoup

import ncs_monitor
from ncs_monitor import (
    BASE,
    DEFAULT_UA,
    PLAYER_LINK_RE,
    TEAM_LINK_RE,
    canonical_team_url,
    discover,
    fetch_html,
    load_config,
    log,
    parse_player_details,
    parse_roster,
    team_id_from_href,
)

import ncs_stats_db

ROOT = Path(__file__).resolve().parent
DEFAULT_CITIES = ["Round Rock", "Georgetown", "Leander", "Hutto",
                  "Cedar Park", "Pflugerville"]


# ---------------------------------------------------------------------------
# small tolerant number parsers
# ---------------------------------------------------------------------------
def _to_float(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s == "" or s in ("-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s):
    f = _to_float(s)
    return int(f) if f is not None else None


def _cells(tr):
    return [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]


def _header_cells(table):
    rows = table.find_all("tr")
    return _cells(rows[0]) if rows else []


# ---------------------------------------------------------------------------
# team page parsers -- classify tables by header/label text
# ---------------------------------------------------------------------------
def _kv_table(soup, label):
    """Return the key/value table whose first row's single cell == label
    (e.g. 'Record' or 'Stats'), or None."""
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        first = _cells(rows[0])
        if first and first[0].strip().lower() == label.lower():
            return table
    return None


def parse_team_stats(soup) -> dict:
    """Extract the Record + Stats key/value fields from a team page.

    Returns a dict with keys: record, win_pct, last10, streak, avg_finish,
    ranking_points, avg_runs_scored, avg_runs_allowed, avg_run_diff,
    runs_scored, runs_allowed. Missing values are None.
    """
    kv = {}
    for label in ("Record", "Stats"):
        table = _kv_table(soup, label)
        if not table:
            continue
        for tr in table.find_all("tr")[1:]:
            c = _cells(tr)
            if len(c) >= 2:
                kv[c[0].strip().lower()] = c[1].strip()

    def g(k):
        return kv.get(k)

    return {
        "record": g("w-l-t"),
        "win_pct": _to_float(g("win %")),
        "last10": g("last 10"),
        "streak": g("streak") or None,
        "avg_finish": _to_float(g("avg finish")),
        "ranking_points": _to_int(g("ranking points")),
        "avg_runs_scored": _to_float(g("avg runs scored")),
        "avg_runs_allowed": _to_float(g("avg runs allowed")),
        "avg_run_diff": _to_float(g("avg runs difference")),
        "runs_scored": _to_int(g("runs scored")),
        "runs_allowed": _to_int(g("runs allowed")),
    }


def parse_class_records(soup):
    """Return list of (opponent_class, record) from the 'vs. Classes' table."""
    out = []
    table = _kv_table(soup, "vs. Classes")
    if not table:
        return out
    for tr in table.find_all("tr")[1:]:
        c = _cells(tr)
        if len(c) >= 2 and c[0].strip():
            out.append((c[0].strip(), c[1].strip()))
    return out


def _is_tournament_table(table):
    hdr = [h.lower() for h in _header_cells(table)]
    if any("place" in h for h in hdr) and any("event" in h for h in hdr):
        return True
    cls = table.get("class") or []
    return "table-convert" in cls


def parse_tournament_results(soup):
    """Return list of tournament-result dicts. Identifies the table by a header
    containing 'Place' and 'Event' (or the table-convert class). Tolerates an
    absent table (brand-new team) -> []."""
    out = []
    target = None
    for table in soup.find_all("table"):
        if _is_tournament_table(table):
            target = table
            break
    if not target:
        return out
    for tr in target.find_all("tr")[1:]:
        c = _cells(tr)
        if len(c) < 10:
            continue
        out.append({
            "place": c[0] or None,
            "points": _to_int(c[1]),
            "date": c[2] or None,
            "event": c[3] or None,
            "division": c[4] or None,
            "record": c[5] or None,
            "avg_rs": _to_float(c[6]),
            "max_rs": _to_float(c[7]),
            "avg_ra": _to_float(c[8]),
            "avg_rd": _to_float(c[9]),
        })
    return out


def _is_game_log_table(table):
    hdr = [h.lower() for h in _header_cells(table)]
    return any("score" in h for h in hdr) and any("teams" in h for h in hdr)


def parse_game_log(soup):
    """Return list of game dicts from the game-log table. Each game spans two
    <tr>: row1 = [Date, our_score, our_name, our_state, our_class] (Date may
    carry rowspan=2) and row2 = [opp_score, opp_name, opp_state, opp_class,
    opp_record, opp_last10, opp_streak]. Result derived by comparing scores.
    Malformed/empty rows are skipped; an absent table -> []."""
    out = []
    target = None
    for table in soup.find_all("table"):
        if _is_game_log_table(table):
            target = table
            break
    if not target:
        return out

    rows = target.find_all("tr")[1:]  # skip header
    idx = 0
    pending = None  # cells of a seen row1 awaiting its row2
    for tr in rows:
        c = _cells(tr)
        if len(c) >= 8:
            # start of a new game (row1); if a prior row1 had no partner, drop it
            pending = c
        elif len(c) == 7 and pending is not None:
            row1, row2 = pending, c
            pending = None
            date = row1[0].strip()
            team_score = _to_int(row1[1])
            opp_score = _to_int(row2[0])
            if team_score is None or opp_score is None:
                idx += 1
                continue
            if team_score > opp_score:
                result = "W"
            elif team_score < opp_score:
                result = "L"
            else:
                result = "T"
            opponent = row2[1].strip()
            game_uid = f"{idx}|{date}|{opponent}|{team_score}-{opp_score}"
            out.append({
                "game_uid": game_uid,
                "date": date or None,
                "team_score": team_score,
                "opp_score": opp_score,
                "result": result,
                "opponent": opponent or None,
                "opp_state": row2[2].strip() or None,
                "opp_class": row2[3].strip() or None,
                "opp_record": row2[4].strip() or None,
            })
            idx += 1
    return out


# ---------------------------------------------------------------------------
# player page parsing -- team_id + location per roster-history row
# ---------------------------------------------------------------------------
def _history_team_meta(soup):
    """Walk tables the same way parse_player_details does, returning a parallel
    list of (team_id, location) so it can be zipped with team_history."""
    meta = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            team_link = row.find("a", href=TEAM_LINK_RE)
            if not team_link:
                continue
            tid, _ = team_id_from_href(team_link["href"])
            c = _cells(row)
            # City/State column: first cell *after* the team-name/location cell
            # containing ", XX" (skip c[0], which holds the team link + its city).
            location = next((x for x in c[1:] if re.search(r",\s*[A-Za-z]{2}\.?$", x)), "")
            meta.append((tid or None, location or None))
    return meta


def parse_player(html: str, player_id: str) -> dict:
    """Parse a player page -> {player_id, name, age, location, url, history}."""
    soup = BeautifulSoup(html, "html.parser")
    details = parse_player_details(html)
    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else ""
    meta = _history_team_meta(soup)
    history = []
    for i, h in enumerate(details.get("team_history", [])):
        tid, loc = meta[i] if i < len(meta) else (None, None)
        history.append({
            "player_id": player_id,
            "team_name": h.get("team", ""),
            "team_id": tid,
            "division": h.get("division", ""),
            "season": h.get("season", ""),
            "status": h.get("status", ""),
            "location": loc,
        })
    return {
        "player_id": player_id,
        "name": name,
        "age": details.get("age", ""),
        "location": details.get("location", ""),
        "url": f"{BASE}/fastpitch/Players/Details/{player_id}/",
        "history": history,
    }


# ---------------------------------------------------------------------------
# team collection
# ---------------------------------------------------------------------------
def collect_team(soup, team_meta: dict, conn, counts: dict, dry_run: bool):
    """Parse all team-level tables + roster from ``soup`` and upsert them.
    Returns the set of player_ids found on the roster."""
    tid = team_meta["team_id"]
    stats = parse_team_stats(soup)
    team_row = {
        "team_id": tid,
        "name": team_meta.get("name") or "",
        "division": team_meta.get("division") or "",
        "city": team_meta.get("city") or "",
        "region": team_meta.get("region") or "",
        "url": team_meta.get("url") or canonical_team_url(tid),
        **stats,
    }
    class_records = parse_class_records(soup)
    tournaments = parse_tournament_results(soup)
    games = parse_game_log(soup)
    roster = parse_roster(soup)

    log(f"  team {tid} '{team_row['name']}': record={stats.get('record')}, "
        f"{len(tournaments)} tourneys, {len(games)} games, {len(roster)} players")

    if not dry_run:
        ncs_stats_db.upsert_team(conn, team_row)
        for oc, rec in class_records:
            ncs_stats_db.upsert_team_class_record(conn, tid, oc, rec)
        for t in tournaments:
            ncs_stats_db.upsert_tournament_result(conn, tid, t)
        for g in games:
            ncs_stats_db.upsert_game(conn, tid, g)
        for p in roster:
            ncs_stats_db.upsert_roster(conn, tid, p["player_id"], p.get("num", ""))
        conn.commit()

    counts["teams"] += 1
    counts["tournaments"] += len(tournaments)
    counts["games"] += len(games)
    counts["roster"] += len(roster)
    return {p["player_id"] for p in roster}


def filter_teams_by_name(teams, patterns):
    """Return the subset of ``teams`` whose ``name`` (lowercased) contains ANY
    of the given case-insensitive substring ``patterns``.

    ``patterns`` is a list of substrings (e.g. ["cortinas"] or
    ["bombers", "outlaws"]). Empty/whitespace-only patterns are ignored; if no
    usable patterns remain the input list is returned unchanged. Matching is a
    plain case-insensitive substring test, so "cortinas" matches
    "CTX Bombers Cortinas". Kept testable/offline: no network or config access.
    """
    pats = [p.strip().lower() for p in patterns if p and p.strip()]
    if not pats:
        return list(teams)
    out = []
    for t in teams:
        name = (t.get("name") or "").lower()
        if any(p in name for p in pats):
            out.append(t)
    return out


def build_team_list(cfg: dict, cities, ua: str, delay: float):
    """Discover teams filtered to ``cities`` (overriding config), union with the
    manual ``teams:`` list from config. Deduped by team_id. Does NOT touch the
    monitor's discovered_teams.json cache."""
    disc = dict(cfg.get("discovery") or {})
    disc["central_tx_cities"] = list(cities)
    run_cfg = {**cfg, "discovery": disc}
    teams, failures = discover(run_cfg, ua, delay)
    if failures:
        log(f"Discovery: {failures} event page(s) failed this run")
    by_id = {t["team_id"]: t for t in teams}
    for mt in (cfg.get("teams") or []):
        url = mt.get("url") or canonical_team_url(str(mt.get("id", "")))
        tid, slug = team_id_from_href(url)
        if tid:
            by_id.setdefault(tid, {
                "team_id": tid, "name": mt.get("name", ""), "division": "",
                "city": "", "region": "", "url": canonical_team_url(tid, slug),
            })
    return list(by_id.values())


def main() -> int:
    ap = argparse.ArgumentParser(description="NCS team-stats collector -> SQLite")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--db", default=str(ROOT / "ncs_stats.db"))
    ap.add_argument("--cities", default=None,
                    help="Comma-separated city list to filter discovery "
                         f"(default: {', '.join(DEFAULT_CITIES)}). Ignored when "
                         "--teams is set.")
    ap.add_argument("--teams", default=None,
                    help="Comma-separated, case-insensitive team-NAME substrings "
                         "(e.g. \"Cortinas\" or \"Cortinas,Bombers\"). When set, "
                         "the city filter is disabled so discovery spans all "
                         "age-matching teams in the crawled events, then only "
                         "teams whose name matches a substring are kept. Takes "
                         "precedence over --cities. NOTE: only finds teams that "
                         "appear in a crawled (seeded + auto-discovered) event.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap number of teams (and players) processed")
    ap.add_argument("--input", help="Parse a saved team HTML file (offline test)")
    ap.add_argument("--team-id", help="URL/id to attach to --input")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and print, but do not write the DB")
    ap.add_argument("--delay", type=float, default=None,
                    help="Override seconds between live requests")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    ua = cfg.get("user_agent", DEFAULT_UA)
    delay = args.delay if args.delay is not None else float(
        cfg.get("request_delay_seconds", 2))

    conn = None if args.dry_run else ncs_stats_db.init_db(args.db)
    counts = {"teams": 0, "players": 0, "roster": 0,
              "tournaments": 0, "games": 0}

    # ---- offline single-team path -----------------------------------------
    if args.input:
        html = Path(args.input).read_text()
        soup = BeautifulSoup(html, "html.parser")
        tid, _ = team_id_from_href(args.team_id or "")
        team_meta = {"team_id": tid or "input", "name": "", "division": "",
                     "city": "", "region": "",
                     "url": args.team_id or f"file://{args.input}"}
        collect_team(soup, team_meta, conn, counts, args.dry_run)
        if conn:
            conn.close()
        log(f"Done (offline). teams={counts['teams']} tourneys="
            f"{counts['tournaments']} games={counts['games']} "
            f"roster={counts['roster']}")
        return 0

    # ---- discovery --------------------------------------------------------
    if args.teams:
        patterns = [p.strip() for p in args.teams.split(",") if p.strip()]
        if args.cities is not None:
            log("Note: --cities is ignored when --teams is set "
                "(team-name filter takes precedence)")
        # Empty city list -> discover() applies only the age-prefix filter, so
        # we see ALL age-matching teams in the crawled events, then keep the
        # ones whose name matches.
        log(f"Discovering all age-matching teams (city filter disabled) to "
            f"name-match {patterns}")
        all_teams = build_team_list(cfg, [], ua, delay)
        watch = filter_teams_by_name(all_teams, patterns)
        log(f"Team-name filter: matched {len(watch)} team(s) for {patterns}")
        if not watch:
            log("  0 matches -- the requested team(s) may not be registered in "
                "any crawled event (seeded + auto-discovered). Only teams that "
                "appear in a crawled central-TX event can be found this way.")
    else:
        cities_arg = args.cities if args.cities is not None else ",".join(DEFAULT_CITIES)
        cities = [c.strip() for c in cities_arg.split(",") if c.strip()]
        log(f"Discovering teams in: {', '.join(cities)}")
        watch = build_team_list(cfg, cities, ua, delay)
    if args.limit:
        watch = watch[:args.limit]
    log(f"Collecting {len(watch)} team(s)")

    # ---- team pass --------------------------------------------------------
    all_player_ids: set[str] = set()
    for i, t in enumerate(watch):
        try:
            html = fetch_html(t["url"], ua)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            log(f"  fetch failed for {t.get('name') or t['url']}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        team_meta = {"team_id": t["team_id"], "name": t.get("name", ""),
                     "division": t.get("division", ""), "city": t.get("city", ""),
                     "region": t.get("region", ""), "url": t["url"]}
        all_player_ids |= collect_team(soup, team_meta, conn, counts, args.dry_run)
        if i < len(watch) - 1:
            time.sleep(delay)

    # ---- player pass ------------------------------------------------------
    player_ids = sorted(all_player_ids)
    if args.limit:
        player_ids = player_ids[:args.limit]
    log(f"Fetching {len(player_ids)} player page(s)")
    for i, pid in enumerate(player_ids):
        url = f"{BASE}/fastpitch/Players/Details/{pid}/"
        try:
            html = fetch_html(url, ua)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            log(f"  fetch failed for player {pid}: {e}")
            continue
        p = parse_player(html, pid)
        log(f"  player {pid} '{p['name']}': age={p['age']}, "
            f"{len(p['history'])} history rows")
        if not args.dry_run:
            ncs_stats_db.upsert_player(conn, {
                "player_id": pid, "name": p["name"], "age": p["age"],
                "location": p["location"], "url": p["url"]})
            for h in p["history"]:
                ncs_stats_db.upsert_player_team_history(conn, h)
            conn.commit()
        counts["players"] += 1
        if i < len(player_ids) - 1:
            time.sleep(delay)

    if conn:
        conn.close()

    log("=" * 60)
    log(f"SUMMARY: teams={counts['teams']}  players={counts['players']}  "
        f"roster_rows={counts['roster']}  tournament_rows={counts['tournaments']}"
        f"  game_rows={counts['games']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
