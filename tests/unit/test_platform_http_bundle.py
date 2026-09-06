from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from autograde.platform_http import HTTPFileResult, create_server


TOKEN = "access-token-0123456789abcdef"
IDEMPOTENCY_KEY = "bundle-submit-01234567"


class BundleFacade:
    def __init__(self, starter: Path) -> None:
        self.starter = starter
        self.received: bytes | None = None
        self.dashboard_authorizations: list[str] = []

    def get_bundle_starter(self, access_token: str, assignment_id: str) -> HTTPFileResult:
        assert access_token == TOKEN
        assert assignment_id == "asn_bundle"
        return HTTPFileResult(
            200,
            self.starter,
            "application/gzip",
            {"Content-Disposition": 'attachment; filename="lab01.tar.gz"'},
        )

    def submit_bundle(
        self,
        access_token: str,
        idempotency_key: str,
        assignment_id: str,
        source: BinaryIO,
        content_length: int,
    ) -> dict[str, Any]:
        assert access_token == TOKEN
        assert idempotency_key == IDEMPOTENCY_KEY
        assert assignment_id == "asn_bundle"
        self.received = source.read()
        assert len(self.received) == content_length
        return {"submission": {"submission_id": "bsub_01", "state": "accepted"}}

    def instructor_dashboard(self, authorization: str) -> dict[str, Any]:
        self.dashboard_authorizations.append(authorization)
        return {"course_key": "cse101", "rows": []}

    def instructor_dashboard_page(self, authorization: str):
        from autograde.platform_http import HTTPResult

        self.dashboard_authorizations.append(authorization)
        return HTTPResult(200, "<h1>Dashboard</h1>")


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
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        return (
            response.status,
            {name.casefold(): value for name, value in response.getheaders()},
            payload,
        )
    finally:
        connection.close()


def auth_headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", **extra}


def test_bundle_download_streams_file_outside_json_response_limit(tmp_path: Path) -> None:
    payload = b"gzip-artifact" * 100
    starter = tmp_path / "starter.tar.gz"
    starter.write_bytes(payload)
    with running_server(BundleFacade(starter), max_response_bytes=32) as server:
        status, headers, body = request(
            server,
            "GET",
            "/v1/assignments/asn_bundle/starter",
            headers=auth_headers(),
        )
    assert status == 200
    assert body == payload
    assert headers["content-type"] == "application/gzip"
    assert headers["content-disposition"] == 'attachment; filename="lab01.tar.gz"'
    assert headers["cache-control"] == "no-store"


def test_bundle_submission_is_bounded_spooled_and_dispatched(tmp_path: Path) -> None:
    starter = tmp_path / "starter.tar.gz"
    starter.write_bytes(b"starter")
    facade = BundleFacade(starter)
    payload = b"submitted-gzip-bytes"
    with running_server(facade, max_bundle_request_bytes=len(payload)) as server:
        status, _, body = request(
            server,
            "POST",
            "/v1/assignments/asn_bundle/submissions",
            body=payload,
            headers=auth_headers(
                **{
                    "Content-Type": "application/gzip",
                    "Idempotency-Key": IDEMPOTENCY_KEY,
                }
            ),
        )
    assert status == 202
    assert json.loads(body)["submission"]["state"] == "accepted"
    assert facade.received == payload


def test_bundle_submission_rejects_wrong_media_type_and_oversize(tmp_path: Path) -> None:
    starter = tmp_path / "starter.tar.gz"
    starter.write_bytes(b"starter")
    facade = BundleFacade(starter)
    with running_server(facade, max_bundle_request_bytes=4) as server:
        status, _, body = request(
            server,
            "POST",
            "/v1/assignments/asn_bundle/submissions",
            body=b"abcd",
            headers=auth_headers(
                **{
                    "Content-Type": "application/octet-stream",
                    "Idempotency-Key": IDEMPOTENCY_KEY,
                }
            ),
        )
        assert status == 415
        assert json.loads(body)["error"]["code"] == "unsupported_media_type"

        status, _, body = request(
            server,
            "POST",
            "/v1/assignments/asn_bundle/submissions",
            body=b"abcde",
            headers=auth_headers(
                **{
                    "Content-Type": "application/gzip",
                    "Idempotency-Key": IDEMPOTENCY_KEY,
                }
            ),
        )
        assert status == 413
        assert json.loads(body)["error"]["code"] == "payload_too_large"
    assert facade.received is None


def test_instructor_routes_forward_basic_authorization_without_bearer_parsing(
    tmp_path: Path,
) -> None:
    starter = tmp_path / "starter.tar.gz"
    starter.write_bytes(b"starter")
    facade = BundleFacade(starter)
    authorization = "Basic aW5zdHJ1Y3RvcjpzZWNyZXQ="
    with running_server(facade) as server:
        status, _, body = request(
            server,
            "GET",
            "/v1/instructor/dashboard",
            headers={"Authorization": authorization},
        )
        assert status == 200
        assert json.loads(body) == {"course_key": "cse101", "rows": []}

        status, headers, body = request(
            server,
            "GET",
            "/instructor",
            headers={"Authorization": authorization},
        )
        assert status == 200
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert b"Dashboard" in body

    assert facade.dashboard_authorizations == [authorization, authorization]
