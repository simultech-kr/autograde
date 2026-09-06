from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlencode

import autograde.platform_service as platform_service
from autograde.platform_bundle import BundleStore
from autograde.platform_http import PlatformHTTPServer, create_server
from autograde.platform_service import StudentPlatformService, TokenPolicy
from autograde.platform_state import PlatformStateStore


NOW = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
COURSE = "software-design-2026f"
PUBLIC_ORIGIN = "https://grade.example.test"
SECRET = b"s" * 32
INSTRUCTOR_TOKEN = "instructor-secret-value-0123456789"
RUNNER = "registry.school/autograde@sha256:" + "e" * 64
PRIMARY_ASSIGNMENT = "basn_observer_java"
OTHER_ASSIGNMENT = "basn_observer_cpp"
STUDENT_COUNT = 25


@contextmanager
def _running_server(service: StudentPlatformService):
    # This loopback listener models the plaintext backend behind an HTTPS reverse
    # proxy.  The service's generated links and security decisions use the
    # configured public HTTPS origin, never this private test transport.
    server = create_server(
        ("127.0.0.1", 0),
        service,
        public_base_url=PUBLIC_ORIGIN,
        max_concurrent_requests=32,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    server: PlatformHTTPServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=20)
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


def _json_request(
    server: PlatformHTTPServer,
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, object]]:
    effective = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        effective["Content-Type"] = "application/json"
    if token is not None:
        effective["Authorization"] = f"Bearer {token}"
    status, response_headers, raw = _request(
        server, method, path, body=body, headers=effective
    )
    decoded = json.loads(raw) if raw else {}
    assert isinstance(decoded, dict)
    return status, response_headers, decoded


def _register_bundle_assignment(
    state: PlatformStateStore,
    *,
    assignment_id: str,
    assignment_key: str,
    title: str,
    starter_path: Path,
    starter_digest: str,
    starter_size_bytes: int,
    assessment_path: Path,
    assessment_digest: str,
) -> None:
    state.register_bundle_assignment_release(
        assignment_id=assignment_id,
        course_key=COURSE,
        assignment_key=assignment_key,
        release_id=f"{assignment_key}-v1",
        title=title,
        starter_path=str(starter_path),
        starter_digest=starter_digest,
        starter_size_bytes=starter_size_bytes,
        assessment_path=str(assessment_path),
        assessment_digest=assessment_digest,
        runner_image=RUNNER,
        rubric_version="v1",
        max_score=10,
        result_policy="immediate",
        opens_at=NOW - timedelta(minutes=1),
        due_at=NOW + timedelta(days=7),
        ready=True,
        at=NOW,
    )


def test_twenty_five_students_claim_and_download_one_scoped_assignment_over_http(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    students = []
    passwords = []
    for index in range(STUDENT_COUNT):
        student_key = f"2026{index + 1:04d}"
        student = state.upsert_local_student(
            student_key=student_key,
            auth_subject=f"school:{student_key}",
            at=NOW,
        )
        state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)
        students.append(student_key)
        passwords.append(f"{index + 1:06d}")

    bundle_store = BundleStore(
        tmp_path / "bundles",
        max_compressed_bytes=2 * 1024 * 1024,
        max_expanded_bytes=8 * 1024 * 1024,
        max_files=100,
    )
    starter_tree = tmp_path / "starter"
    starter_tree.mkdir()
    (starter_tree / "README.md").write_text(
        "Implement the Observer pattern.\n", encoding="utf-8"
    )
    starter = bundle_store.create_from_directory(starter_tree, kind="starter")
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("# hidden checks\n", encoding="utf-8")
    assessment_digest = "a" * 64
    for assignment_id, assignment_key, title in (
        (PRIMARY_ASSIGNMENT, "observer-java", "Observer Pattern - Java"),
        (OTHER_ASSIGNMENT, "observer-cpp", "Observer Pattern - C++"),
    ):
        _register_bundle_assignment(
            state,
            assignment_id=assignment_id,
            assignment_key=assignment_key,
            title=title,
            starter_path=starter.path,
            starter_digest=starter.digest,
            starter_size_bytes=starter.compressed_bytes,
            assessment_path=assessment,
            assessment_digest=assessment_digest,
        )

    service = StudentPlatformService(
        state=state,
        server_secret=SECRET,
        course_key=COURSE,
        public_base_url=PUBLIC_ORIGIN,
        token_policy=TokenPolicy(max_concurrent_password_verifications=4),
        bundle_store=bundle_store,
        instructor_token=INSTRUCTOR_TOKEN,
        now=lambda: NOW,
    )
    for student_key, password in zip(students, passwords, strict=True):
        service.set_student_password(student_key=student_key, password=password)

    # Preserve the real scrypt check while making overlap deterministic enough
    # to prove that the service's password-work semaphore is active under HTTP
    # load rather than merely testing that the policy value exists.
    real_verify = platform_service.verify_student_password
    verification_lock = threading.Lock()
    active_verifications = 0
    maximum_verifications = 0

    def tracked_verify(password: str, encoded_hash: str) -> bool:
        nonlocal active_verifications, maximum_verifications
        with verification_lock:
            active_verifications += 1
            maximum_verifications = max(
                maximum_verifications, active_verifications
            )
        try:
            time.sleep(0.03)
            return real_verify(password, encoded_hash)
        finally:
            with verification_lock:
                active_verifications -= 1

    monkeypatch.setattr(platform_service, "verify_student_password", tracked_verify)

    issue_barrier = threading.Barrier(STUDENT_COUNT)
    observed_statuses: list[int] = []
    status_lock = threading.Lock()

    def record(status: int) -> None:
        with status_lock:
            observed_statuses.append(status)

    with _running_server(service) as server:

        def claim_and_download(index: int) -> dict[str, object]:
            status, page_headers, raw_page = _request(
                server, "GET", f"/assignment-claim/{PRIMARY_ASSIGNMENT}"
            )
            record(status)
            assert status == 200, raw_page
            page = raw_page.decode("utf-8")
            csrf_match = re.search(r'name="csrf" value="([^"]+)"', page)
            assert csrf_match is not None
            cookie = page_headers["set-cookie"].split(";", 1)[0]
            assert "Secure" in page_headers["set-cookie"]
            assert PUBLIC_ORIGIN not in cookie

            issue_barrier.wait(timeout=10)
            form_body = urlencode(
                {
                    "assignment_id": PRIMARY_ASSIGNMENT,
                    "student_key": students[index],
                    "password": passwords[index],
                    "csrf": csrf_match.group(1),
                }
            ).encode("ascii")
            status, _headers, raw_issue = _request(
                server,
                "POST",
                "/assignment-claim/issue",
                body=form_body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookie,
                },
            )
            record(status)
            assert status == 200, raw_issue
            claim_match = re.search(
                rb"<code>(AK1-[A-Z0-9-]+)</code>", raw_issue
            )
            assert claim_match is not None
            claim_code = claim_match.group(1).decode("ascii")

            status, _headers, device = _json_request(
                server,
                "POST",
                "/v1/device-authorizations",
                payload={
                    "client": "vscode-extension",
                    "extension_version": "0.2.0",
                    "device_name": f"Shared seat {index + 1:02d}",
                },
            )
            record(status)
            assert status == 201, device
            assert device["verification_uri"] == f"{PUBLIC_ORIGIN}/activate"

            status, _headers, accepted = _json_request(
                server,
                "POST",
                "/v1/assignment-claims/redeem",
                payload={
                    "claim_code": claim_code,
                    "device_code": device["device_code"],
                },
            )
            record(status)
            assert status == 200, accepted
            assert accepted["assignment_id"] == PRIMARY_ASSIGNMENT
            assert accepted["delivery_mode"] == "bundle"

            status, _headers, tokens = _json_request(
                server,
                "POST",
                "/v1/device-authorizations/token",
                payload={"device_code": device["device_code"]},
            )
            record(status)
            assert status == 200, tokens
            access_token = str(tokens["access_token"])

            status, _headers, me = _json_request(
                server, "GET", "/v1/me", token=access_token
            )
            record(status)
            assert status == 200, me
            assert me["student_key"] == students[index]

            status, _headers, assignments = _json_request(
                server, "GET", "/v1/assignments", token=access_token
            )
            record(status)
            assert status == 200, assignments
            visible = assignments["assignments"]
            assert isinstance(visible, list)
            assert [item["assignment_id"] for item in visible] == [PRIMARY_ASSIGNMENT]

            status, starter_headers, downloaded = _request(
                server,
                "GET",
                f"/v1/assignments/{PRIMARY_ASSIGNMENT}/starter",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            record(status)
            assert status == 200, downloaded
            assert starter_headers["content-type"] == "application/gzip"
            assert downloaded == starter.path.read_bytes()

            status, _headers, denied = _json_request(
                server,
                "GET",
                f"/v1/assignments/{OTHER_ASSIGNMENT}/starter",
                token=access_token,
            )
            record(status)
            assert status == 403, denied
            assert denied["error"]["code"] == "access_denied"
            return {
                "student_key": students[index],
                "acceptance_id": accepted["acceptance_id"],
                "access_token": access_token,
            }

        with ThreadPoolExecutor(max_workers=STUDENT_COUNT) as executor:
            outcomes = list(executor.map(claim_and_download, range(STUDENT_COUNT)))

        basic = base64.b64encode(
            f"instructor:{INSTRUCTOR_TOKEN}".encode("ascii")
        ).decode("ascii")
        status, _headers, dashboard = _json_request(
            server,
            "GET",
            "/v1/instructor/dashboard",
            headers={"Authorization": f"Basic {basic}"},
        )
        record(status)
        assert status == 200, dashboard

    assert len(outcomes) == STUDENT_COUNT
    assert len({item["acceptance_id"] for item in outcomes}) == STUDENT_COUNT
    assert all(status < 500 for status in observed_statuses)
    assert maximum_verifications == 4

    course = dashboard["course"]
    assert course == {
        "course_key": COURSE,
        "enrolled_students": STUDENT_COUNT,
        "active_students": STUDENT_COUNT,
        "assignments": 2,
        "acceptances": STUDENT_COUNT,
        "submissions": 0,
    }
    dashboard_students = dashboard["students"]
    assert len(dashboard_students) == STUDENT_COUNT
    assert all(student["password_configured"] for student in dashboard_students)
    assert all(student["assignments"] == 2 for student in dashboard_students)
    assert all(student["acceptances"] == 1 for student in dashboard_students)
    assert all(student["downloads"] == 1 for student in dashboard_students)
    assert all(student["submissions"] == 0 for student in dashboard_students)

    primary_rows = [
        row for row in dashboard["rows"]
        if row["assignment_id"] == PRIMARY_ASSIGNMENT
    ]
    other_rows = [
        row for row in dashboard["rows"]
        if row["assignment_id"] == OTHER_ASSIGNMENT
    ]
    assert len(primary_rows) == STUDENT_COUNT
    assert len(other_rows) == STUDENT_COUNT
    assert all(row["acceptance_count"] == 1 for row in primary_rows)
    assert all(row["download_count"] == 1 for row in primary_rows)
    assert all(row["acceptance_count"] == 0 for row in other_rows)
    assert all(row["download_count"] == 0 for row in other_rows)

    assert state.get_course_summary(course_key=COURSE)["acceptances"] == STUDENT_COUNT
    assert all(
        state.get_course_student_summary(
            course_key=COURSE, student_key=student_key
        )["acceptances"]
        == 1
        for student_key in students
    )
