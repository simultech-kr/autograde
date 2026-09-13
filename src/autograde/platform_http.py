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
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HTTPFileResult:
    """A trusted local regular file streamed without loading it into memory."""

    status: int
    path: Path
    content_type: str = "application/octet-stream"
    headers: Mapping[str, str] = field(default_factory=dict)


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
        self.max_response_bytes = max_response_bytes
        self.max_bundle_request_bytes = max_bundle_request_bytes
        self.max_file_response_bytes = max_file_response_bytes
        self.max_query_bytes = max_query_bytes
        self.max_form_bytes = max_form_bytes
        self.request_timeout_seconds = float(request_timeout_seconds)
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
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
        self.close_connection = True
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
                )
                mapped = PlatformHTTPError(
                    500,
                    "internal_error",
                    "The service could not process the request",
                )
            self._send_error_result(mapped)

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
        if callable(getattr(facade, "portal_request", None)) and path not in {"/healthz", "/readyz"}:
            if path in {"/", "/courses"} or path.startswith("/courses/"):
                return "portal", {"path": path}, frozenset({"GET", "POST"})
            return None, {}, frozenset()
        if (
            self.platform_server.external_access_mode == "insecure-http"
            and path in {"/instructor", "/v1/instructor/dashboard"}
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
            } and not callable(getattr(facade, optional_method, None)):
                return None, {}, frozenset()
            return route, {}, allowed

        parts = path.split("/")
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
        if route == "portal":
            if query:
                raise PlatformHTTPError(400, "invalid_request", "Unexpected query parameter")
            if self.command == "POST":
                form = self._read_form()
            else:
                self._ensure_no_body()
                form = {}
            return facade.portal_request(
                self.command, parameters["path"], form, self._cookies(),
                self.headers.get("Origin"), self.headers.get("Authorization"),
            ), 200
        if route == "health":
            self._ensure_no_body()
            return {"status": "ok"}, 200
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
        if route == "revoke_current":
            self._ensure_no_body()
            return facade.revoke_current(self._bearer_token()), 204
        if route == "me":
            self._ensure_no_body()
            return facade.get_me(self._bearer_token()), 200
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
        if route == "assignment_repository":
            self._ensure_no_body()
            return (
                facade.get_assignment_repository(
                    self._bearer_token(), parameters["assignment_id"]
                ),
                200,
            )
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
            return facade.submit_bundle(
                access_token,
                idempotency_key,
                assignment_id,
                upload,
                length,
            )

    def _read_json_object(self) -> Mapping[str, Any]:
        body = self._read_body(
            expected_media_type="application/json",
            max_bytes=self.platform_server.max_request_bytes,
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

    def _read_form(self) -> Mapping[str, str]:
        body = self._read_body(
            expected_media_type="application/x-www-form-urlencoded",
            max_bytes=self.platform_server.max_form_bytes,
        )
        try:
            text = body.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise PlatformHTTPError(400, "invalid_form", "Invalid form body") from exc
        return self._parse_parameters(
            text,
            max_bytes=self.platform_server.max_form_bytes,
            max_fields=_MAX_FORM_FIELDS,
            label="form",
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
                or len(value.encode("utf-8")) > _MAX_PARAMETER_BYTES
                or "\x00" in key
                or "\x00" in value
                or any(ord(character) < 32 for character in key + value)
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
        for name, value in _SECURITY_HEADERS.items():
            if name == "Content-Security-Policy" and callable(getattr(self.platform_server.facade, "portal_request", None)):
                value += "; style-src 'unsafe-inline'"
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        if body or status not in {204, 304}:
            self.send_header("Content-Type", content_type)
        for name, value in headers.items():
            if name.casefold() not in _SECURITY_HEADER_NAMES:
                self.send_header(name, value)
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
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("file response must be one regular file")
            if info.st_size < 0 or info.st_size > self.platform_server.max_file_response_bytes:
                raise ValueError("file response is too large")
        except Exception as exc:
            if descriptor is not None:
                os.close(descriptor)
            emit_operator_event(
                "http_file_response_unexpected_exception",
                component="http",
                exception=exc,
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
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Content-Type", content_type)
            for name, value in headers.items():
                if name.casefold() not in _SECURITY_HEADER_NAMES:
                    self.send_header(name, value)
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
            value.status, Path(value.path), value.content_type, value.headers
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


def _safe_headers(value: Mapping[str, str]) -> MutableMapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("headers must be a mapping")
    safe: MutableMapping[str, str] = {}
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
        if not isinstance(header_value, str):
            raise ValueError("invalid response header value")
        if (
            "\r" in header_value
            or "\n" in header_value
            or len(header_value.encode("utf-8")) > _MAX_HEADER_VALUE_BYTES
        ):
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
