# NCS Roster Watch

Monitors NCS fastpitch **12U** team rosters in **Central Texas** and alerts you
when a player is **removed** (or added) from any team you track.

It runs on a schedule via **GitHub Actions** — no machine of yours has to be on.
Each run pulls rosters, diffs them against the last snapshot, commits the new
snapshot back to the repo (so the **git history is a full audit trail of roster
changes**), writes a Markdown report, and notifies you only when something
actually changed.

---

## How it works

```
NCS API ──▶ normalize ──▶ filter to Central TX ──▶ diff vs snapshots/latest.json
                                                        │
                                   ┌────────────────────┼─────────────────────┐
                                   ▼                     ▼                     ▼
                          reports/changes-*.md   notify (issue/email/slack)  commit snapshot
```

- **State:** `snapshots/latest.json` is the baseline. It's committed every run,
  so `git log -p snapshots/latest.json` shows exactly when each roster changed.
- **Audit log:** `reports/changelog.csv` accumulates every add/remove with a timestamp.
- **Notifications fire only on change.** First run just sets the baseline silently.

---

## Setup

1. **Create the repo** (e.g. `jeremiah9980/ncs-roster-watch`) and push these files.

2. **Point it at the real NCS API.** Open `config.yaml` and set:
   - `api.base_url` and `api.endpoint` — the real roster endpoint
   - `api.query_params` — whatever narrows results to 12U / Texas
   - `api.token_header` / `token_scheme` — how the token is passed
   The `base_url`/`endpoint` shipped here are **placeholders** — verify them.

3. **Add the token as a secret.**
   Repo → Settings → Secrets and variables → Actions → New repository secret →
   name it `NCS_TOKEN` (matches `api.token_env` in the config).

4. **Choose how you're notified** (in `config.yaml` under `notify:`):
   - `github_issue` — **on by default, zero setup.** Opens an issue on each change.
   - `email` — uncomment and add `SMTP_*` secrets (use a Gmail *app password*, not your login).
   - `slack` — uncomment and add a `SLACK_WEBHOOK_URL` secret.

5. **Adjust the schedule** in `.github/workflows/roster-watch.yml`
   (`cron: "0 13 * * *"` = daily ~8 AM Central). You can also run it any time from
   the **Actions tab → NCS Roster Watch → Run workflow**.

---

## Test it locally first

No API or token needed — use the bundled sample files:

```bash
pip install -r requirements.txt

python ncs_monitor.py --input samples/sample_response.json        # sets baseline
python ncs_monitor.py --input samples/sample_response_after.json  # detects Mia removed, Quinn added
```

Use `--dry-run` to see changes without saving the snapshot or notifying.

---

## Tuning

- **Which cities count as "Central TX":** edit `central_tx_cities` in `config.yaml`.
- **Tracking specific named teams instead of by city:** clear `central_tx_cities`
  (leave it empty) and add a `free`-text filter, or pre-filter via `query_params`.
- **Field names guessed wrong?** Run once, read the `Field mapping:` line in the
  log, and pin the correct keys in `config.yaml` under `mapping:`.

## Diff keys

A player is matched by stable `player_id` when the API provides one (rename/jersey
changes won't cause false positives). Without an id, the key falls back to
`name + jersey`, so a name typo or number change reads as a remove + add. Mapping
`player_id` is worth it if the field exists.

## Notes

- The diff is **scope-bounded**: a player only counts as "removed" if their team
  still appears in scope. If a whole team disappears from the API response, that
  shows as a "new team" event when it returns, not per-player removals.
- The script uses only the standard library plus `pyyaml`.
