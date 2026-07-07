# Coach Stat Dashboards

These dashboards are built for Central Texas softball coaches around Georgetown, Leander, Round Rock, Liberty Hill, Hutto, Taylor, Cedar Park, and surrounding areas.

They all read the existing GameChanger export files, so the normal workflow is still:

```bash
./run_gc_top_teams.sh --list-file top_10u_teams.txt --headful --include-aliases
./run_gc_top_teams.sh --list-file top_12u_teams.txt --headful --include-aliases
./export_gc_stats.sh --max-games 999
./export_gc_leaders.sh --limit 100 --min-ab 1
./export_gc_target_players.sh
```

Then view locally:

```bash
python3 -m http.server 8123
open http://localhost:8123/gc-coach-command.html
open http://localhost:8123/gc-lineup-builder.html
open http://localhost:8123/gc-development-watch.html
```

## New dashboards

### `gc-coach-command.html`

A team-level command center for coaches. It shows:

- Team coach score
- Scraped games by team
- Players with matched stats
- Team AVG
- Extra-base hits
- RBI
- Stolen bases
- Impact bats

Best use: quick comparison across local teams and quick discovery of who is producing.

### `gc-lineup-builder.html`

A sortable player board for building lineups. Coaches can filter by team and role:

- Best overall lineup value
- Leadoff / get on base
- Contact / move runners
- Middle-order RBI
- Power / extra bases
- Speed pressure
- Bottom-order reset

Best use: choose lineup roles using production signals instead of only season AVG.

### `gc-development-watch.html`

A development and scouting board. It highlights:

- Heating-up players based on latest five matched games
- Contact development players
- Under-the-radar contributors
- Speed pressure players
- Pitching workload / value

Best use: find kids who are improving, kids who need contact work, and pitchers carrying innings.

## Important notes

- `OBP` on the lineup page is estimated as `(H + BB) / (AB + BB)` because HBP/SF/ROE may not be available from the exported data.
- Development trend compares the latest five matched GameChanger games against earlier matched games in the same export.
- These dashboards depend on accurate `gc_team_map.json` entries. Verified GameChanger URLs improve dashboard quality.
- Youth player data should be reviewed before publishing publicly.
