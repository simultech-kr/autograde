"""Small, framework-free HTTP transport for the student platform API.

The transport deliberately owns only HTTP concerns.  Authentication,
authorization, persistence, Git access, and grading remain behind an injected
facade.  This keeps the HTTP server independently testable and prevents test or
development authentication shortcuts from becoming implicit network APIs.

Facade contract
---------------

The facade is duck typed and may implement these methods::

    create_device_authorization(payload, base_url)
    exchange_device_authorization(payload)
    redeem_assignment_claim(payload)
    refresh_tokens(payload)
    revoke_current(access_token)
    get_me(access_token)
    list_sessions(access_token)
    revoke_session(access_token, session_id)
    list_assignments(access_token)
    get_assignment_repository(access_token, assignment_id)
    submit(access_token, idempotency_key, payload)
    get_submission(access_token, submission_id)
    get_result(access_token, submission_id)
    get_bundle_starter(access_token, assignment_id)
    submit_bundle(access_token, idempotency_key, assignment_id, stream, length)

Optional browser endpoints use ``activate_page(query)``,
``github_start(query)``, ``github_callback(query)``, and
``approve_activation(form, cookies)``. Password-backed assignment claims use
``assignment_claim_page(query)`` and ``issue_assignment_claim(form, cookies)``.
A method may return an
:class:`HTTPResult`, ``(status, body)``, ``(status, body, headers)``, or a body
mapping.  It may raise :class:`PlatformHTTPError`.  Application exceptions from
another module can avoid importing this transport by exposing the safe
attributes ``status``, ``code``, ``safe_message``, and optional ``headers``.
All other exceptions become an uninformative 500 response.
"""

from __future__ import annotations

import json
import ipaddress
import os
import re
import socket
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

from .platform_events import emit_operator_event


DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_MAX_RUNTIME_REQUEST_BYTES = 512 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MAX_BUNDLE_REQUEST_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_FILE_RESPONSE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_QUERY_BYTES = 8 * 1024
DEFAULT_MAX_FORM_BYTES = 16 * 1024
DEFAULT_MAX_CONCURRENT_REQUESTS = 32
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0

_MAX_TARGET_BYTES = 4 * 1024
_MAX_IDENTIFIER_BYTES = 128
_MAX_AUTHORIZATION_BYTES = 4 * 1024
_MAX_COOKIE_BYTES = 8 * 1024
_MAX_HEADER_VALUE_BYTES = 8 * 1024
_MAX_QUERY_FIELDS = 32
_MAX_FORM_FIELDS = 32
_MAX_PARAMETER_BYTES = 2 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
_BEARER_TOKEN = re.compile(r"Bearer ([A-Za-z0-9._~-]{16,4096})\Z")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_PATH_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_SECURITY_HEADER_NAMES = {name.casefold() for name in _SECURITY_HEADERS}

_OVERLOAD_BODY = (
    b'{"error":{"code":"temporarily_unavailable",'
    b'"message":"The service is busy; try again later"}}'
)
_OVERLOAD_SEND_TIMEOUT_SECONDS = 0.25
_OVERLOAD_RESPONSE = (
    "HTTP/1.1 503 Service Unavailable\r\n"
    "Server: Autograde\r\n"
    + "".join(f"{name}: {value}\r\n" for name, value in _SECURITY_HEADERS.items())
    + "Connection: close\r\n"
    + f"Content-Length: {len(_OVERLOAD_BODY)}\r\n"
    + "Content-Type: application/json; charset=utf-8\r\n"
    + "Retry-After: 1\r\n"
    + "\r\n"
).encode("ascii") + _OVERLOAD_BODY

_FORBIDDEN_FACADE_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True, slots=True)
class HTTPResult:
    """An application result with an explicit HTTP projection."""

    status: int
    body: Any = None
    headers: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HTTPFileResult:
    """A trusted local regular file streamed without loading it into memory."""

    status: int
    path: Path
    content_type: str = "application/octet-stream"
    headers: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict)
    retained_checkpoint: bool = False
    public_ide_update: bool = False


class PlatformHTTPError(RuntimeError):
    """A facade or transport error that is safe to send to the client."""

    def __init__(
        self,
        status: int,
        code: str,
        safe_message: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.status = _safe_error_status(status)
        self.code = _safe_error_code(code)
        self.safe_message = _safe_error_message(safe_message)
        self.headers = dict(headers or {})
        super().__init__(self.safe_message)


class _DuplicateJSONKey(ValueError):
    pass


class PlatformHTTPServer(ThreadingHTTPServer):
    """Threading server with bounded active handlers and socket timeouts."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        facade: Any,
        *,
        public_base_url: Optional[str],
        external_access_mode: str,
        max_request_bytes: int,
        max_response_bytes: int,
        max_bundle_request_bytes: int,
        max_file_response_bytes: int,
        max_query_bytes: int,
        max_form_bytes: int,
        max_concurrent_requests: int,
        request_timeout_seconds: float,
        readiness_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        _positive_limit(max_request_bytes, "max_request_bytes")
        _positive_limit(max_response_bytes, "max_response_bytes")
        _positive_limit(max_bundle_request_bytes, "max_bundle_request_bytes")
        _positive_limit(max_file_response_bytes, "max_file_response_bytes")
        _positive_limit(max_query_bytes, "max_query_bytes")
        _positive_limit(max_form_bytes, "max_form_bytes")
        _positive_limit(max_concurrent_requests, "max_concurrent_requests")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")

        self.facade = facade
        self.readiness_check = readiness_check
        self.max_request_bytes = max_request_bytes
        # The runtime accepts 64 KiB UTF-8 stdin; JSON escaping can expand it
        # sixfold. Keep this protocol budget separate from ordinary API JSON.
        self.max_runtime_request_bytes = min(DEFAULT_MAX_RUNTIME_REQUEST_BYTES, max_bundle_request_bytes)
        self.max_response_bytes = max_response_bytes
        self.max_bundle_request_bytes = max_bundle_request_bytes
        self.max_file_response_bytes = max_file_response_bytes
        self.max_query_bytes = max_query_bytes
        self.max_form_bytes = max_form_bytes
        self.request_timeout_seconds = float(request_timeout_seconds)
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        # Keep large CMS object validation/streams from exhausting all API handlers.
        self.content_download_slots = threading.BoundedSemaphore(8)
        # Long-lived instructor readers must leave capacity for student uploads
        # and heartbeats. Deployments with fewer than four handlers fail closed.
        self.classroom_stream_slots = threading.BoundedSemaphore(min(8, max_concurrent_requests // 4))
        self._active_requests_lock = threading.Lock()
        self._active_requests: set[socket.socket] = set()
        self._closing = False
        if external_access_mode not in {"disabled", "insecure-http"}:
            raise ValueError(
                "external_access_mode must be disabled or insecure-http"
            )
        if external_access_mode == "insecure-http":
            _validate_insecure_http_binding(server_address, public_base_url)
        else:
            if not _is_loopback_host(server_address[0]):
                raise ValueError(
                    "plain HTTP in disabled mode may only listen on a loopback interface"
                )
        super().__init__(server_address, PlatformRequestHandler)
        self.external_access_mode = external_access_mode

        if public_base_url is None:
            host, port = self.server_address[:2]
            display_host = f"[{host}]" if ":" in host else host
            self.public_base_url = f"http://{display_host}:{port}"
        else:
            parsed = urlsplit(public_base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                self.server_close()
                raise ValueError("public_base_url must be an absolute HTTP(S) origin")
            self.public_base_url = public_base_url.rstrip("/")

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        try:
            request.settimeout(self.request_timeout_seconds)
            with self._active_requests_lock:
                if self._closing:
                    raise OSError("HTTP server is closing")
                self._active_requests.add(request)
        except BaseException:
            request.close()
            raise
        return request, client_address

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        # Never wait for a handler slot on the serve_forever thread.  A blocking
        # acquire here prevents BaseServer.shutdown() from observing its stop
        # flag when existing clients deliberately keep request reads alive.
        if not self._request_slots.acquire(blocking=False):
            self._reject_overloaded_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            try:
                self.shutdown_request(request)
            except BaseException as cleanup_error:
                # Preserve the thread-start failure as the causal exception;
                # cleanup failure is useful operationally but must not mask it.
                emit_operator_event(
                    "http_request_cleanup_failed",
                    component="http",
                    exception=cleanup_error,
                )
            raise

    def _reject_overloaded_request(self, request: socket.socket) -> None:
        try:
            request.settimeout(
                min(self.request_timeout_seconds, _OVERLOAD_SEND_TIMEOUT_SECONDS)
            )
            request.sendall(_OVERLOAD_RESPONSE)
        except (OSError, TimeoutError):
            pass
        finally:
            self.shutdown_request(request)

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def shutdown_request(self, request: socket.socket) -> None:
        try:
            super().shutdown_request(request)
        finally:
            with self._active_requests_lock:
                self._active_requests.discard(request)

    def server_close(self) -> None:
        # Closing accepted sockets interrupts slow request reads without waiting
        # for daemon handlers.  A handler already inside a synchronous facade
        # call keeps running: submission admission remains exact-SHA + durable,
        # and a missed worker notification is recovered from SQLite on restart.
        with self._active_requests_lock:
            self._closing = True
            active_requests = tuple(self._active_requests)
        super().server_close()
        for request in active_requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Replace the stdlib traceback logger with a secret-safe event."""

        _exception_type, exception, _traceback = sys.exc_info()
        emit_operator_event(
            "http_handler_unexpected_exception",
            component="http",
            exception=exception,
        )


class PlatformRequestHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 route adapter with deliberately quiet request handling."""

    protocol_version = "HTTP/1.1"
    server_version = "Autograde"
    sys_version = ""

    # BaseHTTPRequestHandler otherwise prints the request line and may expose
    # query parameters or implementation exception text to stderr.
    def log_message(self, _format: str, *args: Any) -> None:
        return

    def log_error(self, _format: str, *args: Any) -> None:
        return

    def handle_expect_100(self) -> bool:
        self._send_error_result(
            PlatformHTTPError(417, "expectation_failed", "Expect is not supported")
        )
        return False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def do_TRACE(self) -> None:  # noqa: N802
        self._dispatch("TRACE")

    def do_CONNECT(self) -> None:  # noqa: N802
        self._dispatch("CONNECT")

    @property
    def platform_server(self) -> PlatformHTTPServer:
        return self.server  # type: ignore[return-value]

    def _dispatch(self, method: str) -> None:
        import secrets
        self.operation_request_id = secrets.token_hex(16)
        self.close_connection = True
        content_slot = False
        try:
            self._validate_insecure_http_host()
            path, query = self._request_target()
            route, parameters, allowed = self._match_route(path)
            if route is None:
                raise PlatformHTTPError(404, "not_found", "Resource not found")
            if method not in allowed:
                raise PlatformHTTPError(
                    405,
                    "method_not_allowed",
                    "Method not allowed",
                    headers={"Allow": ", ".join(sorted(allowed))},
                )
            if route == "classroom_stream":
                from .classroom_stream import serve
                serve(self, parameters["path"], query)
                return
            if route in {"content_object", "ide_extension_package"}:
                content_slot = self.platform_server.content_download_slots.acquire(blocking=False)
                if not content_slot:
                    raise PlatformHTTPError(429, "content_transfer_busy", "자료 전송 중입니다. 잠시 후 다시 시도하세요.",
                                            headers={"Retry-After": "1"})
            result, default_status = self._invoke(
                route,
                parameters,
                query,
            )
            self._send_result(_normalize_result(result, default_status))
        except PlatformHTTPError as exc:
            self._send_error_result(exc)
        except Exception as exc:
            mapped = _duck_typed_application_error(exc)
            if mapped is None:
                emit_operator_event(
                    "http_request_unexpected_exception",
                    component="http",
                    exception=exc,
                    request_id=self.operation_request_id,
                )
                mapped = PlatformHTTPError(
                    500,
                    "internal_error",
                    "The service could not process the request",
                )
            self._send_error_result(mapped)
        finally:
            if content_slot:
                self.platform_server.content_download_slots.release()

    def _validate_insecure_http_host(self) -> None:
        if self.platform_server.external_access_mode != "insecure-http":
            return
        expected = urlsplit(self.platform_server.public_base_url).netloc.casefold()
        values = self.headers.get_all("Host") or []
        if len(values) != 1 or values[0].strip().casefold() != expected:
            raise PlatformHTTPError(
                400,
                "invalid_host",
                "Host must match the configured private network origin",
            )

    def _request_target(self) -> tuple[str, Mapping[str, str]]:
        try:
            target_size = len(self.path.encode("utf-8", "strict"))
        except UnicodeEncodeError as exc:
            raise PlatformHTTPError(400, "invalid_request", "Invalid request target") from exc
        if target_size > _MAX_TARGET_BYTES:
            raise PlatformHTTPError(414, "uri_too_long", "Request target is too long")

        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise PlatformHTTPError(400, "invalid_request", "Invalid request target")
        if (
            not parsed.path.startswith("/")
            or _BAD_PERCENT_ESCAPE.search(parsed.path)
            or _ENCODED_PATH_SEPARATOR.search(parsed.path)
        ):
            raise PlatformHTTPError(400, "invalid_request", "Invalid request path")
        try:
            decoded_path = unquote(parsed.path, encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PlatformHTTPError(400, "invalid_request", "Invalid request path") from exc
        if (
            "\\" in decoded_path
            or "\x00" in decoded_path
            or any(ord(character) < 32 for character in decoded_path)
        ):
            raise PlatformHTTPError(400, "invalid_request", "Invalid request path")

        query = self._parse_parameters(
            parsed.query,
            max_bytes=self.platform_server.max_query_bytes,
            max_fields=_MAX_QUERY_FIELDS,
            label="query",
        )
        return decoded_path, query

    def _match_route(
        self, path: str
    ) -> tuple[Optional[str], Mapping[str, str], frozenset[str]]:
        facade = self.platform_server.facade
        if path == '/v1/operations/seats/report' and callable(getattr(facade, 'report_seat_status', None)):
            return 'seat_report', {}, frozenset({'POST'})
        if callable(getattr(facade, "portal_request", None)) and path not in {"/healthz", "/readyz"}:
            if path in {"/", "/student", "/courses"} or path.startswith(("/courses/", "/student/")) or (
                getattr(facade, "instructor", None) is not None
                and (path == "/instructor" or path.startswith("/instructor/"))
            ):
                if path == '/instructor/api/v1' or path.startswith('/instructor/api/v1/'):
                    if re.fullmatch(r'/instructor/api/v1/courses/[a-z0-9_-]{1,96}/classroom/events', path):
                        return "classroom_stream", {"path": path}, frozenset({"GET"})
                    if re.fullmatch(r'/instructor/api/v1/courses/[a-z0-9_-]{1,96}/classroom/students/[^/]{1,128}/commands', path):
                        return "portal", {"path": path}, frozenset({"POST"})
                    return "portal", {"path": path}, frozenset({"GET"})
                return "portal", {"path": path}, frozenset({"GET", "POST"})
            return None, {}, frozenset()
        if (
            self.platform_server.external_access_mode == "insecure-http"
            and path in {"/instructor", "/v1/instructor/dashboard", "/v1/instructor/dashboard/live"}
        ):
            # Never solicit or accept the instructor Basic credential over the
            # explicitly plaintext trusted-LAN pilot channel.
            return None, {}, frozenset()
        exact: Mapping[str, tuple[str, frozenset[str], Optional[str]]] = {
            "/readyz": ("readiness", frozenset({"GET"}), None),
            "/healthz": (
                "health",
                frozenset({"GET"}),
                None,
            ),
            "/v1/device-authorizations": (
                "device_authorizations",
                frozenset({"POST"}),
                "create_device_authorization",
            ),
            "/v1/device-authorizations/token": (
                "device_token",
                frozenset({"POST"}),
                "exchange_device_authorization",
            ),
            "/v1/assignment-claims/redeem": (
                "redeem_assignment_claim",
                frozenset({"POST"}),
                "redeem_assignment_claim",
            ),
            "/v1/tokens/refresh": (
                "refresh_tokens",
                frozenset({"POST"}),
                "refresh_tokens",
            ),
            "/v1/sessions/current": (
                "revoke_current",
                frozenset({"DELETE"}),
                "revoke_current",
            ),
            "/v1/me": ("me", frozenset({"GET"}), "get_me"),
            "/v1/classroom/events": ("classroom_event", frozenset({"POST"}), "report_classroom_event"),
            "/v1/classroom/commands/poll": ("classroom_command_poll", frozenset({"POST"}), "poll_classroom_commands"),
            "/v1/materials": ("materials", frozenset({"GET"}), "list_materials"),
            "/v1/content/context": ("content_context", frozenset({"GET"}), "get_content_context"),
            "/v1/content/collections": ("content_collections", frozenset({"GET"}), "list_content_collections"),
            "/v1/ide/extensions": ("ide_extensions", frozenset({"GET"}), "list_ide_extensions"),
            "/v1/me/sessions": (
                "sessions",
                frozenset({"GET"}),
                "list_sessions",
            ),
            "/v1/assignments": (
                "assignments",
                frozenset({"GET"}),
                "list_assignments",
            ),
            "/v1/accepted-assignments": (
                "accepted_assignments", frozenset({"GET"}), "list_accepted_assignments",
            ),
            "/v1/accepted-assignments/catalog": (
                "accepted_assignment_catalog", frozenset({"GET"}), "list_accepted_assignment_catalog",
            ),
            "/v1/submissions": (
                "submit",
                frozenset({"POST"}),
                "submit",
            ),
            "/instructor": (
                "instructor_page",
                frozenset({"GET"}),
                "instructor_dashboard_page",
            ),
            "/v1/instructor/dashboard": (
                "instructor_dashboard",
                frozenset({"GET"}),
                "instructor_dashboard",
            ),
            "/v1/instructor/dashboard/live": (
                "instructor_dashboard_updates", frozenset({"GET"}), "instructor_dashboard_updates",
            ),
            "/activate": (
                "activate_page",
                frozenset({"GET"}),
                "activate_page",
            ),
            "/activate/approve": (
                "approve_activation",
                frozenset({"POST"}),
                "approve_activation",
            ),
            "/assignment-claim": (
                "assignment_claim_page",
                frozenset({"GET"}),
                "assignment_claim_page",
            ),
            "/assignment-claim/issue": (
                "issue_assignment_claim",
                frozenset({"POST"}),
                "issue_assignment_claim",
            ),
            "/auth/github/start": (
                "github_start",
                frozenset({"GET"}),
                "github_start",
            ),
            "/oauth/github/callback": (
                "github_callback",
                frozenset({"GET"}),
                "github_callback",
            ),
        }
        matched = exact.get(path)
        if matched is not None:
            route, allowed, optional_method = matched
            if optional_method in {
                "activate_page",
                "approve_activation",
                "assignment_claim_page",
                "issue_assignment_claim",
                "github_start",
                "github_callback",
                "instructor_dashboard_page",
                "instructor_dashboard",
                "list_materials",
                "report_classroom_event",
                "poll_classroom_commands",
                "get_content_context",
                "list_content_collections",
                "list_ide_extensions",
            } and not callable(getattr(facade, optional_method, None)):
                return None, {}, frozenset()
            return route, {}, allowed

        parts = path.split("/")
        if callable(getattr(facade, 'get_ide_update', None)):
            update = re.fullmatch(
                r'/v1/ide-updates/(?P<channel>pilot|production)/(?:'
                r'(?P<platform>darwin|linux|win32)/(?P<architecture>arm64|x64)/)?'
                r'(?P<name>feed\.json|[0-9a-f]{64}\.agupdate\.gz)', path)
            if update:
                return 'ide_update', update.groupdict(), frozenset({'GET'})
        if (len(parts) == 6 and parts[:4] == ["", "v1", "ide", "extensions"]
                and re.fullmatch(r'er_[0-9a-f]{32}', parts[4])):
            if parts[5] == 'package' and callable(getattr(facade, 'get_ide_extension_package', None)):
                return 'ide_extension_package', {'release_id': parts[4]}, frozenset({'GET'})
            if parts[5] == 'status' and callable(getattr(facade, 'report_ide_extension_status', None)):
                return 'ide_extension_status', {'release_id': parts[4]}, frozenset({'POST'})
        if (len(parts) == 6 and parts[:4] == ["", "v1", "classroom", "commands"]
                and parts[5] == 'responses' and self._identifier(parts[4]) is not None
                and callable(getattr(facade, 'respond_classroom_command', None))):
            return 'classroom_command_response', {'command_id': parts[4]}, frozenset({'POST'})
        if (len(parts) in {6, 7} and parts[:4] == ["", "v1", "content", "releases"]
                and re.fullmatch(r"cr_[0-9a-f]{32}", parts[4])):
            if len(parts) == 6 and parts[5] in {"manifest", "verify"}:
                method = "get_content_manifest" if parts[5] == "manifest" else "verify_content_manifest"
                if callable(getattr(facade, method, None)):
                    return "content_" + parts[5], {"release_id": parts[4]}, frozenset({"GET" if parts[5] == "manifest" else "POST"})
            if (len(parts) == 7 and parts[5] == "objects" and re.fullmatch(r"[0-9a-f]{64}", parts[6])
                    and callable(getattr(facade, "get_content_object", None))):
                return "content_object", {"release_id": parts[4], "sha256": parts[6]}, frozenset({"GET"})
        if (len(parts) == 5 and parts[:3] == ["", "v1", "materials"]
                and parts[4] == "download" and self._identifier(parts[3]) is not None
                and callable(getattr(facade, "get_material_download", None))):
            return "material_download", {"material_id": parts[3]}, frozenset({"GET"})
        if (
            len(parts) == 3
            and parts[:2] == ["", "assignment-claim"]
            and callable(getattr(facade, "assignment_claim_page", None))
        ):
            identifier = self._identifier(parts[2])
            if identifier is not None:
                return (
                    "assignment_claim_page_path",
                    {"assignment_id": identifier},
                    frozenset({"GET"}),
                )
        if len(parts) == 5 and parts[:3] == ["", "v1", "assignments"]:
            identifier = self._identifier(parts[3])
            if identifier is not None and parts[4] == "learning-runtime" and callable(getattr(facade, "get_learning_runtime", None)):
                return ("learning_runtime_status", {"assignment_id": identifier}, frozenset({"GET"}))
            if identifier is not None and parts[4] == "learning-checkpoints" and callable(getattr(facade, "save_learning_checkpoint", None)) and callable(getattr(facade, "list_learning_checkpoints", None)):
                return ("learning_checkpoints", {"assignment_id": identifier}, frozenset({"GET", "POST"}))
            if identifier is not None and parts[4] == "document" and callable(getattr(facade, "get_assignment_document", None)):
                return ("assignment_document", {"assignment_id": identifier}, frozenset({"GET"}))
            if identifier is not None and parts[4] == "download-diagnostics" and callable(getattr(facade, "report_download_diagnostic", None)):
                return ("download_diagnostic", {"assignment_id": identifier}, frozenset({"POST"}))
            if identifier is not None and parts[4] == "history" and callable(getattr(facade, "get_bundle_history", None)):
                return ("bundle_history", {"assignment_id": identifier}, frozenset({"GET"}))
            if identifier is not None and parts[4] == "repository":
                return (
                    "assignment_repository",
                    {"assignment_id": identifier},
                    frozenset({"GET"}),
                )
            if (
                identifier is not None
                and parts[4] == "starter"
                and callable(getattr(facade, "get_bundle_starter", None))
            ):
                return (
                    "bundle_starter",
                    {"assignment_id": identifier},
                    frozenset({"GET"}),
                )
            if (
                identifier is not None
                and parts[4] == "submissions"
                and callable(getattr(facade, "submit_bundle", None))
            ):
                return (
                    "bundle_submit",
                    {"assignment_id": identifier},
                    frozenset({"POST"}),
                )
        if len(parts) == 7 and parts[:3] == ["", "v1", "assignments"] and parts[4] == "learning-checkpoints" and parts[6] == "bundle":
            assignment_id, checkpoint_id = self._identifier(parts[3]), self._identifier(parts[5])
            if assignment_id is not None and checkpoint_id is not None and callable(getattr(facade, "get_learning_checkpoint_bundle", None)):
                return ("learning_checkpoint_bundle", {"assignment_id": assignment_id, "checkpoint_id": checkpoint_id}, frozenset({"GET"}))
        if len(parts) == 6 and parts[:3] == ["", "v1", "assignments"] and parts[4:] == ["learning-runtime", "sessions"]:
            assignment_id = self._identifier(parts[3])
            if assignment_id is not None and callable(getattr(facade, "create_learning_runtime", None)):
                return ("learning_runtime_create", {"assignment_id": assignment_id}, frozenset({"POST"}))
        if len(parts) == 8 and parts[:3] == ["", "v1", "assignments"] and parts[4:6] == ["learning-runtime", "sessions"] and parts[7] in {"run", "close"}:
            assignment_id, session_id = self._identifier(parts[3]), self._identifier(parts[6])
            if assignment_id is not None and session_id is not None and callable(getattr(facade, parts[7] + "_learning_runtime", None)):
                return ("learning_runtime_" + parts[7], {"assignment_id": assignment_id, "session_id": session_id}, frozenset({"POST"}))
        if len(parts) == 5 and parts[:4] == ["", "v1", "me", "sessions"]:
            identifier = self._identifier(parts[4])
            if identifier is not None:
                return (
                    "session",
                    {"session_id": identifier},
                    frozenset({"DELETE"}),
                )
        if len(parts) == 5 and parts[:3] == ["", "v1", "submissions"] and parts[4] == "source":
            identifier = self._identifier(parts[3])
            if identifier is not None and callable(getattr(facade, "get_bundle_source", None)):
                return ("bundle_source", {"submission_id": identifier}, frozenset({"GET"}))
        if len(parts) == 4 and parts[:3] == ["", "v1", "submissions"]:
            identifier = self._identifier(parts[3])
            if identifier is not None:
                return (
                    "submission",
                    {"submission_id": identifier},
                    frozenset({"GET"}),
                )
        if len(parts) == 5 and parts[:3] == ["", "v1", "submissions"]:
            identifier = self._identifier(parts[3])
            if identifier is not None and parts[4] == "result":
                return (
                    "result",
                    {"submission_id": identifier},
                    frozenset({"GET"}),
                )
        return None, {}, frozenset()

    @staticmethod
    def _identifier(value: str) -> Optional[str]:
        try:
            size = len(value.encode("ascii", "strict"))
        except UnicodeEncodeError:
            return None
        if size == 0 or size > _MAX_IDENTIFIER_BYTES or not _IDENTIFIER.fullmatch(value):
            return None
        return value

    def _invoke(
        self,
        route: str,
        parameters: Mapping[str, str],
        query: Mapping[str, str],
    ) -> tuple[Any, int]:
        facade = self.platform_server.facade
        participation_fields = {'assignment_id', 'result_view'}
        portal_path = parameters.get('path', '') if route == 'portal' else ''
        participation_page = route == 'instructor_page' or bool(
            re.fullmatch(r'/courses/[A-Za-z0-9][A-Za-z0-9._~-]{0,127}/instructor/submissions', portal_path)
            or (getattr(facade, 'instructor', None) is None and
                re.fullmatch(r'/courses/[A-Za-z0-9][A-Za-z0-9._~-]{0,127}/instructor', portal_path)))
        if participation_page and query:
            if self.command != 'GET' or not set(query) <= participation_fields:
                raise PlatformHTTPError(400, "invalid_request", "Unexpected query parameter")
            if 'assignment_id' in query and self._identifier(query['assignment_id']) is None:
                raise PlatformHTTPError(400, "invalid_request", "Invalid assignment selection")
            if 'result_view' in query and query['result_view'] not in {'all', 'submitted', 'unsubmitted', 'pending', 'attention'}:
                raise PlatformHTTPError(400, "invalid_request", "Invalid result view")
        elif route in {'instructor_dashboard', 'instructor_dashboard_updates'} and query:
            raise PlatformHTTPError(400, "invalid_request", "Unexpected query parameter")
        if route == "portal":
            path = parameters['path']
            next_api = path == '/instructor/api/v1' or path.startswith('/instructor/api/v1/')
            query_fields = ({'screen', 'course_key', 'assignment_id', 'student', 'status',
                             'sort', 'page', 'page_size', 'version'} if next_api else
                            participation_fields if participation_page else
                            {'course_key'} if path == '/instructor/switch' else
                            {'q', 'status'} if path == '/instructor' else
                            {'q', 'status', 'page'} if re.fullmatch(r'/courses/[a-z0-9_-]{1,96}/instructor/students', path) else
                            {'student_key'} if re.fullmatch(r'/courses/[a-z0-9_-]{1,96}/instructor/registrations', path) else
                            {'assignment_id'} if re.fullmatch(r'/courses/[a-z0-9_-]{1,96}/instructor/classroom', path) else
                            {'q', 'visibility', 'page'} if re.fullmatch(r'/courses/[a-z0-9_-]{1,96}/instructor/assignments', path) else set())
            if query and (self.command != 'GET' or
                          (getattr(facade, 'instructor', None) is None and not participation_page) or
                          not set(query) <= query_fields):
                raise PlatformHTTPError(400, "invalid_request", "Unexpected query parameter")
            if self.command == "POST":
                path = parameters["path"]
                admin = getattr(facade, "instructor", None)
                is_admin = admin is not None and (
                    path == "/instructor" or path.startswith("/instructor/")
                    or re.fullmatch(r"/courses/[^/]+/instructor(?:/.*)?", path)
                )
                if is_admin:
                    admin.authorize_upload(path, self._cookies(), self.headers.get("Origin"),
                                           self.headers.get("Authorization"))
                    upload = re.fullmatch(r"/courses/[^/]+/instructor/drafts/[^/]+/(?:uploads/(starter|solution|negative)|grading-template)", path)
                    csv_upload = re.fullmatch(r"/courses/[^/]+/instructor/students/import/preview", path)
                    membership_roster_upload = re.fullmatch(r"/courses/[a-z0-9_-]{1,96}/instructor/registrations/roster", path)
                    exam_upload = re.fullmatch(r"/courses/[a-z0-9_-]{1,96}/instructor/exam-papers/ep_[A-Za-z0-9_-]+/upload", path)
                    material_upload = re.fullmatch(r"/courses/[a-z0-9_-]{1,96}/instructor/materials/mat_[A-Za-z0-9_-]+/(upload|upload-batch)", path)
                    content_upload = re.fullmatch(r"/courses/[a-z0-9_-]{1,96}/instructor/content/cc_[0-9a-f]{32}/upload", path)
                    extension_upload = re.fullmatch(r"/courses/[a-z0-9_-]{1,96}/instructor/extensions", path)
                    classroom_command = re.fullmatch(r'/instructor/api/v1/courses/[a-z0-9_-]{1,96}/classroom/students/[^/]{1,128}/commands', path)
                    if classroom_command:
                        # Narrow JSON mutation route; all other instructor API
                        # routes remain GET-only. CSRF checked by the facade.
                        form = self._read_json_object(max_bytes=8192)
                    elif upload or csv_upload or membership_roster_upload or exam_upload or material_upload or content_upload or extension_upload:
                        if not facade.admin_upload_slots.acquire(blocking=False):
                            raise PlatformHTTPError(503, "upload_busy", "파일 처리 중입니다. 잠시 후 다시 시도하세요.")
                        try:
                            from .instructor_upload import InstructorUploadUnavailable, parse_instructor_upload
                            from .exam_papers import MAX_PDF_BYTES
                            limit = MAX_PDF_BYTES if exam_upload else (5 if upload else 1) * 1024 * 1024
                            if material_upload:
                                from .student_materials import MAX_MATERIAL_BYTES, MAX_BATCH_BYTES
                                limit = MAX_BATCH_BYTES if material_upload[1] == 'upload-batch' else MAX_MATERIAL_BYTES
                            if content_upload:
                                limit = 25 * 1024 * 1024
                            if extension_upload:
                                from .extension_distribution import MAX_VSIX_BYTES
                                limit = MAX_VSIX_BYTES
                            body = self._read_body(expected_media_type="multipart/form-data", max_bytes=limit + 64 * 1024)
                            try:
                                form = parse_instructor_upload(self.headers.get("Content-Type", ""), body, file_limit=limit,
                                                               capture_filename=bool(material_upload or content_upload or extension_upload),
                                                               allow_content_path=bool(content_upload),
                                                               allow_roster_mode=bool(membership_roster_upload))
                            except InstructorUploadUnavailable as exc:
                                emit_operator_event("instructor_upload_unavailable", component="http", exception=exc.__cause__ or exc)
                                raise PlatformHTTPError(503, "upload_unavailable", str(exc)) from exc
                            except ValueError as exc:
                                raise PlatformHTTPError(400, "invalid_upload", str(exc)) from exc
                            if material_upload or content_upload or extension_upload:
                                # Keep admission bounded through ZIP validation and storage,
                                # not just parsing: each batch may retain up to 100 MiB.
                                return facade.portal_request(
                                    self.command, path, form, self._cookies(),
                                    self.headers.get("Origin"), self.headers.get("Authorization"),
                                ), 200
                        finally:
                            facade.admin_upload_slots.release()
                    else:
                        document_edit = re.fullmatch(r"/courses/[^/]+/instructor/assignments/[^/]+/document", path)
                        form = self._read_form(max_bytes=(256 if document_edit else 64) * 1024, max_fields=400, multiline=True)
                else:
                    form = self._read_form()
            else:
                self._ensure_no_body()
                form = dict(query)
            return facade.portal_request(
                self.command, parameters["path"], form, self._cookies(),
                self.headers.get("Origin"), self.headers.get("Authorization"),
            ), 200
        if route == "health":
            self._ensure_no_body()
            return {"status": "ok"}, 200
        if route == 'seat_report':
            if query:
                raise PlatformHTTPError(400, 'invalid_query', 'Unexpected query parameter')
            return facade.report_seat_status(self._bearer_token(), self._read_json_object(max_bytes=4096)), 200
        if route == "readiness":
            self._ensure_no_body()
            check = self.platform_server.readiness_check
            try:
                ready = check is not None and check() is True
            except Exception:
                # Never expose database paths, runtime errors or credentials.
                ready = False
            return {"status": "ready" if ready else "not_ready"}, 200 if ready else 503
        if route == "device_authorizations":
            payload = self._read_json_object()
            return (
                facade.create_device_authorization(
                    payload, self.platform_server.public_base_url
                ),
                201,
            )
        if route == "device_token":
            return facade.exchange_device_authorization(self._read_json_object()), 200
        if route == "redeem_assignment_claim":
            return facade.redeem_assignment_claim(self._read_json_object()), 200
        if route == "refresh_tokens":
            return facade.refresh_tokens(self._read_json_object()), 200
        if route == "classroom_event":
            if query:
                raise PlatformHTTPError(400, "invalid_query", "Unexpected query parameter")
            return facade.report_classroom_event(self._bearer_token(), self._read_json_object()), 200
        if route in {'classroom_command_poll', 'classroom_command_response'}:
            if query:
                raise PlatformHTTPError(400, 'invalid_query', 'Unexpected query parameter')
            token, payload = self._bearer_token(), self._read_json_object(max_bytes=4096)
            if route == 'classroom_command_poll':
                return facade.poll_classroom_commands(token, payload), 200
            return facade.respond_classroom_command(token, parameters['command_id'], payload), 200
        if route == "revoke_current":
            self._ensure_no_body()
            return facade.revoke_current(self._bearer_token()), 204
        if route == "me":
            self._ensure_no_body()
            return facade.get_me(self._bearer_token()), 200
        if route in {'ide_extensions', 'ide_extension_package', 'ide_extension_status'}:
            if query:
                raise PlatformHTTPError(400, 'invalid_request', 'Unexpected query parameter')
            token = self._bearer_token()
            if route == 'ide_extension_status':
                return facade.report_ide_extension_status(token, parameters['release_id'], self._read_json_object(max_bytes=4096)), 200
            self._ensure_no_body()
            if route == 'ide_extensions':
                return facade.list_ide_extensions(token), 200
            return facade.get_ide_extension_package(token, parameters['release_id']), 200
        if route == 'ide_update':
            if query:
                raise PlatformHTTPError(400, 'invalid_request', 'Unexpected query parameter')
            self._ensure_no_body()
            return facade.get_ide_update(**parameters), 200
        if route in {"content_context", "content_collections", "content_manifest", "content_object", "content_verify"}:
            if query:
                raise PlatformHTTPError(400, "invalid_request", "Unexpected query parameter")
            token = self._bearer_token()
            if route == "content_verify":
                return facade.verify_content_manifest(token, parameters["release_id"], self._read_json_object(max_bytes=4096)), 200
            self._ensure_no_body()
            if route == "content_context":
                return facade.get_content_context(token), 200
            if route == "content_collections":
                return facade.list_content_collections(token), 200
            if route == "content_manifest":
                return facade.get_content_manifest(token, parameters["release_id"]), 200
            pins = self.headers.get_all("X-Autograde-Manifest-SHA256") or []
            if len(pins) != 1 or not re.fullmatch(r"[0-9a-f]{64}", pins[0]):
                raise PlatformHTTPError(400, "invalid_content_manifest", "One valid manifest SHA256 header is required")
            return facade.get_content_object(token, parameters["release_id"], parameters["sha256"], pins[0]), 200
        if route in {"materials", "material_download"}:
            self._ensure_no_body()
            if set(query) - ({"cursor"} if route == "materials" else set()):
                raise PlatformHTTPError(400, "invalid_request", "Unexpected query parameter")
            token = self._bearer_token()
            if route == "materials":
                return facade.list_materials(token, cursor=query.get("cursor")), 200
            pins = self.headers.get_all("X-Autograde-Expected-SHA256") or []
            if len(pins) != 1 or not re.fullmatch(r"[0-9a-f]{64}", pins[0]):
                raise PlatformHTTPError(400, "invalid_material_digest", "One valid expected SHA256 header is required")
            return facade.get_material_download(token, parameters["material_id"], pins[0]), 200
        if route == "sessions":
            self._ensure_no_body()
            return facade.list_sessions(self._bearer_token()), 200
        if route == "session":
            self._ensure_no_body()
            return (
                facade.revoke_session(
                    self._bearer_token(), parameters["session_id"]
                ),
                204,
            )
        if route == "assignments":
            self._ensure_no_body()
            return facade.list_assignments(self._bearer_token()), 200
        if route == "accepted_assignments":
            self._ensure_no_body()
            return facade.list_accepted_assignments(self._bearer_token()), 200
        if route == "accepted_assignment_catalog":
            self._ensure_no_body()
            return facade.list_accepted_assignment_catalog(self._bearer_token()), 200
        if route == "assignment_repository":
            self._ensure_no_body()
            return (
                facade.get_assignment_repository(
                    self._bearer_token(), parameters["assignment_id"]
                ),
                200,
            )
        if route == "download_diagnostic":
            return facade.report_download_diagnostic(self._bearer_token(), parameters["assignment_id"], self._read_json_object(max_bytes=4096)), 200
        if route == "assignment_document":
            self._ensure_no_body()
            return facade.get_assignment_document(self._bearer_token(), parameters["assignment_id"]), 200
        if route == "bundle_starter":
            self._ensure_no_body()
            return (
                facade.get_bundle_starter(
                    self._bearer_token(), parameters["assignment_id"]
                ),
                200,
            )
        if route == "bundle_history":
            self._ensure_no_body()
            return facade.get_bundle_history(self._bearer_token(), parameters["assignment_id"]), 200
        if route == "learning_checkpoints":
            token = self._bearer_token()
            if self.command == "GET":
                self._ensure_no_body()
                return facade.list_learning_checkpoints(token, parameters["assignment_id"]), 200
            return self._invoke_bundle_submission(facade, token, self._idempotency_key(), parameters["assignment_id"], checkpoint=True), 200
        if route == "learning_runtime_status":
            if query:
                raise PlatformHTTPError(400, "invalid_request", "Unexpected query parameter")
            self._ensure_no_body()
            return facade.get_learning_runtime(self._bearer_token(), parameters["assignment_id"]), 200
        if route in {"learning_runtime_create", "learning_runtime_run", "learning_runtime_close"}:
            if query:
                raise PlatformHTTPError(400, "invalid_request", "Unexpected query parameter")
            token, payload = self._bearer_token(), self._read_json_object(runtime=route == "learning_runtime_run")
            if route == "learning_runtime_create":
                return facade.create_learning_runtime(token, parameters["assignment_id"], payload), 200
            operation = facade.run_learning_runtime if route == "learning_runtime_run" else facade.close_learning_runtime
            return operation(token, parameters["assignment_id"], parameters["session_id"], payload), 200
        if route == "learning_checkpoint_bundle":
            self._ensure_no_body()
            return facade.get_learning_checkpoint_bundle(self._bearer_token(), parameters["assignment_id"], parameters["checkpoint_id"]), 200
        if route == "bundle_source":
            self._ensure_no_body()
            return facade.get_bundle_source(self._bearer_token(), parameters["submission_id"]), 200
        if route == "bundle_submit":
            token = self._bearer_token()
            idempotency_key = self._idempotency_key()
            return (
                self._invoke_bundle_submission(
                    facade,
                    token,
                    idempotency_key,
                    parameters["assignment_id"],
                ),
                202,
            )
        if route == "submit":
            token = self._bearer_token()
            idempotency_key = self._idempotency_key()
            return (
                facade.submit(token, idempotency_key, self._read_json_object()),
                202,
            )
        if route == "submission":
            self._ensure_no_body()
            return (
                facade.get_submission(
                    self._bearer_token(), parameters["submission_id"]
                ),
                200,
            )
        if route == "result":
            self._ensure_no_body()
            return (
                facade.get_result(
                    self._bearer_token(), parameters["submission_id"]
                ),
                200,
            )
        if route == "instructor_page":
            self._ensure_no_body()
            return (
                facade.instructor_dashboard_page(self._authorization_header()),
                200,
            )
        if route == "instructor_dashboard":
            self._ensure_no_body()
            return (
                facade.instructor_dashboard(self._authorization_header()),
                200,
            )
        if route == "instructor_dashboard_updates":
            self._ensure_no_body()
            return facade.instructor_dashboard_updates(self._authorization_header(), portal=False), 200
        if route == "activate_page":
            self._ensure_no_body()
            return facade.activate_page(query), 200
        if route == "github_start":
            self._ensure_no_body()
            return facade.github_start(query), 200
        if route == "github_callback":
            self._ensure_no_body()
            return facade.github_callback(query), 200
        if route == "approve_activation":
            form = self._read_form()
            cookies = self._cookies()
            return facade.approve_activation(form, cookies), 200
        if route == "assignment_claim_page":
            self._ensure_no_body()
            return facade.assignment_claim_page(query), 200
        if route == "assignment_claim_page_path":
            self._ensure_no_body()
            if query:
                raise PlatformHTTPError(
                    400, "invalid_request", "Unexpected query parameter"
                )
            return facade.assignment_claim_page(parameters), 200
        if route == "issue_assignment_claim":
            form = self._read_form()
            cookies = self._cookies()
            return facade.issue_assignment_claim(form, cookies), 200
        raise PlatformHTTPError(404, "not_found", "Resource not found")

    def _invoke_bundle_submission(
        self,
        facade: Any,
        access_token: str,
        idempotency_key: str,
        assignment_id: str,
        *,
        checkpoint: bool = False,
    ) -> Any:
        """Spool one bounded gzip request and keep its lifetime inside the call."""

        self._reject_transfer_encoding()
        media_type, charset = self._content_type()
        if media_type not in {"application/gzip", "application/x-gzip"} or charset is not None:
            raise PlatformHTTPError(
                415,
                "unsupported_media_type",
                "Content-Type must be application/gzip",
            )
        length = self._content_length(required=True)
        assert length is not None
        if length <= 0:
            raise PlatformHTTPError(400, "invalid_request", "Submission bundle is empty")
        if length > self.platform_server.max_bundle_request_bytes:
            raise PlatformHTTPError(413, "payload_too_large", "Submission bundle is too large")

        with tempfile.TemporaryFile(mode="w+b") as upload:
            remaining = length
            while remaining:
                try:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                except (OSError, TimeoutError) as exc:
                    raise PlatformHTTPError(
                        400, "invalid_request", "Could not read submission bundle"
                    ) from exc
                if not chunk:
                    raise PlatformHTTPError(
                        400, "invalid_request", "Incomplete submission bundle"
                    )
                upload.write(chunk)
                remaining -= len(chunk)
            upload.flush()
            upload.seek(0)
            operation = facade.save_learning_checkpoint if checkpoint else facade.submit_bundle
            return operation(
                access_token,
                idempotency_key,
                assignment_id,
                upload,
                length,
            )

    def _read_json_object(self, max_bytes=None, *, runtime=False) -> Mapping[str, Any]:
        limit = self.platform_server.max_runtime_request_bytes if runtime else self.platform_server.max_request_bytes
        body = self._read_body(
            expected_media_type="application/json",
            max_bytes=min(limit, max_bytes) if max_bytes is not None else limit,
        )

        def unique_object(pairs: Sequence[tuple[str, Any]]) -> MutableMapping[str, Any]:
            result: MutableMapping[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise _DuplicateJSONKey(key)
                result[key] = value
            return result

        try:
            decoded = json.loads(
                body.decode("utf-8", "strict"),
                object_pairs_hook=unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise PlatformHTTPError(
                400, "invalid_json", "Request body must be a valid JSON object"
            ) from exc
        if not isinstance(decoded, dict):
            raise PlatformHTTPError(
                400, "invalid_json", "Request body must be a JSON object"
            )
        return decoded

    def _read_form(self, *, max_bytes=None, max_fields=_MAX_FORM_FIELDS, multiline=False) -> Mapping[str, str]:
        limit = self.platform_server.max_form_bytes if max_bytes is None else max_bytes
        body = self._read_body(
            expected_media_type="application/x-www-form-urlencoded",
            max_bytes=limit,
        )
        try:
            text = body.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise PlatformHTTPError(400, "invalid_form", "Invalid form body") from exc
        return self._parse_parameters(
            text,
            max_bytes=limit,
            max_fields=max_fields,
            label="form",
            multiline=multiline,
            max_value_bytes=limit if multiline else _MAX_PARAMETER_BYTES,
        )

    def _read_body(self, *, expected_media_type: str, max_bytes: int) -> bytes:
        self._reject_transfer_encoding()
        media_type, charset = self._content_type()
        if media_type != expected_media_type or charset not in {None, "utf-8", "utf8"}:
            raise PlatformHTTPError(
                415,
                "unsupported_media_type",
                f"Content-Type must be {expected_media_type}",
            )
        length = self._content_length(required=True)
        assert length is not None
        if length > max_bytes:
            raise PlatformHTTPError(413, "payload_too_large", "Request body is too large")
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError) as exc:
            raise PlatformHTTPError(400, "invalid_request", "Could not read request body") from exc
        if len(body) != length:
            raise PlatformHTTPError(400, "invalid_request", "Incomplete request body")
        return body

    def _ensure_no_body(self) -> None:
        self._reject_transfer_encoding()
        length = self._content_length(required=False)
        if length not in {None, 0}:
            raise PlatformHTTPError(400, "invalid_request", "Request body is not allowed")

    def _reject_transfer_encoding(self) -> None:
        if self.headers.get_all("Transfer-Encoding"):
            raise PlatformHTTPError(
                400, "invalid_request", "Transfer-Encoding is not supported"
            )

    def _content_length(self, *, required: bool) -> Optional[int]:
        values = self.headers.get_all("Content-Length") or []
        if len(values) > 1:
            raise PlatformHTTPError(400, "invalid_request", "Invalid Content-Length")
        if not values:
            if required:
                raise PlatformHTTPError(411, "length_required", "Content-Length is required")
            return None
        value = values[0]
        if (
            not value
            or len(value) > 20
            or not value.isascii()
            or not value.isdecimal()
        ):
            raise PlatformHTTPError(400, "invalid_request", "Invalid Content-Length")
        length = int(value, 10)
        if length < 0:
            raise PlatformHTTPError(400, "invalid_request", "Invalid Content-Length")
        return length

    def _content_type(self) -> tuple[str, Optional[str]]:
        values = self.headers.get_all("Content-Type") or []
        if len(values) != 1:
            return "", None
        if re.fullmatch(r'multipart/form-data\s*;\s*boundary=(?:"[A-Za-z0-9\x27()+_,./:=? -]{1,200}"|[A-Za-z0-9\x27()+_,./:=?-]{1,200})',
                        values[0], re.IGNORECASE):
            return "multipart/form-data", None
        parts = [part.strip() for part in values[0].split(";")]
        media_type = parts[0].casefold()
        charset: Optional[str] = None
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if not separator or name.strip().casefold() != "charset" or charset is not None:
                return "", None
            charset = value.strip().strip('"').casefold()
        return media_type, charset

    def _bearer_token(self) -> str:
        values = self.headers.get_all("Authorization") or []
        if len(values) != 1:
            raise PlatformHTTPError(
                401,
                "invalid_token",
                "A valid bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        value = values[0]
        if len(value.encode("utf-8", "ignore")) > _MAX_AUTHORIZATION_BYTES:
            match = None
        else:
            match = _BEARER_TOKEN.fullmatch(value)
        if match is None:
            raise PlatformHTTPError(
                401,
                "invalid_token",
                "A valid bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return match.group(1)

    def _authorization_header(self) -> str:
        values = self.headers.get_all("Authorization") or []
        if not values:
            return ""
        if (
            len(values) != 1
            or len(values[0].encode("utf-8", "ignore")) > _MAX_AUTHORIZATION_BYTES
        ):
            raise PlatformHTTPError(
                401,
                "instructor_auth_required",
                "Instructor credentials are required",
                headers={"WWW-Authenticate": 'Basic realm="Autograde instructor"'},
            )
        return values[0]

    def _idempotency_key(self) -> str:
        values = self.headers.get_all("Idempotency-Key") or []
        if len(values) != 1 or not _IDEMPOTENCY_KEY.fullmatch(values[0]):
            raise PlatformHTTPError(
                400,
                "invalid_idempotency_key",
                "A valid Idempotency-Key is required",
            )
        return values[0]

    def _cookies(self) -> Mapping[str, str]:
        values = self.headers.get_all("Cookie") or []
        if not values:
            return {}
        if len(values) != 1 or len(values[0].encode("utf-8", "ignore")) > _MAX_COOKIE_BYTES:
            raise PlatformHTTPError(400, "invalid_request", "Invalid Cookie header")
        parsed = SimpleCookie()
        try:
            parsed.load(values[0])
        except CookieError as exc:
            raise PlatformHTTPError(400, "invalid_request", "Invalid Cookie header") from exc
        return {name: morsel.value for name, morsel in parsed.items()}

    @staticmethod
    def _parse_parameters(
        encoded: str,
        *,
        max_bytes: int,
        max_fields: int,
        label: str,
        multiline: bool = False,
        max_value_bytes: int = _MAX_PARAMETER_BYTES,
    ) -> Mapping[str, str]:
        try:
            encoded_size = len(encoded.encode("ascii", "strict"))
        except UnicodeEncodeError as exc:
            raise PlatformHTTPError(400, "invalid_request", f"Invalid {label}") from exc
        if encoded_size > max_bytes:
            raise PlatformHTTPError(413, "payload_too_large", f"{label.title()} is too large")
        if _BAD_PERCENT_ESCAPE.search(encoded):
            raise PlatformHTTPError(400, "invalid_request", f"Invalid {label}")
        try:
            pairs = parse_qsl(
                encoded,
                keep_blank_values=True,
                strict_parsing=False,
                encoding="utf-8",
                errors="strict",
                max_num_fields=max_fields,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise PlatformHTTPError(400, "invalid_request", f"Invalid {label}") from exc
        result: MutableMapping[str, str] = {}
        for key, value in pairs:
            if (
                not key
                or key in result
                or len(key.encode("utf-8")) > _MAX_PARAMETER_BYTES
                or len(value.encode("utf-8")) > max_value_bytes
                or "\x00" in key
                or "\x00" in value
                or any(ord(character) < 32 for character in key)
                or any(ord(character) < 32 and not (multiline and character in "\r\n\t") for character in value)
            ):
                raise PlatformHTTPError(400, "invalid_request", f"Invalid {label}")
            result[key] = value
        return result

    def _send_error_result(self, error: PlatformHTTPError) -> None:
        result = HTTPResult(
            status=error.status,
            body={"error": {"code": error.code, "message": error.safe_message}},
            headers=error.headers,
        )
        self._send_result(result, allow_fallback=False)

    def _send_result(
        self,
        result: HTTPResult | HTTPFileResult,
        *,
        allow_fallback: bool = True,
    ) -> None:
        if isinstance(result, HTTPFileResult):
            self._send_file_result(result, allow_fallback=allow_fallback)
            return
        try:
            status = _safe_status(result.status)
            headers = _safe_headers(result.headers)
            body, default_content_type = _encode_body(result.body, status)
            if allow_fallback and len(body) > self.platform_server.max_response_bytes:
                raise ValueError("response body is too large")
            content_type = headers.pop("Content-Type", default_content_type)
        except Exception as exc:
            emit_operator_event(
                "http_response_unexpected_exception",
                component="http",
                exception=exc,
                request_id=getattr(self, 'operation_request_id', None),
            )
            if allow_fallback:
                self._send_error_result(
                    PlatformHTTPError(
                        500,
                        "internal_error",
                        "The service could not process the request",
                    )
                )
            else:
                self.close_connection = True
            return

        self.send_response(status)
        if not any(name.casefold() == 'x-request-id' for name in headers):
            self.send_header('X-Request-ID', getattr(self, 'operation_request_id', ''))
        for name, value in _SECURITY_HEADERS.items():
            results_page = content_type.startswith('text/html') and b'<script data-instructor-results-enhancement>' in body
            if name == "Content-Security-Policy" and results_page:
                from .instructor_results_browser import SCRIPT_CSP
                value += "; style-src 'unsafe-inline'" + SCRIPT_CSP
            elif name == "Content-Security-Policy" and callable(getattr(self.platform_server.facade, "portal_request", None)):
                value += "; style-src 'unsafe-inline'"
                instructor = getattr(self.platform_server.facade, 'instructor', None)
                if instructor is not None and instructor.matches(urlsplit(self.path).path) and content_type.startswith('text/html'):
                    from .instructor_browser import SCRIPT_CSP
                    if b'data-classroom-enhancement' in body:
                        from .instructor_classroom import SCRIPT_CSP as CLASSROOM_CSP
                        # One script-src directive: the browser ignores a second
                        # directive with the same name instead of merging hashes.
                        value += CLASSROOM_CSP
                    else:
                        value += SCRIPT_CSP
                elif content_type.startswith('text/html') and b'<script data-student-enhancement>' in body:
                    from .student_browser import SCRIPT_CSP
                    value += SCRIPT_CSP
            if name == "Referrer-Policy" and callable(getattr(self.platform_server.facade, "portal_request", None)):
                # Native form POSTs under no-referrer may send Origin: null.
                # Preserve the same-origin checks without relying on a proxy fix.
                value = "same-origin"
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        if body or status not in {204, 304}:
            self.send_header("Content-Type", content_type)
        for name, value in headers.items():
            if name.casefold() not in _SECURITY_HEADER_NAMES:
                for item in value if isinstance(value, tuple) else (value,):
                    self.send_header(name, item)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    def _send_file_result(
        self,
        result: HTTPFileResult,
        *,
        allow_fallback: bool,
    ) -> None:
        descriptor: Optional[int] = None
        try:
            status_code = _safe_status(result.status)
            headers = _safe_headers(result.headers)
            content_type = _safe_content_type(result.content_type)
            path = Path(result.path)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            allowed_links = (0, 1) if result.retained_checkpoint is True else (1,)
            # A retained checkpoint may be pruned after open(). Its pinned
            # descriptor still streams the exact validated inode safely.
            if not stat.S_ISREG(info.st_mode) or info.st_nlink not in allowed_links:
                raise ValueError("file response must be one regular file")
            limit = self.platform_server.max_file_response_bytes
            if result.public_ide_update is True:
                from .ide_update_distribution import MAX_UPDATE_ARTIFACT_BYTES, MAX_UPDATE_FEED_BYTES
                limit = MAX_UPDATE_FEED_BYTES if path.name == 'feed.json' else MAX_UPDATE_ARTIFACT_BYTES
            if info.st_size < 0 or info.st_size > limit:
                raise ValueError("file response is too large")
        except Exception as exc:
            if descriptor is not None:
                os.close(descriptor)
            if isinstance(exc, FileNotFoundError) and result.retained_checkpoint is True:
                if allow_fallback:
                    self._send_error_result(PlatformHTTPError(
                        410, "checkpoint_expired", "이 저장 버전은 보관 기간이 끝났습니다. 저장 목록을 새로 확인하세요.",
                    ))
                else:
                    self.close_connection = True
                return
            emit_operator_event(
                "http_file_response_unexpected_exception",
                component="http",
                exception=exc,
                request_id=getattr(self, 'operation_request_id', None),
            )
            if allow_fallback:
                self._send_error_result(
                    PlatformHTTPError(
                        500,
                        "internal_error",
                        "The service could not provide the requested artifact",
                    )
                )
            else:
                self.close_connection = True
            return

        assert descriptor is not None
        try:
            self.send_response(status_code)
            if not any(name.casefold() == 'x-request-id' for name in headers):
                self.send_header('X-Request-ID', getattr(self, 'operation_request_id', ''))
            for name, value in _SECURITY_HEADERS.items():
                if name == 'Cache-Control' and result.public_ide_update is True:
                    value = ('no-store' if path.name == 'feed.json'
                             else 'public, max-age=31536000, immutable')
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Content-Type", content_type)
            for name, value in headers.items():
                if name.casefold() not in _SECURITY_HEADER_NAMES:
                    for item in value if isinstance(value, tuple) else (value,):
                        self.send_header(name, item)
            self.end_headers()
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = None
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)


def create_server(
    address: Tuple[str, int],
    facade: Any,
    *,
    public_base_url: Optional[str] = None,
    external_access_mode: str = "disabled",
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_bundle_request_bytes: int = DEFAULT_MAX_BUNDLE_REQUEST_BYTES,
    max_file_response_bytes: int = DEFAULT_MAX_FILE_RESPONSE_BYTES,
    max_query_bytes: int = DEFAULT_MAX_QUERY_BYTES,
    max_form_bytes: int = DEFAULT_MAX_FORM_BYTES,
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    readiness_check: Optional[Callable[[], bool]] = None,
) -> PlatformHTTPServer:
    """Create, but do not start, a configured platform HTTP server."""

    return PlatformHTTPServer(
        address,
        facade,
        public_base_url=public_base_url,
        external_access_mode=external_access_mode,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
        max_bundle_request_bytes=max_bundle_request_bytes,
        max_file_response_bytes=max_file_response_bytes,
        max_query_bytes=max_query_bytes,
        max_form_bytes=max_form_bytes,
        max_concurrent_requests=max_concurrent_requests,
        request_timeout_seconds=request_timeout_seconds,
        readiness_check=readiness_check,
    )


def serve(
    address: Tuple[str, int],
    facade: Any,
    **options: Any,
) -> None:
    """Serve until interrupted, closing the listening socket on exit."""

    with create_server(address, facade, **options) as server:
        server.serve_forever()


def _normalize_result(value: Any, default_status: int) -> HTTPResult | HTTPFileResult:
    if isinstance(value, HTTPFileResult):
        return value
    if isinstance(value, HTTPResult):
        return value
    if all(
        hasattr(value, attribute)
        for attribute in ("status", "path", "content_type", "headers")
    ):
        return HTTPFileResult(
            value.status, Path(value.path), value.content_type, value.headers,
            retained_checkpoint=getattr(value, "retained_checkpoint", False),
            public_ide_update=getattr(value, "public_ide_update", False),
        )
    # Keep the application layer independent from the transport module while
    # still allowing a small response dataclass for HTML and redirects.
    if all(hasattr(value, attribute) for attribute in ("status", "body", "headers")):
        return HTTPResult(value.status, value.body, value.headers)
    if isinstance(value, tuple):
        if len(value) == 2:
            return HTTPResult(value[0], value[1])
        if len(value) == 3:
            return HTTPResult(value[0], value[1], value[2])
        raise ValueError("facade result tuple must contain two or three values")
    return HTTPResult(default_status, value)


def _encode_body(body: Any, status: int) -> tuple[bytes, str]:
    if status in {204, 304}:
        return b"", "application/json; charset=utf-8"
    if body is None:
        body = {}
    if isinstance(body, bytes):
        return body, "application/octet-stream"
    if isinstance(body, str):
        return body.encode("utf-8", "strict"), "text/html; charset=utf-8"
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return encoded, "application/json; charset=utf-8"


def _safe_content_type(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("invalid content type")
    return value


def _safe_status(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 200 or value > 599:
        raise ValueError("invalid HTTP status")
    return value


def _safe_error_status(value: Any) -> int:
    status = _safe_status(value)
    if status < 400:
        raise ValueError("error status must be 4xx or 5xx")
    return status


def _safe_error_code(value: Any) -> str:
    if not isinstance(value, str) or not _ERROR_CODE.fullmatch(value):
        raise ValueError("invalid safe error code")
    return value


def _safe_error_message(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid safe error message")
    normalized = " ".join(value.split())
    if not normalized or len(normalized.encode("utf-8")) > 512:
        raise ValueError("invalid safe error message")
    return normalized


def _safe_headers(value: Mapping[str, str | tuple[str, ...]]) -> MutableMapping[str, str | tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError("headers must be a mapping")
    safe: MutableMapping[str, str | tuple[str, ...]] = {}
    seen: set[str] = set()
    for name, header_value in value.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError("invalid response header name")
        lowered = name.casefold()
        if (
            lowered in seen
            or lowered in _FORBIDDEN_FACADE_HEADERS
            or lowered in _SECURITY_HEADER_NAMES
        ):
            raise ValueError("unsafe response header")
        # Set-Cookie is not a comma-joinable header. Only this canonical name
        # may carry a bounded tuple, emitted as separate HTTP header fields.
        if isinstance(header_value, tuple):
            if name != "Set-Cookie" or not 1 <= len(header_value) <= 8:
                raise ValueError("invalid response header value")
            values = header_value
        elif isinstance(header_value, str):
            values = (header_value,)
        else:
            raise ValueError("invalid response header value")
        total = 0
        for item in values:
            if not isinstance(item, str) or "\r" in item or "\n" in item:
                raise ValueError("invalid response header value")
            size = len(item.encode("utf-8"))
            if size > _MAX_HEADER_VALUE_BYTES:
                raise ValueError("invalid response header value")
            total += size
        if total > _MAX_HEADER_VALUE_BYTES:
            raise ValueError("invalid response header value")
        seen.add(lowered)
        canonical_name = "Content-Type" if lowered == "content-type" else name
        safe[canonical_name] = header_value
    return safe


def _duck_typed_application_error(exc: Exception) -> Optional[PlatformHTTPError]:
    try:
        status = getattr(exc, "status")
        code = getattr(exc, "code")
        safe_message = getattr(exc, "safe_message")
        headers = getattr(exc, "headers", None)
        return PlatformHTTPError(status, code, safe_message, headers=headers)
    except Exception:
        return None


def _positive_limit(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _is_loopback_host(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().strip("[]").casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_insecure_http_binding(
    server_address: Tuple[str, int], public_base_url: Optional[str]
) -> None:
    if public_base_url is None:
        raise ValueError("insecure-http requires an explicit public_base_url")
    try:
        parsed = urlsplit(public_base_url)
        public_port = parsed.port
    except ValueError as exc:
        raise ValueError("insecure-http public_base_url is invalid") from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("insecure-http requires an HTTP origin")
    public_address = _rfc1918_ipv4_address(parsed.hostname)
    listen_address = _rfc1918_ipv4_address(server_address[0])
    if public_address is None:
        raise ValueError(
            "insecure-http public_base_url must use an RFC1918 IPv4 literal"
        )
    if listen_address is None:
        raise ValueError(
            "insecure-http listener must use an RFC1918 IPv4 interface"
        )
    if listen_address != public_address:
        raise ValueError(
            "insecure-http listener must match the public_base_url IPv4 address"
        )
    expected_port = public_port if public_port is not None else 80
    if server_address[1] != expected_port:
        raise ValueError(
            "insecure-http public_base_url port must match the listener port"
        )


def _rfc1918_ipv4_address(value: Any) -> Optional[ipaddress.IPv4Address]:
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv4Address):
        return None
    networks = (
        ipaddress.IPv4Network("10.0.0.0/8"),
        ipaddress.IPv4Network("172.16.0.0/12"),
        ipaddress.IPv4Network("192.168.0.0/16"),
    )
    return address if any(address in network for network in networks) else None


__all__ = [
    "DEFAULT_MAX_CONCURRENT_REQUESTS",
    "DEFAULT_MAX_BUNDLE_REQUEST_BYTES",
    "DEFAULT_MAX_FILE_RESPONSE_BYTES",
    "DEFAULT_MAX_FORM_BYTES",
    "DEFAULT_MAX_QUERY_BYTES",
    "DEFAULT_MAX_REQUEST_BYTES",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "HTTPResult",
    "HTTPFileResult",
    "PlatformHTTPError",
    "PlatformHTTPServer",
    "create_server",
    "serve",
]
