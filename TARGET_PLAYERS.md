# Target Player Stats

This repo now has a focused report for these players:

- Jordyn Haynes
- Brooklyn Franco
- Maisy Finlestein
- Abigail Holland

The report includes:

- Overall batting totals
- Overall pitching totals
- Per-season batting and pitching totals
- Per-year batting and pitching totals
- Recent game lines with batting and pitching columns

## Generate from local `gc_stats.db`

First make sure `gc_stats.db` exists by running the GameChanger collector locally:

```bash
./run_gc_stats.sh --full-season
```

Then export just these target players:

```bash
chmod +x export_gc_target_players.sh
./export_gc_target_players.sh
```

That creates:

```text
reports/gc-target-player-stats.json
```

## View locally

```bash
python3 -m http.server 8123
```

Open:

```text
http://localhost:8123/gc-target-players.html
```

## Publish to GitHub Pages

```bash
git add reports/gc-target-player-stats.json
git commit -m "Update target player stats"
git push
```

Live URL:

```text
https://jeremiah9980.github.io/ncs-monitor/gc-target-players.html
```

## Override players

```bash
./export_gc_target_players.sh --players "Jordyn Haynes,Brooklyn Franco,Maisy Finlestein,Abigail Holland"
```
