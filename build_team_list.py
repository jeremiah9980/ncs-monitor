#!/usr/bin/env python3
"""
Build a consolidated team list from NCS discovery and roster snapshots.

Outputs:
  reports/team-list.json - All tracked teams with their current rosters
  reports/team-list.csv  - Flat CSV export for spreadsheet use

Sourced from:
  - discovered_teams.json (team metadata from NCS tournament Who's Coming pages)
  - snapshots/latest.json (current rosters scraped from NCS portal)
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DISCOVERED = ROOT / "discovered_teams.json"
SNAPSHOT = ROOT / "snapshots" / "latest.json"
REPORTS = ROOT / "reports"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def parse_age_group(division: str) -> str:
    """Extract age group (10U, 12U, 14U, etc.) from division string."""
    import re
    m = re.search(r"(\d+U)", division, re.I)
    return m.group(1).upper() if m else ""


def build_team_list() -> list[dict]:
    discovered = load_json(DISCOVERED)
    snapshot = load_json(SNAPSHOT)

    teams_meta = {t["team_id"]: t for t in discovered.get("teams", [])}
    teams_roster = snapshot.get("teams", {})
    player_details = snapshot.get("player_details", {})

    team_list = []

    for team_id, roster_data in teams_roster.items():
        meta = teams_meta.get(team_id, {})

        players = []
        for p in roster_data.get("players", []):
            player_id = p.get("player_id", "")
            details = player_details.get(player_id, {})
            players.append({
                "player_id": player_id,
                "name": p.get("name", ""),
                "number": p.get("num", ""),
                "age": details.get("age", ""),
                "url": p.get("url", ""),
                "team_history_count": len(details.get("team_history", [])),
            })

        team = {
            "team_id": team_id,
            "name": roster_data.get("team_name", meta.get("name", "")),
            "age_group": parse_age_group(roster_data.get("division", meta.get("division", ""))),
            "division": roster_data.get("division", meta.get("division", "")),
            "city": roster_data.get("city", meta.get("city", "")),
            "region": roster_data.get("region", meta.get("region", "TX")),
            "url": roster_data.get("url", meta.get("url", "")),
            "roster_count": len(players),
            "players": players,
        }
        team_list.append(team)

    # Sort by age group, then name
    team_list.sort(key=lambda t: (t["age_group"], t["name"].lower()))
    return team_list


def write_json(team_list: list[dict], path: Path) -> None:
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "team_count": len(team_list),
        "player_count": sum(t["roster_count"] for t in team_list),
        "teams": team_list,
    }
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote {path} ({len(team_list)} teams)")


def write_csv(team_list: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "team_id", "team_name", "age_group", "division", "city", "region",
            "roster_count", "player_id", "player_name", "player_number", "player_age"
        ])
        for t in team_list:
            if not t["players"]:
                writer.writerow([
                    t["team_id"], t["name"], t["age_group"], t["division"],
                    t["city"], t["region"], t["roster_count"],
                    "", "", "", ""
                ])
            else:
                for p in t["players"]:
                    writer.writerow([
                        t["team_id"], t["name"], t["age_group"], t["division"],
                        t["city"], t["region"], t["roster_count"],
                        p["player_id"], p["name"], p["number"], p["age"]
                    ])
    print(f"Wrote {path}")


def main():
    REPORTS.mkdir(exist_ok=True)
    team_list = build_team_list()

    write_json(team_list, REPORTS / "team-list.json")
    write_csv(team_list, REPORTS / "team-list.csv")

    # Summary
    by_age = {}
    for t in team_list:
        ag = t["age_group"] or "Unknown"
        by_age[ag] = by_age.get(ag, 0) + 1

    print(f"\nTeam list built: {len(team_list)} teams")
    for ag in sorted(by_age.keys()):
        print(f"  {ag}: {by_age[ag]} teams")


if __name__ == "__main__":
    main()
