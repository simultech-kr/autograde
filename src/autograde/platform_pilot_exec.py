"""Trusted POSIX launcher for the explicitly unsafe pilot-local grader.

This module is an implementation detail.  The parent process invokes this
file with an absolute Python executable and a fixed argument vector.  Resource
limits are installed before the instructor-owned ``assessment/grade.py`` is
executed in the same process.

It is intentionally not a security sandbox.  In particular, resource limits
do not block the assessment or student processes from reading host files or
using the network as the service account.
"""

from __future__ import annotations

import os
from pathlib import Path
import resource
import runpy
import stat
import sys


_CONFIG_EXIT = 78
_READY_MARKER = b"autograde-pilot-ready-v1\n"


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _set_limit(kind: int, value: int) -> None:
    """Lower one soft/hard limit without trying to raise the inherited cap."""

    _soft, hard = resource.getrlimit(kind)
    if hard != resource.RLIM_INFINITY:
        value = min(value, int(hard))
    resource.setrlimit(kind, (value, value))


def _entrypoint(value: str) -> Path:
    path = Path(value)
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError("assessment entrypoint must be a regular file")
    return path.resolve(strict=True)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 5:
        return _CONFIG_EXIT
    try:
        entrypoint = _entrypoint(arguments[0])
        memory_bytes = _positive_int(arguments[1], "memory_bytes")
        cpu_seconds = _positive_int(arguments[2], "cpu_seconds")
        max_file_bytes = _positive_int(arguments[3], "max_file_bytes")
        max_open_files = _positive_int(arguments[4], "max_open_files")

        _set_limit(resource.RLIMIT_CPU, cpu_seconds)
        _set_limit(resource.RLIMIT_FSIZE, max_file_bytes)
        _set_limit(resource.RLIMIT_NOFILE, max_open_files)
        if hasattr(resource, "RLIMIT_AS"):
            try:
                _set_limit(resource.RLIMIT_AS, memory_bytes)
            except (OSError, ValueError):
                # Darwin exposes RLIMIT_AS but rejects lowering it for a live
                # Python process.  The pilot remains usable there with wall
                # time/file/fd/CPU bounds; startup output labels memory and
                # process-tree isolation as unavailable.
                if sys.platform != "darwin":
                    raise
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, OverflowError, RuntimeError, TypeError, ValueError):
        return _CONFIG_EXIT

    # Isolated mode omits the script directory from sys.path.  Add only the
    # instructor assessment directory so sibling helper imports remain useful.
    os.write(2, _READY_MARKER)
    sys.path.insert(0, str(entrypoint.parent))
    sys.argv = [str(entrypoint)]
    runpy.run_path(str(entrypoint), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
