#!/usr/bin/env bash
# macOS is a local validation host; a cloud target is an existing Ubuntu server.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf '%s\n' 'This entry point requires macOS. Use ubuntu.sh or windows.ps1 on those systems.' >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'Install Python 3.10 or newer before deploying.' >&2
  exit 2
fi
script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$script_dir/deploy.py" "$@"
