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
    assert dashboard["course"] == {
        "course_key": COURSE,
        "enrolled_students": 1,
        "active_students": 1,
        "assignments": 1,
        "acceptances": 0,
        "submissions": 0,
    }
    assert dashboard["students"][0]["student_key"] == "s001"
    assert dashboard["students"][0]["password_configured"] is False
    assert dashboard["students"][0]["acceptances"] == 0
    assert dashboard["students"][0]["downloads"] == 0
    assert dashboard["students"][0]["submissions"] == 0
    assert dashboard["assignments"] == [
        {
            "assignment_id": "basn_lab01",
            "assignment_key": "lab01",
            "release_id": "lab01-v1",
            "title": "Lab <One>",
            "claim_url": (
                "http://127.0.0.1:8000/assignment-claim/basn_lab01"
            ),
        }
    ]
    assert dashboard["rows"][0]["student_key"] == "s001"
    assert dashboard["rows"][0]["acceptance_count"] == 0
    page = service.instructor_dashboard_page(authorization)
    assert "교과목 요약" in str(page.body)
    assert "학생 관리" in str(page.body)
    assert "전용 비밀번호" in str(page.body)
    assert "등록 학생 1명" in str(page.body)
    assert "Lab &lt;One&gt;" in str(page.body)
    assert "<One>" not in str(page.body)


def test_dashboard_counts_assignment_claim_acceptance(bundle_platform) -> None:
    _state, _store, service, _token, _notifications, _starter, _payload = (
        bundle_platform
    )
    password = "correct horse battery staple"
    service.set_student_password(student_key="s001", password=password)
    claim_page = service.assignment_claim_page({"assignment_id": "basn_lab01"})
    assert "Lab &lt;One&gt; (lab01)" in claim_page.body
    assert "Lab <One>" not in claim_page.body
    csrf = re.search(r'name="csrf" value="([^"]+)"', claim_page.body).group(1)
    cookie = claim_page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    issued = service.issue_assignment_claim(
        {
            "student_key": "s001",
            "password": password,
            "assignment_id": "basn_lab01",
            "csrf": csrf,
        },
        {"autograde_assignment_claim": cookie},
    )
    assert "Lab &lt;One&gt; (lab01)" in issued.body
    claim_code = re.search(r"<code>(AK1-[^<]+)</code>", issued.body).group(1)
    device = service.create_device_authorization({"device_name": "VS Code / WSL"})
    service.redeem_assignment_claim(
        {"claim_code": claim_code, "device_code": device["device_code"]}
    )

    encoded = base64.b64encode(b"instructor:dashboard-secret").decode("ascii")
    authorization = f"Basic {encoded}"
    dashboard = service.instructor_dashboard(authorization)
    assert dashboard["course"]["acceptances"] == 1
    assert dashboard["students"][0]["acceptances"] == 1
    assert dashboard["rows"][0]["acceptance_count"] == 1
    assert dashboard["rows"][0]["latest_claim_accepted_at"] == (
        "2026-09-02T12:00:00.000000Z"
    )
    assert "수락 1건" in service.instructor_dashboard_page(authorization).body


def test_dashboard_qr_list_excludes_unavailable_assignments(bundle_platform) -> None:
    state, _store, service, _token, _notifications, starter, _payload = bundle_platform
    for assignment_id, ready, opens_at, due_at in (
        (
            "basn_draft",
            False,
            NOW - timedelta(minutes=1),
            NOW + timedelta(days=1),
        ),
        (
            "basn_future",
            True,
            NOW + timedelta(days=1),
            NOW + timedelta(days=2),
        ),
        (
            "basn_closed",
            True,
            NOW - timedelta(days=2),
            NOW - timedelta(minutes=1),
        ),
    ):
        state.register_bundle_assignment_release(
            assignment_id=assignment_id,
            course_key=COURSE,
            assignment_key=assignment_id,
            release_id=f"{assignment_id}-v1",
            title=assignment_id,
            starter_path=str(starter.path),
            starter_digest=starter.digest,
            starter_size_bytes=starter.compressed_bytes,
            assessment_path="/private/assessment",
            assessment_digest="a" * 64,
            runner_image=RUNNER,
            rubric_version="v1",
            max_score=10,
            result_policy="immediate",
            opens_at=opens_at,
            due_at=due_at,
            ready=ready,
            at=NOW,
        )

    encoded = base64.b64encode(b"instructor:dashboard-secret").decode("ascii")
    dashboard = service.instructor_dashboard(f"Basic {encoded}")

    assert [item["assignment_id"] for item in dashboard["assignments"]] == [
        "basn_lab01"
    ]


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


def test_assignment_claim_session_is_restricted_to_its_accepted_bundle_assignment(
    bundle_platform,
) -> None:
    state, _store, service, course_token, _notifications, starter, payload = (
        bundle_platform
    )
    state.register_bundle_assignment_release(
        assignment_id="basn_lab02",
        course_key=COURSE,
        assignment_key="lab02",
        release_id="lab02-v1",
        title="Lab Two",
        starter_path=str(starter.path),
        starter_digest=starter.digest,
        starter_size_bytes=starter.compressed_bytes,
        assessment_path="/private/assessment-02",
        assessment_digest="b" * 64,
        runner_image=RUNNER,
        rubric_version="v1",
        max_score=10,
        result_policy="immediate",
        opens_at=NOW - timedelta(minutes=1),
        due_at=NOW + timedelta(days=7),
        ready=True,
        at=NOW,
    )

    other = service.submit_bundle(
        course_token,
        "claim-scope-other",
        "basn_lab02",
        BytesIO(payload),
        len(payload),
    )
    other_submission_id = other["submission"]["submission_id"]
    state.transition_bundle_submission(other_submission_id, "queued", at=NOW)
    state.transition_bundle_submission(other_submission_id, "running", at=NOW)
    state.record_bundle_graded_result(
        other_submission_id,
        result_id="claim-scope-other-result",
        score=8,
        max_score=10,
        at=NOW + timedelta(minutes=1),
    )
    state.publish_bundle_result(
        other_submission_id, at=NOW + timedelta(minutes=2)
    )

    password = "correct horse battery staple"
    service.set_student_password(student_key="s001", password=password)
    claim_page = service.assignment_claim_page({"assignment_id": "basn_lab01"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', claim_page.body).group(1)
    cookie = claim_page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    issued = service.issue_assignment_claim(
        {
            "student_key": "s001",
            "password": password,
            "assignment_id": "basn_lab01",
            "csrf": csrf,
        },
        {"autograde_assignment_claim": cookie},
    )
    claim_code = re.search(r"<code>(AK1-[^<]+)</code>", issued.body).group(1)
    device = service.create_device_authorization({"device_name": "VS Code / WSL"})
    service.redeem_assignment_claim(
        {"claim_code": claim_code, "device_code": device["device_code"]}
    )
    claim_token = service.exchange_device_authorization(
        {"device_code": device["device_code"]}
    )["access_token"]

    assert [
        assignment["assignment_id"]
        for assignment in service.list_assignments(claim_token)["assignments"]
    ] == ["basn_lab01"]

    accepted = service.submit_bundle(
        claim_token,
        "claim-scope-primary",
        "basn_lab01",
        BytesIO(payload),
        len(payload),
    )
    primary_submission_id = accepted["submission"]["submission_id"]
    state.transition_bundle_submission(primary_submission_id, "queued", at=NOW)
    state.transition_bundle_submission(primary_submission_id, "running", at=NOW)
    state.record_bundle_graded_result(
        primary_submission_id,
        result_id="claim-scope-primary-result",
        score=9,
        max_score=10,
        at=NOW + timedelta(minutes=1),
    )
    state.publish_bundle_result(
        primary_submission_id, at=NOW + timedelta(minutes=2)
    )

    assert service.get_submission(claim_token, primary_submission_id)["submission"][
        "state"
    ] == "published"
    assert service.get_result(claim_token, primary_submission_id)["result"][
        "score"
    ] == 9

    with pytest.raises(PlatformAPIError) as submit_denied:
        service.submit_bundle(
            claim_token,
            "claim-scope-denied",
            "basn_lab02",
            BytesIO(payload),
            len(payload),
        )
    assert submit_denied.value.status == 403
    assert submit_denied.value.code == "access_denied"

    with pytest.raises(PlatformAPIError) as submission_denied:
        service.get_submission(claim_token, other_submission_id)
    assert submission_denied.value.status == 403
    assert submission_denied.value.code == "access_denied"

    with pytest.raises(PlatformAPIError) as result_denied:
        service.get_result(claim_token, other_submission_id)
    assert result_denied.value.status == 403
    assert result_denied.value.code == "access_denied"
