# GameChanger Stats Dashboard (Cloudflare Pages + D1)

A Cloudflare Pages dashboard that serves the GameChanger per-player stats
(`gc_stats.db`) live from a **D1** database, rendered by `gc-stats-dashboard.html`.

> ## PRIVACY — READ FIRST
> `gc_stats` contains **statistics for minors**. It must never be reachable
> without authentication. The **`/api/` data endpoints are fail-closed**: the
> Pages Function middleware (`functions/api/_middleware.js`) refuses to serve any
> player data — including the report and health endpoints — unless an access
> mechanism is configured. The gate is scoped to `/api/*`, so the existing static
> site and the dashboard's HTML shell stay reachable, but **no player data is
> served without auth**. To also restrict the HTML shell itself, enable
> **Cloudflare Access** on the whole Pages project. Do NOT commit `gc_stats.db`
> or `reports/gc-player-stats.json` (both are git-ignored).

## Architecture

- `wrangler.jsonc` — Pages config; binds D1 database `gc_stats` as `DB`.
- `migrations/0001_gc_stats.sql` — the schema (mirrors `gc_player_stats.py`).
- `functions/api/_middleware.js` — fail-closed access gate on `/api/*` only
  (Access → password → 503); leaves static assets and the HTML shell reachable.
- `functions/api/health.js` — `GET /api/health` → `{ok, hasDB}` (no data).
- `functions/api/report.js` — `GET /api/report` → the report JSON from D1.
- `functions/api/_report_lib.js` — pure `buildReport()` reshaper (unit-testable).
- `gc-stats-dashboard.html` — the venom-themed UI; fetches `/api/report`.
- `scripts/sync_gc_stats_to_d1.sh` — load a local `gc_stats.db` into D1.

## Setup

### 1. Apply the schema to D1

```sh
wrangler d1 execute gc_stats --remote --file=migrations/0001_gc_stats.sql
```

(Use `--local` to create/refresh the local D1 state under `.wrangler/` first.)

### 2. Load data

The scraper (`gc_player_stats.py`) runs locally and writes `gc_stats.db`. Push
it into D1 with the sync script:

```sh
scripts/sync_gc_stats_to_d1.sh --local     # safe: local D1 only
scripts/sync_gc_stats_to_d1.sh --remote    # UPLOADS minors' data to Cloudflare
```

`--remote` prompts for confirmation because it uploads minors' data.

### 3. REQUIRED before exposing — configure the access gate

The `/api/` data endpoints will return **503 (locked)** until one of these is
set. Pick one:

- **Cloudflare Access (recommended):** enable Access on the Pages project in the
  Cloudflare dashboard. Access injects a verified
  `Cf-Access-Authenticated-User-Email` header and the middleware allows the
  request. Enabling Access on the whole project also restricts the HTML shell and
  static assets, not just `/api/`.
- **Shared password:**

  ```sh
  wrangler pages secret put DASHBOARD_PASSWORD
  ```

  The middleware then requires HTTP Basic Auth whose password equals the secret
  (username is ignored; use anything).

### 4. Deploy

```sh
wrangler pages deploy .
```

…or connect the repo to Cloudflare Pages git integration (build output dir `.`).

### 5. Local development

```sh
# create the local D1 schema + seed a row or two, then:
echo 'DASHBOARD_PASSWORD=test' > .dev.vars   # git-ignored; local only
wrangler pages dev .
# curl -u any:test http://localhost:8788/api/report
```

Without `DASHBOARD_PASSWORD` (or an Access header) the `/api/` endpoints return
503 — the fail-closed default is identical locally and in production. Static
files (the HTML shell, `index.html`) remain served without auth unless you also
enable Cloudflare Access on the whole project.

## Notes

- `wrangler.jsonc` sets `pages_build_output_dir: "."`, so the repo root is served
  as static assets and everything under `functions/` becomes Pages Functions.
- `.wrangler/`, `node_modules/`, and `.dev.vars` are git-ignored — never commit
  them.
