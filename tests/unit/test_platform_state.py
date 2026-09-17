from __future__ import annotations

import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import autograde.platform_state as platform_state_module
from autograde.platform_auth import hash_student_password
from autograde.platform_state import (
    CourseRosterImportEntry,
    DeviceAuthorizationExpired,
    DeviceAuthorizationState,
    PlatformAccessDenied,
    PlatformConflict,
    PlatformIdempotencyConflict,
    PlatformInvalidTransition,
    PlatformNotFound,
    PlatformSessionLimitExceeded,
    PlatformStateStore,
    PlatformSubmissionLimitExceeded,
    RefreshTokenReuseDetected,
    ResultPolicy,
    StudentActivationState,
    StudentIdentityKind,
    SubmissionMode,
    SubmissionState,
)
from autograde.state import StateStore


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
COURSE = "cse101-2026f"
SHA = "a" * 40
OTHER_SHA = "b" * 40
SOURCE_DIGEST = "c" * 64
ASSESSMENT_DIGEST = "d" * 64
RUNNER_IMAGE = "ghcr.io/school/autograde-python@sha256:" + ("e" * 64)


def verifier(character: str) -> str:
    return character * 64


class PlatformFixture:
    def __init__(self, store: PlatformStateStore) -> None:
        self.store = store
        self.student = store.upsert_student(
            student_key="s001",
            auth_subject="github:101",
            github_user_id=101,
            github_login="student-one",
            at=NOW,
        )
        self.enrollment = store.upsert_enrollment(
            student_id=self.student.id,
            course_key=COURSE,
            at=NOW,
        )

    def session(
        self,
        *,
        suffix: str = "1",
        access_hash: str | None = None,
        refresh_hash: str | None = None,
    ):
        access_hash = access_hash or verifier("1")
        refresh_hash = refresh_hash or verifier("2")
        device_hash = verifier("3" if suffix == "1" else "8")
        user_hmac = verifier("4" if suffix == "1" else "9")
        self.store.create_device_authorization(
            authorization_id=f"dev_{suffix}",
            device_code_hash=device_hash,
            user_code_hmac=user_hmac,
            course_key=COURSE,
            device_label="DESKTOP / Ubuntu WSL2",
            expires_at=NOW + timedelta(minutes=5),
            at=NOW,
        )
        self.store.approve_device_authorization(
            user_code_hmac=user_hmac,
            auth_subject=self.student.auth_subject,
            course_key=COURSE,
            at=NOW + timedelta(seconds=1),
        )
        return self.store.consume_device_authorization(
            device_code_hash=device_hash,
            course_key=COURSE,
            session_id=f"ses_{suffix}",
            token_family_id=f"fam_{suffix}",
            access_token_hash=access_hash,
            access_token_expires_at=NOW + timedelta(minutes=15),
            refresh_token_hash=refresh_hash,
            refresh_token_expires_at=NOW + timedelta(days=30),
            at=NOW + timedelta(seconds=2),
        )

    def activation(
        self,
        *,
        suffix: str = "1",
        code_hash: str | None = None,
        expires_at: datetime | None = None,
    ):
        return self.store.issue_student_activation(
            activation_id=f"act_{suffix}",
            student_id=self.student.id,
            course_key=COURSE,
            code_hmac=code_hash or verifier("a"),
            expires_at=expires_at or NOW + timedelta(days=7),
            at=NOW,
        )

    def assignment(
        self,
        *,
        assignment_id: str = "asn_01",
        assignment_key: str = "lab01",
        repository_id: int = 9001,
        mode: SubmissionMode = SubmissionMode.BRANCH,
        result_policy: ResultPolicy = ResultPolicy.IMMEDIATE,
        ready: bool = True,
        due_at: datetime | None = None,
        without_due_at: bool = False,
        assignment_path: str = ".",
        runner_image: str = RUNNER_IMAGE,
    ):
        return self.store.register_assignment(
            assignment_id=assignment_id,
            student_id=self.student.id,
            course_key="cse101-2026f",
            assignment_key=assignment_key,
            release_id=f"{assignment_key}-v1",
            github_repository_id=repository_id,
            repository_owner="school-cse101",
            repository_name=f"{assignment_key}-s001",
            clone_url=f"https://github.com/school-cse101/{assignment_key}-s001.git",
            submission_mode=mode,
            target_ref=f"submission/{assignment_key}",
            allowed_base_ref="main" if mode == SubmissionMode.PULL_REQUEST else None,
            assignment_path=assignment_path,
            assessment_path=f"/srv/assessments/{assignment_key}",
            assessment_digest=ASSESSMENT_DIGEST,
            runner_image=runner_image,
            rubric_version="v1",
            max_score=10,
            result_policy=result_policy,
            opens_at=NOW,
            due_at=None if without_due_at else (due_at or NOW + timedelta(days=7)),
            ready=ready,
            at=NOW,
        )

    def request(
        self,
        *,
        access_hash: str = verifier("1"),
        assignment_id: str = "asn_01",
        repository_id: int = 9001,
        submission_id: str = "sub_01",
        key: str = "idem-01",
        request_hash: str = verifier("5"),
        pr: int | None = None,
    ):
        return self.store.create_submission_request(
            submission_id=submission_id,
            access_token_hash=access_hash,
            course_key=COURSE,
            assignment_id=assignment_id,
            idempotency_key=key,
            request_hash=request_hash,
            github_repository_id=repository_id,
            requested_sha=SHA,
            pull_request_number=pr,
            at=NOW + timedelta(minutes=1),
        )

    def accepted(
        self,
        *,
        submission_id: str = "sub_accepted",
        receipt_id: str = "rcp_accepted",
        key: str = "accepted-key",
        request_hash: str = verifier("6"),
        sha: str = SHA,
        pr: int | None = None,
        at: datetime = NOW + timedelta(minutes=1),
        outstanding: int = 3,
        daily: int = 50,
    ):
        return self.store.create_accepted_submission(
            submission_id=submission_id,
            receipt_id=receipt_id,
            access_token_hash=verifier("1"),
            course_key=COURSE,
            assignment_id="asn_01",
            idempotency_key=key,
            request_hash=request_hash,
            github_repository_id=9001,
            requested_sha=sha,
            pull_request_number=pr,
            source_path=f"/immutable/{submission_id}.tar.gz",
            source_digest=SOURCE_DIGEST,
            snapshot_key=f"refs/autograde/snapshots/{submission_id}",
            max_outstanding_per_student=outstanding,
            max_daily_per_student=daily,
            at=at,
        )


@pytest.fixture()
def database():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory) / "state.sqlite3"


@pytest.fixture()
def store(database: Path):
    return PlatformStateStore(database)


def initialize_legacy_platform_schema(
    database: Path, *, through: int, applied_at: str
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE platform_schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, through + 1):
            connection.executescript(platform_state_module._MIGRATIONS[version])
            connection.execute(
                "INSERT INTO platform_schema_migrations(version, applied_at) "
                "VALUES (?, ?)",
                (version, applied_at),
            )
        connection.commit()


def test_bootstrap_has_an_independent_migration_namespace(database: Path) -> None:
    StateStore(database)
    platform = PlatformStateStore(database)

    assert platform.schema_version() == 12
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "schema_migrations" in tables
    assert "platform_schema_migrations" in tables
    assert {
        "platform_students",
        "platform_enrollments",
        "device_authorizations",
        "platform_sessions",
        "platform_token_families",
        "platform_student_activations",
        "platform_student_passwords",
        "platform_assignment_grants",
        "platform_assignment_acceptances",
        "platform_assignments",
        "submission_requests",
        "submission_receipts",
        "submission_results",
    } <= tables


def test_v4_migration_backfills_existing_refresh_rotation_count(
    database: Path,
) -> None:
    created = "2026-08-31T00:00:00.000000Z"
    expires = "2026-10-01T00:00:00.000000Z"
    initialize_legacy_platform_schema(database, through=3, applied_at=created)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO platform_students ("
            "id, student_key, auth_subject, github_user_id, github_login, active, "
            "created_at, updated_at) VALUES (1, 's001', 'github:101', 101, "
            "'student-one', 1, ?, ?)",
            (created, created),
        )
        connection.execute(
            "INSERT INTO platform_enrollments ("
            "id, student_id, course_key, active, created_at, updated_at) "
            "VALUES (1, 1, ?, 1, ?, ?)",
            (COURSE, created, created),
        )
        connection.execute(
            "INSERT INTO device_authorizations ("
            "authorization_id, device_code_hash, user_code_hmac, course_key, "
            "device_label, state, poll_interval_seconds, expires_at, student_id, "
            "created_at, updated_at, approved_at, consumed_at) "
            "VALUES ('dev_legacy', ?, ?, ?, 'WSL', 'consumed', 5, ?, 1, ?, ?, ?, ?)",
            (verifier("1"), verifier("2"), COURSE, expires, created, created, created, created),
        )
        connection.execute(
            "INSERT INTO platform_token_families ("
            "token_family_id, student_id, current_refresh_token_hash, "
            "refresh_token_expires_at, created_at, updated_at) "
            "VALUES ('fam_legacy', 1, ?, ?, ?, ?)",
            (verifier("5"), expires, created, created),
        )
        for token_hash, state in (
            (verifier("3"), "used"),
            (verifier("4"), "used"),
            (verifier("5"), "active"),
        ):
            connection.execute(
                "INSERT INTO platform_refresh_tokens ("
                "refresh_token_hash, token_family_id, state, issued_at, expires_at, used_at) "
                "VALUES (?, 'fam_legacy', ?, ?, ?, ?)",
                (
                    token_hash,
                    state,
                    created,
                    expires,
                    created if state == "used" else None,
                ),
            )
        connection.execute(
            "INSERT INTO platform_sessions ("
            "session_id, student_id, token_family_id, device_authorization_id, "
            "device_label, access_token_hash, access_token_expires_at, "
            "refresh_token_expires_at, created_at, updated_at) "
            "VALUES ('ses_legacy', 1, 'fam_legacy', 'dev_legacy', 'WSL', ?, ?, ?, ?, ?)",
            (verifier("6"), expires, expires, created, created),
        )
        connection.commit()
    migrated = PlatformStateStore(database)
    assert migrated.schema_version() == 12
    with sqlite3.connect(database) as connection:
        family = connection.execute(
            "SELECT course_key, refresh_rotation_count "
            "FROM platform_token_families WHERE token_family_id = 'fam_legacy'"
        ).fetchone()
        session = connection.execute(
            "SELECT course_key FROM platform_sessions WHERE session_id = 'ses_legacy'"
        ).fetchone()
        activation_attempts = connection.execute(
            "SELECT activation_failed_attempts FROM device_authorizations "
            "WHERE authorization_id = 'dev_legacy'"
        ).fetchone()
        activation_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'platform_student_activations'"
        ).fetchone()
    assert family == (COURSE, 2)
    assert session == (COURSE,)
    assert activation_attempts == (0,)
    assert activation_table == (1,)


def test_v8_migration_removes_inactive_passwords_and_enforces_invariant(
    database: Path,
) -> None:
    created = "2026-09-01T00:00:00.000000Z"
    initialize_legacy_platform_schema(database, through=7, applied_at=created)
    password_hash = hash_student_password("482731")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for student_id, student_key, student_active, enrollment_active in (
            (1, "global-inactive", 0, 1),
            (2, "enrollment-inactive", 1, 0),
            (3, "fully-active", 1, 1),
        ):
            connection.execute(
                "INSERT INTO platform_students ("
                "id, student_key, auth_subject, github_user_id, github_login, "
                "active, created_at, updated_at, identity_kind"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'github')",
                (
                    student_id,
                    student_key,
                    f"github:{100 + student_id}",
                    100 + student_id,
                    f"student-{student_id}",
                    student_active,
                    created,
                    created,
                ),
            )
            connection.execute(
                "INSERT INTO platform_enrollments ("
                "id, student_id, course_key, active, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    student_id,
                    student_id,
                    COURSE,
                    enrollment_active,
                    created,
                    created,
                ),
            )
            connection.execute(
                "INSERT INTO platform_student_passwords ("
                "enrollment_id, password_hash, failed_attempts, locked_until, "
                "updated_at) VALUES (?, ?, 0, NULL, ?)",
                (student_id, password_hash, created),
            )
        connection.commit()
    # A legacy database may also contain an orphan if an old writer disabled
    # foreign keys.  Migration 8 treats it as inactive and removes it.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO platform_student_passwords ("
            "enrollment_id, password_hash, failed_attempts, locked_until, updated_at"
            ") VALUES (999, ?, 0, NULL, ?)",
            (password_hash, created),
        )

    migrated = PlatformStateStore(database)

    assert migrated.schema_version() == 12
    with sqlite3.connect(database) as connection:
        remaining = connection.execute(
            "SELECT enrollment_id FROM platform_student_passwords"
        ).fetchall()
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    assert remaining == [(3,)]
    assert {
        "trg_platform_student_password_active_insert",
        "trg_platform_student_password_active_update",
        "trg_platform_enrollment_password_cleanup",
        "trg_platform_student_password_cleanup",
    } <= triggers

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for enrollment_id in (1, 2):
            with pytest.raises(
                sqlite3.IntegrityError, match="requires active enrollment"
            ):
                connection.execute(
                    "INSERT INTO platform_student_passwords ("
                    "enrollment_id, password_hash, failed_attempts, locked_until, "
                    "updated_at) VALUES (?, ?, 0, NULL, ?)",
                    (enrollment_id, password_hash, created),
                )
            connection.rollback()

        with pytest.raises(
            sqlite3.IntegrityError, match="requires active enrollment"
        ):
            connection.execute(
                "UPDATE platform_student_passwords "
                "SET enrollment_id = 2 WHERE enrollment_id = 3"
            )
        connection.rollback()
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_student_passwords WHERE enrollment_id = 3"
        ).fetchone()[0] == 1

        connection.execute(
            "UPDATE platform_enrollments SET active = 0 WHERE id = 3"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_student_passwords WHERE enrollment_id = 3"
        ).fetchone()[0] == 0
        connection.execute(
            "UPDATE platform_enrollments SET active = 1 WHERE id = 3"
        )
        connection.execute(
            "INSERT INTO platform_student_passwords ("
            "enrollment_id, password_hash, failed_attempts, locked_until, updated_at"
            ") VALUES (3, ?, 0, NULL, ?)",
            (password_hash, created),
        )
        connection.execute("UPDATE platform_students SET active = 0 WHERE id = 3")
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_student_passwords WHERE enrollment_id = 3"
        ).fetchone()[0] == 0

        # A credential must never transfer to another course or student.
        connection.execute("UPDATE platform_students SET active = 1 WHERE id = 3")
        connection.execute(
            "INSERT INTO platform_student_passwords ("
            "enrollment_id, password_hash, failed_attempts, locked_until, updated_at"
            ") VALUES (3, ?, 0, NULL, ?)",
            (password_hash, created),
        )
        connection.execute(
            "UPDATE platform_enrollments SET course_key = 'other-course' WHERE id = 3"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_student_passwords WHERE enrollment_id = 3"
        ).fetchone()[0] == 0
        connection.execute(
            "UPDATE platform_enrollments SET course_key = ? WHERE id = 3",
            (COURSE,),
        )
        connection.execute(
            "INSERT INTO platform_student_passwords ("
            "enrollment_id, password_hash, failed_attempts, locked_until, updated_at"
            ") VALUES (3, ?, 0, NULL, ?)",
            (password_hash, created),
        )
        connection.execute(
            "INSERT INTO platform_students ("
            "id, student_key, auth_subject, github_user_id, github_login, active, "
            "created_at, updated_at, identity_kind"
            ") VALUES (4, 'replacement-owner', 'github:104', 104, "
            "'student-4', 1, ?, ?, 'github')",
            (created, created),
        )
        connection.execute(
            "UPDATE platform_enrollments SET student_id = 4 WHERE id = 3"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_student_passwords WHERE enrollment_id = 3"
        ).fetchone()[0] == 0
        connection.commit()

        # Trigger effects participate in the surrounding transaction.  A
        # rollback restores both the parent state and its credential.
        connection.execute(
            "INSERT INTO platform_student_passwords ("
            "enrollment_id, password_hash, failed_attempts, locked_until, updated_at"
            ") VALUES (3, ?, 0, NULL, ?)",
            (password_hash, created),
        )
        connection.commit()
        connection.execute("BEGIN")
        connection.execute(
            "UPDATE platform_enrollments SET active = 0 WHERE id = 3"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_student_passwords WHERE enrollment_id = 3"
        ).fetchone()[0] == 0
        with pytest.raises(
            sqlite3.IntegrityError, match="requires active enrollment"
        ):
            connection.execute(
                "INSERT INTO platform_student_passwords ("
                "enrollment_id, password_hash, failed_attempts, locked_until, "
                "updated_at) VALUES (3, ?, 0, NULL, ?)",
                (password_hash, created),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT active FROM platform_enrollments WHERE id = 3"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_student_passwords WHERE enrollment_id = 3"
        ).fetchone()[0] == 1

    imported = migrated.import_course_roster(
        course_key=COURSE,
        entries=(
            CourseRosterImportEntry(
                student_key="enrollment-inactive",
                auth_subject="github:102",
                identity_kind=StudentIdentityKind.GITHUB,
                github_user_id=102,
                github_login="student-2",
                active=True,
            ),
        ),
    )
    assert imported[0][1].active is True
    with pytest.raises(PlatformNotFound, match="password credential"):
        migrated.get_student_password_credential(
            student_key="enrollment-inactive",
            course_key=COURSE,
        )


@pytest.mark.parametrize(
    ("student_active", "enrollment_active"),
    [(False, True), (True, False)],
)
def test_v4_migration_terminalizes_credentials_for_inactive_student_or_course(
    database: Path,
    student_active: bool,
    enrollment_active: bool,
) -> None:
    created = "2026-08-31T00:00:00.000000Z"
    expires = "2026-10-01T00:00:00.000000Z"
    initialize_legacy_platform_schema(database, through=3, applied_at=created)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO platform_students ("
            "id, student_key, auth_subject, github_user_id, github_login, active, "
            "created_at, updated_at) VALUES (1, 's001', 'github:101', 101, "
            "'student-one', ?, ?, ?)",
            (int(student_active), created, created),
        )
        connection.execute(
            "INSERT INTO platform_enrollments ("
            "id, student_id, course_key, active, created_at, updated_at) "
            "VALUES (1, 1, ?, ?, ?, ?)",
            (COURSE, int(enrollment_active), created, created),
        )
        connection.execute(
            "INSERT INTO device_authorizations ("
            "authorization_id, device_code_hash, user_code_hmac, course_key, "
            "device_label, state, poll_interval_seconds, expires_at, student_id, "
            "created_at, updated_at, approved_at, consumed_at) "
            "VALUES ('dev_consumed', ?, ?, ?, 'WSL', 'consumed', 5, ?, 1, ?, ?, ?, ?)",
            (
                verifier("1"),
                verifier("2"),
                COURSE,
                expires,
                created,
                created,
                created,
                created,
            ),
        )
        connection.execute(
            "INSERT INTO device_authorizations ("
            "authorization_id, device_code_hash, user_code_hmac, course_key, "
            "device_label, state, poll_interval_seconds, expires_at, student_id, "
            "created_at, updated_at, approved_at) "
            "VALUES ('dev_approved', ?, ?, ?, 'browser', 'approved', 5, ?, 1, ?, ?, ?)",
            (
                verifier("3"),
                verifier("4"),
                COURSE,
                expires,
                created,
                created,
                created,
            ),
        )
        connection.execute(
            "INSERT INTO platform_token_families ("
            "token_family_id, student_id, current_refresh_token_hash, "
            "refresh_token_expires_at, created_at, updated_at) "
            "VALUES ('fam_legacy', 1, ?, ?, ?, ?)",
            (verifier("5"), expires, created, created),
        )
        connection.execute(
            "INSERT INTO platform_refresh_tokens ("
            "refresh_token_hash, token_family_id, state, issued_at, expires_at) "
            "VALUES (?, 'fam_legacy', 'active', ?, ?)",
            (verifier("5"), created, expires),
        )
        connection.execute(
            "INSERT INTO platform_sessions ("
            "session_id, student_id, token_family_id, device_authorization_id, "
            "device_label, access_token_hash, access_token_expires_at, "
            "refresh_token_expires_at, created_at, updated_at) "
            "VALUES ('ses_legacy', 1, 'fam_legacy', 'dev_consumed', 'WSL', ?, ?, ?, ?, ?)",
            (verifier("6"), expires, expires, created, created),
        )
        connection.commit()

    migrated = PlatformStateStore(database)
    with sqlite3.connect(database) as connection:
        session_revoked_at = connection.execute(
            "SELECT revoked_at FROM platform_sessions WHERE session_id = 'ses_legacy'"
        ).fetchone()[0]
        family_revoked_at = connection.execute(
            "SELECT revoked_at FROM platform_token_families "
            "WHERE token_family_id = 'fam_legacy'"
        ).fetchone()[0]
        refresh_state = connection.execute(
            "SELECT state FROM platform_refresh_tokens WHERE refresh_token_hash = ?",
            (verifier("5"),),
        ).fetchone()[0]
        grant = connection.execute(
            "SELECT state, denied_at FROM device_authorizations "
            "WHERE authorization_id = 'dev_approved'"
        ).fetchone()
    assert session_revoked_at is not None
    assert family_revoked_at is not None
    assert refresh_state == "revoked"
    assert grant[0] == "denied"
    assert grant[1] is not None

    migrated.set_student_active(1, True, at=NOW)
    migrated.upsert_enrollment(
        student_id=1,
        course_key=COURSE,
        active=True,
        at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        migrated.authorize_access_token(
            access_token_hash=verifier("6"),
            course_key=COURSE,
            at=NOW + timedelta(seconds=2),
        )
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        migrated.rotate_refresh_token(
            presented_refresh_token_hash=verifier("5"),
            course_key=COURSE,
            replacement_refresh_token_hash=verifier("7"),
            replacement_refresh_token_expires_at=NOW + timedelta(days=20),
            access_token_hash=verifier("8"),
            access_token_expires_at=NOW + timedelta(minutes=20),
            at=NOW + timedelta(seconds=2),
        )


def test_secret_fields_accept_only_verifiers_and_never_surface_them(
    store: PlatformStateStore, database: Path
) -> None:
    fixture = PlatformFixture(store)
    with pytest.raises(ValueError, match="verifier"):
        store.create_device_authorization(
            authorization_id="dev_bad",
            device_code_hash="raw-secret",
            user_code_hmac=verifier("4"),
            course_key="cse101-2026f",
            device_label="WSL",
            expires_at=NOW + timedelta(minutes=5),
            at=NOW,
        )

    authorization = store.create_device_authorization(
        authorization_id="dev_ok",
        device_code_hash=verifier("3"),
        user_code_hmac=verifier("4"),
        course_key="cse101-2026f",
        device_label="WSL",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    assert "hash" not in authorization.__dict__
    assert "hmac" not in authorization.__dict__

    with sqlite3.connect(database) as connection:
        device_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(device_authorizations)")
        }
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(platform_sessions)")
        }
        activation_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(platform_student_activations)"
            )
        }
    assert "device_code" not in device_columns
    assert "user_code" not in device_columns
    assert "refresh_token" not in session_columns
    assert {"device_code_hash", "user_code_hmac"} <= device_columns
    assert "access_token_hash" in session_columns
    assert "activation_code" not in activation_columns
    assert "code_hmac" in activation_columns
    assert fixture.student.active


def test_student_activation_reissue_and_atomic_one_time_redemption(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    first = fixture.activation(suffix="old", code_hash=verifier("a"))
    second = fixture.activation(suffix="new", code_hash=verifier("b"))

    assert store.get_student_activation(
        first.activation_id, course_key=COURSE
    ).state == StudentActivationState.REVOKED
    assert second.state == StudentActivationState.ISSUED
    assert (
        store.revoke_student_activation_by_id(
            first.activation_id,
            course_key=COURSE,
            at=NOW + timedelta(seconds=1),
        )
        is False
    )
    assert store.get_student_activation(
        second.activation_id, course_key=COURSE
    ).state == StudentActivationState.ISSUED

    for suffix, user_hash, device_hash in (
        ("one", verifier("c"), verifier("d")),
        ("two", verifier("e"), verifier("f")),
    ):
        store.create_device_authorization(
            authorization_id=f"dev_{suffix}",
            device_code_hash=device_hash,
            user_code_hmac=user_hash,
            course_key=COURSE,
            device_label=f"WSL {suffix}",
            expires_at=NOW + timedelta(minutes=5),
            at=NOW,
        )

    barrier = threading.Barrier(2)
    successes = []
    failures = []

    def redeem(user_hash: str) -> None:
        barrier.wait(timeout=5)
        try:
            successes.append(
                store.redeem_student_activation(
                    user_code_hmac=user_hash,
                    activation_code_hmac=verifier("b"),
                    course_key=COURSE,
                    at=NOW + timedelta(seconds=1),
                )
            )
        except PlatformAccessDenied as exc:
            failures.append(exc)

    threads = [
        threading.Thread(target=redeem, args=(verifier("c"),)),
        threading.Thread(target=redeem, args=(verifier("e"),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(failures) == 1
    activation, approved = successes[0]
    assert activation.state == StudentActivationState.CONSUMED
    assert activation.device_authorization_id == approved.authorization_id
    assert approved.state == DeviceAuthorizationState.APPROVED
    assert approved.student_id == fixture.student.id
    other_user_hash = verifier("e") if approved.authorization_id == "dev_one" else verifier("c")
    other = store.get_device_authorization_by_user_code_hmac(
        other_user_hash, course_key=COURSE
    )
    assert other.state == DeviceAuthorizationState.PENDING
    assert other.activation_failed_attempts == 1


def test_student_activation_failures_are_bounded_and_do_not_consume_valid_code(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    activation = fixture.activation(code_hash=verifier("a"))
    store.create_device_authorization(
        authorization_id="dev_attempts",
        device_code_hash=verifier("b"),
        user_code_hmac=verifier("c"),
        course_key=COURSE,
        device_label="WSL",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )

    for index in range(5):
        with pytest.raises(PlatformAccessDenied, match="could not be completed"):
            store.redeem_student_activation(
                user_code_hmac=verifier("c"),
                activation_code_hmac=verifier("d"),
                course_key=COURSE,
                max_failed_attempts=5,
                at=NOW + timedelta(seconds=index + 1),
            )

    device = store.get_device_authorization_by_user_code_hmac(
        verifier("c"), course_key=COURSE
    )
    assert device.state == DeviceAuthorizationState.DENIED
    assert device.activation_failed_attempts == 5
    assert store.get_student_activation(
        activation.activation_id, course_key=COURSE
    ).state == StudentActivationState.ISSUED


def test_expired_device_and_foreign_course_do_not_consume_student_activation(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    activation = fixture.activation(code_hash=verifier("a"))
    store.create_device_authorization(
        authorization_id="dev_expiring_activation",
        device_code_hash=verifier("b"),
        user_code_hmac=verifier("c"),
        course_key=COURSE,
        device_label="WSL",
        expires_at=NOW + timedelta(seconds=1),
        at=NOW,
    )
    with pytest.raises(PlatformAccessDenied):
        store.redeem_student_activation(
            user_code_hmac=verifier("c"),
            activation_code_hmac=verifier("a"),
            course_key=COURSE,
            at=NOW + timedelta(seconds=2),
        )
    assert store.get_student_activation(
        activation.activation_id, course_key=COURSE
    ).state == StudentActivationState.ISSUED

    other_enrollment = store.upsert_enrollment(
        student_id=fixture.student.id,
        course_key="other-course",
        at=NOW,
    )
    foreign = store.issue_student_activation(
        activation_id="act_foreign",
        student_id=fixture.student.id,
        course_key=other_enrollment.course_key,
        code_hmac=verifier("d"),
        expires_at=NOW + timedelta(days=7),
        at=NOW,
    )
    store.create_device_authorization(
        authorization_id="dev_local",
        device_code_hash=verifier("e"),
        user_code_hmac=verifier("f"),
        course_key=COURSE,
        device_label="WSL local",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    with pytest.raises(PlatformAccessDenied):
        store.redeem_student_activation(
            user_code_hmac=verifier("f"),
            activation_code_hmac=verifier("d"),
            course_key=COURSE,
            at=NOW + timedelta(seconds=1),
        )
    assert store.get_student_activation(
        foreign.activation_id, course_key="other-course"
    ).state == StudentActivationState.ISSUED


def test_activation_expiry_boundary_never_approves_device(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    activation = fixture.activation(
        code_hash=verifier("a"),
        expires_at=NOW + timedelta(seconds=1),
    )
    store.create_device_authorization(
        authorization_id="dev_activation_expiry",
        device_code_hash=verifier("b"),
        user_code_hmac=verifier("c"),
        course_key=COURSE,
        device_label="WSL",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )

    with pytest.raises(PlatformAccessDenied):
        store.redeem_student_activation(
            user_code_hmac=verifier("c"),
            activation_code_hmac=verifier("a"),
            course_key=COURSE,
            at=NOW + timedelta(seconds=1),
        )

    assert store.get_student_activation(
        activation.activation_id, course_key=COURSE
    ).state == StudentActivationState.EXPIRED
    device = store.get_device_authorization_by_user_code_hmac(
        verifier("c"), course_key=COURSE
    )
    assert device.state == DeviceAuthorizationState.PENDING
    assert device.activation_failed_attempts == 1


@pytest.mark.parametrize("binding", ["student", "course"])
def test_consumed_activation_sql_trigger_rejects_cross_scope_device_binding(
    store: PlatformStateStore,
    database: Path,
    binding: str,
) -> None:
    fixture = PlatformFixture(store)
    activation = fixture.activation(code_hash=verifier("a"))
    if binding == "student":
        owner = store.upsert_student(
            student_key="s002",
            auth_subject="github:202",
            github_user_id=202,
            github_login="student-two",
            at=NOW,
        )
        device_course = COURSE
    else:
        owner = fixture.student
        device_course = "other-course"
    store.upsert_enrollment(
        student_id=owner.id,
        course_key=device_course,
        at=NOW,
    )
    store.create_device_authorization(
        authorization_id=f"dev_cross_{binding}",
        device_code_hash=verifier("b"),
        user_code_hmac=verifier("c"),
        course_key=device_course,
        device_label="cross-scope WSL",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    store.approve_device_authorization(
        user_code_hmac=verifier("c"),
        auth_subject=owner.auth_subject,
        course_key=device_course,
        at=NOW + timedelta(seconds=1),
    )

    consumed_at = (NOW + timedelta(seconds=2)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="binding is inconsistent"):
            connection.execute(
                "UPDATE platform_student_activations "
                "SET state = 'consumed', consumed_at = ?, updated_at = ?, "
                "device_authorization_id = ? WHERE activation_id = ?",
                (
                    consumed_at,
                    consumed_at,
                    f"dev_cross_{binding}",
                    activation.activation_id,
                ),
            )

    assert store.get_student_activation(
        activation.activation_id, course_key=COURSE
    ).state == StudentActivationState.ISSUED


def test_course_deactivation_revokes_activation_without_revival(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    activation = fixture.activation(code_hash=verifier("a"))

    store.upsert_enrollment(
        student_id=fixture.student.id,
        course_key=COURSE,
        active=False,
        at=NOW + timedelta(seconds=1),
    )
    store.upsert_enrollment(
        student_id=fixture.student.id,
        course_key=COURSE,
        active=True,
        at=NOW + timedelta(seconds=2),
    )

    assert store.get_student_activation(
        activation.activation_id, course_key=COURSE
    ).state == StudentActivationState.REVOKED


def test_course_deactivation_requires_a_new_student_password_after_reactivation(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    store.set_student_password_hash(
        student_id=fixture.student.id,
        course_key=COURSE,
        password_hash=hash_student_password("482731"),
        at=NOW,
    )

    store.upsert_enrollment(
        student_id=fixture.student.id,
        course_key=COURSE,
        active=False,
        at=NOW + timedelta(seconds=1),
    )
    store.upsert_enrollment(
        student_id=fixture.student.id,
        course_key=COURSE,
        active=True,
        at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(PlatformNotFound, match="password credential"):
        store.get_student_password_credential(
            student_key="s001", course_key=COURSE
        )


def test_setting_password_requires_active_student_and_enrollment(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    password_hash = hash_student_password("482731")

    store.upsert_enrollment(
        student_id=fixture.student.id,
        course_key=COURSE,
        active=False,
        at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(PlatformAccessDenied, match="active course enrollment"):
        store.set_student_password_hash(
            student_id=fixture.student.id,
            course_key=COURSE,
            password_hash=password_hash,
            at=NOW + timedelta(seconds=2),
        )

    store.upsert_enrollment(
        student_id=fixture.student.id,
        course_key=COURSE,
        active=True,
        at=NOW + timedelta(seconds=3),
    )
    store.set_student_active(
        fixture.student.id,
        False,
        at=NOW + timedelta(seconds=4),
    )
    with pytest.raises(PlatformAccessDenied, match="active course enrollment"):
        store.set_student_password_hash(
            student_id=fixture.student.id,
            course_key=COURSE,
            password_hash=password_hash,
            at=NOW + timedelta(seconds=5),
        )

    with store._connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_student_passwords"
            ).fetchone()[0]
            == 0
        )


def test_password_rotation_revokes_sessions_without_deleting_the_replacement(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    store.set_student_password_hash(
        student_id=fixture.student.id,
        course_key=COURSE,
        password_hash=hash_student_password("123456"),
        at=NOW,
    )
    fixture.session()
    replacement = store.set_student_password_hash(
        student_id=fixture.student.id,
        course_key=COURSE,
        password_hash=hash_student_password("654321"),
        at=NOW + timedelta(seconds=3),
    )

    current = store.get_student_password_credential(
        student_key="s001", course_key=COURSE
    )
    assert current.password_hash == replacement.password_hash
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        store.authorize_access_token(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            at=NOW + timedelta(seconds=4),
        )


def test_legacy_password_hash_is_reported_as_requiring_a_reset(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    current_hash = hash_student_password("123456")
    legacy_hash = current_hash.replace("scrypt$v2$", "scrypt$v1$", 1)
    store.set_student_password_hash(
        student_id=fixture.student.id,
        course_key=COURSE,
        password_hash=legacy_hash,
        at=NOW,
    )

    legacy_summary = store.get_course_student_summary(
        course_key=COURSE, student_key="s001"
    )
    assert legacy_summary["password_configured"] is False
    assert legacy_summary["password_reset_required"] is True

    store.set_student_password_hash(
        student_id=fixture.student.id,
        course_key=COURSE,
        password_hash=current_hash,
        at=NOW + timedelta(seconds=1),
    )
    current_summary = store.get_course_student_summary(
        course_key=COURSE, student_key="s001"
    )
    assert current_summary["password_configured"] is True
    assert current_summary["password_reset_required"] is False


def test_device_authorization_is_one_time_and_creates_a_session(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    session = fixture.session()

    authorization = store.get_device_authorization_by_device_code_hash(
        verifier("3"), course_key=COURSE
    )
    assert authorization.state == DeviceAuthorizationState.CONSUMED
    assert authorization.student_id == fixture.student.id
    assert session.student_id == fixture.student.id
    with pytest.raises(PlatformInvalidTransition, match="consumed"):
        store.consume_device_authorization(
            device_code_hash=verifier("3"),
            course_key=COURSE,
            session_id="ses_again",
            token_family_id="fam_again",
            access_token_hash=verifier("6"),
            access_token_expires_at=NOW + timedelta(minutes=15),
            refresh_token_hash=verifier("7"),
            refresh_token_expires_at=NOW + timedelta(days=30),
            at=NOW + timedelta(seconds=3),
        )


def test_new_session_atomically_revokes_previous_shared_seat_tokens(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    first = fixture.session()
    second = fixture.session(
        suffix="2",
        access_hash=verifier("6"),
        refresh_hash=verifier("7"),
    )

    assert second.revoked_at is None
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        store.authorize_access_token(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            at=NOW + timedelta(minutes=1),
        )
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        store.rotate_refresh_token(
            presented_refresh_token_hash=verifier("2"),
            course_key=COURSE,
            replacement_refresh_token_hash=verifier("a"),
            replacement_refresh_token_expires_at=NOW + timedelta(days=30),
            access_token_hash=verifier("b"),
            access_token_expires_at=NOW + timedelta(minutes=15),
            at=NOW + timedelta(minutes=1),
        )
    sessions = store.list_student_sessions(
        student_id=fixture.student.id,
        course_key=COURSE,
    )
    assert {session.session_id for session in sessions} == {
        first.session_id,
        second.session_id,
    }
    assert sum(session.revoked_at is None for session in sessions) == 1


def test_failed_replacement_session_rolls_back_previous_session_revocation(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    first = fixture.session()
    store.create_device_authorization(
        authorization_id="dev_2",
        device_code_hash=verifier("8"),
        user_code_hmac=verifier("9"),
        course_key=COURSE,
        device_label="replacement seat",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    store.approve_device_authorization(
        user_code_hmac=verifier("9"),
        auth_subject=fixture.student.auth_subject,
        course_key=COURSE,
        at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(PlatformSessionLimitExceeded, match="retained"):
        store.consume_device_authorization(
            device_code_hash=verifier("8"),
            course_key=COURSE,
            session_id="ses_2",
            token_family_id="fam_2",
            access_token_hash=verifier("6"),
            access_token_expires_at=NOW + timedelta(minutes=15),
            refresh_token_hash=verifier("7"),
            refresh_token_expires_at=NOW + timedelta(hours=4),
            max_active_sessions=1,
            max_retained_sessions=1,
            at=NOW + timedelta(seconds=2),
        )

    authorized, _student = store.authorize_access_token(
        access_token_hash=verifier("1"),
        course_key=COURSE,
        at=NOW + timedelta(minutes=1),
    )
    assert authorized.session_id == first.session_id


def test_expired_or_inactively_enrolled_device_cannot_be_approved(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    store.create_device_authorization(
        authorization_id="dev_expired",
        device_code_hash=verifier("3"),
        user_code_hmac=verifier("4"),
        course_key="cse101-2026f",
        device_label="WSL",
        expires_at=NOW + timedelta(seconds=1),
        at=NOW,
    )
    with pytest.raises(DeviceAuthorizationExpired):
        store.approve_device_authorization(
            user_code_hmac=verifier("4"),
            auth_subject=fixture.student.auth_subject,
            course_key=COURSE,
            at=NOW + timedelta(seconds=2),
        )
    assert (
        store.get_device_authorization_by_user_code_hmac(
            verifier("4"), course_key=COURSE
        ).state
        == DeviceAuthorizationState.EXPIRED
    )

    store.upsert_enrollment(
        student_id=fixture.student.id,
        course_key="cse101-2026f",
        active=False,
        at=NOW + timedelta(seconds=3),
    )
    store.create_device_authorization(
        authorization_id="dev_inactive",
        device_code_hash=verifier("6"),
        user_code_hmac=verifier("7"),
        course_key="cse101-2026f",
        device_label="WSL",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    with pytest.raises(PlatformAccessDenied, match="enrollment"):
        store.approve_device_authorization(
            user_code_hmac=verifier("7"),
            auth_subject=fixture.student.auth_subject,
            course_key=COURSE,
            at=NOW + timedelta(seconds=4),
        )


def test_access_authorization_rechecks_expiry_enrollment_and_revocation(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    session = fixture.session()
    authorized, student = store.authorize_access_token(
        access_token_hash=verifier("1"),
        course_key="cse101-2026f",
        at=NOW + timedelta(minutes=2),
    )
    assert authorized.session_id == session.session_id
    assert student.id == fixture.student.id

    store.upsert_enrollment(
        student_id=student.id,
        course_key="cse101-2026f",
        active=False,
        at=NOW + timedelta(minutes=3),
    )
    with pytest.raises(PlatformAccessDenied, match="revoked|inactive"):
        store.authorize_access_token(
            access_token_hash=verifier("1"),
            course_key="cse101-2026f",
            at=NOW + timedelta(minutes=4),
        )
    store.upsert_enrollment(
        student_id=student.id,
        course_key="cse101-2026f",
        active=True,
        at=NOW + timedelta(minutes=5),
    )
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        store.authorize_access_token(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            at=NOW + timedelta(minutes=6),
        )


def test_operator_session_recovery_is_student_and_course_scoped(
    store: PlatformStateStore,
) -> None:
    first = PlatformFixture(store)
    first_session = first.session()
    second_student = store.upsert_student(
        student_key="s002",
        auth_subject="github:202",
        github_user_id=202,
        github_login="student-two",
        at=NOW,
    )
    store.upsert_enrollment(
        student_id=second_student.id,
        course_key=COURSE,
        at=NOW,
    )
    second = PlatformFixture.__new__(PlatformFixture)
    second.store = store
    second.student = second_student
    second.enrollment = store.require_active_enrollment(
        student_id=second_student.id,
        course_key=COURSE,
    )
    second_session = second.session(
        suffix="2",
        access_hash=verifier("6"),
        refresh_hash=verifier("7"),
    )

    assert store.list_student_sessions(
        student_id=first.student.id,
        course_key=COURSE,
    ) == [first_session]
    assert store.list_student_sessions(
        student_id=second_student.id,
        course_key=COURSE,
    ) == [second_session]
    with pytest.raises(PlatformAccessDenied, match="another student"):
        store.revoke_session(
            session_id=second_session.session_id,
            course_key=COURSE,
            owner_student_id=first.student.id,
            at=NOW + timedelta(minutes=1),
        )

    assert store.revoke_student_sessions(
        student_id=first.student.id,
        course_key=COURSE,
        at=NOW + timedelta(minutes=2),
    ) == 1
    assert store.revoke_student_sessions(
        student_id=first.student.id,
        course_key=COURSE,
        at=NOW + timedelta(minutes=3),
    ) == 0
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        store.authorize_access_token(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            at=NOW + timedelta(minutes=4),
        )
    authorized, student = store.authorize_access_token(
        access_token_hash=verifier("6"),
        course_key=COURSE,
        at=NOW + timedelta(minutes=4),
    )
    assert authorized.session_id == second_session.session_id
    assert student.id == second_student.id
    with pytest.raises(PlatformNotFound, match="not enrolled"):
        store.list_student_sessions(
            student_id=first.student.id,
            course_key="other-course",
        )


def test_refresh_rotation_and_reuse_revoke_the_token_family(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    rotated = store.rotate_refresh_token(
        presented_refresh_token_hash=verifier("2"),
        course_key=COURSE,
        replacement_refresh_token_hash=verifier("6"),
        replacement_refresh_token_expires_at=NOW + timedelta(days=30),
        access_token_hash=verifier("7"),
        access_token_expires_at=NOW + timedelta(minutes=20),
        at=NOW + timedelta(minutes=10),
    )
    assert rotated.session_id == "ses_1"
    with pytest.raises(PlatformAccessDenied):
        store.authorize_access_token(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            at=NOW + timedelta(minutes=11),
        )
    store.authorize_access_token(
        access_token_hash=verifier("7"),
        course_key=COURSE,
        at=NOW + timedelta(minutes=11),
    )

    with pytest.raises(RefreshTokenReuseDetected):
        store.rotate_refresh_token(
            presented_refresh_token_hash=verifier("2"),
            course_key=COURSE,
            replacement_refresh_token_hash=verifier("8"),
            replacement_refresh_token_expires_at=NOW + timedelta(days=30),
            access_token_hash=verifier("9"),
            access_token_expires_at=NOW + timedelta(minutes=25),
            at=NOW + timedelta(minutes=12),
        )
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        store.authorize_access_token(
            access_token_hash=verifier("7"),
            course_key=COURSE,
            at=NOW + timedelta(minutes=13),
        )


def test_refresh_rotation_cannot_move_the_original_session_deadline(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    original = fixture.session()
    absolute_deadline = original.refresh_token_expires_at

    rotated = store.rotate_refresh_token(
        presented_refresh_token_hash=verifier("2"),
        course_key=COURSE,
        replacement_refresh_token_hash=verifier("6"),
        replacement_refresh_token_expires_at=NOW + timedelta(days=60),
        access_token_hash=verifier("7"),
        access_token_expires_at=NOW + timedelta(days=30, minutes=10),
        at=NOW + timedelta(days=29, hours=23, minutes=50),
    )

    assert rotated.refresh_token_expires_at == absolute_deadline
    assert rotated.access_token_expires_at == absolute_deadline
    store.authorize_access_token(
        access_token_hash=verifier("7"),
        course_key=COURSE,
        at=NOW + timedelta(days=29, hours=23, minutes=59),
    )
    with pytest.raises(PlatformAccessDenied, match="expired"):
        store.authorize_access_token(
            access_token_hash=verifier("7"),
            course_key=COURSE,
            at=NOW + timedelta(days=30),
        )
    with pytest.raises(PlatformAccessDenied, match="expired|revoked"):
        store.rotate_refresh_token(
            presented_refresh_token_hash=verifier("6"),
            course_key=COURSE,
            replacement_refresh_token_hash=verifier("8"),
            replacement_refresh_token_expires_at=NOW + timedelta(days=60),
            access_token_hash=verifier("9"),
            access_token_expires_at=NOW + timedelta(days=30, minutes=15),
            at=NOW + timedelta(days=30),
        )


def test_identity_change_revokes_old_sessions_refresh_and_approved_device_grants(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    activation = fixture.activation(code_hash=verifier("a"))
    store.create_device_authorization(
        authorization_id="dev_pending_exchange",
        device_code_hash=verifier("6"),
        user_code_hmac=verifier("7"),
        course_key="cse101-2026f",
        device_label="old account browser",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    store.approve_device_authorization(
        user_code_hmac=verifier("7"),
        auth_subject="github:101",
        course_key=COURSE,
        at=NOW + timedelta(seconds=1),
    )

    updated = store.upsert_student(
        student_key="s001",
        auth_subject="github:202",
        github_user_id=202,
        github_login="replacement-account",
        at=NOW + timedelta(minutes=2),
    )

    assert updated.id == fixture.student.id
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        store.authorize_access_token(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            at=NOW + timedelta(minutes=3),
        )
    assert store.get_student_activation(
        activation.activation_id, course_key=COURSE
    ).state == StudentActivationState.REVOKED
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        store.rotate_refresh_token(
            presented_refresh_token_hash=verifier("2"),
            course_key=COURSE,
            replacement_refresh_token_hash=verifier("8"),
            replacement_refresh_token_expires_at=NOW + timedelta(days=30),
            access_token_hash=verifier("9"),
            access_token_expires_at=NOW + timedelta(minutes=20),
            at=NOW + timedelta(minutes=3),
        )
    assert (
        store.get_device_authorization_by_device_code_hash(
            verifier("6"), course_key=COURSE
        ).state
        == DeviceAuthorizationState.DENIED
    )


def test_identity_change_deletes_every_course_password(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    store.set_student_password_hash(
        student_id=fixture.student.id,
        course_key=COURSE,
        password_hash=hash_student_password("482731"),
        at=NOW,
    )

    store.upsert_student(
        student_key="s001",
        auth_subject="github:202",
        github_user_id=202,
        github_login="replacement-account",
        at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(PlatformNotFound, match="password credential"):
        store.get_student_password_credential(
            student_key="s001", course_key=COURSE
        )


def test_deactivation_does_not_revive_old_session_after_reactivation(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    activation = fixture.activation(code_hash=verifier("a"))

    store.set_student_active(fixture.student.id, False, at=NOW + timedelta(minutes=1))
    store.set_student_active(fixture.student.id, True, at=NOW + timedelta(minutes=2))

    with pytest.raises(PlatformAccessDenied, match="revoked"):
        store.authorize_access_token(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            at=NOW + timedelta(minutes=3),
        )
    assert store.get_student_activation(
        activation.activation_id, course_key=COURSE
    ).state == StudentActivationState.REVOKED


def test_assignment_is_student_scoped_available_and_grading_inputs_are_immutable(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    hidden = fixture.assignment(ready=False)
    assert store.list_owned_assignments(
        access_token_hash=verifier("1"), course_key=COURSE, at=NOW
    ) == []

    with pytest.raises(PlatformNotFound, match="this course"):
        store.set_assignment_availability(
            hidden.assignment_id,
            course_key="other-course",
            ready=True,
            at=NOW,
        )
    assert store.get_assignment(hidden.assignment_id).ready is False

    available = store.set_assignment_availability(
        hidden.assignment_id,
        course_key=COURSE,
        ready=True,
        at=NOW,
    )
    listed = store.list_owned_assignments(
        access_token_hash=verifier("1"), course_key="cse101-2026f", at=NOW
    )
    assert listed == [available]
    assert listed[0].assessment_digest == "sha256:" + ASSESSMENT_DIGEST
    assert listed[0].runner_image == RUNNER_IMAGE
    assert listed[0].opens_at < listed[0].due_at
    # Availability is mutable and must not make an otherwise identical
    # assignment release fail idempotent re-registration.
    assert fixture.assignment(ready=False) == available
    with pytest.raises(PlatformConflict, match="different assignment"):
        fixture.assignment(
            ready=False,
            runner_image="ghcr.io/example/runner@sha256:" + "c" * 64,
        )

    with pytest.raises(PlatformConflict, match="different assignment"):
        store.register_assignment(
            assignment_id="asn_01",
            student_id=fixture.student.id,
            course_key="cse101-2026f",
            assignment_key="lab01",
            release_id="lab01-v2",
            github_repository_id=9001,
            repository_owner="school-cse101",
            repository_name="lab01-s001",
            clone_url="https://github.com/school-cse101/lab01-s001.git",
            submission_mode="branch",
            target_ref="submission/lab01",
            result_policy="immediate",
            assessment_path="/srv/assessments/lab01",
            assessment_digest=ASSESSMENT_DIGEST,
            runner_image=RUNNER_IMAGE,
            rubric_version="v1",
            max_score=10,
        )
    with pytest.raises(ValueError, match="immutable"):
        store.register_assignment(
            assignment_id="asn_bad",
            student_id=fixture.student.id,
            course_key="cse101-2026f",
            assignment_key="bad",
            release_id="bad-v1",
            github_repository_id=9999,
            repository_owner="school-cse101",
            repository_name="bad-s001",
            clone_url="https://github.com/school-cse101/bad-s001.git",
            submission_mode="branch",
            target_ref="submission/bad",
            result_policy="manual",
            assessment_path="/srv/assessments/bad",
            assessment_digest=ASSESSMENT_DIGEST,
            runner_image="python:3.12",
            rubric_version="v1",
            max_score=10,
        )


def test_operator_assignment_listing_is_course_scoped_and_filterable(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    ready = fixture.assignment()
    hidden = fixture.assignment(
        assignment_id="asn_hidden",
        assignment_key="lab02",
        repository_id=9002,
        ready=False,
    )

    assert store.list_operator_assignments(
        course_key=COURSE,
        ready_only=True,
    ) == [ready]
    assert store.list_operator_assignments(course_key=COURSE) == [ready, hidden]
    assert store.list_operator_assignments(course_key="other-course") == []

    store.set_assignment_availability(
        hidden.assignment_id,
        course_key=COURSE,
        active=False,
        at=NOW,
    )
    assert store.list_operator_assignments(course_key=COURSE) == [ready]
    assert store.list_operator_assignments(
        course_key=COURSE,
        active_only=False,
    ) == [ready, store.get_assignment(hidden.assignment_id)]

    with pytest.raises(ValueError, match="between 1 and 10000"):
        store.list_operator_assignments(course_key=COURSE, limit=0)
    with pytest.raises(TypeError, match="booleans"):
        store.list_operator_assignments(course_key=COURSE, ready_only=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "assignment_path",
    ("../outside", "/absolute", "assignments/../outside", ".git/tests", "a\\b", "a//b"),
)
def test_assignment_registration_rejects_unsafe_repository_subpaths(
    store: PlatformStateStore,
    assignment_path: str,
) -> None:
    fixture = PlatformFixture(store)

    with pytest.raises(ValueError, match="assignment_subpath"):
        fixture.assignment(assignment_path=assignment_path)
    with pytest.raises(PlatformNotFound):
        store.get_assignment("asn_01")


def test_submission_idempotency_is_scoped_and_conflicting_reuse_is_rejected(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment(due_at=NOW + timedelta(minutes=5))
    created = fixture.request()
    replay = fixture.request(submission_id="sub_should_not_replace")
    assert not created.replayed
    assert replay.replayed
    assert replay.request.submission_id == created.request.submission_id
    assert replay.request.received_at == created.request.received_at

    with pytest.raises(PlatformIdempotencyConflict):
        fixture.request(submission_id="sub_conflict", request_hash=verifier("6"))
    with pytest.raises(PlatformAccessDenied, match="repository"):
        fixture.request(submission_id="sub_wrong_repo", key="other", repository_id=9999)


def test_submission_window_is_checked_atomically_but_exact_replay_survives_deadline(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment(due_at=NOW + timedelta(minutes=5))
    created = fixture.request().request

    replay = store.create_submission_request(
        submission_id="sub_replay_after_due",
        access_token_hash=verifier("1"),
        course_key=COURSE,
        assignment_id="asn_01",
        idempotency_key="idem-01",
        request_hash=verifier("5"),
        github_repository_id=9001,
        requested_sha=SHA,
        at=NOW + timedelta(minutes=10),
    )
    assert replay.replayed is True
    assert replay.request.submission_id == created.submission_id

    with pytest.raises(PlatformAccessDenied, match="unavailable"):
        store.create_submission_request(
            submission_id="sub_late",
            access_token_hash=verifier("1"),
            course_key=COURSE,
            assignment_id="asn_01",
            idempotency_key="late-request",
            request_hash=verifier("6"),
            github_repository_id=9001,
            requested_sha=SHA,
            at=NOW + timedelta(minutes=10),
        )


def test_atomic_admission_writes_receipt_and_coalesces_semantic_duplicates(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment(due_at=NOW + timedelta(minutes=5))

    first = fixture.accepted()
    receipt = store.get_receipt(first.request.submission_id)
    assert first.request.state == SubmissionState.ACCEPTED
    assert receipt.commit_sha == SHA
    assert receipt.received_at == receipt.accepted_at

    duplicate = fixture.accepted(
        submission_id="sub_unused",
        receipt_id="rcp_unused",
        key="semantic-key",
        request_hash=verifier("7"),
    )
    assert duplicate.replayed is True
    assert duplicate.request.submission_id == first.request.submission_id

    replay = store.find_submission_replay(
        access_token_hash=verifier("1"),
        course_key=COURSE,
        assignment_id="asn_01",
        idempotency_key="semantic-key",
        request_hash=verifier("7"),
        github_repository_id=9001,
        requested_sha=SHA,
        at=NOW + timedelta(minutes=10),
    )
    assert replay is not None
    assert replay.request.submission_id == first.request.submission_id
    with pytest.raises(PlatformIdempotencyConflict):
        store.find_submission_replay(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            assignment_id="asn_01",
            idempotency_key="semantic-key",
            request_hash=verifier("8"),
            github_repository_id=9001,
            requested_sha=OTHER_SHA,
            at=NOW + timedelta(minutes=2),
        )


def test_submission_replay_requires_current_active_enrollment(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment()
    fixture.accepted()
    store.upsert_enrollment(
        student_id=fixture.student.id,
        course_key="cse101-2026f",
        active=False,
        at=NOW + timedelta(minutes=2),
    )

    with pytest.raises(PlatformAccessDenied, match="revoked|inactive"):
        store.find_submission_replay(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            assignment_id="asn_01",
            idempotency_key="accepted-key",
            request_hash=verifier("6"),
            github_repository_id=9001,
            requested_sha=SHA,
            at=NOW + timedelta(minutes=3),
        )


def test_atomic_admission_enforces_outstanding_and_daily_limits(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment()

    fixture.accepted(
        submission_id="sub_1",
        receipt_id="rcp_1",
        key="key-1",
        request_hash=verifier("6"),
        sha="1" * 40,
        outstanding=2,
        daily=10,
    )
    fixture.accepted(
        submission_id="sub_2",
        receipt_id="rcp_2",
        key="key-2",
        request_hash=verifier("7"),
        sha="2" * 40,
        outstanding=2,
        daily=10,
    )
    with pytest.raises(PlatformSubmissionLimitExceeded, match="outstanding"):
        fixture.accepted(
            submission_id="sub_3",
            receipt_id="rcp_3",
            key="key-3",
            request_hash=verifier("8"),
            sha="3" * 40,
            outstanding=2,
            daily=10,
        )

    for submission_id in ("sub_1", "sub_2"):
        store.transition_submission(submission_id, "queued", at=NOW)
        store.transition_submission(
            submission_id,
            "infra_failed",
            failure_code="test_failure",
            failure_message="test failure",
            at=NOW,
        )
    with pytest.raises(PlatformSubmissionLimitExceeded, match="daily"):
        fixture.accepted(
            submission_id="sub_daily",
            receipt_id="rcp_daily",
            key="daily-key",
            request_hash=verifier("9"),
            sha="4" * 40,
            outstanding=10,
            daily=2,
        )


def test_graded_manual_result_does_not_consume_outstanding_slot(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment(result_policy=ResultPolicy.MANUAL)
    first = fixture.accepted(
        submission_id="sub_graded",
        receipt_id="rcp_graded",
        key="graded-key",
        outstanding=1,
    ).request
    store.transition_submission(first.submission_id, "queued", at=NOW)
    store.transition_submission(first.submission_id, "running", at=NOW)
    store.record_graded_result(
        first.submission_id,
        result_id="res_graded",
        score=8,
        max_score=10,
        at=NOW,
    )

    second = fixture.accepted(
        submission_id="sub_after_grade",
        receipt_id="rcp_after_grade",
        key="after-grade-key",
        request_hash=verifier("7"),
        sha=OTHER_SHA,
        outstanding=1,
    )
    assert second.request.state == SubmissionState.ACCEPTED

def test_atomic_admission_after_deadline_leaves_no_unpinned_request(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment(due_at=NOW + timedelta(minutes=5))

    with pytest.raises(PlatformAccessDenied, match="unavailable"):
        fixture.accepted(at=NOW + timedelta(minutes=6))
    assert store.list_submissions_for_processing(course_key=COURSE) == []


def test_submission_mode_enforces_pull_request_number(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment(mode=SubmissionMode.PULL_REQUEST)
    with pytest.raises(ValueError, match="required"):
        fixture.request()
    accepted = fixture.request(pr=4)
    assert accepted.request.pull_request_number == 4


def test_after_deadline_result_policy_requires_due_at(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    with pytest.raises(ValueError, match="requires due_at"):
        fixture.assignment(
            result_policy=ResultPolicy.AFTER_DEADLINE,
            without_due_at=True,
        )


def test_exact_sha_receipt_full_state_machine_and_owned_published_result(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment()
    request = fixture.request().request
    request = store.transition_submission(request.submission_id, "verifying", at=NOW)
    request = store.transition_submission(request.submission_id, "pinning", at=NOW)

    with pytest.raises(PlatformConflict, match="exact SHA"):
        store.accept_submission(
            request.submission_id,
            receipt_id="rcp_wrong",
            commit_sha=OTHER_SHA,
            source_path="/immutable/wrong.tar.gz",
            source_digest=SOURCE_DIGEST,
            at=NOW,
        )
    assert store.get_submission(request.submission_id).state == SubmissionState.PINNING

    request, receipt = store.accept_submission(
        request.submission_id,
        receipt_id="rcp_01",
        commit_sha=SHA,
        source_path="/immutable/sub_01.tar.gz",
        source_digest=SOURCE_DIGEST,
        snapshot_key="snapshot-01",
        at=NOW + timedelta(minutes=2),
    )
    assert request.state == SubmissionState.ACCEPTED
    assert receipt.received_at < receipt.accepted_at
    assert receipt.assessment_digest == "sha256:" + ASSESSMENT_DIGEST
    assert receipt.runner_image == RUNNER_IMAGE

    store.transition_submission(request.submission_id, "queued", at=NOW)
    store.transition_submission(request.submission_id, "running", at=NOW)
    request, result = store.record_graded_result(
        request.submission_id,
        result_id="res_01",
        score=8.5,
        max_score=10,
        rubric={"correctness": {"score": 8.5, "max_score": 10}},
        diagnostics=[{"path": "src/main.py", "line": 4, "message": "Check edge case"}],
        at=NOW + timedelta(minutes=3),
    )
    assert request.state == SubmissionState.GRADED
    with pytest.raises(PlatformNotFound, match="published"):
        store.get_owned_result(
            access_token_hash=verifier("1"),
            course_key=COURSE,
            submission_id=request.submission_id,
            at=NOW,
        )

    request, published = store.publish_result(
        request.submission_id, at=NOW + timedelta(minutes=4)
    )
    owned = store.get_owned_result(
        access_token_hash=verifier("1"),
        course_key=COURSE,
        submission_id=request.submission_id,
        at=NOW,
    )
    assert request.state == SubmissionState.PUBLISHED
    assert owned == published
    assert owned.commit_sha == SHA
    assert owned.diagnostics[0]["path"] == "src/main.py"


def test_result_and_submission_ids_do_not_bypass_student_ownership(
    store: PlatformStateStore,
) -> None:
    first = PlatformFixture(store)
    first.session()
    first.assignment()
    submission = first.request().request

    second = store.upsert_student(
        student_key="s002",
        auth_subject="github:202",
        github_user_id=202,
        github_login="student-two",
        at=NOW,
    )
    store.upsert_enrollment(student_id=second.id, course_key="cse101-2026f", at=NOW)
    other = PlatformFixture.__new__(PlatformFixture)
    other.store = store
    other.student = second
    other.enrollment = store.require_active_enrollment(
        student_id=second.id, course_key="cse101-2026f"
    )
    other.session(
        suffix="2", access_hash=verifier("6"), refresh_hash=verifier("7")
    )

    with pytest.raises(PlatformNotFound, match="this student"):
        store.get_owned_submission(
            access_token_hash=verifier("6"),
            course_key=COURSE,
            submission_id=submission.submission_id,
            at=NOW,
        )


def test_failure_states_are_terminal_and_require_structured_reason(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment()
    submission = fixture.request().request
    store.transition_submission(submission.submission_id, "verifying", at=NOW)
    with pytest.raises(ValueError, match="failure_code"):
        store.transition_submission(submission.submission_id, "rejected", at=NOW)
    rejected = store.transition_submission(
        submission.submission_id,
        "rejected",
        failure_code="source_changed_or_unavailable",
        failure_message="The requested ref no longer points to the submitted SHA.",
        at=NOW,
    )
    assert rejected.state == SubmissionState.REJECTED
    with pytest.raises(PlatformInvalidTransition):
        store.transition_submission(rejected.submission_id, "verifying", at=NOW)
    with pytest.raises(PlatformInvalidTransition, match="atomic"):
        store.transition_submission(rejected.submission_id, "graded", at=NOW)


def test_worker_recovery_scan_returns_only_requested_nonterminal_states(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment()
    first = fixture.request(submission_id="sub_first", key="first").request
    second = fixture.request(submission_id="sub_second", key="second").request
    store.transition_submission(first.submission_id, "verifying", at=NOW)
    store.transition_submission(
        second.submission_id,
        "verifying",
        at=NOW,
    )
    store.transition_submission(
        second.submission_id,
        "rejected",
        failure_code="invalid_source",
        failure_message="invalid source",
        at=NOW,
    )

    pending = store.list_submissions_for_processing(
        course_key=COURSE, states=["verifying", "queued"]
    )

    assert [item.submission_id for item in pending] == [first.submission_id]
    assert store.list_submissions_for_processing(course_key=COURSE, states=[]) == []
    with pytest.raises(ValueError, match="limit"):
        store.list_submissions_for_processing(course_key=COURSE, limit=0)


def test_operator_submission_lookup_is_course_scoped_filtered_and_bounded(
    store: PlatformStateStore,
) -> None:
    fixture = PlatformFixture(store)
    fixture.session()
    fixture.assignment()
    request = fixture.accepted().request
    store.transition_submission(request.submission_id, "queued", at=NOW)
    store.transition_submission(request.submission_id, "running", at=NOW)
    store.record_graded_result(
        request.submission_id,
        result_id="res_operator",
        score=7.5,
        max_score=10,
        rubric={"private_feedback": "must not appear in operator projection"},
        diagnostics=[{"message": "student raw output must not appear"}],
        at=NOW + timedelta(minutes=2),
    )
    store.publish_result(request.submission_id, at=NOW + timedelta(minutes=3))

    listed = store.list_operator_submissions(
        course_key="cse101-2026f",
        student_key="s001",
        assignment_key="lab01",
        state=SubmissionState.PUBLISHED,
        limit=1,
    )
    assert len(listed) == 1
    item = listed[0]
    assert item.submission_id == request.submission_id
    assert item.student_key == "s001"
    assert item.commit_sha == SHA
    assert item.source_digest == "sha256:" + SOURCE_DIGEST
    assert item.score == 7.5
    assert item.published_at is not None
    assert not hasattr(item, "source_path")
    assert not hasattr(item, "rubric")
    assert not hasattr(item, "diagnostics")

    assert store.list_operator_submissions(course_key="other-course") == []
    with pytest.raises(PlatformNotFound, match="this course"):
        store.get_operator_submission(
            request.submission_id,
            course_key="other-course",
        )
    with pytest.raises(ValueError, match="between 1 and 500"):
        store.list_operator_submissions(course_key="cse101-2026f", limit=501)
