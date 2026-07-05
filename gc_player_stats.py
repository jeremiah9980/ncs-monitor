#!/usr/bin/env python3
"""
GC Player Stats -- last-10-game stats for every tracked NCS player
==================================================================
Bridges the NCS roster monitor to GameChanger:

  1. TEAMS    -- read snapshots/latest.json; for each player use their NCS
                 team_history to get the current team AND last season's team.
  2. MAP      -- find each of those teams on GameChanger (web.gc.com search),
                 score candidates by name similarity, save to gc_team_map.json.
                 Low-confidence matches are recorded unverified so you can fix
                 the URL by hand; hand-edited entries are never overwritten.
  3. FOLLOW   -- click "Follow" on each matched team page (best effort).
  4. GAMES    -- open each team's schedule, take the 10 most recent completed
                 games, scrape each box score (cached in gc_cache/ by game id).
  5. STORE    -- match box-score lines back to NCS players (first name + last
                 initial) and write reports/gc-player-stats.json with each
                 player's last 10 games of batting/pitching plus totals.
                 View with gc-player-stats.html.

GameChanger requires a login, so this runs LOCALLY with a cloned, already
logged-in Chrome profile (same technique as the gamechanger-stats repo).
It cannot run on GitHub Actions.

Usage:
  python gc_player_stats.py                       # full pipeline, all teams
  python gc_player_stats.py --map-only            # just build/refresh the team map
  python gc_player_stats.py --teams "Venom"       # only teams whose name matches
  python gc_player_stats.py --current-only        # skip last-season teams
  python gc_player_stats.py --skip-follow         # don't click Follow
  python gc_player_stats.py --headful             # watch the browser work

Deps: pip install -r requirements-gc.txt   (selenium, webdriver-manager, bs4)
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing beautifulsoup4. Run: pip install -r requirements-gc.txt")
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    sys.exit("Missing selenium/webdriver-manager. Run: pip install -r requirements-gc.txt")

ROOT = Path(__file__).resolve().parent
GC_BASE = "https://web.gc.com"
SNAPSHOT = ROOT / "snapshots" / "latest.json"
TEAM_MAP = ROOT / "gc_team_map.json"
CACHE_DIR = ROOT / "gc_cache" / "boxscores"
OUT_JSON = ROOT / "reports" / "gc-player-stats.json"

# Default Chrome profile location per platform; override with --profile.
if sys.platform == "darwin":
    DEFAULT_PROFILE = Path.home() / "Library/Application Support/Google/Chrome/Default"
elif sys.platform.startswith("win"):
    DEFAULT_PROFILE = Path.home() / "AppData/Local/Google/Chrome/User Data/Default"
else:
    DEFAULT_PROFILE = Path.home() / ".config/google-chrome/Default"

# Search-result hrefs are slugless ("/teams/zqb6dvBsqBCb"); team-page links
# add a season slug ("/teams/zqb6dvBsqBCb/2026-spring-.../schedule").
GC_TEAM_URL_RE = re.compile(r"/teams/([A-Za-z0-9]{6,})(?:/([a-z0-9\-]+))?", re.I)
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
# GC teams are per-season entities ("Spring 2026", "Fall 2025", ...) -- search
# results repeat the same team once per season.
GC_SEASON_RE = re.compile(r"\b(Winter|Spring|Summer|Fall)\s+(20\d{2})\b", re.I)
GC_LOCATION_RE = re.compile(r"\b([A-Z][A-Za-z .']+),\s*([A-Z]{2})\b")
SEASON_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}
MONTH_HEADER_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM)$", re.I)
SEASON_RE = re.compile(r"^(20\d{2})\s+Fastpitch", re.I)

# Words that carry no identity when comparing team names across sites.
GENERIC_TOKENS = {
    "fastpitch", "softball", "select", "team", "the", "tx", "texas", "u",
    "6u", "8u", "9u", "10u", "12u", "14u", "16u", "18u",
    "2k11", "2k12", "2k13", "2k14", "2k15",
    "a", "b", "c", "open", "gold", "black", "blue", "red", "white",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# Selenium (cloned logged-in Chrome profile -- same approach as gamechanger-stats)
# ----------------------------------------------------------------------------
# Volatile/heavy dirs that aren't needed for login state. Skipping them makes
# the clone fast and avoids copy errors while Chrome is running.
PROFILE_SKIP = {"Cache", "Code Cache", "GPUCache", "Service Worker", "DawnCache",
                "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache", "GrShaderCache",
                "OptimizationGuide", "Download Service", "blob_storage", "IndexedDB",
                "File System", "Site Characteristics Database", "Safe Browsing"}


def clone_profile(src: Path) -> Path:
    if not src.exists():
        sys.exit(f"Chrome profile not found: {src}\n"
                 "Open chrome://version in Chrome and copy the Profile Path.")
    tmp = Path(tempfile.mkdtemp(prefix="gc_profile_"))

    def ignore(directory, names):
        skip = {n for n in names if n in PROFILE_SKIP}
        skip.update(n for n in names if n.startswith("Singleton"))
        return skip

    # Chrome mutates its profile constantly while running; individual files
    # vanishing mid-copy is normal and safe to ignore -- we only need the
    # login/cookie state.
    try:
        shutil.copytree(src, tmp / "Default", dirs_exist_ok=True, ignore=ignore)
    except shutil.Error as e:
        skipped = len(e.args[0]) if e.args and isinstance(e.args[0], list) else "?"
        log(f"Profile clone: skipped {skipped} in-use file(s) (Chrome is running); continuing.")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    return tmp


def make_driver(profile: Path, headless: bool) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--user-data-dir={profile}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)


def dismiss_any_popups(driver) -> None:
    for xp in ("//button[contains(., 'Maybe later')]", "//button[contains(., 'Got it')]",
               "//button[contains(., 'Accept')]",
               "//button[@aria-label='Close' or @aria-label='Dismiss']"):
        try:
            WebDriverWait(driver, 1).until(EC.element_to_be_clickable((By.XPATH, xp))).click()
            time.sleep(0.2)
        except Exception:
            pass


def smart_scroll(driver, max_scrolls: int = 30, sleep_s: float = 0.4) -> None:
    last_height = driver.execute_script("return document.body.scrollHeight;")
    stagnant = 0
    for _ in range(max_scrolls):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(sleep_s)
        dismiss_any_popups(driver)
        new_height = driver.execute_script("return document.body.scrollHeight;")
        stagnant = stagnant + 1 if new_height <= last_height else 0
        last_height = new_height
        if stagnant >= 2:
            break


# ----------------------------------------------------------------------------
# Stage 1: players -> teams (from the NCS snapshot)
# ----------------------------------------------------------------------------
def season_year(season: str) -> int:
    m = SEASON_RE.match(season or "")
    return int(m.group(1)) if m else 0


def load_players_and_teams(current_only: bool) -> tuple[dict, dict]:
    """Returns (players, team_names).

    players[pid] = {name, current_teams: [..], last_season_teams: [..]}
    team_names[team_name] = {"players": [pid..], "kind": "current"|"last_season"}
    """
    if not SNAPSHOT.exists():
        sys.exit(f"Snapshot not found: {SNAPSHOT} -- run ncs_monitor.py first.")
    snap = json.loads(SNAPSHOT.read_text())
    details = snap.get("player_details", {})

    players: dict[str, dict] = {}
    team_names: dict[str, dict] = {}

    def add_team(name: str, pid: str, kind: str) -> None:
        entry = team_names.setdefault(name, {"players": [], "kind": kind})
        if pid not in entry["players"]:
            entry["players"].append(pid)
        if kind == "current":          # current beats last_season if a name is both
            entry["kind"] = "current"

    for tkey, team in snap.get("teams", {}).items():
        for p in team.get("players", []):
            pid = p["player_id"]
            rec = players.setdefault(pid, {
                "name": p["name"], "roster_team": team.get("team_name", ""),
                "current_teams": [], "last_season_teams": [],
            })
            hist = details.get(pid, {}).get("team_history", [])
            years = sorted({season_year(h.get("season", "")) for h in hist if season_year(h.get("season", ""))}, reverse=True)
            cur_year = years[0] if years else 0
            last_year = years[1] if len(years) > 1 else 0
            for h in hist:
                y = season_year(h.get("season", ""))
                nm = (h.get("team") or "").strip()
                if not nm:
                    continue
                status = (h.get("status") or "").lower()
                if y == cur_year and status in ("active", "guest"):
                    if nm not in rec["current_teams"]:
                        rec["current_teams"].append(nm)
                elif y == last_year and status in ("past", "active", "guest", "removed"):
                    if nm not in rec["last_season_teams"]:
                        rec["last_season_teams"].append(nm)
            # Roster team is always current even if history parsing missed it.
            if rec["roster_team"] and rec["roster_team"] not in rec["current_teams"]:
                rec["current_teams"].append(rec["roster_team"])
            for nm in rec["current_teams"]:
                add_team(nm, pid, "current")
            if not current_only:
                for nm in rec["last_season_teams"]:
                    add_team(nm, pid, "last_season")

    return players, team_names


# ----------------------------------------------------------------------------
# Stage 2: map NCS team names to GameChanger teams
# ----------------------------------------------------------------------------
def tokenize(name: str) -> set[str]:
    toks = set(re.findall(r"[a-z0-9]+", name.lower()))
    return {t for t in toks if t not in GENERIC_TOKENS and len(t) > 1}


def name_similarity(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def load_team_map() -> dict:
    if TEAM_MAP.exists():
        try:
            return json.loads(TEAM_MAP.read_text())
        except json.JSONDecodeError:
            log(f"{TEAM_MAP.name} is unreadable; starting fresh.")
    return {"_comment": "NCS team name -> GameChanger team. Set 'gc_url' by hand "
                        "for wrong/missing matches and set 'verified': true so the "
                        "auto-matcher leaves the entry alone.", "teams": {}}


def save_team_map(team_map: dict) -> None:
    TEAM_MAP.write_text(json.dumps(team_map, indent=2))


def season_sort_key(season_text: str) -> tuple[int, int]:
    """'Spring 2026' -> (2026, 1); unknown -> (0, 0). Higher = more recent."""
    m = GC_SEASON_RE.search(season_text or "")
    if not m:
        return (0, 0)
    return (int(m.group(2)), SEASON_ORDER.get(m.group(1).lower(), 0))


def gc_search_team(driver, team_name: str, home_cities: set[str]) -> list[dict]:
    """Search web.gc.com for a team name; return candidate team links.

    GC lists the same team once per season (e.g. Spring 2026 / Fall 2025 /
    Spring 2025), so candidates carry season + location parsed from the result
    card, and scoring prefers name overlap, then a Central-TX location match,
    then season recency.

    Verified result-card shape (web.gc.com/search?search=...):
      <a href="/teams/zqb6dvBsqBCb">BYBC Starfire Ochoa 12U
        Spring 2026 • Georgetown, TX • Staff: ... • 18 players</a>
    """
    from urllib.parse import quote
    driver.get(f"{GC_BASE}/search?search={quote(team_name)}")
    try:
        WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/teams/"]')))
    except Exception:
        return []
    dismiss_any_popups(driver)
    time.sleep(2.0)   # results replace the "Recently Viewed" list once loaded
    soup = BeautifulSoup(driver.page_source, "html.parser")
    cands, seen = [], set()
    for a in soup.select('a[href*="/teams/"]'):
        m = GC_TEAM_URL_RE.search(a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        # Card text is "TeamName Season • City, ST • Staff: ... • N players";
        # the team name is everything before the season marker.
        full = a.get_text(" ", strip=True)
        season_m = GC_SEASON_RE.search(full)
        if season_m:
            text = full[:season_m.start()].strip(" •-•") or (m.group(2) or "").replace("-", " ")
            card_text = full[season_m.start():][:300]
        else:
            text = full or (m.group(2) or "").replace("-", " ")
            card_text = full[:300]
        loc_m = GC_LOCATION_RE.search(card_text)
        season = f"{season_m.group(1)} {season_m.group(2)}" if season_m else ""
        city = loc_m.group(1).strip() if loc_m else ""
        state = loc_m.group(2) if loc_m else ""

        name_score = max(name_similarity(team_name, text),
                         name_similarity(team_name, (m.group(2) or "").replace("-", " ")))
        loc_bonus = 0.15 if city and city.lower() in home_cities else (0.05 if state == "TX" else 0)
        yr, part = season_sort_key(season)
        recency_bonus = 0.1 if yr >= datetime.now().year - 1 else 0
        cands.append({
            "gc_team_id": m.group(1),
            "gc_url": f"{GC_BASE}/teams/{m.group(1)}",
            "gc_name": text,
            "season": season,
            "location": f"{city}, {state}".strip(", "),
            "name_score": round(name_score, 3),
            "score": round(name_score + loc_bonus + recency_bonus, 3),
        })
    cands.sort(key=lambda c: (c["score"], season_sort_key(c["season"])), reverse=True)
    return cands[:8]


GC_FULL_URL_RE = re.compile(
    r"^https?://(?:web\.)?gc\.com/teams/([A-Za-z0-9]{6,})(?:/[^\s]*)?$", re.I)


def normalize_gc_url(value: str) -> str:
    """Accept a web.gc.com team URL or a bare Team ID pasted from the GC app
    (Team Info -> Team ID, e.g. 'mJOyqEd9wlql'). Anything else is rejected so
    a bad map entry can't send the browser to an arbitrary site."""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("http"):
        m = GC_FULL_URL_RE.match(value)
        if not m:
            log(f"  ignoring non-GameChanger url in team map: {value[:80]}")
            return ""
        return f"{GC_BASE}/teams/{m.group(1)}"
    if re.fullmatch(r"[A-Za-z0-9]{6,}", value):
        return f"{GC_BASE}/teams/{value}"
    log(f"  ignoring invalid team id in team map: {value[:80]}")
    return ""


def load_home_cities() -> set[str]:
    """Central-TX city list from config.yaml, for location-aware match scoring."""
    cfg_path = ROOT / "config.yaml"
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        cities = (cfg.get("discovery") or {}).get("central_tx_cities") or []
        return {c.lower() for c in cities}
    except Exception:
        return set()


def build_team_map(driver, team_names: dict, team_map: dict, min_score: float) -> dict:
    teams = team_map.setdefault("teams", {})
    home_cities = load_home_cities()
    todo = [n for n in team_names if not teams.get(n, {}).get("gc_url")
            or not teams.get(n, {}).get("verified")]
    log(f"Team map: {len(team_names)} team(s) needed, {len(todo)} to (re)search on GC")
    for i, name in enumerate(todo, 1):
        existing = teams.get(name, {})
        if existing.get("verified"):
            continue
        log(f"  [{i}/{len(todo)}] GC search: {name}")
        try:
            cands = gc_search_team(driver, name, home_cities)
        except Exception as e:
            log(f"    search failed: {e}")
            cands = []
        best = cands[0] if cands else None
        accepted = bool(best and best["score"] >= min_score)
        # GC lists the same team once per season; keep the older seasons of the
        # accepted team so the scraper can roll back into them when the current
        # season has fewer than --max-games completed games.
        prior = []
        if accepted:
            for c in cands[1:]:
                same_team = name_similarity(best["gc_name"], c["gc_name"]) >= 0.99 \
                    or abs(c["name_score"] - best["name_score"]) < 0.01
                if same_team and season_sort_key(c["season"]) < season_sort_key(best["season"]):
                    prior.append({"gc_url": c["gc_url"], "season": c["season"]})
        teams[name] = {
            "kind": team_names[name]["kind"],
            "gc_url": best["gc_url"] if accepted else normalize_gc_url(existing.get("gc_url", "")),
            "gc_name": best["gc_name"] if accepted else existing.get("gc_name", ""),
            "season": best["season"] if accepted else existing.get("season", ""),
            "gc_prior_urls": prior if accepted else existing.get("gc_prior_urls", []),
            "match_score": best["score"] if best else 0,
            "verified": False,
            "candidates": cands,
        }
        if not teams[name]["gc_url"]:
            log(f"    NO confident match (best score "
                f"{best['score'] if best else 0}) -- fill gc_url in {TEAM_MAP.name}")
        else:
            extra = f" (+{len(prior)} prior season(s))" if prior else ""
            log(f"    matched -> {teams[name]['gc_name']} "
                f"[{teams[name].get('season') or '?'}] ({teams[name]['match_score']}){extra}")
        time.sleep(1.0)
    # normalize any hand-pasted team ids/URLs
    for t in teams.values():
        t["gc_url"] = normalize_gc_url(t.get("gc_url", ""))
    save_team_map(team_map)
    mapped = sum(1 for t in teams.values() if t.get("gc_url"))
    log(f"Team map saved: {mapped}/{len(teams)} mapped -> {TEAM_MAP.name}")
    return team_map


# ----------------------------------------------------------------------------
# Stage 3: follow team + collect last N completed games from its schedule
# ----------------------------------------------------------------------------
def follow_team(driver, gc_url: str) -> bool:
    try:
        driver.get(gc_url)
        dismiss_any_popups(driver)
        btn = WebDriverWait(driver, 6).until(EC.element_to_be_clickable(
            (By.XPATH, "//button[normalize-space()='Follow' or contains(., 'Follow team')]")))
        btn.click()
        time.sleep(0.5)
        return True
    except Exception:
        return False   # already following, or button not found -- fine either way


def build_event_url(href: str, score_or_time: str) -> str:
    from urllib.parse import urljoin
    base = urljoin(GC_BASE, href).rstrip("/")
    base = re.sub(r"/(box-score|info)$", "", base)
    suffix = "info" if TIME_RE.match(score_or_time.strip()) else "box-score"
    return f"{base}/{suffix}"


def parse_schedule_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows, current_month = [], None
    for el in soup.select("span.ScheduleSection__sectionTitle, div.ScheduleListByMonth__dayRow"):
        if el.name == "span":
            text = el.get_text(strip=True)
            if MONTH_HEADER_RE.match(text):
                current_month = text
            continue
        if not current_month:
            continue
        day_tag = el.select_one(".ScheduleListByMonth__dateText")
        if not day_tag:
            continue
        try:
            game_date = datetime.strptime(
                f"{current_month} {day_tag.get_text(strip=True)}", "%B %Y %d").date()
        except ValueError:
            continue
        for a in el.select('a.ScheduleListByMonth__event[href*="/schedule/"]'):
            m = UUID_RE.search(a.get("href", ""))
            if not m:
                continue
            title = a.select_one(".ScheduleListByMonth__title")
            sot = a.select_one(".ScheduleListByMonth__scoreOrTimeText")
            score_or_time = sot.get_text(strip=True) if sot else ""
            url = build_event_url(a["href"], score_or_time)
            rows.append({"game_date": game_date.isoformat(), "game_id": m.group(1),
                         "event_url": url,
                         "url_type": "info" if url.endswith("/info") else "box_score",
                         "opponent": title.get_text(" ", strip=True) if title else "",
                         "score_or_time": score_or_time})
    return rows


def last_completed_games(driver, gc_url: str, max_games: int) -> list[dict]:
    sched_url = gc_url.rstrip("/") + "/schedule"
    driver.get(sched_url)
    try:
        WebDriverWait(driver, 20).until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "span.ScheduleSection__sectionTitle, a.ScheduleListByMonth__event")))
    except Exception:
        log(f"    schedule did not load: {sched_url}")
        return []
    dismiss_any_popups(driver)
    smart_scroll(driver)
    rows = [r for r in parse_schedule_rows(driver.page_source) if r["url_type"] == "box_score"]
    seen, unique = set(), []
    for r in rows:
        if r["game_id"] not in seen:
            seen.add(r["game_id"])
            unique.append(r)
    unique.sort(key=lambda r: r["game_date"], reverse=True)
    return unique[:max_games]


# ----------------------------------------------------------------------------
# Stage 4: box score scraping (ag-grid) with a per-game cache
# ----------------------------------------------------------------------------
def grid_rows(root) -> list[dict]:
    headers = {h["col-id"]: h.get_text(strip=True) for h in root.select("div.ag-header-cell[col-id]")}
    out = []
    for row in root.select('div[role="row"][row-index]'):
        rec = {headers.get(c["col-id"], c["col-id"]): c.get_text(strip=True)
               for c in row.select("div[col-id]")}
        if rec:
            out.append(rec)
    return out


def parse_box_score(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    data = {"batting": [[], []], "pitching": [[], []]}   # [away, home]
    bat_i = pit_i = 0
    for root in soup.select("div.ag-root"):
        rows = grid_rows(root)
        if not rows:
            continue
        cols = set(rows[0])
        if {"AB", "R", "H"} <= cols and bat_i < 2:
            data["batting"][bat_i] = rows
            bat_i += 1
        elif {"IP", "ER", "SO"} <= cols and pit_i < 2:
            data["pitching"][pit_i] = rows
            pit_i += 1
    away = soup.select_one('[data-testid="away-team-name"]')
    home = soup.select_one('[data-testid="home-team-name"]')
    data["away_team"] = away.get_text(strip=True) if away else ""
    data["home_team"] = home.get_text(strip=True) if home else ""
    return data


def scrape_box_score(driver, game: dict) -> dict | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{game['game_id']}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass
    driver.get(game["event_url"])
    if "login" in driver.current_url:
        raise RuntimeError("Chrome profile is not logged in to GameChanger. "
                           "Open Chrome with that profile and sign in once.")
    try:
        WebDriverWait(driver, 20).until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, 'div[col-id="player"], div.ag-root')))
    except Exception:
        log(f"    box score did not load: {game['event_url']}")
        return None
    time.sleep(0.5)
    data = parse_box_score(driver.page_source)
    data["game_id"] = game["game_id"]
    data["game_date"] = game["game_date"]
    data["opponent"] = game.get("opponent", "")
    data["source_url"] = game["event_url"]
    cache_file.write_text(json.dumps(data))
    return data


# ----------------------------------------------------------------------------
# Stage 5: match GC stat lines to NCS players and build the report
# ----------------------------------------------------------------------------
LINEUP_RE = re.compile(r"^\s*(?P<player>.*?)\s*(?:\((?P<pos>[^)]*)\))?\s*$")
PITCH_DECOR_RE = re.compile(r"\((?:W|L|H|S|SV|BS)\)")
NUM_TAG_RE = re.compile(r"#\d+")

BAT_COLS = ["AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB", "AVG"]
PIT_COLS = ["IP", "H", "R", "ER", "BB", "SO", "HR"]


def clean_gc_player(raw: str) -> str:
    name = LINEUP_RE.match(raw or "").group("player") or ""
    name = PITCH_DECOR_RE.sub("", name)
    name = NUM_TAG_RE.sub("", name)
    return re.sub(r"\s+", " ", name).strip()


def match_key(name: str) -> str:
    """'Katie Brown' and GC's 'Katie B' both -> 'katie b'."""
    parts = re.findall(r"[A-Za-z']+", name.lower())
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}"


def pick_stats(rec: dict, cols: list[str]) -> dict:
    out = {}
    for c in cols:
        v = rec.get(c, "")
        if v not in ("", None):
            out[c] = v
    return out


def build_report(players: dict, team_map: dict, box_scores: dict, max_games: int) -> dict:
    """box_scores: gc_url -> {"ncs_teams":[...], "games":[parsed box score...]}"""
    # index NCS players by (team name, match key) and by match key alone
    report_players: dict[str, dict] = {}
    for pid, p in players.items():
        report_players[pid] = {
            "name": p["name"], "roster_team": p["roster_team"],
            "current_teams": p["current_teams"], "last_season_teams": p["last_season_teams"],
            "games": [],
        }

    by_key: dict[str, list[str]] = {}
    for pid, p in players.items():
        by_key.setdefault(match_key(p["name"]), []).append(pid)

    for gc_url, bundle in box_scores.items():
        # older reports stored a single "ncs_team"; new ones a list
        ncs_names = bundle.get("ncs_teams") or [bundle.get("ncs_team", "")]
        gc_name = bundle.get("gc_name", "")
        team_pids = {pid for pid, p in players.items()
                     if any(n in p["current_teams"] + p["last_season_teams"]
                            for n in ncs_names)}
        for game in bundle["games"]:
            if not game:
                continue
            sides = [("away", 0, game.get("away_team", "")), ("home", 1, game.get("home_team", ""))]
            # Which side is our team? Compare against the GC team name.
            our_side = max(sides, key=lambda s: name_similarity(gc_name or ncs_names[0], s[2] or ""))
            side_name, side_idx, _ = our_side
            opponent = sides[1 - side_idx][2] or game.get("opponent", "")

            def rows_for(section: str) -> list[dict]:
                return game.get(section, [[], []])[side_idx]

            for section, cols, name_col in (("batting", BAT_COLS, "LINEUP"),
                                            ("pitching", PIT_COLS, "PITCHING")):
                for rec in rows_for(section):
                    raw = rec.get(name_col) or rec.get("player") or ""
                    nm = clean_gc_player(raw)
                    if not nm or nm.upper() == "TEAM":
                        continue
                    key = match_key(nm)
                    pids = [pid for pid in by_key.get(key, []) if pid in team_pids]
                    if len(pids) != 1:
                        continue   # unknown or ambiguous -- skip rather than guess
                    pid = pids[0]
                    entry = next((g for g in report_players[pid]["games"]
                                  if g["game_id"] == game["game_id"]), None)
                    if entry is None:
                        # label the game with the NCS team this player belongs to
                        p_teams = players[pid]["current_teams"] + players[pid]["last_season_teams"]
                        team_label = next((n for n in ncs_names if n in p_teams), ncs_names[0])
                        entry = {"game_id": game["game_id"], "date": game["game_date"],
                                 "team": team_label, "gc_team": gc_name,
                                 "opponent": opponent, "home_away": side_name,
                                 "source_url": game.get("source_url", "")}
                        report_players[pid]["games"].append(entry)
                    entry[section] = pick_stats(rec, cols)
                    if section == "batting":
                        pos = LINEUP_RE.match(raw)
                        if pos and pos.group("pos"):
                            entry["positions"] = pos.group("pos")

    # keep the N most recent games per player + compute simple batting totals
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    for pid, rp in report_players.items():
        rp["games"].sort(key=lambda g: g["date"], reverse=True)
        rp["games"] = rp["games"][:max_games]
        bat = [g["batting"] for g in rp["games"] if g.get("batting")]
        totals = {c: sum(_int(b.get(c)) for b in bat) for c in ("AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO")}
        totals["GP"] = len(rp["games"])
        totals["AVG"] = round(totals["H"] / totals["AB"], 3) if totals["AB"] else 0.0
        rp["totals"] = totals

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_games": max_games,
        "teams_scraped": {u: {"ncs_teams": b.get("ncs_teams") or [b.get("ncs_team", "")],
                              "gc_name": b.get("gc_name", ""),
                              "games": len(b["games"])} for u, b in box_scores.items()},
        "players": report_players,
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Last-10-game GameChanger stats for tracked NCS players")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE),
                    help="Logged-in Chrome profile path (chrome://version -> Profile Path)")
    ap.add_argument("--headful", action="store_true", help="Show the browser")
    ap.add_argument("--teams", help="Only process NCS teams whose name contains this text")
    ap.add_argument("--current-only", action="store_true", help="Skip last-season teams")
    ap.add_argument("--map-only", action="store_true", help="Only build/refresh gc_team_map.json")
    ap.add_argument("--skip-follow", action="store_true", help="Don't click Follow on team pages")
    ap.add_argument("--max-games", type=int, default=10, help="Games per player (default 10)")
    ap.add_argument("--min-score", type=float, default=0.34,
                    help="Min composite match score (name similarity + up to 0.15 "
                         "Central-TX location bonus + 0.1 season-recency bonus) "
                         "to auto-accept a GC team match")
    args = ap.parse_args()

    players, team_names = load_players_and_teams(args.current_only)
    log(f"Players tracked: {len(players)}; teams to find on GC: {len(team_names)}")

    if args.teams:
        needle = args.teams.lower()
        team_names = {n: v for n, v in team_names.items() if needle in n.lower()}
        keep_pids = {pid for v in team_names.values() for pid in v["players"]}
        players = {pid: p for pid, p in players.items() if pid in keep_pids}
        log(f"Filtered by --teams '{args.teams}': {len(team_names)} team(s), {len(players)} player(s)")

    profile = clone_profile(Path(args.profile))
    driver = make_driver(profile, headless=not args.headful)
    try:
        team_map = build_team_map(driver, team_names, load_team_map(), args.min_score)
        if args.map_only:
            return 0

        box_scores: dict[str, dict] = {}
        mapped = [(n, t) for n, t in team_map["teams"].items() if t.get("gc_url") and n in team_names]
        log(f"Scraping {len(mapped)} mapped team(s)...")
        for i, (ncs_name, t) in enumerate(mapped, 1):
            # Two NCS names (e.g. season aliases) can map to the same GC team:
            # scrape once, credit every NCS name so no player loses their stats.
            if t["gc_url"] in box_scores:
                box_scores[t["gc_url"]]["ncs_teams"].append(ncs_name)
                log(f"[{i}/{len(mapped)}] {ncs_name} -> already scraped "
                    f"({t.get('gc_name') or t['gc_url']})")
                continue
            log(f"[{i}/{len(mapped)}] {ncs_name} -> {t.get('gc_name') or t['gc_url']}")
            if not args.skip_follow:
                if follow_team(driver, t["gc_url"]):
                    log("    followed team on GC")
            # GC teams are per-season: if the current season has fewer than
            # --max-games completed games, roll back into prior-season entries
            # of the same team until we have enough.
            games = last_completed_games(driver, t["gc_url"], args.max_games)
            log(f"    {len(games)} completed game(s) in {t.get('season') or 'current season'}")
            for prior in t.get("gc_prior_urls", []):
                if len(games) >= args.max_games:
                    break
                more = last_completed_games(driver, prior["gc_url"],
                                            args.max_games - len(games))
                log(f"    +{len(more)} from {prior.get('season') or 'prior season'}")
                games.extend(more)
            parsed = []
            for g in games:
                try:
                    parsed.append(scrape_box_score(driver, g))
                except RuntimeError as e:
                    sys.exit(str(e))
                except Exception as e:
                    log(f"    box score failed ({g['game_id'][:8]}): {e}")
                time.sleep(0.5)
            box_scores[t["gc_url"]] = {"ncs_teams": [ncs_name], "gc_name": t.get("gc_name", ""),
                                       "games": [p for p in parsed if p]}
    finally:
        driver.quit()

    report = build_report(players, team_map, box_scores, args.max_games)
    # Merge with the existing report so a filtered run (--teams X) refreshes
    # only those players instead of wiping everyone else's stats.
    if OUT_JSON.exists():
        try:
            old = json.loads(OUT_JSON.read_text())
            for pid, p in old.get("players", {}).items():
                if pid not in report["players"]:
                    report["players"][pid] = p
            for url, t in old.get("teams_scraped", {}).items():
                report["teams_scraped"].setdefault(url, t)
        except json.JSONDecodeError:
            pass
    OUT_JSON.parent.mkdir(exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2))
    with_stats = sum(1 for p in report["players"].values() if p["games"])
    log(f"Report saved: {with_stats}/{len(report['players'])} player(s) with stats -> {OUT_JSON}")
    log("View it: open gc-player-stats.html (serve the repo root, e.g. python3 -m http.server)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
