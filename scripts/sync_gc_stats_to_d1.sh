#!/usr/bin/env bash
# Sync the local gc_stats.db into the Cloudflare D1 database `gc_stats`.
#
# PRIVACY WARNING: gc_stats.db contains MINORS' statistics. The `--remote` mode
# UPLOADS that data to Cloudflare's D1. Only run `--remote` once you have
# confirmed the Pages project is access-gated (Cloudflare Access or the
# DASHBOARD_PASSWORD secret) and you are cleared to publish it. Default is
# `--local` (writes only to the on-disk local D1 state under .wrangler/).
#
# Usage:
#   scripts/sync_gc_stats_to_d1.sh            # local D1 (default, safe)
#   scripts/sync_gc_stats_to_d1.sh --local
#   scripts/sync_gc_stats_to_d1.sh --remote   # UPLOADS minors' data to Cloudflare
#
# Requires: sqlite3, npx wrangler, and gc_stats.db in the repo root.
set -euo pipefail

MODE="--local"
if [[ "${1:-}" == "--remote" ]]; then
  MODE="--remote"
elif [[ "${1:-}" == "--local" || -z "${1:-}" ]]; then
  MODE="--local"
else
  echo "Unknown argument: ${1}. Use --local (default) or --remote." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${ROOT}/gc_stats.db"

if [[ ! -f "${DB}" ]]; then
  echo "gc_stats.db not found at ${DB}. Run gc_player_stats.py first." >&2
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required to dump the database." >&2
  exit 1
fi

if [[ "${MODE}" == "--remote" ]]; then
  echo "WARNING: --remote uploads minors' stats to Cloudflare D1 (gc_stats)."
  read -r -p "Type 'upload' to continue: " confirm
  if [[ "${confirm}" != "upload" ]]; then
    echo "Aborted."
    exit 1
  fi
fi

TMP_SQL="$(mktemp "${TMPDIR:-/tmp}/gc_stats_sync.XXXXXX.sql")"
trap 'rm -f "${TMP_SQL}"' EXIT

# Dump data, dropping transaction/PRAGMA control lines D1 rejects.
sqlite3 "${DB}" .dump | grep -vE '^(PRAGMA|BEGIN|COMMIT)' > "${TMP_SQL}"

echo "Applying $(wc -l < "${TMP_SQL}") lines of SQL to D1 (gc_stats) ${MODE}..."
npx wrangler d1 execute gc_stats "${MODE}" --file="${TMP_SQL}"
echo "Done."
