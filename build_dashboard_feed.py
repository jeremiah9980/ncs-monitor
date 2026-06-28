import json
import re
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).parent
reports = root / "reports"
feed_file = reports / "slack-feed.json"


def parse_file(path):
    text = path.read_text(encoding="utf-8")
    title = re.search(r"# NCS Roster Changes - (.+)", text)
    totals = re.search(r"\*\*(\d+) removed - (\d+) added\*\* across (\d+) team", text)
    if not title or not totals:
        return None
    removed, added, team_count = map(int, totals.groups())
    try:
        stamp = datetime.strptime(title.group(1).strip(), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        stamp = title.group(1).strip()
    teams = []
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    for part in parts:
        lines = part.strip().splitlines()
        header = lines[0]
        name, city, region = header, "", ""
        location = re.match(r"(.+?) - ([^,]+), (.+)$", header)
        if location:
            name, city, region = location.groups()
        team = {"name": name, "city": city, "region": region, "new_team": False, "removed": [], "added": []}
        for line in lines[1:]:
            line = line.strip()
            if "New team now tracked" in line:
                team["new_team"] = True
            elif line.startswith("- REMOVED:"):
                team["removed"].append(line.split(":", 1)[1].strip())
            elif line.startswith("- added:"):
                team["added"].append(line.split(":", 1)[1].strip())
        teams.append(team)
    return {"id": stamp, "timestamp": stamp, "subject": f"NCS roster: {removed} removed, {added} added", "removed": removed, "added": added, "team_count": team_count, "teams": teams, "markdown": text.strip(), "source_report": path.name}


reports.mkdir(exist_ok=True)
existing = []
if feed_file.exists():
    try:
        existing = json.loads(feed_file.read_text(encoding="utf-8")).get("alerts", [])
    except Exception:
        existing = []
by_id = {item["id"]: item for item in existing if item.get("id")}
for path in reports.glob("changes-*.md"):
    item = parse_file(path)
    if item:
        by_id[item["id"]] = item
alerts = sorted(by_id.values(), key=lambda item: item.get("timestamp", ""), reverse=True)[:100]
feed_file.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "alerts": alerts}, indent=2), encoding="utf-8")
print(f"Dashboard feed contains {len(alerts)} alerts")
