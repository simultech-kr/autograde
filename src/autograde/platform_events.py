"""Secret-safe, one-line operational events for the platform service.

The platform deliberately does not log exception messages, HTTP targets,
headers, request bodies, or grading output.  stderr is captured by systemd and
other common service supervisors, so compact JSON lines are sufficient for the
MVP without coupling the service to one logging backend.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from typing import Optional


_EVENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_COMPONENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_EXCEPTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SUBMISSION_ID = re.compile(r"sub_[A-Za-z0-9._:-]{1,123}\Z")
_WRITE_LOCK = threading.Lock()


def emit_operator_event(
    event: str,
    *,
    component: str,
    level: str = "error",
    exception: Optional[BaseException] = None,
    submission_id: Optional[str] = None,
) -> None:
    """Write one allowlisted event without serializing attacker-controlled detail.

    The exception's type is useful for grouping failures, while its string
    representation is intentionally never evaluated or emitted because it may
    contain credentials, paths, source code, or student output.
    """

    if not isinstance(event, str) or _EVENT_NAME.fullmatch(event) is None:
        event = "invalid_event"
    if not isinstance(component, str) or _COMPONENT_NAME.fullmatch(component) is None:
        component = "platform"
    if level not in {"error", "warning", "info"}:
        level = "error"
    payload = {
        "component": component,
        "event": event,
        "level": level,
    }
    if exception is not None:
        exception_name = type(exception).__name__
        payload["exception_type"] = (
            exception_name
            if _EXCEPTION_NAME.fullmatch(exception_name) is not None
            else "Exception"
        )
    if (
        isinstance(submission_id, str)
        and _SUBMISSION_ID.fullmatch(submission_id) is not None
    ):
        payload["submission_id"] = submission_id

    try:
        line = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with _WRITE_LOCK:
            print(line, file=sys.stderr, flush=True)
    except Exception:
        # Logging must never become a second failure path.  This constant
        # fallback contains no caller-controlled data.
        with _WRITE_LOCK:
            print(
                '{"component":"platform","event":"event_write_failed","level":"error"}',
                file=sys.stderr,
                flush=True,
            )


__all__ = ["emit_operator_event"]
