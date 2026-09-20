from __future__ import annotations

import sqlite3
import tempfile
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import autograde.platform_state as platform_state_module
from autograde.platform_state import (
    BundleSubmissionState,
    PlatformAccessDenied,
    PlatformConflict,
    PlatformIdempotencyConflict,
    PlatformInvalidTransition,
    PlatformNotFound,
    PlatformStateStore,
    PlatformSubmissionLimitExceeded,
    ResultPolicy,
    StudentIdentityKind,
)


NOW = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)
COURSE = "cse101-2026f"
STARTER_DIGEST = "a" * 64
ASSESSMENT_DIGEST = "b" * 64
DATASET_DIGEST = "c" * 64
SOURCE_DIGEST = "d" * 64
RUNNER_IMAGE = "registry.school.local/autograde/python@sha256:" + ("e" * 64)


def verifier(character: str) -> str:
    return character * 64


@pytest.fixture()
def database():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory) / "state.sqlite3"


@pytest.fixture()
def store(database: Path):
    return PlatformStateStore(database)


class BundleFixture:
    def __init__(
        self,
        store: PlatformStateStore,
        *,
        student_key: str = "s001",
        token_character: str = "1",
        suffix: str = "1",
    ) -> None:
        self.store = store
        self.token = verifier(token_character)
        self.student = store.upsert_local_student(
            student_key=student_key,
            auth_subject=f"school:{student_key}",
            at=NOW,
        )
        self.enrollment = store.upsert_enrollment(
            student_id=self.student.id,
            course_key=COURSE,
            at=NOW,
        )
        device_hash = verifier(chr(ord("2") + int(suffix)))
        user_hmac = verifier(chr(ord("6") + int(suffix)))
        store.create_device_authorization(
            authorization_id=f"dev_bundle_{suffix}",
            device_code_hash=device_hash,
            user_code_hmac=user_hmac,
            course_key=COURSE,
            device_label="VS Code / Linux",
            expires_at=NOW + timedelta(minutes=5),
            at=NOW,
        )
        store.approve_device_authorization(
            user_code_hmac=user_hmac,
            auth_subject=self.student.auth_subject,
            course_key=COURSE,
            at=NOW + timedelta(seconds=1),
        )
        self.session = store.consume_device_authorization(
            device_code_hash=device_hash,
            course_key=COURSE,
            session_id=f"ses_bundle_{suffix}",
            token_family_id=f"fam_bundle_{suffix}",
            access_token_hash=self.token,
            access_token_expires_at=NOW + timedelta(hours=1),
            refresh_token_hash=verifier(chr(ord("a") + int(suffix))),
            refresh_token_expires_at=NOW + timedelta(days=30),
            at=NOW + timedelta(seconds=2),
        )

    def assignment(
        self,
        *,
        assignment_id: str = "bundle_asn_01",
        assignment_key: str = "lab01",
        ready: bool = True,
        opens_at: datetime | None = NOW,
        due_at: datetime | None = NOW + timedelta(days=7),
        result_policy: ResultPolicy = ResultPolicy.IMMEDIATE,
    ):
        return self.store.register_bundle_assignment_release(
            assignment_id=assignment_id,
            course_key=COURSE,
            assignment_key=assignment_key,
            release_id=f"{assignment_key}-v1",
            title=f"{assignment_key} practice",
            starter_path=f"/immutable/starter/{assignment_key}.tar.gz",
            starter_digest=STARTER_DIGEST,
            starter_size_bytes=1234,
            assessment_path=f"/immutable/assessment/{assignment_key}",
            assessment_digest=ASSESSMENT_DIGEST,
            data_path=f"/immutable/data/{assignment_key}",
            dataset_digest=DATASET_DIGEST,
            runner_image=RUNNER_IMAGE,
            rubric_version="v1",
            max_score=10,
            result_policy=result_policy,
            opens_at=opens_at,
            due_at=due_at,
            ready=ready,
            at=NOW,
        )

    def submit(
        self,
        *,
        submission_id: str = "bundle_sub_01",
        receipt_id: str = "bundle_rcp_01",
        key: str = "bundle-idem-01",
        request_hash: str = verifier("f"),
        source_digest: str = SOURCE_DIGEST,
        source_size_bytes: int = 2345,
        max_outstanding: int = 3,
        max_daily: int = 50,
        at: datetime = NOW + timedelta(minutes=1),
    ):
        return self.store.create_accepted_bundle_submission(
            submission_id=submission_id,
            receipt_id=receipt_id,
            access_token_hash=self.token,
            course_key=COURSE,
            assignment_id="bundle_asn_01",
            idempotency_key=key,
            request_hash=request_hash,
            source_path=f"/immutable/submissions/{submission_id}.tar.gz",
            source_digest=source_digest,
            source_size_bytes=source_size_bytes,
            max_outstanding_per_student=max_outstanding,
            max_daily_per_student=max_daily,
            at=at,
        )


def test_owned_submission_history_is_bounded_ordered_and_isolated(store):
    fixture = BundleFixture(store)
    fixture.assignment()
    other = BundleFixture(store, student_key="s002", token_character="a", suffix="2")
    for index in range(102):
        fixture.submit(submission_id=f"bsub_history_{index:03d}", receipt_id=f"brcp_history_{index:03d}",
                       key=f"history-{index}", request_hash=hashlib.sha256(f"req{index}".encode()).hexdigest(),
                       source_digest=hashlib.sha256(f"src{index}".encode()).hexdigest(),
                       max_outstanding=200, max_daily=200,
                       at=NOW + timedelta(minutes=1, seconds=index))
    rows = store.list_owned_bundle_submissions(access_token_hash=fixture.token, course_key=COURSE,
        assignment_id="bundle_asn_01", at=NOW + timedelta(minutes=4))
    assert len(rows) == 101
    assert rows[0].submission_id == "bsub_history_101"
    assert rows[-1].submission_id == "bsub_history_001"
    assert store.list_owned_bundle_submissions(access_token_hash=other.token, course_key=COURSE,
        assignment_id="bundle_asn_01", at=NOW + timedelta(minutes=4)) == ()


def test_v6_migrates_existing_students_and_creates_bundle_tables(
    database: Path,
) -> None:
    applied = "2026-09-01T00:00:00.000000Z"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE platform_schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 6):
            sql = platform_state_module._MIGRATIONS[version].replace(
                "__PLATFORM_MIGRATION_TIMESTAMP__", applied
            )
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO platform_schema_migrations(version, applied_at) "
                "VALUES (?, ?)",
                (version, applied),
            )
        connection.execute(
            "INSERT INTO platform_students ("
            "student_key, auth_subject, github_user_id, github_login, active, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
            ("legacy", "github:99", 99, "legacy-user", applied, applied),
        )
        connection.commit()

    migrated = PlatformStateStore(database)

    assert migrated.schema_version() == 12
    assert migrated.get_student_by_key("legacy").identity_kind == (
        StudentIdentityKind.GITHUB
    )
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "bundle_assignment_releases",
        "bundle_download_events",
        "bundle_submission_requests",
        "bundle_submission_idempotency_keys",
        "bundle_submission_admission_counters",
        "bundle_submission_receipts",
        "bundle_submission_results",
    } <= tables


def test_local_student_is_deterministic_and_cannot_change_identity_kind(
    store: PlatformStateStore,
) -> None:
    first = store.upsert_local_student(
        student_key="s001", auth_subject="school:s001", at=NOW
    )
    again = store.upsert_local_student(
        student_key="s001", auth_subject="school:s001", at=NOW
    )

    assert first == again
    assert first.identity_kind == StudentIdentityKind.LOCAL
    assert first.github_user_id >= 1 << 62
    assert first.github_login.startswith("autograde-local-")

    with pytest.raises(PlatformConflict, match="local student identity"):
        store.upsert_student(
            student_key="s001",
            auth_subject="github:1",
            github_user_id=1,
            github_login="real-login",
            at=NOW,
        )
    collision_digest = hashlib.sha256(
        b"autograde-local-student:s002"
    ).hexdigest()
    collision_id = (1 << 62) | (
        int(collision_digest[:16], 16) & ((1 << 62) - 1)
    )
    store.upsert_student(
        student_key="github-owner",
        auth_subject="github:2",
        github_user_id=collision_id,
        github_login="unlikely-but-explicit-collision",
        at=NOW,
    )
    with pytest.raises(PlatformConflict, match="reserved placeholder"):
        store.upsert_local_student(
            student_key="s002", auth_subject="school:s002", at=NOW
        )


def test_course_wide_release_is_visible_to_every_active_enrollment(
    store: PlatformStateStore,
) -> None:
    first = BundleFixture(store)
    release = first.assignment()
    second = BundleFixture(
        store, student_key="s002", token_character="2", suffix="2"
    )

    first_items = store.list_owned_bundle_assignments(
        access_token_hash=first.token, course_key=COURSE, at=NOW
    )
    second_items = store.list_owned_bundle_assignments(
        access_token_hash=second.token, course_key=COURSE, at=NOW
    )

    assert first_items == [release]
    assert second_items == [release]
    assert release.starter_digest == "sha256:" + STARTER_DIGEST
    assert release.assessment_digest == "sha256:" + ASSESSMENT_DIGEST
    assert release.dataset_digest == "sha256:" + DATASET_DIGEST

    store.upsert_enrollment(
        student_id=second.student.id,
        course_key=COURSE,
        active=False,
        at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(PlatformAccessDenied):
        store.list_owned_bundle_assignments(
            access_token_hash=second.token,
            course_key=COURSE,
            at=NOW + timedelta(minutes=2),
        )


def test_release_identity_is_immutable_but_availability_is_mutable(
    store: PlatformStateStore,
) -> None:
    fixture = BundleFixture(store)
    release = fixture.assignment(ready=False)
    same = fixture.assignment(ready=True)
    assert same == release
    assert not same.ready

    ready = store.set_bundle_assignment_availability(
        release.assignment_id, course_key=COURSE, ready=True, at=NOW
    )
    assert ready.ready

    with pytest.raises(PlatformConflict, match="different bundle release"):
        store.register_bundle_assignment_release(
            assignment_id=release.assignment_id,
            course_key=COURSE,
            assignment_key="lab01",
            release_id="lab01-v1",
            title="changed title",
            starter_path=release.starter_path,
            starter_digest=release.starter_digest,
            starter_size_bytes=release.starter_size_bytes,
            assessment_path=release.assessment_path,
            assessment_digest=release.assessment_digest,
            data_path=release.data_path,
            dataset_digest=release.dataset_digest,
            runner_image=release.runner_image,
            rubric_version=release.rubric_version,
            max_score=release.max_score,
            result_policy=release.result_policy,
            opens_at=NOW,
            due_at=NOW + timedelta(days=7),
            at=NOW,
        )

    with sqlite3.connect(store.database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE bundle_assignment_releases SET title = 'tampered' "
                "WHERE assignment_id = ?",
                (release.assignment_id,),
            )


def test_download_event_is_scoped_retry_safe_and_tracks_platform(
    store: PlatformStateStore,
) -> None:
    fixture = BundleFixture(store)
    fixture.assignment()
    event = store.record_bundle_download(
        download_id="download-01",
        access_token_hash=fixture.token,
        course_key=COURSE,
        assignment_id="bundle_asn_01",
        client_platform="darwin-arm64",
        at=NOW + timedelta(minutes=1),
    )
    replay = store.record_bundle_download(
        download_id="download-01",
        access_token_hash=fixture.token,
        course_key=COURSE,
        assignment_id="bundle_asn_01",
        client_platform="ignored-on-replay",
        at=NOW + timedelta(minutes=2),
    )

    assert replay == event
    assert event.student_id == fixture.student.id
    assert event.starter_digest == "sha256:" + STARTER_DIGEST
    assert event.client_platform == "darwin-arm64"

    other = BundleFixture(store, student_key="s002", token_character="2", suffix="2")
    with pytest.raises(PlatformConflict, match="different download"):
        store.record_bundle_download(
            download_id="download-01",
            access_token_hash=other.token,
            course_key=COURSE,
            assignment_id="bundle_asn_01",
            at=NOW + timedelta(minutes=2),
        )


def test_accepted_bundle_submission_snapshots_inputs_and_coalesces_replays(
    store: PlatformStateStore,
) -> None:
    fixture = BundleFixture(store)
    fixture.assignment()

    created = fixture.submit()
    receipt = store.get_bundle_receipt(created.request.submission_id)

    assert not created.replayed
    assert created.request.state == BundleSubmissionState.ACCEPTED
    assert receipt.source_digest == "sha256:" + SOURCE_DIGEST
    assert receipt.source_size_bytes == 2345
    assert receipt.starter_digest == "sha256:" + STARTER_DIGEST
    assert receipt.assessment_digest == "sha256:" + ASSESSMENT_DIGEST
    assert receipt.dataset_digest == "sha256:" + DATASET_DIGEST
    assert receipt.runner_image == RUNNER_IMAGE
    assert receipt.opens_at is not None
    assert receipt.due_at is not None
    store.set_bundle_assignment_availability(
        "bundle_asn_01", course_key=COURSE, active=False, at=NOW
    )
    assert store.get_bundle_receipt(created.request.submission_id) == receipt
    with sqlite3.connect(store.database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE bundle_submission_receipts SET source_path = 'tampered' "
                "WHERE submission_id = ?",
                (created.request.submission_id,),
            )
    store.set_bundle_assignment_availability(
        "bundle_asn_01", course_key=COURSE, active=True, at=NOW
    )

    exact = fixture.submit(
        submission_id="unused-exact",
        receipt_id="unused-exact-receipt",
    )
    assert exact.replayed
    assert exact.request.submission_id == created.request.submission_id

    semantic = fixture.submit(
        submission_id="unused-semantic",
        receipt_id="unused-semantic-receipt",
        key="another-key",
        request_hash=verifier("9"),
    )
    assert not semantic.replayed
    assert semantic.request.submission_id == "unused-semantic"

    with pytest.raises(PlatformIdempotencyConflict):
        fixture.submit(
            submission_id="conflict",
            receipt_id="conflict-receipt",
            request_hash=verifier("8"),
        )


def test_bundle_submission_lifecycle_result_and_student_ownership(
    store: PlatformStateStore,
) -> None:
    fixture = BundleFixture(store)
    fixture.assignment()
    request = fixture.submit().request

    request = store.transition_bundle_submission(request.submission_id, "queued", at=NOW)
    request = store.transition_bundle_submission(request.submission_id, "running", at=NOW)
    request, result = store.record_bundle_graded_result(
        request.submission_id,
        result_id="bundle_result_01",
        score=8.5,
        max_score=10,
        rubric={"tests": 8.5},
        diagnostics=[{"message": "two tests failed"}],
        at=NOW + timedelta(minutes=2),
    )
    assert request.state == BundleSubmissionState.GRADED
    assert result.score == 8.5
    with pytest.raises(PlatformNotFound, match="published"):
        store.get_owned_bundle_result(
            access_token_hash=fixture.token,
            course_key=COURSE,
            submission_id=request.submission_id,
            at=NOW + timedelta(minutes=2),
        )

    request, result = store.publish_bundle_result(
        request.submission_id, at=NOW + timedelta(minutes=3)
    )
    assert request.state == BundleSubmissionState.PUBLISHED
    assert result.published_at is not None
    assert store.get_owned_bundle_result(
        access_token_hash=fixture.token,
        course_key=COURSE,
        submission_id=request.submission_id,
        at=NOW + timedelta(minutes=3),
    ) == result

    other = BundleFixture(store, student_key="s002", token_character="2", suffix="2")
    with pytest.raises(PlatformNotFound, match="this student"):
        store.get_owned_bundle_submission(
            access_token_hash=other.token,
            course_key=COURSE,
            submission_id=request.submission_id,
            at=NOW + timedelta(minutes=3),
        )
    with pytest.raises(PlatformInvalidTransition):
        store.transition_bundle_submission(request.submission_id, "queued", at=NOW)


def test_bundle_limits_are_atomic_under_concurrent_admission(
    store: PlatformStateStore,
) -> None:
    fixture = BundleFixture(store)
    fixture.assignment()

    def submit(index: int) -> str:
        try:
            fixture.submit(
                submission_id=f"bundle_concurrent_{index:02d}",
                receipt_id=f"bundle_concurrent_receipt_{index:02d}",
                key=f"concurrent-key-{index:02d}",
                request_hash=f"{index + 1:064x}",
                source_digest=f"{index + 100:064x}",
                source_size_bytes=1000 + index,
                max_outstanding=3,
                max_daily=50,
            )
        except PlatformSubmissionLimitExceeded:
            return "limited"
        return "accepted"

    with ThreadPoolExecutor(max_workers=10) as executor:
        outcomes = list(executor.map(submit, range(10)))

    assert outcomes.count("accepted") == 3
    assert outcomes.count("limited") == 7
    processing = store.list_bundle_submissions_for_processing(course_key=COURSE)
    assert len(processing) == 3


def test_daily_limit_does_not_charge_replays(
    store: PlatformStateStore,
) -> None:
    fixture = BundleFixture(store)
    fixture.assignment()
    first = fixture.submit(max_daily=1)
    replay = fixture.submit(
        submission_id="unused",
        receipt_id="unused-receipt",
        max_daily=1,
    )
    assert replay.replayed
    assert replay.request.submission_id == first.request.submission_id
    with pytest.raises(PlatformSubmissionLimitExceeded, match="daily"):
        fixture.submit(submission_id="same-source-new", receipt_id="new-receipt", key="new-key", max_daily=1)

    store.transition_bundle_submission(first.request.submission_id, "queued", at=NOW)
    store.transition_bundle_submission(
        first.request.submission_id,
        "infra_failed",
        failure_code="runner_unavailable",
        failure_message="runner unavailable",
        at=NOW,
    )
    with pytest.raises(PlatformSubmissionLimitExceeded, match="daily"):
        fixture.submit(
            submission_id="new-submission",
            receipt_id="new-receipt",
            key="new-key",
            request_hash=verifier("8"),
            source_digest="7" * 64,
            max_daily=1,
        )


def test_bundle_assignment_open_and_due_boundaries_are_server_time_based(
    store: PlatformStateStore,
) -> None:
    fixture = BundleFixture(store)
    fixture.assignment(
        opens_at=NOW + timedelta(minutes=5),
        due_at=NOW + timedelta(minutes=10),
    )

    assert store.list_owned_bundle_assignments(
        access_token_hash=fixture.token, course_key=COURSE, at=NOW
    ) == []
    with pytest.raises(PlatformAccessDenied, match="not available"):
        store.record_bundle_download(
            download_id="too-early",
            access_token_hash=fixture.token,
            course_key=COURSE,
            assignment_id="bundle_asn_01",
            at=NOW,
        )
    with pytest.raises(PlatformAccessDenied, match="unavailable"):
        fixture.submit(at=NOW + timedelta(minutes=10))


def test_dashboard_contains_unsubmitted_students_and_latest_result(
    store: PlatformStateStore,
) -> None:
    first = BundleFixture(store)
    first.assignment()
    BundleFixture(store, student_key="s002", token_character="2", suffix="2")
    store.record_bundle_download(
        download_id="dashboard-download",
        access_token_hash=first.token,
        course_key=COURSE,
        assignment_id="bundle_asn_01",
        at=NOW + timedelta(minutes=1),
    )
    request = first.submit(at=NOW + timedelta(minutes=2)).request
    store.transition_bundle_submission(request.submission_id, "queued", at=NOW)
    store.transition_bundle_submission(request.submission_id, "running", at=NOW)
    store.record_bundle_graded_result(
        request.submission_id,
        result_id="dashboard-result",
        score=9,
        max_score=10,
        at=NOW + timedelta(minutes=3),
    )
    store.publish_bundle_result(
        request.submission_id, at=NOW + timedelta(minutes=4)
    )

    rows = store.list_bundle_dashboard_rows(course_key=COURSE)

    assert len(rows) == 2
    submitted, missing = rows
    assert submitted.student_key == "s001"
    assert submitted.download_count == 1
    assert submitted.submission_count == 1
    assert submitted.latest_state == BundleSubmissionState.PUBLISHED
    assert submitted.latest_score == 9
    assert missing.student_key == "s002"
    assert missing.download_count == 0
    assert missing.submission_count == 0
    assert missing.latest_state is None
    assert missing.latest_score is None
