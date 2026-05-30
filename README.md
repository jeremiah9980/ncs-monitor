# NCS Roster Watch (auto-discovery)

Finds NCS fastpitch teams in your area on their own and alerts you when a player
is **removed** (or added) from any of their rosters. Runs on a schedule via
**GitHub Actions** -- nothing of yours has to be on.

Everything is public server-rendered HTML on playncs.com: no API, token, or login.

## How it works

```
seed events --> read each "Who's Coming" --> keep 12U + central-TX cities  (DISCOVERY)
       |
       v
   team list (cached) --> fetch each roster --> diff vs snapshots/latest.json   (MONITOR)
                                                      |
                                +---------------------+---------------------+
                                v                     v                     v
                         reports/changes-*.md   notify (issue/email/slack)  commit snapshot
```

- **Discovery**: you list a few central-TX *events*; the script reads each
  event's public "Who's Coming" table and keeps the teams whose division starts
  with `12U` and whose city is in your list. Cached to `discovered_teams.json`
  and refreshed once a day (teams don't come and go every 30 minutes).
- **Monitor**: fetch each team's roster page, compare to the last snapshot,
  report + notify on changes. Players are keyed on their **stable player id**
  (`/Players/Details/<id>/`), so jersey/spelling edits never cause false alerts.

State lives in `snapshots/latest.json`, committed every run, so
`git log -p snapshots/latest.json` is a full history of who joined/left and when.
`reports/changelog.csv` accumulates every change with a timestamp.

---

## These rosters contain minors' names -- keep the repo PRIVATE

Snapshots/reports contain kids' names, numbers, and player ids. It's public on
playNCS, but committing it into a **public** repo re-publishes and aggregates it.
Make this repository **private** (Settings -> General -> Change visibility)
before the first run. Actions, schedules, issues, and snapshot commits all work
fine in a private repo.

---

## About "every 30 minutes"

The workflow is set to `*/30 * * * *` as requested, but two honest caveats:

1. **Cost.** A *private* repo on the Free plan gets ~2,000 Actions minutes/month.
   48 runs/day at ~1.5 min each is ~2,100/month -- right at the edge, and more if
   you watch many teams. If you hit the cap, change the cron to hourly
   (`0 * * * *`). Rosters change over days, so you lose almost nothing.
2. **Timing.** GitHub runs scheduled jobs late or irregularly under load. `*/30`
   means "roughly every 30 minutes," not a guarantee.

Also be a good neighbor to playNCS: each run fetches one page per team. Watching
30 teams every 30 min is ~1,400 requests/day. Hourly halves it. The script
already waits `request_delay_seconds` (default 2s) between requests.

---

## Setup

1. **Make the repo private**, then push these files.

2. **Pick your area** in `config.yaml`:
   - `age_prefixes` -- e.g. `["12U"]`
   - `central_tx_cities` -- edit the list
   - `events` -- add central-TX 12U tournaments. Open an event on playncs.com and
     copy the number from its URL (`.../Events/Details/<ID>/...`). Seeded with
     `10093` (2025 Central TX 12U Summer State, Class C) as a working example;
     add your current-season events for live coverage.

3. **Notifications** (`notify:` in config): `github_issue` is on by default,
   zero setup. Uncomment `email` (Gmail app password) or `slack` (webhook) and
   add the matching repo secrets.

4. **Schedule** lives in `.github/workflows/roster-watch.yml`. Run it any time
   from **Actions -> NCS Roster Watch -> Run workflow**.

First run records the baseline silently; later runs report changes.

---

## Test locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# See who discovery finds for your area (reads the seed events live, no monitoring):
python ncs_monitor.py --discover-only

# Offline roster diff against the bundled sample pages:
TID="https://www.playncs.com/Fastpitch/Teams/Details/39016/bananas-2k15"
python ncs_monitor.py --input samples/bananas_before.html --team-id "$TID"
python ncs_monitor.py --input samples/bananas_after.html  --team-id "$TID"
```

Then a full live run:
```bash
python ncs_monitor.py            # first run = baseline for every discovered team
python ncs_monitor.py --dry-run  # later: show changes without saving/notifying
```

## Notes & limits

- A player counts as "removed" only for a team currently in your watchlist. If a
  team's page lists nobody, every prior player reads as removed -- glance before
  trusting a mass-removal alert.
- A team drops off the list when it's no longer in any seed event for your age +
  cities. Add multiple/season-long events for stable coverage.
- Dependencies: `pyyaml`, `beautifulsoup4` (stdlib `html.parser`, no lxml).
