#!/usr/bin/env bash
# Run the common deployment tool in an Ubuntu host or Ubuntu WSL2 distribution.
set -euo pipefail

if [[ "$(uname -s)" != "Linux" || ! -r /etc/os-release ]]; then
  printf '%s\n' 'This entry point requires Ubuntu. Use macos.sh or windows.ps1 on those systems.' >&2
  exit 2
fi
# /etc/os-release belongs to the operating system, not the deployment profile.
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  printf '%s\n' 'This entry point supports Ubuntu only.' >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'Install Python 3.10 or newer and python3-venv before deploying.' >&2
  exit 2
fi
script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$script_dir/deploy.py" "$@"
