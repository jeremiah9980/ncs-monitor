# NCS Stats DB (`gc_stats.db`)

A SQLite stats-collection pipeline for the NCS fastpitch site (playncs.com),
built on top of the existing `ncs_monitor.py` scraper (it reuses that module's
fetch/parse helpers rather than duplicating them).

> **Important — no per-player stats exist on NCS.** NCS publishes **no**
> per-player statistical lines (no batting average, ERA, etc.). A player page is
> just a bio (name, age, home state) plus a "Roster History" table. This DB is
> therefore **team-level stats** (season record, run totals, per-tournament
> results, full game logs) **plus a player → team/season linkage** built from
> each player's roster history. If you need per-player stats, they are not
> available from this source.

## What the DB contains

| Table | Grain | Key columns |
|-------|-------|-------------|
| `teams` | one row per team | `team_id` PK, `name`, `division`, `city`, `region`, `record`, `win_pct`, `last10`, `streak`, `avg_finish`, `ranking_points`, `avg_runs_scored`, `avg_runs_allowed`, `avg_run_diff`, `runs_scored`, `runs_allowed`, `first_seen`, `last_updated` |
| `players` | one row per player | `player_id` PK, `name`, `age`, `location`, `url`, `first_seen`, `last_updated` |
| `roster` | player ↔ team membership | PK `(team_id, player_id)`, `number`, `first_seen`, `last_seen` |
| `player_team_history` | every team/season a player has been rostered on | PK `(player_id, team_name, season, division, status)`, `team_id`, `location` |
| `team_class_records` | a team's record vs each opponent class | PK `(team_id, opponent_class)`, `record` |
| `team_tournament_results` | one row per tournament a team played | PK `(team_id, date, event, division)`, `place`, `points`, `record`, `avg_rs`, `max_rs`, `avg_ra`, `avg_rd` |
| `team_games` | one row per game (full game log) | PK `(team_id, game_uid)`, `date`, `team_score`, `opp_score`, `result` (W/L/T), `opponent`, `opp_state`, `opp_class`, `opp_record` |
| `rankings` | ranking snapshots over time | PK `(season_id, age_id, class_id, state, team_id, captured_at)`, `rank`, `team_label`, `record`, `in_class`, `win_pct`, `events_played`, `events_won`, `points` |

All writes are idempotent upserts (`INSERT ... ON CONFLICT DO UPDATE`) that keep
the original `first_seen` and bump `last_updated`/`last_seen`. The DB opens in
WAL mode. `gc_stats.db` is a generated artifact and is git-ignored.

## Scripts

### `collect_stats.py` — team + player collector

Discovers NCS teams in a set of central-TX cities (reusing
`ncs_monitor.discover`), fetches each team page, and writes season stats,
per-class records, tournament results, the full game log, and the roster; then
walks every rostered player's page for their bio + roster history.

```bash
# discover + collect (default six cities: Round Rock, Georgetown, Leander,
# Hutto, Cedar Park, Pflugerville)
python collect_stats.py

python collect_stats.py --db gc_stats.db --cities "Georgetown,Leander" --limit 10
python collect_stats.py --teams "Cortinas"              # collect by team NAME
python collect_stats.py --dry-run                       # parse + print, no writes
python collect_stats.py --input team.html --team-id \
    "https://www.playncs.com/fastpitch/Teams/Details/73839/texas-venom"   # offline
```

Flags: `--config` (default `config.yaml`), `--db` (default `gc_stats.db`),
`--cities` (comma list), `--teams` (comma list of team-name substrings),
`--limit N` (cap teams/players, for testing), `--input`/`--team-id` (offline
single-team parse), `--dry-run`, `--delay` (override the polite request delay).

#### Collecting a specific team by name (`--teams`)

By default the collector only keeps teams in the six central-TX cities. Pass
`--teams` with a comma-separated list of **case-insensitive name substrings** to
instead target a specific team (or club) by name:

```bash
python collect_stats.py --teams "Cortinas"             # one club
python collect_stats.py --teams "Cortinas,Bombers"     # multiple, OR-matched
```

Behavior:

- **The city filter is disabled** when `--teams` is set. Discovery is run with
  an empty city list, so `ncs_monitor` applies only the age-prefix filter and
  returns *all* age-matching teams found in the crawled events; the collector
  then keeps only teams whose `name` (lowercased) contains any of the given
  substrings. Any manual `teams:` from `config.yaml` whose name matches are
  included too.
- **`--teams` takes precedence over `--cities`.** If both are passed, `--cities`
  is ignored (a note is logged). `--limit` still applies *after* name selection.
- **Caveat — the team must appear in a crawled event.** This only finds teams
  registered in the crawled events (the seeded event ids plus any auto-discovered
  upcoming events in the search radius). A team that has played *no* crawled
  central-TX event will not be found this way, and the run logs `matched 0
  team(s)`. Add the relevant event id to `discovery.events` in `config.yaml`, or
  the team to the manual `teams:` list, to pick it up.

### `ncs_rankings.py` — rankings snapshotter

Fetches the NCS Team Rankings table for each `(season, age, class, state)` combo
and upserts a timestamped snapshot into `rankings`.

```bash
python ncs_rankings.py                                   # 2026+2027, 10U/12U/14U, C/B/A/Open, TX
python ncs_rankings.py --db gc_stats.db --seasons 30 --ages 4 --classes 7 --state TX
```

ID reference: seasons `2024=18, 2025=23, 2026=30, 2027=33`; ages
`10U=4, 12U=6, 14U=8`; classes `C-Rec=9, C=7, B=6, A=5, Open=68`.

### `ncs_stats_db.py` — DB layer

`init_db(path)` opens the DB, sets WAL, creates the schema, and returns the
connection. Also exposes the `upsert_*` helpers used by the collectors.

## Example SQL

A team's game log (most recent first):

```sql
SELECT date, result, team_score, opp_score, opponent, opp_class
FROM team_games
WHERE team_id = '73839'
ORDER BY rowid;
```

Every team a given player has been on, with that team's current record:

```sql
SELECT h.season, h.division, h.status, h.team_name, t.record, t.win_pct
FROM player_team_history h
LEFT JOIN teams t ON t.team_id = h.team_id
WHERE h.player_id = '318680'
ORDER BY h.season DESC;
```

Top teams by ranking points:

```sql
SELECT name, division, city, record, ranking_points
FROM teams
ORDER BY ranking_points DESC
LIMIT 20;
```

A team's win/loss breakdown from the game log:

```sql
SELECT result, COUNT(*) AS games
FROM team_games
WHERE team_id = '73839'
GROUP BY result;   -- W/L/T totals reconcile with teams.record
```

Latest ranking snapshot for a class:

```sql
SELECT rank, team_label, record, points
FROM rankings
WHERE season_id='30' AND age_id='4' AND class_id='7' AND state='TX'
  AND captured_at = (SELECT MAX(captured_at) FROM rankings
                     WHERE season_id='30' AND age_id='4' AND class_id='7' AND state='TX')
ORDER BY rank
LIMIT 25;
```
