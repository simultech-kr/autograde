from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

import pytest

import autograde.platform_http as platform_http
from autograde.platform_http import HTTPResult, PlatformHTTPError, create_server


TOKEN = "access-token-0123456789abcdef"
IDEMPOTENCY_KEY = "submit-01234567"


class FakeFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def create_device_authorization(
        self, payload: Mapping[str, Any], base_url: str
    ) -> Mapping[str, Any]:
        self.calls.append(("create_device_authorization", payload, base_url))
        return {"device_code": "private", "user_code": "ABCD-EFGH"}

    def exchange_device_authorization(
        self, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append(("exchange_device_authorization", payload))
        return {"access_token": "issued-access-token"}

    def redeem_assignment_claim(
        self, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append(("redeem_assignment_claim", payload))
        return {
            "assignment_id": "asn_01",
            "delivery_mode": "bundle",
            "acceptance_id": "aac_01",
        }

    def refresh_tokens(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("refresh_tokens", payload))
        return {"access_token": "refreshed-access-token"}

    def revoke_current(self, access_token: str) -> None:
        self.calls.append(("revoke_current", access_token))

    def get_me(self, access_token: str) -> Mapping[str, Any]:
        self.calls.append(("get_me", access_token))
        return {"student_key": "s001"}

    def list_sessions(self, access_token: str) -> Mapping[str, Any]:
        self.calls.append(("list_sessions", access_token))
        return {"sessions": [{"session_id": "ses_01", "current": True}]}

    def revoke_session(self, access_token: str, session_id: str) -> None:
        self.calls.append(("revoke_session", access_token, session_id))

    def list_assignments(self, access_token: str) -> Mapping[str, Any]:
        self.calls.append(("list_assignments", access_token))
        return {"assignments": [{"id": "asn_01"}]}

    def get_assignment_repository(
        self, access_token: str, assignment_id: str
    ) -> Mapping[str, Any]:
        self.calls.append(
            ("get_assignment_repository", access_token, assignment_id)
        )
        return {"github_repository_id": 101}

    def submit(
        self,
        access_token: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append(("submit", access_token, idempotency_key, payload))
        return {"id": "sub_01", "state": "received"}

    def get_submission(
        self, access_token: str, submission_id: str
    ) -> Mapping[str, Any]:
        self.calls.append(("get_submission", access_token, submission_id))
        return {"id": submission_id, "state": "accepted"}

    def get_result(
        self, access_token: str, submission_id: str
    ) -> Mapping[str, Any]:
        self.calls.append(("get_result", access_token, submission_id))
        return {"id": submission_id, "score": 9}

    def activate_page(self, query: Mapping[str, str]) -> HTTPResult:
        self.calls.append(("activate_page", query))
        return HTTPResult(200, "<h1>Activate</h1>")

    def github_start(self, query: Mapping[str, str]) -> HTTPResult:
        self.calls.append(("github_start", query))
        return HTTPResult(302, "", {"Location": "https://github.example/login"})

    def github_callback(self, query: Mapping[str, str]) -> HTTPResult:
        self.calls.append(("github_callback", query))
        return HTTPResult(303, "", {"Location": "/activate"})

    def approve_activation(
        self, form: Mapping[str, str], cookies: Mapping[str, str]
    ) -> HTTPResult:
        self.calls.append(("approve_activation", form, cookies))
        return HTTPResult(303, "", {"Location": "/activate?approved=1"})

    def assignment_claim_page(self, query: Mapping[str, str]) -> HTTPResult:
        self.calls.append(("assignment_claim_page", query))
        return HTTPResult(200, "<h1>Assignment claim</h1>")


@contextmanager
def running_server(facade: Any, **options: Any) -> Iterator[Any]:
    server = create_server(("127.0.0.1", 0), facade, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(
    server: Any,
    method: str,
    path: str,
    *,
    body: bytes | str | Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, Mapping[str, str], bytes]:
    effective_headers = dict(headers or {})
    encoded: bytes | None
    if isinstance(body, Mapping):
        encoded = json.dumps(body).encode("utf-8")
        effective_headers.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        encoded = body.encode("utf-8")
    else:
        encoded = body
    connection = http.client.HTTPConnection(
        server.server_address[0], server.server_address[1], timeout=3
    )
    try:
        connection.request(method, path, body=encoded, headers=effective_headers)
        response = connection.getresponse()
        payload = response.read()
        response_headers = {name.casefold(): value for name, value in response.getheaders()}
        return response.status, response_headers, payload
    finally:
        connection.close()


def auth_headers() -> Mapping[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def json_payload(body: bytes) -> Mapping[str, Any]:
    decoded = json.loads(body)
    assert isinstance(decoded, dict)
    return decoded


def test_healthz_is_unauthenticated_minimal_and_does_not_call_facade() -> None:
    facade = FakeFacade()
    with running_server(facade) as server:
        status, headers, body = request(server, "GET", "/healthz")

    assert status == 200
    assert json_payload(body) == {"status": "ok"}
    assert headers["cache-control"] == "no-store"
    assert facade.calls == []


def test_plain_http_cannot_bind_a_non_loopback_interface() -> None:
    with pytest.raises(ValueError, match="plain HTTP.*loopback"):
        create_server(
            ("0.0.0.0", 0),
            FakeFacade(),
            public_base_url="http://127.0.0.1:8000",
        )


def test_insecure_http_can_bind_only_the_matching_rfc1918_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        platform_http.ThreadingHTTPServer,
        "__init__",
        lambda _self, *_args, **_kwargs: None,
    )

    server = create_server(
        ("192.168.50.9", 18080),
        FakeFacade(),
        public_base_url="http://192.168.50.9:18080",
        external_access_mode="insecure-http",
    )

    assert server.public_base_url == "http://192.168.50.9:18080"
    assert server.external_access_mode == "insecure-http"


@pytest.mark.parametrize(
    ("address", "public_url", "message"),
    (
        ("0.0.0.0", "http://192.168.50.9:18080", "RFC1918 IPv4 interface"),
        ("192.168.50.10", "http://192.168.50.9:18080", "must match"),
        ("192.168.50.9", "http://grade.lan:18080", "RFC1918 IPv4 literal"),
        ("192.168.50.9", "http://203.0.113.10:18080", "RFC1918 IPv4 literal"),
        ("192.168.50.9", "http://192.168.50.9:18081", "port must match"),
    ),
)
def test_insecure_http_server_rejects_unsafe_bindings_before_listening(
    address: str, public_url: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        create_server(
            (address, 18080),
            FakeFacade(),
            public_base_url=public_url,
            external_access_mode="insecure-http",
        )


@pytest.mark.parametrize(
    "path", ("/instructor", "/v1/instructor/dashboard")
)
def test_insecure_http_hides_instructor_routes(path: str) -> None:
    class InstructorFacade(FakeFacade):
        def instructor_dashboard_page(self, authorization: str | None) -> HTTPResult:
            self.calls.append(("instructor_dashboard_page", authorization))
            return HTTPResult(200, "dashboard")

        def instructor_dashboard(self, authorization: str | None) -> Mapping[str, Any]:
            self.calls.append(("instructor_dashboard", authorization))
            return {"submissions": []}

    facade = InstructorFacade()
    with running_server(facade) as server:
        # Binding safety is covered independently above. Switch the live
        # loopback fixture into the routing mode without requiring a private
        # interface to exist on every CI host.
        server.external_access_mode = "insecure-http"
        server.public_base_url = "http://192.168.50.9:18080"
        status, _headers, body = request(
            server,
            "GET",
            path,
            headers={
                "Authorization": "Basic should-never-be-processed",
                "Host": "192.168.50.9:18080",
            },
        )

    assert status == 404
    assert json_payload(body)["error"]["code"] == "not_found"
    assert not any(call[0].startswith("instructor") for call in facade.calls)


def test_insecure_http_requires_the_configured_private_host_header() -> None:
    facade = FakeFacade()
    with running_server(facade) as server:
        server.external_access_mode = "insecure-http"
        server.public_base_url = "http://192.168.50.9:18080"

        rejected, _headers, rejected_body = request(
            server,
            "GET",
            "/healthz",
            headers={"Host": "attacker.example"},
        )
        accepted, _headers, accepted_body = request(
            server,
            "GET",
            "/healthz",
            headers={"Host": "192.168.50.9:18080"},
        )

    assert rejected == 400
    assert json_payload(rejected_body)["error"]["code"] == "invalid_host"
    assert accepted == 200
    assert json_payload(accepted_body) == {"status": "ok"}


def test_disabled_mode_rejects_non_loopback_even_with_https_public_url() -> None:
    with pytest.raises(ValueError, match="loopback interface"):
        create_server(
            ("0.0.0.0", 18080),
            FakeFacade(),
            public_base_url="https://grade.example.test",
        )


def test_api_routes_dispatch_to_the_facade_with_transport_context() -> None:
    facade = FakeFacade()
    with running_server(facade) as server:
        status, headers, body = request(
            server,
            "POST",
            "/v1/device-authorizations",
            body={"device_name": "Ubuntu WSL"},
        )
        assert status == 201
        assert json_payload(body)["user_code"] == "ABCD-EFGH"
        assert facade.calls[-1] == (
            "create_device_authorization",
            {"device_name": "Ubuntu WSL"},
            f"http://127.0.0.1:{server.server_address[1]}",
        )

        status, _, _ = request(
            server,
            "POST",
            "/v1/device-authorizations/token",
            body={"device_code": "private"},
        )
        assert status == 200
        assert facade.calls[-1] == (
            "exchange_device_authorization",
            {"device_code": "private"},
        )

        status, _, body = request(
            server,
            "POST",
            "/v1/assignment-claims/redeem",
            body={
                "claim_code": "AK1-2345-6789-ABCD",
                "device_code": "private",
            },
        )
        assert status == 200
        assert json_payload(body)["assignment_id"] == "asn_01"
        assert facade.calls[-1] == (
            "redeem_assignment_claim",
            {
                "claim_code": "AK1-2345-6789-ABCD",
                "device_code": "private",
            },
        )

        status, _, _ = request(
            server,
            "POST",
            "/v1/tokens/refresh",
            body={"refresh_token": "refresh-value"},
        )
        assert status == 200
        assert facade.calls[-1] == (
            "refresh_tokens",
            {"refresh_token": "refresh-value"},
        )

        status, _, body = request(
            server, "GET", "/v1/me", headers=auth_headers()
        )
        assert status == 200
        assert json_payload(body) == {"student_key": "s001"}
        assert facade.calls[-1] == ("get_me", TOKEN)

        status, _, body = request(
            server, "GET", "/v1/me/sessions", headers=auth_headers()
        )
        assert status == 200
        assert json_payload(body)["sessions"][0]["session_id"] == "ses_01"
        assert facade.calls[-1] == ("list_sessions", TOKEN)

        status, _, body = request(
            server,
            "DELETE",
            "/v1/me/sessions/ses_01",
            headers=auth_headers(),
        )
        assert status == 204
        assert body == b""
        assert facade.calls[-1] == ("revoke_session", TOKEN, "ses_01")

        status, _, _ = request(
            server, "GET", "/v1/assignments", headers=auth_headers()
        )
        assert status == 200
        assert facade.calls[-1] == ("list_assignments", TOKEN)

        status, _, _ = request(
            server,
            "GET",
            "/v1/assignments/asn_01/repository",
            headers=auth_headers(),
        )
        assert status == 200
        assert facade.calls[-1] == (
            "get_assignment_repository",
            TOKEN,
            "asn_01",
        )

        status, _, body = request(
            server,
            "POST",
            "/v1/submissions",
            body={"assignment_id": "asn_01", "head_sha": "a" * 40},
            headers={
                **auth_headers(),
                "Idempotency-Key": IDEMPOTENCY_KEY,
            },
        )
        assert status == 202
        assert json_payload(body)["state"] == "received"
        assert facade.calls[-1] == (
            "submit",
            TOKEN,
            IDEMPOTENCY_KEY,
            {"assignment_id": "asn_01", "head_sha": "a" * 40},
        )

        status, _, _ = request(
            server, "GET", "/v1/submissions/sub_01", headers=auth_headers()
        )
        assert status == 200
        assert facade.calls[-1] == ("get_submission", TOKEN, "sub_01")

        status, _, _ = request(
            server,
            "GET",
            "/v1/submissions/sub_01/result",
            headers=auth_headers(),
        )
        assert status == 200
        assert facade.calls[-1] == ("get_result", TOKEN, "sub_01")

        status, _, body = request(
            server, "DELETE", "/v1/sessions/current", headers=auth_headers()
        )
        assert status == 204
        assert body == b""
        assert facade.calls[-1] == ("revoke_current", TOKEN)

        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["connection"] == "close"
        assert "access-control-allow-origin" not in headers


@pytest.mark.parametrize(
    ("body", "content_type", "expected_status", "expected_code"),
    [
        ('["not", "an", "object"]', "application/json", 400, "invalid_json"),
        ('{"key":1,"key":2}', "application/json", 400, "invalid_json"),
        ('{"score":NaN}', "application/json", 400, "invalid_json"),
        ('{"key":1}', "text/plain", 415, "unsupported_media_type"),
        ('{"key":1}', "application/json; charset=latin-1", 415, "unsupported_media_type"),
    ],
)
def test_json_requests_are_strict_objects(
    body: str, content_type: str, expected_status: int, expected_code: str
) -> None:
    facade = FakeFacade()
    with running_server(facade) as server:
        status, _, response = request(
            server,
            "POST",
            "/v1/device-authorizations",
            body=body,
            headers={"Content-Type": content_type},
        )
    assert status == expected_status
    assert json_payload(response)["error"]["code"] == expected_code
    assert facade.calls == []


def test_request_and_response_size_limits_fail_safely() -> None:
    facade = FakeFacade()
    with running_server(
        facade, max_request_bytes=20, max_response_bytes=100
    ) as server:
        status, _, body = request(
            server,
            "POST",
            "/v1/device-authorizations",
            body={"device_name": "this value is too long"},
        )
        assert status == 413
        assert json_payload(body)["error"]["code"] == "payload_too_large"

        class LargeResponseFacade(FakeFacade):
            def get_me(self, access_token: str) -> Mapping[str, Any]:
                return {"value": "x" * 500}

        server.facade = LargeResponseFacade()
        status, _, body = request(
            server, "GET", "/v1/me", headers=auth_headers()
        )
        assert status == 500
        assert json_payload(body)["error"]["code"] == "internal_error"


def test_overload_fails_fast_and_slow_client_cannot_block_shutdown() -> None:
    facade = FakeFacade()
    server = create_server(
        ("127.0.0.1", 0),
        facade,
        max_concurrent_requests=1,
        request_timeout_seconds=1.0,
    )
    handler_started = threading.Event()
    handler_finished = threading.Event()
    original_process_request_thread = server.process_request_thread

    def tracked_process_request_thread(request_socket, client_address):
        handler_started.set()
        try:
            original_process_request_thread(request_socket, client_address)
        finally:
            handler_finished.set()

    server.process_request_thread = tracked_process_request_thread
    server_thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01),
        daemon=True,
    )
    server_thread.start()
    slow_client = socket.create_connection(server.server_address, timeout=1)
    overload_client = None
    drip_stopped = threading.Event()
    shutdown_done = threading.Event()

    def drip_header() -> None:
        while not drip_stopped.wait(0.05):
            try:
                slow_client.sendall(b"x")
            except OSError:
                return

    drip_thread = threading.Thread(target=drip_header, daemon=True)

    def shutdown_server() -> None:
        server.shutdown()
        shutdown_done.set()

    shutdown_thread = threading.Thread(target=shutdown_server, daemon=True)
    shutdown_finished_while_slow_client_open = False
    handler_finished_after_server_close = False
    shutdown_thread_started = False
    server_closed = False
    slot_was_released = False
    active_request_count_after_close = -1
    try:
        slow_client.sendall(
            b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nX-Slow: "
        )
        drip_thread.start()
        assert handler_started.wait(timeout=1)

        overload_client = socket.create_connection(server.server_address, timeout=1)
        overload_client.sendall(
            b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        overload_response = http.client.HTTPResponse(overload_client)
        overload_response.begin()
        overload_body = overload_response.read()
        overload_headers = {
            name.casefold(): value for name, value in overload_response.getheaders()
        }

        assert overload_response.status == 503
        assert json_payload(overload_body) == {
            "error": {
                "code": "temporarily_unavailable",
                "message": "The service is busy; try again later",
            }
        }
        assert overload_headers["retry-after"] == "1"
        assert overload_headers["connection"] == "close"
        assert int(overload_headers["content-length"]) == len(overload_body)
        assert overload_headers["content-type"] == "application/json; charset=utf-8"
        expected_security_headers = {
            "cache-control": "no-store",
            "content-security-policy": (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
                "form-action 'self'"
            ),
            "cross-origin-resource-policy": "same-origin",
            "permissions-policy": "camera=(), microphone=(), geolocation=()",
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
        }
        for name, value in expected_security_headers.items():
            assert overload_headers[name] == value
        overload_client.settimeout(1)
        try:
            overload_socket_closed = overload_client.recv(1) == b""
        except ConnectionResetError:
            overload_socket_closed = True
        assert overload_socket_closed

        shutdown_thread.start()
        shutdown_thread_started = True
        shutdown_finished_while_slow_client_open = shutdown_done.wait(timeout=1)
        server.server_close()
        server_closed = True
        handler_finished_after_server_close = handler_finished.wait(timeout=1)
        slot_was_released = server._request_slots.acquire(blocking=False)
        if slot_was_released:
            server._request_slots.release()
        with server._active_requests_lock:
            active_request_count_after_close = len(server._active_requests)
    finally:
        if not shutdown_thread_started:
            shutdown_thread.start()
            shutdown_thread_started = True
        if not shutdown_done.is_set():
            shutdown_done.wait(timeout=2)
        if not server_closed:
            server.server_close()
            server_closed = True
        if not handler_finished_after_server_close:
            handler_finished_after_server_close = handler_finished.wait(timeout=1)
        if not slot_was_released:
            slot_was_released = server._request_slots.acquire(blocking=False)
            if slot_was_released:
                server._request_slots.release()
        if active_request_count_after_close < 0:
            with server._active_requests_lock:
                active_request_count_after_close = len(server._active_requests)
        drip_stopped.set()
        try:
            slow_client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        slow_client.close()
        if overload_client is not None:
            overload_client.close()
        drip_thread.join(timeout=1)
        server_thread.join(timeout=2)
        shutdown_thread.join(timeout=2)

    assert shutdown_finished_while_slow_client_open
    assert handler_finished_after_server_close
    assert slot_was_released
    assert active_request_count_after_close == 0
    assert not server_thread.is_alive()


def test_handler_thread_start_failure_closes_socket_without_leaking_slot(
    monkeypatch,
) -> None:
    server = create_server(
        ("127.0.0.1", 0),
        FakeFacade(),
        max_concurrent_requests=1,
    )
    client = socket.create_connection(server.server_address, timeout=1)
    accepted, client_address = server.get_request()
    original_shutdown_request = server.shutdown_request
    events = []

    def fail_thread_start(_thread) -> None:
        raise RuntimeError("handler thread start failed")

    def cleanup_then_fail(request_socket) -> None:
        original_shutdown_request(request_socket)
        raise OSError("simulated cleanup reporting failure")

    monkeypatch.setattr(threading.Thread, "start", fail_thread_start)
    monkeypatch.setattr(server, "shutdown_request", cleanup_then_fail)
    monkeypatch.setattr(
        platform_http,
        "emit_operator_event",
        lambda event, **fields: events.append((event, fields)),
    )
    try:
        with pytest.raises(RuntimeError, match="handler thread start failed"):
            server.process_request(accepted, client_address)

        slot_was_released = server._request_slots.acquire(blocking=False)
        if slot_was_released:
            server._request_slots.release()
        with server._active_requests_lock:
            active_request_count = len(server._active_requests)
        client.settimeout(1)
        try:
            client_socket_closed = client.recv(1) == b""
        except ConnectionResetError:
            client_socket_closed = True
    finally:
        client.close()
        server.server_close()

    assert slot_was_released
    assert active_request_count == 0
    assert client_socket_closed
    assert len(events) == 1
    event, fields = events[0]
    assert event == "http_request_cleanup_failed"
    assert fields["component"] == "http"
    assert isinstance(fields["exception"], OSError)


def test_bearer_idempotency_path_and_method_validation() -> None:
    facade = FakeFacade()
    with running_server(facade) as server:
        status, headers, body = request(server, "GET", "/v1/me")
        assert status == 401
        assert headers["www-authenticate"] == "Bearer"
        assert json_payload(body)["error"]["code"] == "invalid_token"

        status, _, body = request(
            server,
            "POST",
            "/v1/submissions",
            body={"assignment_id": "asn_01"},
            headers={**auth_headers(), "Idempotency-Key": "short"},
        )
        assert status == 400
        assert json_payload(body)["error"]["code"] == "invalid_idempotency_key"

        status, headers, body = request(server, "POST", "/v1/me", body={})
        assert status == 405
        assert headers["allow"] == "GET"
        assert json_payload(body)["error"]["code"] == "method_not_allowed"

        status, _, body = request(server, "GET", "/v1/unknown")
        assert status == 404
        assert json_payload(body)["error"]["code"] == "not_found"

        status, _, _ = request(
            server,
            "GET",
            "/v1/submissions/not%2Fa%2Fsingle%2Fid",
            headers=auth_headers(),
        )
        assert status in {400, 404}

        status, _, body = request(
            server,
            "GET",
            "/v1/submissions/" + ("a" * 129),
            headers=auth_headers(),
        )
        assert status == 404
        assert json_payload(body)["error"]["code"] == "not_found"

    assert facade.calls == []


def test_facade_safe_errors_are_mapped_and_unexpected_details_are_hidden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "refresh-secret-that-must-not-leak"

    class DuckError(RuntimeError):
        status = 429
        code = "slow_down"
        safe_message = "Poll less frequently"
        headers = {"Retry-After": "10"}

    class ErrorFacade(FakeFacade):
        def exchange_device_authorization(self, payload: Mapping[str, Any]) -> Any:
            raise DuckError(secret)

        def get_me(self, access_token: str) -> Any:
            raise RuntimeError(f"database failed with {secret} and {access_token}")

        def get_result(self, access_token: str, submission_id: str) -> Any:
            raise PlatformHTTPError(404, "not_found", "Result not found")

    with running_server(ErrorFacade()) as server:
        status, headers, body = request(
            server,
            "POST",
            "/v1/device-authorizations/token",
            body={"device_code": secret},
        )
        assert status == 429
        assert headers["retry-after"] == "10"
        assert json_payload(body)["error"] == {
            "code": "slow_down",
            "message": "Poll less frequently",
        }

        status, _, body = request(
            server, "GET", "/v1/me", headers=auth_headers()
        )
        assert status == 500
        assert secret.encode() not in body
        assert TOKEN.encode() not in body

        status, _, body = request(
            server,
            "GET",
            "/v1/submissions/sub_01/result",
            headers=auth_headers(),
        )
        assert status == 404
        assert json_payload(body)["error"]["message"] == "Result not found"

    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert TOKEN not in captured.out + captured.err
    event = json.loads(captured.err)
    assert event == {
        "component": "http",
        "event": "http_request_unexpected_exception",
        "exception_type": "RuntimeError",
        "level": "error",
    }


def test_optional_web_routes_support_html_redirect_form_and_cookies() -> None:
    facade = FakeFacade()
    with running_server(facade) as server:
        status, headers, body = request(
            server, "GET", "/activate?user_code=ABCD-EFGH"
        )
        assert status == 200
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert body == b"<h1>Activate</h1>"
        assert facade.calls[-1] == (
            "activate_page",
            {"user_code": "ABCD-EFGH"},
        )

        status, headers, _ = request(
            server, "GET", "/auth/github/start?user_code=ABCD-EFGH"
        )
        assert status == 302
        assert headers["location"] == "https://github.example/login"

        status, headers, _ = request(
            server, "GET", "/oauth/github/callback?code=oauth-code&state=state-value"
        )
        assert status == 303
        assert headers["location"] == "/activate"

        status, headers, _ = request(
            server,
            "POST",
            "/activate/approve",
            body=(
                "user_code=ABCD-EFGH&"
                "activation_code=AG1-2345-6789-ABCD-EFGH-JKLM-NPQR-ST&"
                "csrf=csrf-value"
            ),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": "activation_session=session-value",
            },
        )
        assert status == 303
        assert headers["location"] == "/activate?approved=1"
        assert facade.calls[-1] == (
            "approve_activation",
            {
                "user_code": "ABCD-EFGH",
                "activation_code": "AG1-2345-6789-ABCD-EFGH-JKLM-NPQR-ST",
                "csrf": "csrf-value",
            },
            {"activation_session": "session-value"},
        )


def test_assignment_claim_path_binds_the_assignment_without_query_override() -> None:
    facade = FakeFacade()
    with running_server(facade) as server:
        status, headers, body = request(
            server, "GET", "/assignment-claim/asn_lab01"
        )
        assert status == 200
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert body == b"<h1>Assignment claim</h1>"
        assert facade.calls[-1] == (
            "assignment_claim_page",
            {"assignment_id": "asn_lab01"},
        )

        status, _, body = request(
            server,
            "GET",
            "/assignment-claim/asn_lab01?assignment_id=asn_other",
        )
        assert status == 400
        assert json_payload(body)["error"]["code"] == "invalid_request"

        status, _, body = request(
            server, "GET", "/assignment-claim/not%2Fa%2Fsingle%2Fid"
        )
        assert status in {400, 404}
        assert json_payload(body)["error"]["code"] in {
            "invalid_request",
            "not_found",
        }

    assert facade.calls == [
        ("assignment_claim_page", {"assignment_id": "asn_lab01"})
    ]


def test_absent_optional_web_routes_are_not_advertised() -> None:
    class APIOnlyFacade:
        pass

    with running_server(APIOnlyFacade()) as server:
        status, _, body = request(server, "GET", "/activate")
        assert status == 404
        assert json_payload(body)["error"]["code"] == "not_found"


def test_invalid_facade_headers_cannot_override_transport_security() -> None:
    class HeaderFacade(FakeFacade):
        def get_me(self, access_token: str) -> HTTPResult:
            return HTTPResult(
                200,
                {"student_key": "s001"},
                {"Content-Length": "1", "Cache-Control": "public"},
            )

    with running_server(HeaderFacade()) as server:
        status, headers, body = request(
            server, "GET", "/v1/me", headers=auth_headers()
        )
        assert status == 500
        assert headers["cache-control"] == "no-store"
        assert json_payload(body)["error"]["code"] == "internal_error"
