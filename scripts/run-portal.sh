#!/usr/bin/env bash
# Run from any working directory; keep the existing checkout and pilot data.
set -euo pipefail
script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(CDPATH= cd -- "$script_dir/.." && pwd)"
if [[ ! -x "$repo_dir/.venv/bin/python" ]]; then
  printf '%s\n' '[launcher] .venv/bin/python이 없습니다. 기존 서버 가상환경을 먼저 준비하세요.' >&2
  exit 1
fi
exec "$repo_dir/.venv/bin/python" "$script_dir/run_portal.py" "$@"
