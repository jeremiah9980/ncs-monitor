# GameChanger Player Stats Local Workflow

`gc-player-stats.html`, `gc-leaderboard.html`, `gc-target-players.html`, and `gc-specific-player-stats.html` are static GitHub Pages pages. They cannot read your local `gc_stats.db` directly. The browser loads committed JSON files instead:

```text
reports/gc-player-stats.json
reports/gc-player-leaders.json
reports/gc-target-player-stats.json
reports/gc-specific-player-stats.json
```

Starter JSON files are committed so the GitHub Pages pages do not return 404s. To show real stats, generate the reports locally and push the updated JSON.

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
7. Writes `reports/gc-player-stats.json` for the player stats page.

Useful flags:

```bash
./run_gc_stats.sh --map-only
./run_gc_stats.sh --teams "Bombers"
./run_gc_stats.sh --current-only
./run_gc_stats.sh --skip-follow
./run_gc_stats.sh --max-games 10
./run_gc_stats.sh --profile "/path/from/chrome-profile"
```

## Pull only selected players from GameChanger

The selected-player pull is set for:

- Jordyn Haynes
- Brooklyn Franco
- Maisy Finlestein
- Abigail Holland

The names live in:

```text
specific_players.txt
```

Run this to scrape only those players' current and last-season teams, then write a focused batting/pitching report:

```bash
./run_gc_specific_players.sh --headful
```

For a full-season pull of only those selected players:

```bash
./run_gc_specific_players.sh --full-season --headful
```

That writes:

```text
reports/gc-specific-player-stats.json
```

and the page is:

```text
gc-specific-player-stats.html
```

## Export the player stats page JSON

After `gc_stats.db` exists, regenerate the static player stats JSON without opening GameChanger again:

```bash
./export_gc_stats.sh --max-games 10
```

That writes:

```text
reports/gc-player-stats.json
```

## Export the leaderboard JSON

Generate HR, triples, doubles, hits, RBI, runs, stolen bases, walks, AVG, and pitching leaderboards per season and per year:

```bash
./export_gc_leaders.sh --limit 25 --min-ab 10
```

That writes:

```text
reports/gc-player-leaders.json
```

## Export target-player batting and pitching stats from existing DB

This does not scrape GameChanger. It only filters whatever is already in `gc_stats.db`:

```bash
./export_gc_target_players.sh
```

That writes:

```text
reports/gc-target-player-stats.json
```

and the page is:

```text
gc-target-players.html
```

## View locally

```bash
python3 -m http.server 8123
# open http://localhost:8123/gc-player-stats.html
# open http://localhost:8123/gc-leaderboard.html
# open http://localhost:8123/gc-target-players.html
# open http://localhost:8123/gc-specific-player-stats.html
```

Serve the repo folder over HTTP so the browser can fetch the JSON reports.

## Publish to GitHub Pages

```bash
git add reports/gc-player-stats.json reports/gc-player-leaders.json reports/gc-target-player-stats.json reports/gc-specific-player-stats.json
git commit -m "Update GC stats reports"
git push
```

Then refresh:

```text
https://jeremiah9980.github.io/ncs-monitor/gc-player-stats.html
https://jeremiah9980.github.io/ncs-monitor/gc-leaderboard.html
https://jeremiah9980.github.io/ncs-monitor/gc-target-players.html
https://jeremiah9980.github.io/ncs-monitor/gc-specific-player-stats.html
```

## Important

`gc_stats.db` stays ignored and should not be committed. The JSON files under `reports/` are intentionally tracked because GitHub Pages needs them. Review the generated JSON before pushing it to a public repo.
