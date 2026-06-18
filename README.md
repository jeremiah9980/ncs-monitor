# NCS Roster Watch

Automated roster monitoring for NCS (National Championship Sports) fastpitch softball teams in Central Texas. Tracks 10U and 12U teams within a 25-mile radius of Georgetown, TX (78628), alerting you when players are added or removed from rosters.

**Live Dashboard**: Open `ncs-dashboard.html` in a browser to view all teams, rosters, player details, and recent changes.

## Features

- **Auto-Discovery**: Finds teams automatically from event "Who's Coming" pages
- **Geographic Filtering**: Tracks only teams within 25 miles of Georgetown using city coordinates
- **Age Group Filtering**: Monitors 10U and 12U divisions
- **Player History Tracking**: Fetches each player's age and complete team history from NCS
- **Real-Time Dashboard**: Interactive HTML dashboard with team overview, roster viewer, and change tracker
- **Multiple Notifications**: GitHub Issues, Slack webhooks, and email alerts
- **Scheduled Monitoring**: Runs every 30 minutes via GitHub Actions
- **Full Audit Trail**: Git history of snapshots tracks every roster change

## How It Works

```
seed events --> read each "Who's Coming" --> keep 10U/12U + central-TX cities  (DISCOVERY)
       |
       v
   team list (cached) --> fetch each roster --> fetch player details --> diff vs snapshot
                                                      |
                                +---------------------+---------------------+
                                v                     v                     v
                         reports/changes-*.md   notify (issue/slack/email)  commit snapshot
```

### Discovery Phase
The script reads seed event "Who's Coming" pages and keeps teams that:
- Have a division starting with `10U` or `12U`
- Are located in a Central Texas city within 25 miles of Georgetown

The team list is cached in `discovered_teams.json` and refreshed every 24 hours.

### Monitor Phase
For each discovered team (plus manually configured teams):
1. Fetch the team's roster page
2. Fetch each player's detail page (age, team history) - cached to avoid re-fetching
3. Compare against the last snapshot
4. Report and notify on any changes
5. Commit the updated snapshot

### Player Details
Each player's NCS profile is fetched to extract:
- **Age**: e.g., "10y 11m"
- **Team History**: Complete list of teams with division, season, and status (Active/Guest/Past/Removed)

Player details are cached in the snapshot, so only new players are fetched each run.

## Dashboard

Open `ncs-dashboard.html` to access the interactive dashboard:

- **Team Overview**: Grid of all teams with player counts, filterable by age group and city
- **Roster Viewer**: View any team's roster with player ages and expandable team histories
- **Changes Tracker**: See recent roster additions, removals, and new teams
- **Team Directory**: Searchable list of all monitored teams

The dashboard reads from `snapshots/latest.json` which is updated every 30 minutes.

## Configuration

Edit `config.yaml` to customize monitoring:

```yaml
discovery:
  enabled: true
  age_prefixes: ["10U", "12U"]      # Track these age groups
  refresh_hours: 24                  # Re-crawl events daily
  
  central_tx_cities:                 # Cities within 25mi of Georgetown
    - Georgetown
    - Round Rock
    - Cedar Park
    - Austin
    # ... (full list in config.yaml)
  
  events:                            # Seed events for team discovery
    - 12473   # 2026 Central TX 12U Summer State
    - 13484   # 2026 NCS Central Texas Fall State
    - 10093   # 2025 Central TX 12U Summer State (Class C)

# Teams to always track (regardless of event registration)
teams:
  - url: "https://www.playncs.com/Fastpitch/Teams/Details/39016/bananas-2k15"
  - url: "https://www.playncs.com/fastpitch/Teams/Details/73839/texas-venom"
  - url: "https://www.playncs.com/fastpitch/Teams/Details/79552/ctx-bombers-meza"

notify:
  github_issue:
    labels: ["roster-change"]
  slack:
    webhook_env: SLACK_WEBHOOK_URL
```

### Adding Events
Find event IDs from playncs.com URLs: `.../Events/Details/<ID>/...`

### Adding Teams Manually
Add teams to the `teams:` section to track them regardless of event registration. This is useful for teams that don't appear in seed events.

## Files

| File | Purpose |
|------|---------|
| `ncs_monitor.py` | Main monitoring script |
| `ncs-dashboard.html` | Interactive web dashboard |
| `discover_all_teams.py` | Standalone team discovery with geographic filtering |
| `config.yaml` | Configuration (events, cities, notifications) |
| `snapshots/latest.json` | Current roster snapshot with player details |
| `discovered_teams.json` | Cached team list from discovery |
| `reports/changelog.csv` | Historical log of all roster changes |
| `reports/changes-*.md` | Individual change reports |

## Setup

### 1. Make the Repository Private

Snapshots contain players' names and IDs. Keep this repo **private** to avoid re-publishing aggregated data.

### 2. Configure Your Area

Edit `config.yaml`:
- `age_prefixes`: Age groups to track (e.g., `["10U", "12U"]`)
- `central_tx_cities`: Cities within your target radius
- `events`: Seed event IDs for team discovery
- `teams`: Any teams to always track

### 3. Set Up Notifications (Optional)

**Slack**: Add `SLACK_WEBHOOK_URL` to repository secrets

**Email**: Uncomment the `email:` section and add SMTP secrets

### 4. Run the Workflow

The workflow runs automatically every 30 minutes. To trigger manually:
- Go to **Actions > NCS Roster Watch > Run workflow**

First run establishes a baseline; subsequent runs report changes.

## Local Testing

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# See discovered teams (no monitoring)
python ncs_monitor.py --discover-only

# Full run (first run = baseline)
python ncs_monitor.py

# Dry run (show changes without saving/notifying)
python ncs_monitor.py --dry-run

# Force re-discovery even if cache is fresh
python ncs_monitor.py --force-discover
```

### Running the Dashboard Locally

```bash
# Serve files to avoid CORS issues
python -m http.server 8000
# Open http://localhost:8000/ncs-dashboard.html
```

## GitHub Actions

The workflow (`.github/workflows/roster-watch.yml`) runs every 30 minutes:

```yaml
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:  # Manual trigger
```

### Actions Minutes Usage

A private repo on the Free plan gets ~2,000 minutes/month. At 48 runs/day (~1.5 min each), you'll use ~2,100 minutes. If you hit the cap, change to hourly: `0 * * * *`.

## Notes

- **Player keying**: Players are identified by their stable NCS player ID, so jersey number or name spelling changes don't cause false alerts
- **Rate limiting**: The script waits 2 seconds between requests to be a good neighbor to playncs.com
- **Team removal**: A team drops off the watchlist when it's no longer in any seed event. Add multiple events for stable coverage
- **Dependencies**: `pyyaml`, `beautifulsoup4` (uses stdlib `html.parser`, no lxml required)

## Privacy Notice

This tool monitors publicly available data from playncs.com, but the snapshots contain minors' names and player IDs. Keep this repository **private** and use the data responsibly.
