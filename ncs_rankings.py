#!/usr/bin/env python3
"""
NCS rankings snapshotter
========================
Fetches the public NCS Team Rankings table for each (season, age, class, state)
combo and upserts the rows into ncs_stats.db's ``rankings`` table with a
captured_at timestamp, so repeated runs build a time series.

Rankings endpoint:
  GET /fastpitch/Teams/Rankings?seasonId=<S>&ageId=<A>&classificationId=<C>&usState=<ST>

ID reference (from recon):
  seasons: 2024=18, 2025=23, 2026=30, 2027=33
  ages:    10U=4, 12U=6, 14U=8
  classes: C-Rec=9, C=7, B=6, A=5, Open=68

Usage:
  python ncs_rankings.py                       # 2026+2027, 10U/12U/14U, C/B/A/Open, TX
  python ncs_rankings.py --seasons 30 --ages 4 --classes 7
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error

from bs4 import BeautifulSoup

from ncs_monitor import BASE, DEFAULT_UA, TEAM_LINK_RE, fetch_html, log, team_id_from_href
import ncs_stats_db
from ncs_stats_db import now_iso


def rankings_url(season_id, age_id, class_id, state) -> str:
    return (f"{BASE}/fastpitch/Teams/Rankings?seasonId={season_id}"
            f"&ageId={age_id}&classificationId={class_id}&usState={state}")


def _to_float(s):
    s = (s or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s):
    f = _to_float(s)
    return int(f) if f is not None else None


def parse_rankings(html: str):
    """Parse the single results table. Header:
    Rank|Team|Record|In-Class|Win %|Events Played|Events Won|Points.
    Team cell has a /Teams/Details/<id>/ anchor. Returns list of dicts."""
    soup = BeautifulSoup(html, "html.parser")
    target = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        hdr = [c.get_text(" ", strip=True).lower()
               for c in rows[0].find_all(["td", "th"])]
        if any("rank" in h for h in hdr) and any("points" in h for h in hdr):
            target = table
            break
    if not target:
        return []

    out = []
    for tr in target.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        c = [x.get_text(" ", strip=True) for x in cells]
        if len(c) < 8:
            continue
        a = tr.find("a", href=TEAM_LINK_RE)
        tid, _ = team_id_from_href(a["href"]) if a else ("", "")
        if not tid:
            continue
        out.append({
            "team_id": tid,
            "rank": _to_int(c[0]),
            "team_label": c[1] or None,
            "record": c[2] or None,
            "in_class": c[3] or None,
            "win_pct": _to_float(c[4]),
            "events_played": _to_int(c[5]),
            "events_won": _to_int(c[6]),
            "points": _to_int(c[7]),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Snapshot NCS Team Rankings -> SQLite")
    ap.add_argument("--db", default="ncs_stats.db")
    ap.add_argument("--state", default="TX")
    ap.add_argument("--seasons", default="30,33", help="seasonId list (default 2026,2027)")
    ap.add_argument("--ages", default="4,6,8", help="ageId list (default 10U,12U,14U)")
    ap.add_argument("--classes", default="7,6,5,68", help="classId list (default C,B,A,Open)")
    ap.add_argument("--delay", type=float, default=2.0)
    args = ap.parse_args()

    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    ages = [a.strip() for a in args.ages.split(",") if a.strip()]
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]

    conn = ncs_stats_db.init_db(args.db)
    ua = DEFAULT_UA
    combos = [(s, a, c) for s in seasons for a in ages for c in classes]
    total = 0
    for i, (s, a, c) in enumerate(combos):
        url = rankings_url(s, a, c, args.state)
        try:
            html = fetch_html(url, ua)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            log(f"  fetch failed season={s} age={a} class={c}: {e}")
            continue
        rows = parse_rankings(html)
        captured = now_iso()
        for r in rows:
            ncs_stats_db.upsert_ranking(conn, {
                "season_id": s, "age_id": a, "class_id": c, "state": args.state,
                "captured_at": captured, **r})
        conn.commit()
        total += len(rows)
        log(f"  season={s} age={a} class={c} state={args.state}: {len(rows)} rows")
        if i < len(combos) - 1:
            time.sleep(args.delay)

    conn.close()
    log(f"SUMMARY: {total} ranking rows across {len(combos)} combo(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
