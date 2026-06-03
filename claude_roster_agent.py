#!/usr/bin/env python3
"""
Claude Roster Agent
===================

Creates a team-level AI intelligence report for the NCS roster portal.

Security model:
- Runs server-side in GitHub Actions, never in the browser.
- Reads ANTHROPIC_API_KEY from GitHub Secrets.
- Writes static report files the portal can safely fetch.
- Keeps analysis team-level and avoids ranking/evaluating youth athletes.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency 'pyyaml'. Run: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports"
REPORT_MD = REPORT_DIR / "claude-agent.md"
REPORT_JSON = REPORT_DIR / "claude-agent.json"
DEFAULT_MODEL = "claude-sonnet-4-20250514"
ANTHROPIC_VERSION = "2023-06-01"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def read_changelog(path: Path, limit: int = 80) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return rows[:limit]


def snapshot_path_from_config(cfg: dict[str, Any]) -> Path:
    return ROOT / cfg.get("snapshot_file", "snapshots/latest.json")


def build_context(cfg: dict[str, Any], snapshot: dict[str, Any], changes: list[dict[str, str]]) -> dict[str, Any]:
    raw_teams = snapshot.get("teams") or {}
    teams = []
    for key, team in raw_teams.items():
        players = team.get("players") or []
        teams.append(
            {
                "key": key,
                "team_name": team.get("team_name", ""),
                "city": team.get("city", ""),
                "region": team.get("region", ""),
                "division": team.get("division", ""),
                "player_count": len(players),
                "url": team.get("url", ""),
            }
        )
    teams.sort(key=lambda t: (t["division"], t["city"], t["team_name"]))

    adds = sum(1 for row in changes if row.get("type") == "added")
    removals = sum(1 for row in changes if row.get("type") == "removed")

    discovery = cfg.get("discovery") or {}
    return {
        "generated_at": utc_now(),
        "snapshot_saved_at": snapshot.get("saved_at"),
        "watch_filters": {
            "age_prefixes": discovery.get("age_prefixes", []),
            "central_tx_cities": discovery.get("central_tx_cities", []),
            "events": discovery.get("events", []),
            "refresh_hours": discovery.get("refresh_hours", 24),
        },
        "team_count": len(teams),
        "teams": teams,
        "recent_changes": changes,
        "recent_change_summary": {
            "added": adds,
            "removed": removals,
            "total": len(changes),
        },
    }


def deterministic_report(context: dict[str, Any], reason: str | None = None) -> str:
    teams = context["teams"]
    changes = context["recent_changes"]
    summary = context["recent_change_summary"]
    filters = context["watch_filters"]

    lines = [
        "# Claude Roster Agent Report",
        "",
        f"Generated: **{context['generated_at']}**",
        f"Snapshot saved: **{context.get('snapshot_saved_at') or 'not available yet'}**",
        "",
    ]
    if reason:
        lines.extend([
            "> Claude API was not used for this run, so this is a deterministic fallback report.",
            f"> Reason: {reason}",
            "",
        ])

    lines.extend([
        "## Team-level summary",
        "",
        f"- Teams currently tracked: **{context['team_count']}**",
        f"- Recent changes in changelog sample: **{summary['total']}** total — **{summary['removed']}** removed, **{summary['added']}** added",
        f"- Active age filters: **{', '.join(filters.get('age_prefixes') or []) or 'not set'}**",
        f"- City coverage count: **{len(filters.get('central_tx_cities') or [])}**",
        "",
        "## Watchlist health",
        "",
    ])

    if not teams:
        lines.append("No teams are in the current snapshot yet. Run the NCS Roster Watch workflow first.")
    else:
        for team in teams[:30]:
            loc = ", ".join(x for x in (team.get("city"), team.get("region")) if x)
            lines.append(
                f"- **{team.get('team_name') or 'Unnamed team'}**"
                f" — {team.get('division') or 'division unknown'}"
                f" — {loc or 'location unknown'}"
                f" — {team.get('player_count', 0)} rostered players"
            )
        if len(teams) > 30:
            lines.append(f"- ...and {len(teams) - 30} more tracked teams.")

    lines.extend([
        "",
        "## Next best checks",
        "",
        "1. Confirm the seed event list still covers the tournaments you care about.",
        "2. Check any team with repeated roster movement before bracket play starts.",
        "3. Keep this report team-level; do not use it to profile or rank youth athletes.",
    ])
    return "\n".join(lines) + "\n"


def build_prompt(context: dict[str, Any]) -> str:
    safe_context = json.dumps(context, indent=2, ensure_ascii=False)
    return f"""
You are Claude Roster Agent for a youth fastpitch NCS team-level roster monitor.

Your job: produce a concise Markdown intelligence report for a coach/admin portal.

Hard rules:
- Keep the analysis at the team and roster-movement level.
- Do not rank, evaluate, profile, or make claims about individual youth athletes.
- You may mention counts and team names.
- If recent change rows include player names, treat them only as factual roster-change records; do not infer ability, motive, eligibility, or personal details.
- Give practical monitoring actions only: coverage gaps, event coverage, teams to re-check, workflow/data health.
- Return Markdown only. No JSON wrapper. No code fences.

Report format:
# Claude Roster Agent Report
## What changed
## Teams to watch
## Coverage gaps
## Recommended next actions

Context:
{safe_context}
""".strip()


def call_claude(prompt: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    model = os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL
    payload = {
        "model": model,
        "max_tokens": 1800,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    report = "".join(text_blocks).strip()
    if not report:
        raise RuntimeError("Claude returned an empty report")
    return report + "\n"


def main() -> int:
    REPORT_DIR.mkdir(exist_ok=True)

    cfg = load_yaml(ROOT / "config.yaml")
    snapshot = load_json(snapshot_path_from_config(cfg))
    changes = read_changelog(REPORT_DIR / "changelog.csv")
    context = build_context(cfg, snapshot, changes)

    mode = "claude"
    error = None
    try:
        markdown = call_claude(build_prompt(context))
    except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        mode = "fallback"
        error = str(exc)
        markdown = deterministic_report(context, error)

    REPORT_MD.write_text(markdown)
    REPORT_JSON.write_text(
        json.dumps(
            {
                "generated_at": context["generated_at"],
                "mode": mode,
                "error": error,
                "team_count": context["team_count"],
                "recent_change_summary": context["recent_change_summary"],
                "report_markdown_path": str(REPORT_MD.relative_to(ROOT)),
            },
            indent=2,
        )
        + "\n"
    )

    print(f"Claude roster agent wrote {REPORT_MD.relative_to(ROOT)} ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
