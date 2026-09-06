from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

import autograde.platform_service as platform_service
from autograde.platform_auth import secret_digest
from autograde.platform_service import (
    PlatformAPIError,
    StudentPlatformService,
    SubmissionPolicy,
    TokenPolicy,
)
from autograde.platform_pinner import (
    PinnedSubmission,
    SubmissionSourceInvalid,
    SubmissionSourceUnavailable,
)
from autograde.platform_github import GitHubIdentity
from autograde.platform_state import (
    DeviceAuthorizationState,
    PlatformAccessDenied,
    PlatformConflict,
    PlatformStateStore,
    StudentActivationState,
)


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
COURSE = "cse101-2026f"
ASSESSMENT_DIGEST = "a" * 64
RUNNER = "ghcr.io/example/autograde@sha256:" + "b" * 64
SHA = "c" * 40


class Clock:
    def __init__(self) -> None:
        self.value = NOW
        self.monotonic_value = 100.0

    def now(self):
        return self.value

    def monotonic(self):
        return self.monotonic_value


class FakePinner:
    def __init__(self, tmp_path, clock: Clock) -> None:
        self.tmp_path = tmp_path
        self.clock = clock
        self.calls = 0
        self.advance = timedelta(0)
        self.unavailable = False
        self.invalid: SubmissionSourceInvalid | None = None
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def pin(self, *, assignment, requested_sha, submission_id):
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(5):
            raise AssertionError("test did not release pinner")
        if self.unavailable:
            raise SubmissionSourceUnavailable("not pushed")
        if self.invalid is not None:
            raise self.invalid
        self.clock.value += self.advance
        archive = self.tmp_path / f"{submission_id}.tar.gz"
        archive.write_bytes(b"snapshot")
        return PinnedSubmission(
            commit_sha=requested_sha,
            source_path=str(archive),
            source_digest="f" * 64,
            snapshot_key=f"refs/autograde/snapshots/{submission_id}",
        )


@pytest.fixture()
def platform(tmp_path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    student = state.upsert_student(
        student_key="20260001",
        auth_subject="github:101",
        github_user_id=101,
        github_login="student-one",
        at=NOW,
    )
    state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)
    clock = Clock()
    notifications: list[str] = []
    pinner = FakePinner(tmp_path, clock)
    service = StudentPlatformService(
        state=state,
        server_secret=b"s" * 32,
        course_key=COURSE,
        public_base_url="http://127.0.0.1:8000",
        token_policy=TokenPolicy(
            device_lifetime_seconds=300,
            poll_interval_seconds=5,
            access_lifetime_seconds=900,
            refresh_lifetime_seconds=3600,
        ),
        notify_submission=notifications.append,
        submission_pinner=pinner,
        now=clock.now,
        monotonic=clock.monotonic,
    )
    return state, service, clock, notifications, student


def sign_in(service: StudentPlatformService):
    authorization = service.create_device_authorization(
        {
            "client": "vscode-extension",
            "extension_version": "0.1.0",
            "device_name": "DESKTOP / wsl",
        }
    )
    service.approve_user_code(user_code=authorization["user_code"], github_user_id=101)
    tokens = service.exchange_device_authorization(
        {"device_code": authorization["device_code"]}
    )
    return authorization, tokens


def activation_entry(service: StudentPlatformService, user_code: str):
    page = service.activate_page({"user_code": user_code})
    csrf_match = re.search(r'name="csrf" value="([^"]+)"', page.body)
    assert csrf_match is not None
    cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    return (
        page,
        {"user_code": user_code, "csrf": csrf_match.group(1)},
        {"autograde_activation": cookie},
    )


@pytest.mark.parametrize(
    ("notification_outcome", "expected_event"),
    [
        ("exception", "submission_notify_failed"),
        ("rejected", "submission_notify_rejected"),
    ],
)
def test_submission_notification_failure_is_logged_without_losing_durable_acceptance(
    platform,
    monkeypatch,
    notification_outcome,
    expected_event,
) -> None:
    state, service, _clock, _notifications, student = platform
    register_assignment(state, student)
    _authorization, tokens = sign_in(service)
    events = []

    def notify(_submission_id):
        if notification_outcome == "exception":
            raise RuntimeError("queue-secret-must-not-be-logged")
        return False

    service.notify_submission = notify
    monkeypatch.setattr(
        platform_service,
        "emit_operator_event",
        lambda event, **fields: events.append((event, fields)),
    )

    response = service.submit(
        tokens["access_token"],
        f"notify-{notification_outcome}",
        {
            "assignment_id": "asn_lab01",
            "github_repository_id": 9001,
            "head_sha": SHA,
        },
    )

    submission_id = response["submission"]["submission_id"]
    assert response["submission"]["state"] == "accepted"
    assert state.get_submission(submission_id).state.value == "accepted"
    assert len(events) == 1
    event, fields = events[0]
    assert event == expected_event
    assert fields["component"] == "worker"
    assert fields["submission_id"] == submission_id
    if notification_outcome == "exception":
        assert isinstance(fields["exception"], RuntimeError)
    else:
        assert "exception" not in fields


def register_assignment(state, student, *, due_at=None):
    return state.register_assignment(
        assignment_id="asn_lab01",
        student_id=student.id,
        course_key=COURSE,
        assignment_key="lab01",
        release_id="lab01-v1",
        github_repository_id=9001,
        repository_owner="school",
        repository_name="lab01-student-one",
        clone_url="https://github.com/school/lab01-student-one.git",
        submission_mode="branch",
        target_ref="main",
        result_policy="immediate",
        assessment_path="/srv/autograde/lab01",
        assessment_digest=ASSESSMENT_DIGEST,
        runner_image=RUNNER,
        rubric_version="v1",
        max_score=10,
        opens_at=NOW - timedelta(minutes=1),
        due_at=due_at or NOW + timedelta(days=7),
        ready=True,
        at=NOW,
    )


def test_device_pairing_pending_slowdown_and_one_time_exchange(platform) -> None:
    _state, service, clock, _notifications, _student = platform
    authorization = service.create_device_authorization({"device_name": "Ubuntu WSL2"})

    with pytest.raises(PlatformAPIError) as pending:
        service.exchange_device_authorization({"device_code": authorization["device_code"]})
    assert pending.value.code == "authorization_pending"
    with pytest.raises(PlatformAPIError) as fast:
        service.exchange_device_authorization({"device_code": authorization["device_code"]})
    assert fast.value.code == "slow_down"

    service.approve_user_code(user_code=authorization["user_code"], github_user_id=101)
    tokens = service.exchange_device_authorization({"device_code": authorization["device_code"]})
    assert tokens["token_type"] == "Bearer"
    assert service.get_me(tokens["access_token"])["github"]["id"] == 101
    with pytest.raises(PlatformAPIError) as replay:
        service.exchange_device_authorization({"device_code": authorization["device_code"]})
    assert replay.value.code == "invalid_grant"


def test_oauth_free_student_activation_approves_device_without_secret_persistence(
    platform,
) -> None:
    state, service, _clock, _notifications, _student = platform
    issued = service.issue_student_activation(student_key="20260001")
    authorization = service.create_device_authorization({"device_name": "Ubuntu WSL2"})

    page, confirmation, cookies = activation_entry(
        service, authorization["user_code"]
    )
    assert page.status == 200
    assert "학생 활성화 코드" in page.body
    assert "Ubuntu WSL2" in page.body
    assert "GitHub로 로그인" not in page.body
    assert issued["activation_code"] not in page.body
    assert issued["activation_code"] not in page.headers["Set-Cookie"]
    assert "HttpOnly" in page.headers["Set-Cookie"]
    assert "SameSite=Lax" in page.headers["Set-Cookie"]

    approved = service.approve_activation(
        {
            **confirmation,
            "activation_code": issued["activation_code"].lower(),
        },
        cookies,
    )
    assert approved.status == 200
    assert issued["activation_code"] not in approved.body
    assert issued["activation_code"] not in approved.headers.get("Set-Cookie", "")

    tokens = service.exchange_device_authorization(
        {"device_code": authorization["device_code"]}
    )
    assert service.get_me(tokens["access_token"])["student_key"] == "20260001"
    persisted = b"".join(
        path.read_bytes() for path in state.database.parent.glob("state.sqlite3*")
    )
    assert issued["activation_code"].encode("ascii") not in persisted


def test_each_one_time_code_login_revokes_the_previous_shared_seat_session(
    platform,
) -> None:
    _state, service, _clock, _notifications, _student = platform

    first_code = service.issue_student_activation(student_key="20260001")
    first_device = service.create_device_authorization({"device_name": "Lab seat 01"})
    _page, first_form, first_cookies = activation_entry(
        service, first_device["user_code"]
    )
    service.approve_activation(
        {**first_form, "activation_code": first_code["activation_code"]},
        first_cookies,
    )
    first_tokens = service.exchange_device_authorization(
        {"device_code": first_device["device_code"]}
    )

    second_code = service.issue_student_activation(student_key="20260001")
    second_device = service.create_device_authorization({"device_name": "Lab seat 19"})
    _page, second_form, second_cookies = activation_entry(
        service, second_device["user_code"]
    )
    service.approve_activation(
        {**second_form, "activation_code": second_code["activation_code"]},
        second_cookies,
    )
    second_tokens = service.exchange_device_authorization(
        {"device_code": second_device["device_code"]}
    )

    with pytest.raises(PlatformAPIError) as old_access:
        service.get_me(first_tokens["access_token"])
    assert old_access.value.code == "invalid_token"
    with pytest.raises(PlatformAPIError) as old_refresh:
        service.refresh_tokens({"refresh_token": first_tokens["refresh_token"]})
    assert old_refresh.value.code == "invalid_grant"
    assert service.get_me(second_tokens["access_token"])["student_key"] == "20260001"


def test_student_activation_reissue_replay_and_invalid_values_share_safe_error(
    platform,
) -> None:
    _state, service, _clock, _notifications, _student = platform
    replaced = service.issue_student_activation(student_key="20260001")
    active = service.issue_student_activation(student_key="20260001")

    for candidate in (replaced["activation_code"], "AG1-" + "2" * 26, "bad"):
        authorization = service.create_device_authorization({"device_name": "WSL attempt"})
        _page, confirmation, cookies = activation_entry(
            service, authorization["user_code"]
        )
        with pytest.raises(PlatformAPIError) as denied:
            service.approve_activation(
                {
                    **confirmation,
                    "activation_code": candidate,
                },
                cookies,
            )
        assert denied.value.status == 403
        assert denied.value.code == "activation_denied"
        assert denied.value.safe_message == "student activation could not be completed"
        assert candidate not in denied.value.safe_message

    authorization = service.create_device_authorization({"device_name": "WSL success"})
    _page, confirmation, cookies = activation_entry(
        service, authorization["user_code"]
    )
    service.approve_activation(
        {
            **confirmation,
            "activation_code": active["activation_code"],
        },
        cookies,
    )
    replay_device = service.create_device_authorization({"device_name": "WSL replay"})
    _page, replay_confirmation, replay_cookies = activation_entry(
        service, replay_device["user_code"]
    )
    with pytest.raises(PlatformAPIError) as replay:
        service.approve_activation(
            {
                **replay_confirmation,
                "activation_code": active["activation_code"],
            },
            replay_cookies,
        )
    assert replay.value.code == "activation_denied"


def test_student_activation_form_requires_signed_device_binding_and_csrf(
    platform,
) -> None:
    state, service, _clock, _notifications, _student = platform
    issued = service.issue_student_activation(student_key="20260001")
    authorization = service.create_device_authorization({"device_name": "Victim WSL2"})
    other_authorization = service.create_device_authorization(
        {"device_name": "Other WSL2"}
    )
    _page, confirmation, cookies = activation_entry(
        service, authorization["user_code"]
    )

    for bad_form, bad_cookies in (
        (
            {**confirmation, "activation_code": issued["activation_code"]},
            {},
        ),
        (
            {
                **confirmation,
                "csrf": "wrong-csrf",
                "activation_code": issued["activation_code"],
            },
            cookies,
        ),
        (
            {
                "user_code": authorization["user_code"],
                "activation_code": issued["activation_code"],
            },
            cookies,
        ),
        (
            {
                **confirmation,
                "user_code": other_authorization["user_code"],
                "activation_code": issued["activation_code"],
            },
            cookies,
        ),
    ):
        with pytest.raises(PlatformAPIError) as denied:
            service.approve_activation(bad_form, bad_cookies)
        assert denied.value.status == 403
        assert denied.value.code == "activation_denied"

    for pending in (authorization, other_authorization):
        device = state.get_device_authorization_by_user_code_hmac(
            secret_digest(b"s" * 32, "user-code", pending["user_code"]),
            course_key=COURSE,
        )
        assert device.state == DeviceAuthorizationState.PENDING
        assert device.activation_failed_attempts == 0
    assert state.get_student_activation(
        issued["activation_id"], course_key=COURSE
    ).state == StudentActivationState.ISSUED

    service.approve_activation(
        {**confirmation, "activation_code": issued["activation_code"]},
        cookies,
    )
    tokens = service.exchange_device_authorization(
        {"device_code": authorization["device_code"]}
    )
    assert service.get_me(tokens["access_token"])["student_key"] == "20260001"


def test_student_activation_issuance_requires_active_enrollment_and_bounded_lifetime(
    platform,
) -> None:
    state, service, _clock, _notifications, student = platform
    with pytest.raises(ValueError, match="30 days"):
        service.issue_student_activation(
            student_key=student.student_key,
            lifetime_seconds=31 * 24 * 60 * 60,
        )

    state.upsert_enrollment(
        student_id=student.id,
        course_key=COURSE,
        active=False,
        at=NOW,
    )
    with pytest.raises(PlatformAccessDenied, match="active student"):
        service.issue_student_activation(student_key=student.student_key)


def test_device_authorization_quota_is_a_safe_retryable_error(
    platform, monkeypatch
) -> None:
    state, service, _clock, _notifications, _student = platform

    def reject_authorization(**_kwargs):
        raise PlatformConflict("internal quota detail")

    monkeypatch.setattr(state, "create_device_authorization", reject_authorization)

    with pytest.raises(PlatformAPIError) as rejected:
        service.create_device_authorization({"device_name": "Ubuntu WSL2"})

    assert rejected.value.status == 429
    assert rejected.value.code == "temporarily_unavailable"
    assert rejected.value.headers == {"Retry-After": "5"}
    assert "internal quota detail" not in rejected.value.safe_message


def test_device_poll_cadence_entries_expire_and_remain_bounded(platform) -> None:
    _state, service, clock, _notifications, _student = platform
    authorization = service.create_device_authorization({"device_name": "Ubuntu WSL2"})
    with pytest.raises(PlatformAPIError, match="pending"):
        service.exchange_device_authorization({"device_code": authorization["device_code"]})
    assert len(service._next_poll) == 1

    clock.value += timedelta(seconds=301)
    clock.monotonic_value += 301
    service.create_device_authorization({"device_name": "replacement"})
    assert service._next_poll == {}

    service._next_poll.update(
        {
            f"verifier-{index}": (clock.monotonic_value + 5, clock.monotonic_value + 300)
            for index in range(2_000)
        }
    )
    assert service._poll_is_too_fast("new-verifier", 5, 300) is False
    assert len(service._next_poll) == 2_000
    assert "new-verifier" in service._next_poll


def test_students_can_list_and_revoke_only_their_sessions(platform) -> None:
    state, service, _clock, _notifications, _student = platform
    _authorization, first = sign_in(service)
    _authorization, second = sign_in(service)

    other_student = state.upsert_student(
        student_key="20260002",
        auth_subject="github:202",
        github_user_id=202,
        github_login="student-two",
        at=NOW,
    )
    state.upsert_enrollment(student_id=other_student.id, course_key=COURSE, at=NOW)
    other_authorization = service.create_device_authorization({"device_name": "other WSL"})
    service.approve_user_code(
        user_code=other_authorization["user_code"], github_user_id=202
    )
    other = service.exchange_device_authorization(
        {"device_code": other_authorization["device_code"]}
    )

    sessions = service.list_sessions(second["access_token"])["sessions"]
    assert len(sessions) == 2
    current = next(session for session in sessions if session["current"])
    previous = next(session for session in sessions if not session["current"])
    assert current["device_label"] == "DESKTOP / wsl"
    assert "token_family_id" not in current

    other_session = service.list_sessions(other["access_token"])["sessions"][0]
    with pytest.raises(PlatformAPIError) as foreign:
        service.revoke_session(second["access_token"], other_session["session_id"])
    assert foreign.value.status == 404
    assert service.get_me(other["access_token"])["student_key"] == "20260002"

    service.revoke_session(second["access_token"], previous["session_id"])
    with pytest.raises(PlatformAPIError) as revoked:
        service.get_me(first["access_token"])
    assert revoked.value.status == 401

    with pytest.raises(PlatformAPIError) as missing:
        service.revoke_session(second["access_token"], "ses_missing")
    assert missing.value.status == 404


def test_latest_login_replaces_abandoned_shared_seat_session(
    platform,
) -> None:
    _state, service, _clock, _notifications, _student = platform
    issued = [sign_in(service)[1] for _index in range(5)]

    for superseded in issued[:-1]:
        with pytest.raises(PlatformAPIError) as invalid:
            service.get_me(superseded["access_token"])
        assert invalid.value.code == "invalid_token"
    assert service.get_me(issued[-1]["access_token"])["student_key"] == "20260001"

    replacement_authorization = service.create_device_authorization(
        {"device_name": "replacement WSL"}
    )
    service.approve_user_code(
        user_code=replacement_authorization["user_code"], github_user_id=101
    )
    replacement = service.exchange_device_authorization(
        {"device_code": replacement_authorization["device_code"]}
    )

    listed = service.list_student_sessions(student_key="20260001")
    assert listed["count"] == 6
    assert sum(item["active"] for item in listed["sessions"]) == 1
    assert "access_token" not in json.dumps(listed)
    assert "refresh_token" not in json.dumps(listed)
    with pytest.raises(PlatformAPIError) as superseded:
        service.get_me(issued[-1]["access_token"])
    assert superseded.value.code == "invalid_token"

    reset = service.reset_student_sessions(student_key="20260001")
    assert reset["revoked"] == 1
    with pytest.raises(PlatformAPIError) as reset_access:
        service.get_me(replacement["access_token"])
    assert reset_access.value.code == "invalid_token"

    _authorization, recovered = sign_in(service)
    assert service.get_me(recovered["access_token"])["student_key"] == "20260001"


def test_one_student_enrolled_in_two_courses_cannot_cross_session_scope(
    platform, tmp_path
) -> None:
    state, course_a, clock, _notifications, student = platform
    course_b_key = "cse202-2026f"
    state.upsert_enrollment(
        student_id=student.id, course_key=course_b_key, at=NOW
    )
    course_b = StudentPlatformService(
        state=state,
        server_secret=b"s" * 32,
        course_key=course_b_key,
        public_base_url="http://127.0.0.1:8001",
        token_policy=course_a.policy,
        submission_pinner=FakePinner(tmp_path, clock),
        now=clock.now,
        monotonic=clock.monotonic,
    )
    register_assignment(state, student)
    _authorization, token_a = sign_in(course_a)
    _authorization, token_b = sign_in(course_b)

    assert len(course_a.list_sessions(token_a["access_token"])["sessions"]) == 1
    assert len(course_b.list_sessions(token_b["access_token"])["sessions"]) == 1
    session_a = course_a.list_sessions(token_a["access_token"])["sessions"][0]

    for operation in (
        lambda: course_b.get_me(token_a["access_token"]),
        lambda: course_b.list_assignments(token_a["access_token"]),
        lambda: course_b.refresh_tokens({"refresh_token": token_a["refresh_token"]}),
    ):
        with pytest.raises(PlatformAPIError) as denied:
            operation()
        assert denied.value.status == 401

    with pytest.raises(PlatformAPIError) as foreign_session:
        course_b.revoke_session(token_b["access_token"], session_a["session_id"])
    assert foreign_session.value.status == 404
    with pytest.raises(PlatformAPIError) as foreign_assignment:
        course_b.get_assignment_repository(token_b["access_token"], "asn_lab01")
    assert foreign_assignment.value.status == 404

    submitted = course_a.submit(
        token_a["access_token"],
        "course-scope-request",
        {
            "assignment_id": "asn_lab01",
            "github_repository_id": 9001,
            "head_sha": SHA,
        },
    )["submission"]
    state.transition_submission(submitted["submission_id"], "queued", at=NOW)
    state.transition_submission(submitted["submission_id"], "running", at=NOW)
    state.record_graded_result(
        submitted["submission_id"],
        result_id="res_course_scope",
        score=10,
        max_score=10,
        at=NOW,
    )
    state.publish_result(submitted["submission_id"], at=NOW)
    with pytest.raises(PlatformAPIError) as foreign_submission:
        course_b.get_submission(token_b["access_token"], submitted["submission_id"])
    assert foreign_submission.value.status == 404
    with pytest.raises(PlatformAPIError) as foreign_result:
        course_b.get_result(token_b["access_token"], submitted["submission_id"])
    assert foreign_result.value.status == 404

    # A failed cross-course refresh did not mutate the originating family.
    replacement_a = course_a.refresh_tokens(
        {"refresh_token": token_a["refresh_token"]}
    )
    assert replacement_a["token_type"] == "Bearer"

    # Withdrawing from A revokes only A credentials, and re-enrollment must not
    # revive them.  The independently issued B session remains valid.
    state.upsert_enrollment(
        student_id=student.id,
        course_key=COURSE,
        active=False,
        at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(PlatformAPIError) as inactive_a:
        course_a.get_me(replacement_a["access_token"])
    assert inactive_a.value.status == 401
    assert course_b.get_me(token_b["access_token"])["course_key"] == course_b_key
    state.upsert_enrollment(
        student_id=student.id,
        course_key=COURSE,
        active=True,
        at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(PlatformAPIError) as still_revoked_a:
        course_a.get_me(replacement_a["access_token"])
    assert still_revoked_a.value.status == 401


def test_concurrent_device_exchanges_leave_exactly_one_active_session(
    platform, tmp_path
) -> None:
    state, _service, clock, _notifications, _student = platform
    service = StudentPlatformService(
        state=state,
        server_secret=b"s" * 32,
        course_key=COURSE,
        public_base_url="http://127.0.0.1:8000",
        token_policy=TokenPolicy(
            access_lifetime_seconds=900,
            refresh_lifetime_seconds=3600,
            max_active_sessions=1,
            max_daily_session_issuances=10,
            max_retained_sessions=10,
        ),
        submission_pinner=FakePinner(tmp_path, clock),
        now=clock.now,
        monotonic=clock.monotonic,
    )
    authorizations = [
        service.create_device_authorization({"device_name": f"WSL {index}"})
        for index in range(2)
    ]
    for authorization in authorizations:
        service.approve_user_code(
            user_code=authorization["user_code"], github_user_id=101
        )

    barrier = threading.Barrier(3)
    outcomes: list[object] = []

    def exchange(device_code: str) -> None:
        barrier.wait()
        try:
            outcomes.append(
                service.exchange_device_authorization({"device_code": device_code})
            )
        except PlatformAPIError as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=exchange, args=(item["device_code"],))
        for item in authorizations
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(outcomes) == 2
    assert all(isinstance(item, dict) for item in outcomes)
    valid = 0
    for item in outcomes:
        try:
            service.get_me(item["access_token"])
        except PlatformAPIError as invalid:
            assert invalid.code == "invalid_token"
        else:
            valid += 1
    assert valid == 1


def test_overlapping_refresh_requests_share_one_rotation_but_later_reuse_revokes(
    platform, monkeypatch
) -> None:
    state, service, _clock, _notifications, _student = platform
    _authorization, original_tokens = sign_in(service)
    original_rotate = state.rotate_refresh_token
    entered = threading.Event()
    release = threading.Event()

    def blocked_rotate(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_rotate(**kwargs)

    monkeypatch.setattr(state, "rotate_refresh_token", blocked_rotate)
    results: list[dict] = []
    failures: list[BaseException] = []

    def refresh() -> None:
        try:
            results.append(
                dict(
                    service.refresh_tokens(
                        {"refresh_token": original_tokens["refresh_token"]}
                    )
                )
            )
        except BaseException as exc:
            failures.append(exc)

    leader = threading.Thread(target=refresh)
    leader.start()
    assert entered.wait(timeout=5)
    follower = threading.Thread(target=refresh)
    follower.start()
    for _attempt in range(1_000):
        with service._refresh_flights_lock:
            flights = tuple(service._refresh_flights.values())
            if flights and flights[0].followers == 1:
                break
        threading.Event().wait(0.001)
    else:
        raise AssertionError("second refresh did not join the active single flight")
    release.set()
    leader.join(timeout=5)
    follower.join(timeout=5)

    assert not leader.is_alive() and not follower.is_alive()
    assert failures == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert service.get_me(results[0]["access_token"])["student_key"] == "20260001"
    assert service._refresh_flights == {}

    # Once the overlapping flight is complete, the old credential is no longer
    # coalesced. Its reuse remains a compromise signal and revokes the winner.
    with pytest.raises(PlatformAPIError) as reuse:
        service.refresh_tokens({"refresh_token": original_tokens["refresh_token"]})
    assert reuse.value.code == "invalid_grant"
    with pytest.raises(PlatformAPIError) as compromised:
        service.get_me(results[0]["access_token"])
    assert compromised.value.code == "invalid_token"


def test_refresh_rotation_cannot_extend_the_absolute_lab_session(platform) -> None:
    _state, service, clock, _notifications, _student = platform
    assert TokenPolicy().refresh_lifetime_seconds == 4 * 60 * 60
    _authorization, original = sign_in(service)

    clock.value += timedelta(minutes=10)
    first_rotation = service.refresh_tokens(
        {"refresh_token": original["refresh_token"]}
    )
    assert first_rotation["expires_in"] == 900

    clock.value += timedelta(minutes=40)
    final_rotation = service.refresh_tokens(
        {"refresh_token": first_rotation["refresh_token"]}
    )
    assert final_rotation["expires_in"] == 600

    clock.value += timedelta(minutes=9)
    assert service.get_me(final_rotation["access_token"])["student_key"] == "20260001"
    clock.value += timedelta(minutes=1)
    with pytest.raises(PlatformAPIError) as expired_access:
        service.get_me(final_rotation["access_token"])
    assert expired_access.value.code == "invalid_token"
    with pytest.raises(PlatformAPIError) as expired_refresh:
        service.refresh_tokens({"refresh_token": final_rotation["refresh_token"]})
    assert expired_refresh.value.code == "invalid_grant"


def test_session_history_and_refresh_history_are_bounded_and_gc_safe(
    platform, tmp_path
) -> None:
    state, _service, clock, _notifications, _student = platform
    service = StudentPlatformService(
        state=state,
        server_secret=b"s" * 32,
        course_key=COURSE,
        public_base_url="http://127.0.0.1:8000",
        token_policy=TokenPolicy(
            access_lifetime_seconds=900,
            refresh_lifetime_seconds=3600,
            max_active_sessions=1,
            max_daily_session_issuances=10,
            max_retained_sessions=3,
            max_refresh_rotations=1,
            session_list_limit=1,
            auth_history_retention_seconds=60,
        ),
        submission_pinner=FakePinner(tmp_path, clock),
        now=clock.now,
        monotonic=clock.monotonic,
    )
    _authorization, first = sign_in(service)
    rotated = service.refresh_tokens({"refresh_token": first["refresh_token"]})
    with pytest.raises(PlatformAPIError) as refresh_limited:
        service.refresh_tokens({"refresh_token": rotated["refresh_token"]})
    assert refresh_limited.value.code == "reauthentication_required"
    with sqlite3.connect(state.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_refresh_tokens"
        ).fetchone()[0] == 2

    _authorization, second = sign_in(service)
    service.revoke_current(second["access_token"])
    _authorization, third = sign_in(service)
    service.revoke_current(third["access_token"])
    pending = service.create_device_authorization({"device_name": "history cap"})
    service.approve_user_code(user_code=pending["user_code"], github_user_id=101)
    with pytest.raises(PlatformAPIError) as history_limited:
        service.exchange_device_authorization({"device_code": pending["device_code"]})
    assert history_limited.value.code == "session_limit_exceeded"

    clock.value += timedelta(hours=2)
    clock.monotonic_value += 2 * 60 * 60
    _authorization, replacement = sign_in(service)
    listed = service.list_sessions(replacement["access_token"])["sessions"]
    assert len(listed) == 1
    assert listed[0]["current"] is True
    with sqlite3.connect(state.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_sessions WHERE course_key = ?",
            (COURSE,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_refresh_tokens"
        ).fetchone()[0] == 1


def test_daily_session_issuance_budget_counts_repeated_pairing(platform, tmp_path) -> None:
    state, _service, clock, _notifications, _student = platform
    service = StudentPlatformService(
        state=state,
        server_secret=b"s" * 32,
        course_key=COURSE,
        public_base_url="http://127.0.0.1:8000",
        token_policy=TokenPolicy(
            access_lifetime_seconds=900,
            refresh_lifetime_seconds=3600,
            max_active_sessions=2,
            max_daily_session_issuances=2,
            max_retained_sessions=10,
        ),
        submission_pinner=FakePinner(tmp_path, clock),
        now=clock.now,
        monotonic=clock.monotonic,
    )
    for _index in range(2):
        _authorization, tokens = sign_in(service)
        service.revoke_current(tokens["access_token"])

    authorization = service.create_device_authorization({"device_name": "third WSL"})
    service.approve_user_code(user_code=authorization["user_code"], github_user_id=101)
    with pytest.raises(PlatformAPIError) as limited:
        service.exchange_device_authorization(
            {"device_code": authorization["device_code"]}
        )
    assert limited.value.status == 429
    assert limited.value.code == "session_limit_exceeded"


def test_assignments_submission_idempotency_refresh_and_revocation(platform) -> None:
    state, service, _clock, notifications, student = platform
    register_assignment(state, student)
    _authorization, tokens = sign_in(service)

    assignments = service.list_assignments(tokens["access_token"])["assignments"]
    assert assignments[0]["assignment_id"] == "asn_lab01"
    assert assignments[0]["assignment_path"] == "."
    assert assignments[0]["repository"]["github_repository_id"] == 9001
    payload = {
        "assignment_id": "asn_lab01",
        "github_repository_id": 9001,
        "head_sha": SHA,
    }
    first = service.submit(tokens["access_token"], "request-1", payload)
    replay = service.submit(tokens["access_token"], "request-1", payload)
    semantic = service.submit(tokens["access_token"], "request-2", payload)
    assert replay["submission"]["submission_id"] == first["submission"]["submission_id"]
    assert replay["replayed"] is True
    assert semantic["submission"]["submission_id"] == first["submission"]["submission_id"]
    assert semantic["replayed"] is True
    assert service.submission_pinner.calls == 1
    assert notifications == [first["submission"]["submission_id"]]

    changed = dict(payload, head_sha="d" * 40)
    with pytest.raises(PlatformAPIError) as conflict:
        service.submit(tokens["access_token"], "request-1", changed)
    assert conflict.value.status == 409
    with pytest.raises(PlatformAPIError) as repository_conflict:
        service.submit(
            tokens["access_token"],
            "request-1",
            dict(payload, github_repository_id=9999),
        )
    assert repository_conflict.value.status == 409

    replacement = service.refresh_tokens({"refresh_token": tokens["refresh_token"]})
    with pytest.raises(PlatformAPIError) as old_access:
        service.get_me(tokens["access_token"])
    assert old_access.value.status == 401
    service.revoke_current(replacement["access_token"])
    with pytest.raises(PlatformAPIError):
        service.get_me(replacement["access_token"])


def test_submission_pin_completion_is_authoritative_for_deadline(platform) -> None:
    state, service, clock, _notifications, student = platform
    register_assignment(state, student, due_at=NOW + timedelta(seconds=5))
    _authorization, tokens = sign_in(service)
    service.submission_pinner.advance = timedelta(seconds=6)

    with pytest.raises(PlatformAPIError) as closed:
        service.submit(
            tokens["access_token"],
            "slow-pin",
            {
                "assignment_id": "asn_lab01",
                "github_repository_id": 9001,
                "head_sha": SHA,
            },
        )

    assert closed.value.code == "assignment_closed"
    assert state.list_submissions_for_processing(course_key=COURSE) == []


def test_exact_and_semantic_replays_survive_deadline_and_branch_change(platform) -> None:
    state, service, clock, _notifications, student = platform
    register_assignment(state, student, due_at=NOW + timedelta(minutes=1))
    _authorization, tokens = sign_in(service)
    payload = {
        "assignment_id": "asn_lab01",
        "github_repository_id": 9001,
        "head_sha": SHA,
    }
    first = service.submit(tokens["access_token"], "original-key", payload)
    service.submission_pinner.unavailable = True
    clock.value = NOW + timedelta(minutes=2)

    exact = service.submit(tokens["access_token"], "original-key", payload)
    semantic = service.submit(tokens["access_token"], "new-key", payload)
    assert exact["submission"]["submission_id"] == first["submission"]["submission_id"]
    assert semantic["submission"]["submission_id"] == first["submission"]["submission_id"]
    assert service.submission_pinner.calls == 1


def test_unpushed_sha_before_due_cannot_be_accepted_after_later_push(platform) -> None:
    state, service, clock, _notifications, student = platform
    register_assignment(state, student, due_at=NOW + timedelta(minutes=1))
    _authorization, tokens = sign_in(service)
    payload = {
        "assignment_id": "asn_lab01",
        "github_repository_id": 9001,
        "head_sha": SHA,
    }
    service.submission_pinner.unavailable = True
    with pytest.raises(PlatformAPIError) as not_pushed:
        service.submit(tokens["access_token"], "not-yet-pushed", payload)
    assert not_pushed.value.code == "source_changed_or_unavailable"
    assert state.list_submissions_for_processing(course_key=COURSE) == []

    service.submission_pinner.unavailable = False
    clock.value = NOW + timedelta(minutes=2)
    with pytest.raises(PlatformAPIError) as too_late:
        service.submit(tokens["access_token"], "not-yet-pushed", payload)
    assert too_late.value.code == "assignment_closed"
    assert state.list_submissions_for_processing(course_key=COURSE) == []


def test_known_invalid_source_code_is_actionable_without_internal_detail(platform) -> None:
    state, service, _clock, _notifications, student = platform
    register_assignment(state, student)
    _authorization, tokens = sign_in(service)
    service.submission_pinner.invalid = SubmissionSourceInvalid(
        "secret internal path",
        code="git_lfs_object_unsupported",
        public_message="Git LFS pointers are not supported for submissions",
    )

    with pytest.raises(PlatformAPIError) as invalid:
        service.submit(
            tokens["access_token"],
            "lfs-source",
            {
                "assignment_id": "asn_lab01",
                "github_repository_id": 9001,
                "head_sha": SHA,
            },
        )

    assert invalid.value.status == 422
    assert invalid.value.code == "git_lfs_object_unsupported"
    assert str(invalid.value) == "Git LFS pointers are not supported for submissions"
    assert "secret" not in str(invalid.value)
    assert state.list_submissions_for_processing(course_key=COURSE) == []


def test_submission_outstanding_limit_maps_to_429(platform) -> None:
    state, service, _clock, _notifications, student = platform
    register_assignment(state, student)
    _authorization, tokens = sign_in(service)
    service.submission_policy = SubmissionPolicy(
        max_outstanding_per_student=2,
        max_daily_per_student=10,
    )
    for index, sha in enumerate(("1" * 40, "2" * 40)):
        service.submit(
            tokens["access_token"],
            f"request-{index}",
            {
                "assignment_id": "asn_lab01",
                "github_repository_id": 9001,
                "head_sha": sha,
            },
        )
    calls_before_limit = service.submission_pinner.calls
    with pytest.raises(PlatformAPIError) as limited:
        service.submit(
            tokens["access_token"],
            "request-over-limit",
            {
                "assignment_id": "asn_lab01",
                "github_repository_id": 9001,
                "head_sha": "3" * 40,
            },
        )
    assert limited.value.status == 429
    assert limited.value.headers["Retry-After"] == "60"
    assert service.submission_pinner.calls == calls_before_limit


def test_failed_pin_attempts_consume_bounded_daily_quota(platform) -> None:
    state, service, _clock, _notifications, student = platform
    register_assignment(state, student)
    _authorization, tokens = sign_in(service)
    service.submission_policy = SubmissionPolicy(
        max_outstanding_per_student=10,
        max_daily_per_student=2,
    )
    service.submission_pinner.unavailable = True
    for index in range(2):
        with pytest.raises(PlatformAPIError) as unavailable:
            service.submit(
                tokens["access_token"],
                f"wrong-sha-{index}",
                {
                    "assignment_id": "asn_lab01",
                    "github_repository_id": 9001,
                    "head_sha": f"{index + 1}" * 40,
                },
            )
        assert unavailable.value.code == "source_changed_or_unavailable"

    with pytest.raises(PlatformAPIError) as limited:
        service.submit(
            tokens["access_token"],
            "wrong-sha-limited",
            {
                "assignment_id": "asn_lab01",
                "github_repository_id": 9001,
                "head_sha": "3" * 40,
            },
        )
    assert limited.value.status == 429
    assert service.submission_pinner.calls == 2


def test_concurrent_semantic_duplicate_pins_only_once(platform) -> None:
    state, service, _clock, _notifications, student = platform
    register_assignment(state, student)
    _authorization, tokens = sign_in(service)
    payload = {
        "assignment_id": "asn_lab01",
        "github_repository_id": 9001,
        "head_sha": SHA,
    }
    pinner = service.submission_pinner
    pinner.entered = threading.Event()
    pinner.release = threading.Event()
    results: list[dict] = []
    failures: list[BaseException] = []

    def submit(key: str) -> None:
        try:
            results.append(service.submit(tokens["access_token"], key, payload))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    first = threading.Thread(target=submit, args=("concurrent-1",))
    second = threading.Thread(target=submit, args=("concurrent-2",))
    first.start()
    assert pinner.entered.wait(2)
    second.start()
    pinner.release.set()
    first.join(5)
    second.join(5)

    assert failures == []
    assert len(results) == 2
    assert results[0]["submission"]["submission_id"] == results[1]["submission"]["submission_id"]
    assert pinner.calls == 1


def test_service_rejects_untrusted_course_and_submission_fields(platform) -> None:
    state, service, _clock, _notifications, student = platform
    register_assignment(state, student)
    _authorization, tokens = sign_in(service)

    with pytest.raises(PlatformAPIError, match="request fields"):
        service.submit(
            tokens["access_token"],
            "request-1",
            {
                "assignment_id": "asn_lab01",
                "github_repository_id": 9001,
                "head_sha": SHA,
                "score": 10,
            },
        )
    with pytest.raises(PlatformAPIError) as repository:
        service.submit(
            tokens["access_token"],
            "request-2",
            {
                "assignment_id": "asn_lab01",
                "github_repository_id": 9999,
                "head_sha": SHA,
            },
        )
    assert repository.value.status == 403


def test_overlong_bearer_is_consistently_an_invalid_token(platform) -> None:
    _state, service, _clock, _notifications, _student = platform

    with pytest.raises(PlatformAPIError) as rejected:
        service.get_me("a" * 300)

    assert rejected.value.status == 401
    assert rejected.value.code == "invalid_token"
    assert rejected.value.headers["WWW-Authenticate"].startswith("Bearer ")


def test_public_base_url_requires_https_except_loopback(tmp_path) -> None:
    with pytest.raises(ValueError, match="session_list_limit"):
        TokenPolicy(max_active_sessions=2, session_list_limit=1)
    with pytest.raises(ValueError, match="max_activation_attempts"):
        TokenPolicy(max_activation_attempts=21)

    state = PlatformStateStore(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="HTTPS"):
        StudentPlatformService(
            state=state,
            server_secret=b"s" * 32,
            course_key=COURSE,
            public_base_url="http://grade.example",
        )
    with pytest.raises(ValueError, match="without credentials, path"):
        StudentPlatformService(
            state=state,
            server_secret=b"s" * 32,
            course_key=COURSE,
            public_base_url="https://grade.example/base",
        )

    insecure = StudentPlatformService(
        state=state,
        server_secret=b"s" * 32,
        course_key=COURSE,
        public_base_url="http://192.168.50.9:18080",
        external_access_mode="insecure-http",
    )
    assert insecure.public_base_url == "http://192.168.50.9:18080"

    for rejected_url in (
        "http://grade.lan:18080",
        "http://203.0.113.10:18080",
        "http://127.0.0.1:18080",
    ):
        with pytest.raises(ValueError, match="RFC1918"):
            StudentPlatformService(
                state=state,
                server_secret=b"s" * 32,
                course_key=COURSE,
                public_base_url=rejected_url,
                external_access_mode="insecure-http",
            )


def test_github_oauth_browser_flow_requires_confirmation_before_device_exchange(
    platform,
) -> None:
    _state, service, _clock, _notifications, _student = platform

    class FakeOAuth:
        def authorization_url(self, *, state):
            return "https://github.example/authorize?" + "state=" + state

        def exchange_identity(self, *, code):
            assert code == "oauth-code"
            return GitHubIdentity(user_id=101, login="student-one")

    service.github_oauth = FakeOAuth()
    authorization = service.create_device_authorization({"device_name": "WSL2"})
    started = service.github_start({"user_code": authorization["user_code"]})
    assert started.status == 302
    signed_state = parse_qs(urlsplit(started.headers["Location"]).query)["state"][0]
    callback = service.github_callback({"code": "oauth-code", "state": signed_state})
    assert callback.status == 200
    session_cookie = callback.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    csrf = re.search(r'name="csrf" value="([^"]+)"', callback.body).group(1)

    approved = service.approve_activation(
        {"csrf": csrf}, {"autograde_activation": session_cookie}
    )

    assert approved.status == 200
    tokens = service.exchange_device_authorization(
        {"device_code": authorization["device_code"]}
    )
    assert service.get_me(tokens["access_token"])["student_key"] == "20260001"
