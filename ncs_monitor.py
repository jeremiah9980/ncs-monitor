#!/usr/bin/env python3
"""
NCS Roster Watch
================
Pulls NCS fastpitch team rosters (12U, Central Texas by default), diffs them
against the last saved snapshot, and reports any players added or — the part
you care about — removed from a roster.

Designed to run on a schedule via GitHub Actions:
  - state lives in snapshots/latest.json (committed back to the repo, so git
    history is itself a full audit trail of roster changes)
  - a human-readable report is written to reports/ on every change
  - notifications fire only when something actually changed (removed/added)

The NCS API field names are unknown up front, so normalization is fully
config-driven (see config.yaml). Leave a mapping blank to auto-detect it.

Usage:
  python ncs_monitor.py                      # normal run (uses config.yaml)
  python ncs_monitor.py --input samples/sample_response.json   # offline test
  python ncs_monitor.py --dry-run            # don't write snapshot or notify
  python ncs_monitor.py --config other.yaml
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import smtplib
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency 'pyyaml'. Run: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent

# ----------------------------------------------------------------------------
# Key-detection vocabularies (mirror the artifact's parser)
# ----------------------------------------------------------------------------
TEAM_KEYS   = ["teamname", "team_name", "name", "team", "title", "clubname", "club"]
CITY_KEYS   = ["city", "town", "locationcity", "homecity", "hometown"]
REGION_KEYS = ["state", "region", "province", "area", "locationstate", "st"]
ROSTER_KEYS = ["roster", "players", "athletes", "members", "playerlist", "lineup"]
PNAME_KEYS  = ["name", "playername", "fullname", "full_name", "displayname", "athletename"]
FIRST_KEYS  = ["firstname", "first_name", "first", "fname", "givenname"]
LAST_KEYS   = ["lastname", "last_name", "last", "lname", "surname", "familyname"]
NUM_KEYS    = ["number", "jersey", "jerseynumber", "jersey_number", "uniform", "uniformnumber", "no"]
PID_KEYS    = ["id", "playerid", "player_id", "athleteid", "athlete_id", "uuid", "guid", "_id"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def norm(v: Any) -> str:
    return ("" if v is None else str(v)).strip().lower()


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config not found: {path}")
    with path.open() as f:
        return yaml.safe_load(f) or {}


# ----------------------------------------------------------------------------
# Fetch
# ----------------------------------------------------------------------------
def fetch_from_api(api_cfg: dict) -> Any:
    """Config-driven GET against the NCS API. Token is read from an env var so
    it never lives in the repo."""
    base = api_cfg.get("base_url", "").rstrip("/")
    path = api_cfg.get("endpoint", "")
    if not base or not path:
        sys.exit("api.base_url and api.endpoint must be set in config.yaml "
                 "(or run with --input to test against a local JSON file).")

    params = api_cfg.get("query_params") or {}
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{base}/{path.lstrip('/')}" + (f"?{qs}" if qs else "")

    headers = dict(api_cfg.get("headers") or {})
    token_env = api_cfg.get("token_env")
    if token_env:
        token = os.environ.get(token_env, "")
        if not token:
            sys.exit(f"Env var {token_env} is empty — set it as a GitHub secret.")
        scheme = api_cfg.get("token_scheme", "Bearer")
        header_name = api_cfg.get("token_header", "Authorization")
        headers[header_name] = f"{scheme} {token}".strip()

    log(f"GET {url}")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"NCS API returned HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        sys.exit(f"Could not reach NCS API: {e.reason}")


def load_input(args, cfg: dict) -> Any:
    if args.input:
        log(f"Reading local file: {args.input}")
        return json.loads(Path(args.input).read_text())
    return fetch_from_api(cfg.get("api") or {})


# ----------------------------------------------------------------------------
# Normalize (tolerant, config-overridable)
# ----------------------------------------------------------------------------
def pick(keys: list[str], candidates: list[str]) -> str:
    low = [(k, norm(k)) for k in keys]
    for c in candidates:
        for raw, lo in low:
            if lo == c:
                return raw
    for c in candidates:
        for raw, lo in low:
            if c in lo:
                return raw
    return ""


def locate_team_array(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("teams", "data", "results", "items", "rows"):
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [x for x in v if isinstance(x, dict)]
    return []


def find_roster_key(team: dict) -> str:
    for k, v in team.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            if any(r in norm(k) for r in ROSTER_KEYS):
                return k
    for k, v in team.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return k
    return ""


def resolve_mapping(teams: list[dict], cfg_map: dict) -> dict:
    """Use explicit config mapping where provided; auto-detect the rest."""
    t0 = teams[0] if teams else {}
    tkeys = list(t0.keys())
    roster_key = cfg_map.get("roster") or find_roster_key(t0)
    p0 = (t0.get(roster_key) or [{}])[0] if roster_key else {}
    pkeys = list(p0.keys()) if isinstance(p0, dict) else []

    def m(name, keys, cands):
        return cfg_map.get(name) or pick(keys, cands)

    # Detect a *full-name* field, but never let firstName/lastName masquerade
    # as one (they contain the substring "name"). If the only match is a
    # first/last field, blank it so normalize() composes first + last instead.
    player_name = m("player_name", pkeys, PNAME_KEYS)
    if not cfg_map.get("player_name") and player_name:
        if any(x in norm(player_name) for x in ("first", "last", "fname", "lname")):
            player_name = ""

    return {
        "team":        m("team", tkeys, TEAM_KEYS),
        "city":        m("city", tkeys, CITY_KEYS),
        "region":      m("region", tkeys, REGION_KEYS),
        "roster":      roster_key,
        "player_name": player_name,
        "first_name":  m("first_name", pkeys, FIRST_KEYS),
        "last_name":   m("last_name", pkeys, LAST_KEYS),
        "jersey":      m("jersey", pkeys, NUM_KEYS),
        "player_id":   m("player_id", pkeys, PID_KEYS),
    }


def normalize(teams: list[dict], mp: dict) -> list[dict]:
    out = []
    for i, t in enumerate(teams):
        team_name = str(t.get(mp["team"], "")).strip() if mp["team"] else f"Team {i+1}"
        city = str(t.get(mp["city"], "")).strip() if mp["city"] else ""
        region = str(t.get(mp["region"], "")).strip() if mp["region"] else ""
        roster = t.get(mp["roster"], []) if mp["roster"] else []
        players = []
        for p in roster:
            if not isinstance(p, dict):
                continue
            if mp["player_name"] and p.get(mp["player_name"]):
                name = str(p[mp["player_name"]]).strip()
            else:
                f = p.get(mp["first_name"], "") if mp["first_name"] else ""
                l = p.get(mp["last_name"], "") if mp["last_name"] else ""
                name = " ".join(x for x in (str(f), str(l)) if x.strip()).strip()
            num = str(p.get(mp["jersey"], "")).strip() if mp["jersey"] else ""
            pid = str(p.get(mp["player_id"], "")).strip() if mp["player_id"] else ""
            key = f"id:{pid}" if pid else f"nm:{norm(name)}|{norm(num)}"
            players.append({"name": name or "(unnamed)", "num": num, "key": key})
        tkey = f"tn:{norm(team_name)}" if team_name else f"ti:{i}"
        out.append({"team_name": team_name or f"Team {i+1}",
                    "city": city, "region": region,
                    "players": players, "tkey": tkey})
    return out


def filter_central_tx(teams: list[dict], cities: list[str]) -> list[dict]:
    if not cities:
        return teams
    wanted = [norm(c) for c in cities]
    keep = []
    for t in teams:
        loc = norm(f"{t['city']} {t['region']}")
        if any(c in loc or norm(t["city"]) == c for c in wanted):
            keep.append(t)
    return keep


# ----------------------------------------------------------------------------
# Snapshot + diff
# ----------------------------------------------------------------------------
def load_snapshot(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log("Existing snapshot was unreadable; treating as no baseline.")
    return None


def snapshot_from(teams: list[dict]) -> dict:
    return {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "teams": {
            t["tkey"]: {
                "team_name": t["team_name"], "city": t["city"], "region": t["region"],
                "players": [{"name": p["name"], "num": p["num"], "key": p["key"]}
                            for p in t["players"]],
            } for t in teams
        },
    }


def diff(current: list[dict], baseline: dict | None) -> list[dict]:
    """Return per-team change records. Empty list == no changes."""
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
def render_markdown(changes: list[dict], scope: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_rem = sum(len(c["removed"]) for c in changes)
    n_add = sum(len(c["added"]) for c in changes)
    lines = [f"# NCS Roster Changes — {ts}",
             f"_Scope: {scope}_", "",
             f"**{n_rem} removed · {n_add} added** across {len(changes)} team(s).", ""]
    for c in changes:
        loc = ", ".join(x for x in (c["city"], c["region"]) if x)
        lines.append(f"## {c['team']}" + (f" — {loc}" if loc else ""))
        if c.get("new_team"):
            lines.append("- 🆕 New team appeared in this scope (no prior baseline).")
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
            w.writerow(["timestamp", "type", "team", "city", "region", "player", "number"])
        for c in changes:
            for p in c["removed"]:
                w.writerow([ts, "removed", c["team"], c["city"], c["region"], p["name"], p["num"]])
            for p in c["added"]:
                w.writerow([ts, "added", c["team"], c["city"], c["region"], p["name"], p["num"]])


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
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
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
    """Surface the report in the Actions run summary."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(markdown)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="NCS roster change monitor")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--input", help="Read a local JSON file instead of the API")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report changes but do not save snapshot or notify")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    cities = cfg.get("central_tx_cities") or []
    snap_path = ROOT / cfg.get("snapshot_file", "snapshots/latest.json")
    snap_path.parent.mkdir(parents=True, exist_ok=True)

    raw = load_input(args, cfg)
    team_objs = locate_team_array(raw)
    if not team_objs:
        log("No team array found in response — check the API shape / mapping.")
        return 2

    mp = resolve_mapping(team_objs, cfg.get("mapping") or {})
    log(f"Field mapping: {mp}")
    teams = normalize(team_objs, mp)
    teams = filter_central_tx(teams, cities)
    total_players = sum(len(t["players"]) for t in teams)
    scope = f"12U · {', '.join(cities) if cities else 'all cities'}"
    log(f"{len(teams)} teams / {total_players} players in scope")

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
    md = render_markdown(changes, scope)
    log(f"CHANGES: {n_rem} removed, {n_add} added")
    print("\n" + md)

    if args.dry_run:
        log("Dry run — not saving snapshot or notifying.")
        return 1

    # write report + changelog
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (reports / f"changes-{stamp}.md").write_text(md)
    append_changelog(reports / "changelog.csv", changes)
    write_job_summary(md)

    # notify
    nc = cfg.get("notify") or {}
    subject = f"NCS roster: {n_rem} removed, {n_add} added"
    if nc.get("email"):
        notify_email(nc["email"], subject, md)
    if nc.get("slack"):
        notify_slack(nc["slack"], f"*{subject}*\n```{md[:2500]}```")
    if nc.get("github_issue"):
        notify_github_issue(nc["github_issue"], subject, md)

    # advance baseline
    snap_path.write_text(json.dumps(snapshot_from(teams), indent=2))
    log("Snapshot updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
