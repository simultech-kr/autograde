"""Bounded, reauthorized SSE transport for instructor classroom snapshots.

The persistent read model is the source of truth, not an in-process pub/sub
queue. This also observes grades written by a separate worker and repairs a
browser reconnect by sending a complete snapshot. No student code is streamed.
"""
from __future__ import annotations

import json
import re
import time

from .platform_events import emit_operator_event

STREAM_SECONDS = 30.0
POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0


def serve(handler, path, query):
    from .platform_http import PlatformHTTPError, _SECURITY_HEADERS, _normalize_result

    handler._ensure_no_body()
    if set(query) - {"assignment_id"}:
        raise PlatformHTTPError(400, "invalid_query", "조회 조건을 확인해 주세요.")
    ids = handler.headers.get_all("Last-Event-ID") or []
    if len(ids) > 1 or (ids and not re.fullmatch(r"[a-f0-9]{64}", ids[0])):
        raise PlatformHTTPError(400, "invalid_event_cursor", "실시간 화면을 새로 열어 주세요.")
    server = handler.platform_server
    origin = handler.headers.get("Origin")
    if (origin and origin != server.public_base_url) or handler.headers.get("Sec-Fetch-Site") == "cross-site":
        raise PlatformHTTPError(403, "instructor_origin", "교수자 웹 주소에서 다시 시도해 주세요.")
    if not server.classroom_stream_slots.acquire(blocking=False):
        raise PlatformHTTPError(429, "classroom_stream_busy", "열린 교실 화면을 줄인 뒤 다시 연결해 주세요.",
                                headers={"Retry-After": "3"})
    started = False
    try:
        cookies = handler._cookies()
        authorization = handler.headers.get("Authorization")
        snapshot_path = path.removesuffix("/events")

        def read():
            # Reauthenticate Basic identity, session cookie, course ACL and
            # availability on EVERY read, including already-open browser tabs.
            return _normalize_result(server.facade.portal_request(
                "GET", snapshot_path, query, cookies, origin, authorization,
            ), 200)

        def frame(name, data, event_id=None):
            encoded = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            if len(encoded) > server.max_response_bytes:
                raise ValueError("classroom snapshot exceeds response limit")
            prefix = ("id: " + event_id + "\n") if event_id else ""
            handler.wfile.write((prefix + "event: " + name + "\ndata: ").encode("ascii") + encoded + b"\n\n")
            handler.wfile.flush()

        response = read()
        if response.status != 200:
            handler._send_result(response)
            return
        # Validate before committing streaming headers so a bad read model
        # still receives a regular, safe HTTP failure.
        encoded = json.dumps(response.body, ensure_ascii=True).encode("ascii")
        if len(encoded) > server.max_response_bytes:
            raise ValueError("classroom snapshot exceeds response limit")
        version = response.body["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
            raise ValueError("invalid classroom snapshot version")
        handler.send_response(200)
        for name, value in _SECURITY_HEADERS.items():
            handler.send_header(name, value)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("X-Accel-Buffering", "no")
        handler.send_header("Connection", "close")
        handler.end_headers()
        started = True
        handler.wfile.write(b"retry: 3000\n\n")
        frame("snapshot", response.body, version)
        deadline = time.monotonic() + STREAM_SECONDS
        last_heartbeat = time.monotonic()
        while not server._closing and time.monotonic() < deadline:
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
            if server._closing or time.monotonic() >= deadline:
                break
            response = read()
            if response.status != 200:
                frame("access_revoked" if response.status in {401, 403, 404} else "stream_error",
                      response.body)
                return
            new_version = response.body.get("version")
            if not isinstance(new_version, str) or not re.fullmatch(r"[a-f0-9]{64}", new_version):
                raise ValueError("invalid classroom snapshot version")
            if new_version != version:
                version = new_version
                frame("snapshot", response.body, version)
            if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                frame("heartbeat", {"server_time": response.body["server_time"]})
                last_heartbeat = time.monotonic()
    except (BrokenPipeError, ConnectionResetError, OSError):
        # A closed tab/slow client is not an application failure. Never append a
        # second HTTP response after SSE headers have already been written.
        if not started:
            raise
    except Exception as exc:
        if not started:
            raise
        emit_operator_event("classroom_stream_unexpected_exception", component="http", exception=exc)
        try:
            frame("stream_error", {"error": {"code": "classroom_unavailable",
                  "message": "실시간 상태를 확인하지 못했습니다. 다시 연결해 주세요."}})
        except (OSError, ValueError):
            pass
    finally:
        handler.close_connection = True
        server.classroom_stream_slots.release()
