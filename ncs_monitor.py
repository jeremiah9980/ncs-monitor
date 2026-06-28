#!/usr/bin/env python3
"""
NCS Roster Watch (auto-discovery edition)
=========================================
Finds NCS fastpitch teams in your area by reading the public "Who's Coming"
pages of the events you seed, filters them to your age group + central-TX
cities, then watches each team's roster page and reports when a player is
removed (or added).

Everything is public server-rendered HTML on playncs.com -- no API, token, or
login. Two stages:

  DISCOVERY  -- read each seed event's Who's Coming table, keep teams whose
                division matches (e.g. "12U") and whose city is in your list.
                Cached to discovered_teams.json and refreshed every
                discovery.refresh_hours so we don't re-crawl every run.
  MONITOR    -- fetch each team's roster, diff vs snapshots/latest.json,
                report + notify on changes, commit the new snapshot.

Run on a schedule via GitHub Actions. git history of snapshots/latest.json is a
full audit trail of who joined/left and when.

Usage:
  python ncs_monitor.py                 # discover (if stale) + monitor
  python ncs_monitor.py --discover-only # refresh the team list, don't monitor
  python ncs_monitor.py --force-discover
  python ncs_monitor.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency 'pyyaml'. Run: pip install -r requirements.txt")
try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency 'beautifulsoup4'. Run: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
PLAYER_LINK_RE = re.compile(r"/Players/Details/(\d+)/", re.I)
TEAM_LINK_RE = re.compile(r"/Teams/Details/(\d+)/([A-Za-z0-9\-]*)", re.I)
AGE_RE = re.compile(r"(\d+)y\s*(\d+)m", re.I)
DEFAULT_UA = "ncs-roster-watch/3.0 (personal roster monitor)"
BASE = "https://www.playncs.com"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config not found: {path}")
    with path.open() as f:
        return yaml.safe_load(f) or {}


def fetch_html(url: str, ua: str, retries: int = 3) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries - 1:
                wait = 2 ** attempt
                log(f"Fetch failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
    raise last_err


def team_id_from_href(href: str) -> tuple[str, str]:
    m = TEAM_LINK_RE.search(href)
    return (m.group(1), m.group(2)) if m else ("", "")


def canonical_team_url(team_id: str, slug: str = "team") -> str:
    return f"{BASE}/Fastpitch/Teams/Details/{team_id}/{slug or 'team'}"


# ----------------------------------------------------------------------------
# DISCOVERY -- parse an event "Who's Coming" page
# ----------------------------------------------------------------------------
def whos_coming_url(event: str | int) -> str:
    s = str(event)
    if s.startswith("http"):
        return s
    return f"{BASE}/fastpitch!/Events/WhosComing/{s}/event"


def parse_whos_coming(html: str) -> list[dict]:
    """Each registered team is a row: [#, TeamName(link), Division, City/State, W-L-T]."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=TEAM_LINK_RE):
        tid, slug = team_id_from_href(a["href"])
        if not tid or tid in seen:
            continue
        row = a.find_parent("tr")
        if not row:
            continue
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        division = next((c for c in cells if re.match(r"^\s*\d+U", c)), "")
        loc = next((c for c in cells if re.search(r",\s*[A-Za-z]{2}\.?$", c)), "")
        city, region = "", ""
        if "," in loc:
            city, region = (x.strip() for x in loc.rsplit(",", 1))
        seen.add(tid)
        out.append({"team_id": tid, "name": a.get_text(strip=True),
                    "division": division, "city": city, "region": region,
                    "url": canonical_team_url(tid, slug)})
    return out


def keep_team(t: dict, age_prefixes: list[str], cities: list[str]) -> bool:
    div = t["division"].upper().replace(" ", "")
    if age_prefixes and not any(div.startswith(p.upper().replace(" ", "")) for p in age_prefixes):
        return False
    if cities:
        city = t["city"].lower()
        if not any(c.lower() == city or c.lower() in city for c in cities):
            return False
    return True


def discover(cfg: dict, ua: str, delay: float) -> list[dict]:
    disc = cfg.get("discovery") or {}
    events = disc.get("events") or []
    if not events:
        return []
    ages = disc.get("age_prefixes") or ["12U"]
    cities = disc.get("central_tx_cities") or []
    found: dict[str, dict] = {}
    for i, ev in enumerate(events):
        url = whos_coming_url(ev)
        try:
            log(f"Discovery: reading {url}")
            teams = parse_whos_coming(fetch_html(url, ua))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            log(f"  could not read event {ev}: {e} -- skipping")
            teams = []
        kept = [t for t in teams if keep_team(t, ages, cities)]
        log(f"  {len(teams)} registered, {len(kept)} match {ages} + central-TX cities")
        for t in kept:
            found[t["team_id"]] = t  # dedupe across events
        if i < len(events) - 1:
            time.sleep(delay)
    return list(found.values())


def load_discovery_cache(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
    return None


def get_watchlist(cfg: dict, ua: str, delay: float, force: bool) -> list[dict]:
    """Discovered teams (cached/refreshed) unioned with any manual teams."""
    disc = cfg.get("discovery") or {}
    cache_path = ROOT / disc.get("cache_file", "discovered_teams.json")
    refresh_hours = float(disc.get("refresh_hours", 24))
    cache = load_discovery_cache(cache_path)

    fresh = False
    if cache and not force:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cache["discovered_at"])
        fresh = age < timedelta(hours=refresh_hours)

    if disc.get("enabled") and not fresh:
        teams = discover(cfg, ua, delay)
        if teams:
            cache_path.write_text(json.dumps(
                {"discovered_at": datetime.now(timezone.utc).isoformat(), "teams": teams},
                indent=2))
            log(f"Discovery cache updated: {len(teams)} teams -> {cache_path.name}")
        elif cache:
            log("Discovery found nothing; keeping previous cache.")
            teams = cache["teams"]
    elif cache:
        teams = cache["teams"]
        log(f"Using cached team list ({len(teams)} teams, still fresh).")
    else:
        teams = []

    # union manual teams
    by_id = {t["team_id"]: t for t in teams}
    for mt in (cfg.get("teams") or []):
        url = mt.get("url") or canonical_team_url(str(mt.get("id", "")))
        tid, slug = team_id_from_href(url)
        if tid:
            by_id.setdefault(tid, {"team_id": tid, "name": mt.get("name", ""),
                                   "division": "", "city": "", "region": "",
                                   "url": canonical_team_url(tid, slug)})
    return list(by_id.values())


# ----------------------------------------------------------------------------
# MONITOR -- parse a team roster page
# ----------------------------------------------------------------------------
def parse_team_meta(soup: BeautifulSoup) -> dict:
    name = city = region = division = ""
    tag = soup.find("meta", attrs={"name": "description"}) \
        or soup.find("meta", attrs={"property": "og:description"})
    if tag and tag.get("content"):
        parts = [p.strip() for p in tag["content"].split("|")]
        if parts:
            name = parts[0]
        if len(parts) >= 2:
            division = parts[1]
        if len(parts) >= 3 and "," in parts[2]:
            city, region = (x.strip() for x in parts[2].rsplit(",", 1))
    if not name:
        h1 = soup.find("h1")
        if h1:
            name = h1.get_text(strip=True)
    return {"team_name": name, "city": city, "region": region, "division": division}


def parse_roster(soup: BeautifulSoup) -> list[dict]:
    players, seen = [], set()
    for a in soup.find_all("a", href=PLAYER_LINK_RE):
        m = PLAYER_LINK_RE.search(a["href"])
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        name = a.get_text(strip=True)
        num = ""
        row = a.find_parent("tr")
        if row:
            cells = row.find_all(["td", "th"])
            if cells:
                first = cells[0].get_text(strip=True)
                if first and first.lower() != name.lower():
                    num = first
        players.append({"name": name or "(unnamed)", "num": num,
                        "player_id": pid, "key": f"id:{pid}",
                        "url": f"{BASE}/fastpitch/Players/Details/{pid}/"})
    return players


def parse_player_details(html: str) -> dict:
    """Parse a player detail page to extract age, location, and team history."""
    soup = BeautifulSoup(html, "html.parser")
    details = {"age": "", "location": "", "team_history": []}

    text = soup.get_text(" ", strip=True)
    age_match = AGE_RE.search(text)
    if age_match:
        details["age"] = f"{age_match.group(1)}y {age_match.group(2)}m"

    loc_match = re.search(r"\b([A-Z]{2})\s*$", text[:500])
    if loc_match:
        details["location"] = loc_match.group(1)

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            team_link = row.find("a", href=TEAM_LINK_RE)
            if not team_link:
                continue
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            team_name = team_link.get_text(strip=True)
            division = next((c for c in cells if re.match(r"^\d+U", c)), "")
            season = next((c for c in cells if re.match(r"20\d{2}\s+Fastpitch", c)), "")
            status = ""
            for cell in cells:
                cl = cell.lower()
                if cl in ("active", "guest", "past", "removed"):
                    status = cell
                    break
            details["team_history"].append({
                "team": team_name,
                "division": division,
                "season": season,
                "status": status
            })

    return details


def fetch_player_details(player_ids: list[str], ua: str, delay: float,
                         existing: dict | None = None) -> dict[str, dict]:
    """Fetch details for a list of player IDs, using cache when available."""
    results = {}
    existing = existing or {}

    for i, pid in enumerate(player_ids):
        if pid in existing and existing[pid].get("team_history"):
            results[pid] = existing[pid]
            continue

        url = f"{BASE}/fastpitch/Players/Details/{pid}/"
        try:
            html = fetch_html(url, ua)
            details = parse_player_details(html)
            results[pid] = details
            log(f"  Fetched player {pid}: age={details.get('age', '?')}, history={len(details.get('team_history', []))} teams")
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            log(f"  Failed to fetch player {pid}: {e}")
            results[pid] = {"age": "", "location": "", "team_history": []}

        if i < len(player_ids) - 1:
            time.sleep(delay)

    return results


def parse_team(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    return {**parse_team_meta(soup), "url": url,
            "players": parse_roster(soup), "tkey": f"id:{url}"}


# ----------------------------------------------------------------------------
# Snapshot + diff
# ----------------------------------------------------------------------------
def load_snapshot(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log("Existing snapshot unreadable; treating as no baseline.")
    return None


def snapshot_from(teams: list[dict], player_details: dict | None = None) -> dict:
    player_details = player_details or {}
    return {"saved_at": datetime.now(timezone.utc).isoformat(),
            "player_details": player_details,
            "teams": {t["tkey"]: {
                "team_name": t["team_name"], "city": t["city"], "region": t["region"],
                "division": t.get("division", ""), "url": t["url"],
                "players": [{"name": p["name"], "num": p["num"],
                             "player_id": p["player_id"], "key": p["key"],
                             "url": p.get("url", "")}
                            for p in t["players"]]} for t in teams}}


def diff(current: list[dict], baseline: dict | None) -> list[dict]:
    changes = []
    base_teams = (baseline or {}).get("teams", {})
    for t in current:
        base = base_teams.get(t["tkey"])
        if base is None:
            if baseline is not None:
                changes.append({"team": t["team_name"], "city": t["city"],
                                "region": t["region"], "new_team": True,
                                "removed": [], "added": []})
            continue
        cur = {p["key"] for p in t["players"]}
        old = {p["key"] for p in base["players"]}
        removed = [p for p in base["players"] if p["key"] not in cur]
        added = [p for p in t["players"] if p["key"] not in old]
        if removed or added:
            changes.append({"team": t["team_name"], "city": t["city"],
                            "region": t["region"], "new_team": False,
                            "removed": removed, "added": added})
    return changes


# ----------------------------------------------------------------------------
# Reporting + notifications
# ----------------------------------------------------------------------------
def render_markdown(changes: list[dict]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_rem = sum(len(c["removed"]) for c in changes)
    n_add = sum(len(c["added"]) for c in changes)
    lines = [f"# NCS Roster Changes - {ts}", "",
             f"**{n_rem} removed - {n_add} added** across {len(changes)} team(s).", ""]
    for c in changes:
        loc = ", ".join(x for x in (c["city"], c["region"]) if x)
        lines.append(f"## {c['team']}" + (f" - {loc}" if loc else ""))
        if c.get("new_team"):
            lines.append("- New team now tracked (no prior baseline).")
        for p in c["removed"]:
            lines.append(f"- REMOVED: {p['name']}" + (f" #{p['num']}" if p['num'] else ""))
        for p in c["added"]:
            lines.append(f"- added: {p['name']}" + (f" #{p['num']}" if p['num'] else ""))
        lines.append("")
    return "\n".join(lines)


def append_changelog(path: Path, changes: list[dict]) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "type", "team", "city", "region", "player", "number", "player_id"])
        for c in changes:
            for p in c["removed"]:
                w.writerow([ts, "removed", c["team"], c["city"], c["region"], p["name"], p["num"], p.get("player_id", "")])
            for p in c["added"]:
                w.writerow([ts, "added", c["team"], c["city"], c["region"], p["name"], p["num"], p.get("player_id", "")])


def notify_email(cfg, subject, body):
    host = os.environ.get(cfg.get("host_env", "SMTP_HOST"))
    if not host:
        return
    port = int(os.environ.get(cfg.get("port_env", "SMTP_PORT"), "587"))
    user = os.environ.get(cfg.get("user_env", "SMTP_USER"), "")
    pw = os.environ.get(cfg.get("pass_env", "SMTP_PASS"), "")
    to_addr = cfg.get("to", user)
    msg = MIMEText(body)
    msg["Subject"], msg["From"], msg["To"] = subject, user, to_addr
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            if user:
                s.login(user, pw)
            s.send_message(msg)
        log(f"Email sent to {to_addr}")
    except Exception as e:  # noqa: BLE001
        log(f"Email failed: {e}")


def notify_slack(cfg, text):
    url = os.environ.get(cfg.get("webhook_env", "SLACK_WEBHOOK_URL"))
    if not url:
        return
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        log("Slack notification sent")
    except Exception as e:  # noqa: BLE001
        log(f"Slack failed: {e}")


def notify_github_issue(cfg, title, body):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    data = json.dumps({"title": title, "body": body,
                       "labels": cfg.get("labels", ["roster-change"])}).encode()
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues", data=data,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        log("GitHub issue opened")
    except Exception as e:  # noqa: BLE001
        log(f"GitHub issue failed: {e}")


def write_job_summary(md):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(md)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="NCS roster change monitor (auto-discovery)")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--discover-only", action="store_true")
    ap.add_argument("--force-discover", action="store_true")
    ap.add_argument("--input", help="Parse a saved roster HTML file (offline test)")
    ap.add_argument("--team-id", help="URL/id to attach to --input")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    ua = cfg.get("user_agent", DEFAULT_UA)
    delay = float(cfg.get("request_delay_seconds", 2))
    snap_path = ROOT / cfg.get("snapshot_file", "snapshots/latest.json")
    snap_path.parent.mkdir(parents=True, exist_ok=True)

    # Offline single-page test path
    if args.input:
        teams = [parse_team(Path(args.input).read_text(),
                            args.team_id or "file://" + args.input)]
    else:
        watch = get_watchlist(cfg, ua, delay, force=args.force_discover or args.discover_only)
        log(f"Watchlist: {len(watch)} team(s)")
        if args.discover_only:
            for t in watch:
                print(f"  {t['team_id']:>7}  {t.get('division',''):8} {t.get('city',''):16} {t['name']}")
            return 0
        teams = []
        for i, t in enumerate(watch):
            try:
                teams.append(parse_team(fetch_html(t["url"], ua), t["url"]))
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                log(f"  fetch failed for {t['name'] or t['url']}: {e}")
            if i < len(watch) - 1:
                time.sleep(delay)

    for t in teams:
        log(f"  {t['team_name'] or t['url']}: {len(t['players'])} players")

    # Collect all unique player IDs
    all_player_ids = set()
    for t in teams:
        for p in t["players"]:
            all_player_ids.add(p["player_id"])
    log(f"Found {len(all_player_ids)} unique players across all teams")

    baseline = load_snapshot(snap_path)
    existing_player_details = (baseline or {}).get("player_details", {})

    # Fetch player details (with caching from previous run)
    if all_player_ids:
        log(f"Fetching player details...")
        player_details = fetch_player_details(
            list(all_player_ids), ua, delay, existing_player_details
        )
    else:
        player_details = {}

    # Treat an empty teams dict as "no baseline" to avoid spurious notifications
    # when the snapshot file exists but was initialized empty
    if baseline is not None and not baseline.get("teams"):
        log("Baseline exists but has no teams -- treating as first run.")
        baseline = None

    changes = diff(teams, baseline)

    if baseline is None:
        log("No baseline yet -- saving the first snapshot.")
        if not args.dry_run:
            snap_path.write_text(json.dumps(snapshot_from(teams, player_details), indent=2))
        return 0

    if not changes:
        log("No roster changes since last run.")
        # Still save the snapshot if we have new player details
        if player_details and not args.dry_run:
            existing_details = (baseline or {}).get("player_details", {})
            new_details = {k: v for k, v in player_details.items() if k not in existing_details}
            if new_details:
                log(f"Saving {len(new_details)} new player details to snapshot.")
                snap_path.write_text(json.dumps(snapshot_from(teams, player_details), indent=2))
        return 0

    n_rem = sum(len(c["removed"]) for c in changes)
    n_add = sum(len(c["added"]) for c in changes)
    md = render_markdown(changes)
    log(f"CHANGES: {n_rem} removed, {n_add} added")
    print("\n" + md)

    if args.dry_run:
        log("Dry run -- not saving or notifying.")
        return 1

    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (reports / f"changes-{stamp}.md").write_text(md)
    append_changelog(reports / "changelog.csv", changes)
    write_job_summary(md)

    nc = cfg.get("notify") or {}
    subject = f"NCS roster: {n_rem} removed, {n_add} added"
    if nc.get("email"):
        notify_email(nc["email"], subject, md)
    if nc.get("slack"):
        notify_slack(nc["slack"], f"*{subject}*\n```{md[:2500]}```")
    if nc.get("github_issue"):
        notify_github_issue(nc["github_issue"], subject, md)

    snap_path.write_text(json.dumps(snapshot_from(teams, player_details), indent=2))
    log("Snapshot updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
