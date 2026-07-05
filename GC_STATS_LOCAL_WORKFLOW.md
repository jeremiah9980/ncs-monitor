# GameChanger Player Stats Local Workflow

`gc-player-stats.html` is a static GitHub Pages page. It cannot read your local `gc_stats.db` directly. The browser loads this committed JSON file instead:

```text
reports/gc-player-stats.json
```

A starter JSON file is committed so the GitHub Pages page no longer returns a 404. To show real stats, generate the report locally and push the updated JSON.

## Pull GameChanger data into `gc_stats.db`

GameChanger requires your logged-in account session, so the collector runs locally using your Chrome profile.

```bash
./run_gc_stats.sh --teams "Venom" --headful
```

Review `gc_team_map.json` afterward. For wrong or missing teams, paste the GameChanger Team ID or `web.gc.com` team URL into `gc_url` and set `verified` to `true`.

When the map looks right, run the broader scrape:

```bash
./run_gc_stats.sh --full-season
```

What it does:

1. Reads `snapshots/latest.json`.
2. Uses each player's current NCS team and last-season teams.
3. Maps those team names to GameChanger teams in `gc_team_map.json`.
4. Reads mapped GameChanger schedules and box scores.
5. Matches stat lines conservatively to NCS players.
6. Stores the data in local SQLite: `gc_stats.db`.
7. Writes `reports/gc-player-stats.json` for the web page.

Useful flags:

```bash
./run_gc_stats.sh --map-only
./run_gc_stats.sh --teams "Bombers"
./run_gc_stats.sh --current-only
./run_gc_stats.sh --skip-follow
./run_gc_stats.sh --max-games 10
./run_gc_stats.sh --profile "/path/from/chrome-profile"
```

## Export the page JSON from the local database

After `gc_stats.db` exists, regenerate the static JSON without opening GameChanger again:

```bash
./export_gc_stats.sh --max-games 10
```

That runs:

```bash
python gc_db_report.py --max-games 10
```

and writes:

```text
reports/gc-player-stats.json
```

## View locally

```bash
python3 -m http.server 8123
# open http://localhost:8123/gc-player-stats.html
```

Serve the repo folder over HTTP so the browser can fetch `reports/gc-player-stats.json`.

## Publish to GitHub Pages

```bash
git add reports/gc-player-stats.json
git commit -m "Update GC player stats report"
git push
```

Then refresh:

```text
https://jeremiah9980.github.io/ncs-monitor/gc-player-stats.html
```

## Important

`gc_stats.db` stays ignored and should not be committed. `reports/gc-player-stats.json` is intentionally tracked because GitHub Pages needs it. Review the generated JSON before pushing it to a public repo.
