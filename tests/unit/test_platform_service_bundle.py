from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import re

import pytest

from autograde.platform_bundle import BundleStore
from autograde.platform_service import PlatformAPIError, StudentPlatformService
from autograde.platform_state import PlatformStateStore


NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
COURSE = "cse101-2026f"
RUNNER = "registry.school/autograde@sha256:" + "e" * 64


def _connect(service: StudentPlatformService) -> str:
    issued = service.issue_student_activation(student_key="s001")
    authorization = service.create_device_authorization({"device_name": "VS Code / Linux"})
    page = service.activate_page({"user_code": authorization["user_code"]})
    match = re.search(r'name="csrf" value="([^"]+)"', str(page.body))
    assert match is not None
    cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    service.approve_activation(
        {
            "user_code": authorization["user_code"],
            "activation_code": issued["activation_code"],
            "csrf": match.group(1),
        },
        {"autograde_activation": cookie},
    )
    return str(
        service.exchange_device_authorization(
            {"device_code": authorization["device_code"]}
        )["access_token"]
    )


@pytest.fixture()
def bundle_platform(tmp_path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    student = state.upsert_local_student(
        student_key="s001", auth_subject="school:s001", at=NOW
    )
    state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)
    store = BundleStore(tmp_path / "bundles")
    starter_tree = tmp_path / "starter"
    starter_tree.mkdir()
    (starter_tree / "README.md").write_text("solve me\n", encoding="utf-8")
    starter = store.create_from_directory(starter_tree, kind="starter")
    state.register_bundle_assignment_release(
        assignment_id="basn_lab01",
        course_key=COURSE,
        assignment_key="lab01",
        release_id="lab01-v1",
        title="Lab <One>",
        starter_path=str(starter.path),
        starter_digest=starter.digest,
        starter_size_bytes=starter.compressed_bytes,
        assessment_path=str(tmp_path / "assessment"),
        assessment_digest="a" * 64,
        runner_image=RUNNER,
        rubric_version="v1",
        max_score=10,
        result_policy="immediate",
        opens_at=NOW - timedelta(minutes=1),
        due_at=NOW + timedelta(days=7),
        ready=True,
        at=NOW,
    )
    notifications: list[str] = []
    service = StudentPlatformService(
        state=state,
        server_secret=b"s" * 32,
        course_key=COURSE,
        public_base_url="http://127.0.0.1:8000",
        bundle_store=store,
        notify_bundle_submission=notifications.append,
        instructor_token="dashboard-secret",
        now=lambda: NOW,
    )
    token = _connect(service)
    submission_tree = tmp_path / "submission"
    submission_tree.mkdir()
    (submission_tree / "main.py").write_text("print(42)\n", encoding="utf-8")
    submission = store.create_from_directory(submission_tree, kind="submission")
    payload = submission.path.read_bytes()
    return state, store, service, token, notifications, starter, payload


def test_local_student_lists_downloads_and_submits_bundle(bundle_platform) -> None:
    state, _store, service, token, notifications, starter, payload = bundle_platform

    me = service.get_me(token)
    assert me["identity"] == {"kind": "local"}
    assert "github" not in me
    listed = service.list_assignments(token)["assignments"]
    assert listed[0]["delivery_mode"] == "bundle"
    assert listed[0]["starter"]["sha256"] == starter.digest
    assert "assessment" not in listed[0]

    download = service.get_bundle_starter(token, "basn_lab01")
    assert download.path == starter.path
    assert download.headers["X-Autograde-SHA256"] == starter.digest

    accepted = service.submit_bundle(
        token,
        "request-key-0001",
        "basn_lab01",
        BytesIO(payload),
        len(payload),
    )
    submission_id = accepted["submission"]["submission_id"]
    assert accepted["submission"]["state"] == "accepted"
    assert accepted["submission"]["source_sha256"].startswith("sha256:")
    assert notifications == [submission_id]
    assert service.get_submission(token, submission_id)["submission"]["state"] == "accepted"

    replay = service.submit_bundle(
        token,
        "request-key-0001",
        "basn_lab01",
        BytesIO(payload),
        len(payload),
    )
    assert replay["replayed"] is True
    assert replay["submission"]["submission_id"] == submission_id
    assert notifications == [submission_id]

    rows = state.list_bundle_dashboard_rows(course_key=COURSE)
    assert rows[0].download_count == 1
    assert rows[0].submission_count == 1


def test_bundle_upload_rejects_invalid_archive(bundle_platform) -> None:
    _state, _store, service, token, _notifications, _starter, _payload = bundle_platform

    with pytest.raises(PlatformAPIError) as caught:
        service.submit_bundle(
            token,
            "request-key-0002",
            "basn_lab01",
            BytesIO(b"not-a-gzip"),
            len(b"not-a-gzip"),
        )

    assert caught.value.status == 422
    assert caught.value.code == "invalid_bundle"


def test_dashboard_requires_basic_auth_and_escapes_content(bundle_platform) -> None:
    _state, _store, service, _token, _notifications, _starter, _payload = bundle_platform

    with pytest.raises(PlatformAPIError) as caught:
        service.instructor_dashboard("")
    assert caught.value.status == 401
    assert caught.value.headers["WWW-Authenticate"].startswith("Basic ")

    unicode_password = base64.b64encode("instructor:비밀번호".encode()).decode("ascii")
    with pytest.raises(PlatformAPIError) as unicode_denied:
        service.instructor_dashboard(f"Basic {unicode_password}")
    assert unicode_denied.value.status == 401

    encoded = base64.b64encode(b"instructor:dashboard-secret").decode("ascii")
    authorization = f"Basic {encoded}"
    dashboard = service.instructor_dashboard(authorization)
    assert dashboard["course_key"] == COURSE
    assert dashboard["rows"][0]["student_key"] == "s001"
    page = service.instructor_dashboard_page(authorization)
    assert "Lab &lt;One&gt;" in str(page.body)
    assert "<One>" not in str(page.body)


def test_unpublished_bundle_result_is_not_exposed(bundle_platform) -> None:
    _state, _store, service, token, _notifications, _starter, payload = bundle_platform
    accepted = service.submit_bundle(
        token,
        "request-key-0003",
        "basn_lab01",
        BytesIO(payload),
        len(payload),
    )

    with pytest.raises(PlatformAPIError) as caught:
        service.get_result(token, accepted["submission"]["submission_id"])

    assert caught.value.code == "result_not_available"
