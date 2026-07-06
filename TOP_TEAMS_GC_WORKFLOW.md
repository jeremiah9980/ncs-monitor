# GameChanger Top Teams Workflow

Use this when you want to collect GameChanger batting and pitching stats for your own curated top-team list.

## Team list

Create a local file named:

```text
top_teams.txt
```

Put one team-name search term per line. Blank lines and lines starting with `#` are ignored.

Example format:

```text
Team Name One
Team Name Two
Team Name Three
```

If `top_teams.txt` is missing or empty, the runner falls back to `config.yaml` under `special_watch`.

## Map-only review pass

Start with a map-only pass so you can review `gc_team_map.json` before scraping box scores:

```bash
./run_gc_top_teams.sh --map-only --headful
```

Open `gc_team_map.json` and verify each listed team. For wrong or missing matches, paste the correct GameChanger Team ID or `web.gc.com` team URL into `gc_url` and set:

```json
"verified": true
```

## Full top-team stat pull

After the map looks right:

```bash
./run_gc_top_teams.sh --headful
```

By default this runs full-season collection for each listed team and then regenerates:

```text
reports/gc-player-stats.json
reports/gc-player-leaders.json
reports/gc-top-teams-run.json
```

## Faster recent-games-only pull

```bash
./run_gc_top_teams.sh --no-full-season --max-games 10 --headful
```

## Limit while testing

```bash
./run_gc_top_teams.sh --limit 3 --map-only --headful
./run_gc_top_teams.sh --limit 3 --headful
```

## View locally

```bash
python3 -m http.server 8123
```

Open:

```text
http://localhost:8123/gc-player-stats.html
http://localhost:8123/gc-leaderboard.html
```

## Publish

```bash
git add reports/gc-player-stats.json reports/gc-player-leaders.json reports/gc-top-teams-run.json
git commit -m "Update GC top team stats"
git push
```

## Notes

- GameChanger scraping must run locally because it uses your logged-in Chrome profile.
- The runner processes your curated list only; it does not auto-rank or guess top teams.
- The `gc_stats.db` file remains local and should not be committed.
- The published JSON files may include player names and stats, so review before pushing.
