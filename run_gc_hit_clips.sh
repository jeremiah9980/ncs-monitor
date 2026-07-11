#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv-gc ]; then
  python3 -m venv .venv-gc
fi

./.venv-gc/bin/python -m pip install --upgrade pip >/dev/null
./.venv-gc/bin/python -m pip install -r requirements-gc.txt

./.venv-gc/bin/python gc_hit_clip_downloader.py "$@"
