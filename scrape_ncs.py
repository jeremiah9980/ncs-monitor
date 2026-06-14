#!/usr/bin/env python3
"""
scrape_ncs.py
=============
Scrape an NCS Fastpitch event (playncs.com) into the exact JSON feed the
Primetime portal reads (matches the portal's SNAPSHOT schema).

Pages parsed (server-rendered HTML tables):
  /Events/Schedule/<id>/<slug>?division=<div>    -> pool[]  + teams seen
  /Events/Standings/<id>/<slug>?division=<div>   -> teams[] (seed, rec, rs/ra/rd/pts)
  /Events/Bracket/<id>/<slug>?division=<div>     -> champ[] + silver[]

Bracket flow (winner/loser routing) is read straight off the page text
("Winner of Game 2" -> {"w":"C2"}), so the feed mirrors NCS exactly --
including any if-game routing on Games 9/10. No assumptions baked in.

Usage:
  python scrape_ncs.py \
    --event 12472 \
    --slug 2026-central-texas-ncs-10u-summer-state-triple-points-open-6gg \
    --division "10U OPEN" \
    --team 71355 \
    --out data/ncs-12472-10U-OPEN.json \
    --day-map Sat=2026-06-13 Sun=2026-06-14 \
    --tz -05:00

Deps:  pip install requests beautifulsoup4
Exit codes: 0 ok, 2 fetch/parse error (Action should not commit on non-zero).
"""

import argparse, json, re, sys, datetime
import requests
from bs4 import BeautifulSoup

BASE = "https://playncs.com/FASTPITCH/Events"
UA = {"User-Agent": "Mozilla/5.0 (compatible; ncs-portal-scraper/1.1; +github-actions)"}

TEAM_RE   = re.compile(r"/Teams/Details/(\d+)/")
SCORE_RE  = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,2})\b")
GAMEREF_RE= re.compile(r"\b(Winner|Loser)\s+of\s+Game\s+(\d+)", re.I)
GAMENUM_RE= re.compile(r"\bGame\s+(\d+)", re.I)
FIELD_RE  = re.compile(r"(\d+)\s*$")                       # trailing number in site name
TIME_RE   = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)", re.I)
DOW_RE    = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", re.I)

# Standard 10-game double-elim label map (cosmetic tag only; flow comes from page).
DE10_TAGS = {1:"win",2:"win",3:"win",4:"win",5:"elim",6:"elim",7:"elim",8:"chip",9:"elim",10:"chip"}


def fetch(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def to_iso(dow, hh, mm, ampm, day_map, tz):
    """Build an ISO timestamp from weekday + time text."""
    date = day_map.get(dow.title()) if dow else None
    if not date:                          # fall back to the only/first date if weekday missing
        date = sorted(day_map.values())[0] if day_map else "1970-01-01"
    h = int(hh) % 12 + (12 if ampm.upper() == "PM" else 0)
    return f"{date}T{h:02d}:{int(mm):02d}:00{tz}"


def parse_time(text, day_map, tz):
    tm = TIME_RE.search(text)
    if not tm:
        return None
    dm = DOW_RE.search(text)
    dow = dm.group(1) if dm else None
    return to_iso(dow, tm.group(1), tm.group(2), tm.group(3), day_map, tz)


def data_tables(soup):
    """Tables that actually contain team rows (skip nav/info tables)."""
    return [t for t in soup.find_all("table") if t.find("a", href=TEAM_RE)]


def field_from_row(row):
    # site link text like "Taylor Athletic Complex 5" -> 5
    site = row.find("a", href=re.compile(r"/Sites/Details/"))
    src = site.get_text(" ", strip=True) if site else row.get_text(" ", strip=True)
    fm = re.search(r"Complex\s+(\d+)", src) or FIELD_RE.search(src)
    return int(fm.group(1)) if fm else None


# ----------------------------------------------------------------------------- schedule
def parse_schedule(soup, day_map, tz):
    pool, seen = [], {}
    tables = data_tables(soup)
    if not tables:
        return pool, seen
    for row in tables[0].find_all("tr"):
        team_links = row.find_all("a", href=TEAM_RE)
        if len(team_links) < 2:
            continue                          # header / non-game row
        rowtext = row.get_text(" ", strip=True)
        gm = GAMENUM_RE.search(rowtext)
        if not gm:
            continue
        a_id = int(TEAM_RE.search(team_links[0]["href"]).group(1))
        b_id = int(TEAM_RE.search(team_links[1]["href"]).group(1))
        seen[a_id] = team_links[0].get_text(strip=True)
        seen[b_id] = team_links[1].get_text(strip=True)
        sc = SCORE_RE.search(rowtext)
        sa = int(sc.group(1)) if sc else None
        sb = int(sc.group(2)) if sc else None
        pool.append({
            "g": int(gm.group(1)),
            "start": parse_time(rowtext, day_map, tz),
            "field": field_from_row(row),
            "a": a_id, "b": b_id, "sa": sa, "sb": sb,
            "status": "final" if sc else "scheduled",
        })
    pool.sort(key=lambda g: g["g"])
    return pool, seen


# ----------------------------------------------------------------------------- standings
def parse_standings(soup):
    teams = []
    tables = data_tables(soup)
    if not tables:
        return teams
    for row in tables[0].find_all("tr"):
        link = row.find("a", href=TEAM_RE)
        if not link:
            continue
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        seed = next((int(c) for c in cells if c.isdigit()), None)
        rec = next((c for c in cells if re.fullmatch(r"\d+-\d+-\d+", c)), None)
        # numeric cells AFTER the record, in NCS column order: RA, RD, RS, PTS
        nums, after = [], False
        for c in cells:
            if c == rec:
                after = True
                continue
            if after and re.fullmatch(r"-?\d+", c):
                nums.append(int(c))
        ra, rd, rs, pts = (nums + [None, None, None, None])[:4]
        teams.append({
            "id": int(TEAM_RE.search(link["href"]).group(1)),
            "name": link.get_text(strip=True),
            "seed": seed, "rec": rec,
            "rs": rs, "ra": ra, "rd": rd, "pts": pts,
        })
    teams.sort(key=lambda t: (t["seed"] is None, t["seed"]))
    return teams


# ----------------------------------------------------------------------------- bracket
def parse_bracket_table(table, prefix, day_map, tz):
    games = []
    for row in table.find_all("tr"):
        rowtext = row.get_text(" ", strip=True)
        gm = GAMENUM_RE.search(rowtext)
        if not gm or "/Sites/Details/" not in str(row):
            continue                          # only true game rows have a field/site link
        gnum = int(gm.group(1))

        # Collect the two matchup slots in document order:
        # a slot is either a team link or a "Winner/Loser of Game N" text node.
        slots = []
        for cell in row.find_all(["td", "th"]):
            link = cell.find("a", href=TEAM_RE)
            if link:
                slots.append({"id": int(TEAM_RE.search(link["href"]).group(1))})
                continue
            ref = GAMEREF_RE.search(cell.get_text(" ", strip=True))
            if ref:
                key = "w" if ref.group(1).lower() == "winner" else "l"
                slots.append({key: prefix + ref.group(2)})
        if len(slots) < 2:
            continue

        sc = SCORE_RE.search(rowtext)
        games.append({
            "code": prefix + str(gnum),
            "g": gnum,
            "start": parse_time(rowtext, day_map, tz),
            "field": field_from_row(row),
            "tag": DE10_TAGS.get(gnum, "win"),
            "a": slots[0], "b": slots[1],
            "sa": int(sc.group(1)) if sc else None,
            "sb": int(sc.group(2)) if sc else None,
            "status": "final" if sc else "scheduled",
        })
    games.sort(key=lambda g: g["g"])
    return games


def parse_bracket(soup, day_map, tz):
    tables = data_tables(soup)               # championship table first, silver second
    champ  = parse_bracket_table(tables[0], "C", day_map, tz) if len(tables) > 0 else []
    silver = parse_bracket_table(tables[1], "S", day_map, tz) if len(tables) > 1 else []
    return champ, silver


# ----------------------------------------------------------------------------- assemble
def build(args):
    div = requests.utils.quote(args.division)
    sched_url = f"{BASE}/Schedule/{args.event}/{args.slug}?division={div}"
    stand_url = f"{BASE}/Standings/{args.event}/{args.slug}?division={div}"
    brkt_url  = f"{BASE}/Bracket/{args.event}/{args.slug}?division={div}"

    day_map = dict(kv.split("=", 1) for kv in args.day_map)

    pool, seen = parse_schedule(fetch(sched_url), day_map, args.tz)
    teams      = parse_standings(fetch(stand_url))
    champ, silver = parse_bracket(fetch(brkt_url), day_map, args.tz)

    # Backfill any team that appeared in the schedule but (rarely) not standings.
    known = {t["id"] for t in teams}
    for tid, nm in seen.items():
        if tid not in known:
            teams.append({"id": tid, "name": nm, "seed": None,
                          "rec": None, "rs": None, "ra": None, "rd": None, "pts": None})

    return {
        "event": {
            "id": args.event, "division": args.division, "name": args.name,
            "venue": args.venue, "city": args.city, "dates": args.dates,
            "scrapedAt": datetime.datetime.now(datetime.timezone.utc)
                         .astimezone().replace(microsecond=0).isoformat(),
        },
        "teams": teams, "pool": pool, "champ": champ, "silver": silver,
    }


def validate(data):
    """Print a parse summary; warn (don't fail) on surprises."""
    p, t = len(data["pool"]), len(data["teams"])
    c, s = len(data["champ"]), len(data["silver"])
    print(f"  teams={t}  pool={p}  champ={c}  silver={s}", file=sys.stderr)
    finals = sum(1 for g in data["pool"] if g["status"] == "final")
    bfinals = sum(1 for g in data["champ"] + data["silver"] if g["status"] == "final")
    print(f"  pool finals={finals}/{p}   bracket finals={bfinals}/{c+s}", file=sys.stderr)
    if t == 0 or p == 0:
        print("  WARNING: empty teams/pool -- NCS markup may have changed.", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Scrape an NCS event into the portal JSON feed.")
    ap.add_argument("--event", type=int, required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--division", required=True)
    ap.add_argument("--team", type=int, default=0, help="your team id (informational)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--day-map", nargs="+", default=["Sat=2026-06-13", "Sun=2026-06-14"],
                    help='weekday=YYYY-MM-DD pairs, e.g. Sat=2026-06-13 Sun=2026-06-14')
    ap.add_argument("--tz", default="-05:00", help="event timezone offset (CDT = -05:00)")
    ap.add_argument("--name", default="2026 Central TX NCS 10U Summer State (Triple Points) Open")
    ap.add_argument("--venue", default="Taylor Athletic Complex")
    ap.add_argument("--city", default="Taylor, TX")
    ap.add_argument("--dates", default="Jun 13\u201314, 2026")
    args = ap.parse_args()

    try:
        data = build(args)
        validate(data)
    except Exception as e:
        print(f"scrape failed: {e}", file=sys.stderr)
        return 2

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
