#!/usr/bin/env python3
"""
NCS Roster Watch
================
Watches NCS fastpitch team roster pages on playNCS.com and reports when a
player is removed (or added) from any team you track.

playNCS team pages are plain server-rendered HTML — no API, token, or login.
Each team you watch is just a Team Details URL, e.g.
  https://www.playncs.com/Fastpitch/Teams/Details/39016/bananas-2k15
The roster is a "Number | Player" table where each player links to a stable
player id (/Fastpitch/Players/Details/<id>/...). We key the diff on that id,
so jersey or name-spelling changes never read as false add/removes.

Designed to run on a schedule via GitHub Actions:
  - state lives in snapshots/latest.json (committed back, so git history is a
    full audit trail of roster changes)
  - a Markdown report is written to reports/ on every change
  - notifications fire only when a roster actually changed

Usage:
  python ncs_monitor.py                 # fetch all teams in config.yaml
  python ncs_monitor.py --input page.html --team-id 39016   # parse a saved page (offline test)
  python ncs_monitor.py --dry-run       # report but don't save snapshot or notify
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
from datetime import datetime, timezone
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
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "ncs-roster-watch/2.0 (+personal roster monitor)")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config not found: {path}")
    with path.open() as f:
        return yaml.safe_load(f) or {}


def team_url(team: dict) -> str:
    """Accept either a full url or a bare id (slug is cosmetic, any value works)."""
    if team.get("url"):
        return team["url"]
    tid = team.get("id")
    if not tid:
        sys.exit(f"Each team needs a 'url' or 'id'. Got: {team}")
    return f"https://www.playncs.com/Fastpitch/Teams/Details/{tid}/team"


# ----------------------------------------------------------------------------
# Fetch
# ----------------------------------------------------------------------------
def fetch_html(url: str, ua: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------
# Parse
# ----------------------------------------------------------------------------
def parse_team_meta(soup: BeautifulSoup) -> dict:
    """Team name / class / location come cleanly from the description meta:
       'Bananas 2k15 | 8U C | Georgetown, TX'"""
    name = city = region = division = ""
    tag = soup.find("meta", attrs={"name": "description"}) \
        or soup.find("meta", attrs={"property": "og:description"})
    if tag and tag.get("content"):
        parts = [p.strip() for p in tag["content"].split("|")]
        if parts:
            name = parts[0]
        if len(parts) >= 2:
            division = parts[1]
        if len(parts) >= 3:
            loc = parts[2]
            if "," in loc:
                city, region = (x.strip() for x in loc.rsplit(",", 1))
            else:
                city = loc
    if not name:  # fallback to the H1
        h1 = soup.find("h1")
        if h1:
            name = h1.get_text(strip=True)
    return {"team_name": name, "city": city, "region": region, "division": division}


def parse_roster(soup: BeautifulSoup) -> list[dict]:
    """Find the roster table by the player-detail links it contains, and pull
    (jersey number, player id, name) from each row. Coaches have no player
    links, so this cleanly isolates the roster."""
    players = []
    seen = set()
    for a in soup.find_all("a", href=PLAYER_LINK_RE):
        m = PLAYER_LINK_RE.search(a["href"])
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        name = a.get_text(strip=True)
        # jersey number = text of the first cell in this player's row
        num = ""
        row = a.find_parent("tr")
        if row:
            cells = row.find_all(["td", "th"])
            if cells:
                first = cells[0].get_text(strip=True)
                if first and first.lower() != name.lower():
                    num = first
        players.append({"name": name or "(unnamed)", "num": num,
                        "player_id": pid, "key": f"id:{pid}"})
    return players


def parse_team(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    meta = parse_team_meta(soup)
    players = parse_roster(soup)
    tkey = f"id:{url}"  # team keyed by its url (stable per team)
    return {**meta, "url": url, "players": players, "tkey": tkey}


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


def snapshot_from(teams: list[dict]) -> dict:
    return {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "teams": {
            t["tkey"]: {
                "team_name": t["team_name"], "city": t["city"],
                "region": t["region"], "division": t.get("division", ""),
                "url": t["url"],
                "players": [{"name": p["name"], "num": p["num"],
                             "player_id": p["player_id"], "key": p["key"]}
                            for p in t["players"]],
            } for t in teams
        },
    }


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
        cur_keys = {p["key"] for p in t["players"]}
        base_keys = {p["key"] for p in base["players"]}
        removed = [p for p in base["players"] if p["key"] not in cur_keys]
        added = [p for p in t["players"] if p["key"] not in base_keys]
        if removed or added:
            changes.append({"team": t["team_name"], "city": t["city"],
                            "region": t["region"], "new_team": False,
                            "removed": removed, "added": added})
    return changes


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------
def render_markdown(changes: list[dict]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_rem = sum(len(c["removed"]) for c in changes)
    n_add = sum(len(c["added"]) for c in changes)
    lines = [f"# NCS Roster Changes — {ts}", "",
             f"**{n_rem} removed · {n_add} added** across {len(changes)} team(s).", ""]
    for c in changes:
        loc = ", ".join(x for x in (c["city"], c["region"]) if x)
        lines.append(f"## {c['team']}" + (f" — {loc}" if loc else ""))
        if c.get("new_team"):
            lines.append("- 🆕 New team now being tracked (no prior baseline).")
        for p in c["removed"]:
            tag = f" #{p['num']}" if p["num"] else ""
            lines.append(f"- ❌ **Removed:** {p['name']}{tag}")
        for p in c["added"]:
            tag = f" #{p['num']}" if p["num"] else ""
            lines.append(f"- ➕ Added: {p['name']}{tag}")
        lines.append("")
    return "\n".join(lines)


def append_changelog(path: Path, changes: list[dict]) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "type", "team", "city", "region",
                        "player", "number", "player_id"])
        for c in changes:
            for p in c["removed"]:
                w.writerow([ts, "removed", c["team"], c["city"], c["region"],
                            p["name"], p["num"], p.get("player_id", "")])
            for p in c["added"]:
                w.writerow([ts, "added", c["team"], c["city"], c["region"],
                            p["name"], p["num"], p.get("player_id", "")])


# ----------------------------------------------------------------------------
# Notifications (only fire when there are changes)
# ----------------------------------------------------------------------------
def notify_email(cfg: dict, subject: str, body: str) -> None:
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


def notify_slack(cfg: dict, text: str) -> None:
    url = os.environ.get(cfg.get("webhook_env", "SLACK_WEBHOOK_URL"))
    if not url:
        return
    data = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        log("Slack notification sent")
    except Exception as e:  # noqa: BLE001
        log(f"Slack failed: {e}")


def notify_github_issue(cfg: dict, title: str, body: str) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    data = json.dumps({"title": title, "body": body,
                       "labels": cfg.get("labels", ["roster-change"])}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues", data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        log("GitHub issue opened")
    except Exception as e:  # noqa: BLE001
        log(f"GitHub issue failed: {e}")


def write_job_summary(markdown: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(markdown)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="NCS roster change monitor")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--input", help="Parse a saved HTML file instead of fetching")
    ap.add_argument("--team-id", help="Team id/url to attach to --input page")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    snap_path = ROOT / cfg.get("snapshot_file", "snapshots/latest.json")
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    ua = cfg.get("user_agent", DEFAULT_UA)
    delay = float(cfg.get("request_delay_seconds", 2))

    teams = []
    if args.input:
        html = Path(args.input).read_text()
        url = args.team_id or "file://" + args.input
        teams.append(parse_team(html, url))
    else:
        team_cfgs = cfg.get("teams") or []
        if not team_cfgs:
            sys.exit("No teams configured. Add them under 'teams:' in config.yaml.")
        for i, tc in enumerate(team_cfgs):
            url = team_url(tc)
            try:
                log(f"Fetching {url}")
                html = fetch_html(url, ua)
                teams.append(parse_team(html, url))
            except urllib.error.HTTPError as e:
                log(f"  HTTP {e.code} for {url} — skipping")
            except urllib.error.URLError as e:
                log(f"  Could not reach {url}: {e.reason} — skipping")
            if i < len(team_cfgs) - 1:
                time.sleep(delay)  # be polite

    for t in teams:
        log(f"  {t['team_name'] or t['url']}: {len(t['players'])} players")

    baseline = load_snapshot(snap_path)
    changes = diff(teams, baseline)

    if baseline is None:
        log("No baseline yet — saving the first snapshot.")
        if not args.dry_run:
            snap_path.write_text(json.dumps(snapshot_from(teams), indent=2))
        return 0

    if not changes:
        log("No roster changes since last run. ✓")
        return 0

    n_rem = sum(len(c["removed"]) for c in changes)
    n_add = sum(len(c["added"]) for c in changes)
    md = render_markdown(changes)
    log(f"CHANGES: {n_rem} removed, {n_add} added")
    print("\n" + md)

    if args.dry_run:
        log("Dry run — not saving snapshot or notifying.")
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

    snap_path.write_text(json.dumps(snapshot_from(teams), indent=2))
    log("Snapshot updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
