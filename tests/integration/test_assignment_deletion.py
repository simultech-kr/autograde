"""Recoverable assignment deletion preserves grading evidence and fences writes."""
from datetime import datetime, timedelta, timezone

import pytest

from autograde.assignment_admin import AssignmentAdminService, initialize_assignment_admin
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, PlatformStateStore
from autograde.settings import AppPaths


COURSE = "come2201"
ASSIGNMENT = "synthetic-deletion-release"


@pytest.fixture
def published(tmp_path):
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    state = PlatformStateStore(paths.database)
    admin = AssignmentAdminService(state, paths)
    draft = admin.create_draft(COURSE, title="Synthetic lifecycle fixture", due_at="2098-01-01T00:00:00Z")
    starter = tmp_path / "starter.txt"
    starter.write_text("synthetic immutable starter", encoding="utf-8")
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("# Synthetic fixture, never executed.\n", encoding="utf-8")
    release = state.register_bundle_assignment_release(
        assignment_id=ASSIGNMENT, course_key=COURSE, assignment_key="lifecycle-fixture",
        release_id="v1", title=draft["title"], starter_path=str(starter), starter_digest="a" * 64,
        starter_size_bytes=starter.stat().st_size, assessment_path=str(assessment), assessment_digest="b" * 64,
        runner_image="pilot-local:v1", rubric_version="v1", max_score=10, result_policy="immediate",
        due_at="2098-01-01T00:00:00Z", ready=True,
    )
    # Model an already published release without invoking a compiler. These
    # tests exercise lifecycle transactions, not grading or publishing validation.
    with state._write() as connection:
        connection.execute("UPDATE instructor_assignment_drafts SET published_assignment_id=? WHERE draft_id=?",
            (release.assignment_id, draft["draft_id"]))
    return admin, draft, release


def deletion_events(admin):
    with admin.state._connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM instructor_assignment_events WHERE assignment_id=? ORDER BY id", (ASSIGNMENT,),
        )]


def test_delete_requires_both_archive_flags_and_restore_stays_archived(published):
    admin, draft, release = published
    with pytest.raises(PlatformConflict, match="보관"):
        admin.delete_release(COURSE, ASSIGNMENT)
    admin.hide_release(COURSE, ASSIGNMENT)
    with pytest.raises(PlatformConflict, match="보관"):
        admin.delete_release(COURSE, ASSIGNMENT)
    admin.state.set_bundle_assignment_availability(ASSIGNMENT, course_key=COURSE, active=False, ready=True)
    with pytest.raises(PlatformConflict, match="보관"):
        admin.delete_release(COURSE, ASSIGNMENT)
    archived = admin.archive_release(COURSE, ASSIGNMENT)
    assert not archived.active and not archived.ready
    deleted = admin.delete_release(COURSE, ASSIGNMENT)
    assert not deleted.active and not deleted.ready
    current = admin.get_draft(COURSE, draft["draft_id"])
    assert current["visibility"] == "deleted" and current["deletion_at"]
    assert not current["can_publish"]
    assert admin.list_drafts(COURSE) == []
    restored = admin.restore_release(COURSE, ASSIGNMENT)
    assert not restored.active and not restored.ready
    current = admin.get_draft(COURSE, draft["draft_id"])
    assert current["visibility"] == "inactive" and current["deletion_at"] is None
    assert len(admin.list_drafts(COURSE)) == 1


def test_repeated_delete_restore_and_cli_origin_have_single_audit_identity(published):
    admin, draft, _ = published
    with admin.state._write() as connection:
        connection.execute("UPDATE instructor_assignment_drafts SET published_assignment_id=NULL WHERE draft_id=?",
            (draft["draft_id"],))
    admin.archive_release(COURSE, ASSIGNMENT)
    admin.archive_release(COURSE, ASSIGNMENT)
    admin.delete_release(COURSE, ASSIGNMENT)
    with admin.state._connection() as connection:
        first_deleted_at = connection.execute("SELECT deleted_at FROM instructor_assignment_deletions").fetchone()[0]
    admin.delete_release(COURSE, ASSIGNMENT)
    with admin.state._connection() as connection:
        assert connection.execute("SELECT deleted_at FROM instructor_assignment_deletions").fetchone()[0] == first_deleted_at
    admin.restore_release(COURSE, ASSIGNMENT)
    admin.restore_release(COURSE, ASSIGNMENT)
    events = deletion_events(admin)
    assert [event["action"] for event in events] == ["archived", "release_deleted", "release_restored"]
    assert all(event["draft_id"] is None and event["course_key"] == COURSE for event in events)
    assert all(event["assignment_id"] == ASSIGNMENT for event in events)


@pytest.mark.parametrize("operation", ["delete_release", "restore_release"])
def test_lifecycle_operations_recheck_course_scope_and_archival_inside_write(published, operation):
    admin, _, _ = published
    admin.archive_release(COURSE, ASSIGNMENT)
    if operation == "restore_release":
        admin.delete_release(COURSE, ASSIGNMENT)
    mutate = getattr(admin, operation)
    with pytest.raises(PlatformNotFound):
        mutate("come3105", ASSIGNMENT)
    before = deletion_events(admin)
    admin.course_status = lambda _: "active"  # stale preflight must not authorize the write
    with admin.state._write() as connection:
        connection.execute("UPDATE admin_courses SET status='archived' WHERE course_key=?", (COURSE,))
    with pytest.raises(PlatformConflict):
        mutate(COURSE, ASSIGNMENT)
    assert deletion_events(admin) == before


def test_deleted_release_blocks_service_and_cli_mutation_paths(published):
    admin, draft, _ = published
    admin.archive_release(COURSE, ASSIGNMENT)
    admin.delete_release(COURSE, ASSIGNMENT)
    operations = [
        lambda: admin.copy_release(COURSE, ASSIGNMENT),
        lambda: admin.hide_release(COURSE, ASSIGNMENT),
        lambda: admin.archive_release(COURSE, ASSIGNMENT),
        lambda: admin.update_assignment_document(COURSE, ASSIGNMENT, 0, "changed", "correction"),
        lambda: admin.extend_deadline(COURSE, ASSIGNMENT, "2099-01-01T00:00:00Z", "extension"),
        lambda: admin.publish(COURSE, draft["draft_id"], draft["revision"]),
        lambda: admin.queue_check(COURSE, draft["draft_id"], draft["revision"], True),
        lambda: admin.state.begin_bundle_release_check(ASSIGNMENT, course_key=COURSE),
        lambda: admin.state.publish_validated_bundle_assignment(ASSIGNMENT, course_key=COURSE),
        lambda: admin.state.set_bundle_assignment_availability(ASSIGNMENT, course_key=COURSE, active=True, ready=True),
        lambda: admin.state.set_bundle_assignment_availability(ASSIGNMENT, course_key=COURSE, ready=False),
    ]
    before = deletion_events(admin)
    for operation in operations:
        with pytest.raises(PlatformConflict):
            operation()
    assert deletion_events(admin) == before
    assert admin.state.bundle_operation_history(ASSIGNMENT, course_key=COURSE)["deadline_changes"] == []


@pytest.mark.parametrize("availability", ["active=1", "ready=1", "active=1,ready=1"])
def test_database_guard_prevents_deleted_release_reactivation(published, availability):
    admin, _, _ = published
    admin.archive_release(COURSE, ASSIGNMENT)
    admin.delete_release(COURSE, ASSIGNMENT)
    with pytest.raises(PlatformConflict, match="삭제"):
        with admin.state._write() as connection:
            connection.execute(f"UPDATE bundle_assignment_releases SET {availability} WHERE assignment_id=?", (ASSIGNMENT,))
    release = admin.state.get_bundle_assignment(ASSIGNMENT)
    assert not release.active and not release.ready


def test_pending_validation_must_finish_before_deletion(published):
    admin, _, _ = published
    admin.archive_release(COURSE, ASSIGNMENT)
    check = admin.state.begin_bundle_release_check(ASSIGNMENT, course_key=COURSE)
    with pytest.raises(PlatformConflict, match="검증"):
        admin.delete_release(COURSE, ASSIGNMENT)
    admin.state.finish_bundle_release_check(check, passed=True, details={"synthetic": True})
    admin.delete_release(COURSE, ASSIGNMENT)


def test_deleted_published_draft_frees_management_capacity(published):
    admin, draft, _ = published
    with admin.state._write() as connection:
        for index in range(99):
            connection.execute(
                "INSERT INTO instructor_assignment_drafts "
                "(draft_id,course_key,revision,document_json,created_at,updated_at) "
                "SELECT ?,course_key,revision,document_json,created_at,updated_at "
                "FROM instructor_assignment_drafts WHERE draft_id=?", (f"synthetic-capacity-{index}", draft["draft_id"]),
            )
    with pytest.raises(PlatformConflict, match="한도"):
        admin.create_draft(COURSE)
    admin.archive_release(COURSE, ASSIGNMENT)
    admin.delete_release(COURSE, ASSIGNMENT)
    replacement = admin.create_draft(COURSE)
    assert len(admin.list_drafts(COURSE)) == 100
    with pytest.raises(PlatformConflict, match="한도"):
        admin.restore_release(COURSE, ASSIGNMENT)
    assert admin.get_draft(COURSE, draft["draft_id"])["visibility"] == "deleted"
    admin.delete_draft(COURSE, replacement["draft_id"], replacement["revision"])
    admin.restore_release(COURSE, ASSIGNMENT)
    assert len(admin.list_drafts(COURSE)) == 100


def test_upgrade_adds_deletion_repository_without_losing_existing_draft_or_audit(published):
    admin, draft, _ = published
    # Recreate the immediately preceding schema only in this synthetic database.
    with admin.state._write() as connection:
        for name in ("scope", "release_guard", "check_guard", "document_guard"):
            prefix = "instructor_assignment_deletion_" if name == "scope" else "instructor_assignment_deleted_"
            connection.execute(f"DROP TRIGGER {prefix}{name}")
        connection.execute("DROP TABLE instructor_assignment_deletions")
        connection.execute("ALTER TABLE instructor_assignment_events DROP COLUMN assignment_id")
        connection.execute("DELETE FROM platform_schema_migrations WHERE version=15")
    upgraded = PlatformStateStore(admin.paths.database)
    assert upgraded.schema_version() == upgraded.LATEST_SCHEMA_VERSION
    initialize_assignment_admin(upgraded)
    initialize_assignment_admin(upgraded)
    service = AssignmentAdminService(upgraded, admin.paths)
    assert service.get_draft(COURSE, draft["draft_id"])["title"] == draft["title"]
    with upgraded._connection() as connection:
        old = connection.execute("SELECT action,assignment_id FROM instructor_assignment_events").fetchone()
        assert old["action"] == "created" and old["assignment_id"] is None
    service.archive_release(COURSE, ASSIGNMENT)
    service.delete_release(COURSE, ASSIGNMENT)
    service.restore_release(COURSE, ASSIGNMENT)


def admit_synthetic_submission(admin, tmp_path):
    state = admin.state
    now = datetime.now(timezone.utc)
    student = state.upsert_local_student(student_key="synthetic-delete-student", auth_subject="school:synthetic-delete-student")
    state.upsert_enrollment(student_id=student.id, course_key=COURSE)
    state.create_device_authorization(authorization_id="synthetic-device", device_code_hash="1" * 64,
        user_code_hmac="2" * 64, course_key=COURSE, device_label="Synthetic lifecycle test", expires_at=now + timedelta(hours=1))
    state.approve_device_authorization(user_code_hmac="2" * 64, auth_subject=student.auth_subject, course_key=COURSE)
    state.consume_device_authorization(device_code_hash="1" * 64, course_key=COURSE,
        session_id="synthetic-session", token_family_id="synthetic-family", access_token_hash="3" * 64,
        access_token_expires_at=now + timedelta(hours=1), refresh_token_hash="4" * 64,
        refresh_token_expires_at=now + timedelta(days=1))
    source = tmp_path / "submitted-source.cpp"
    source.write_text("int main(){return 0;}\n", encoding="utf-8")
    state.create_accepted_bundle_submission(submission_id="synthetic-submission", receipt_id="synthetic-receipt",
        access_token_hash="3" * 64, course_key=COURSE, assignment_id=ASSIGNMENT,
        idempotency_key="synthetic-request", request_hash="5" * 64, source_path=str(source),
        source_digest="6" * 64, source_size_bytes=source.stat().st_size)
    owned = dict(access_token_hash="3" * 64, course_key=COURSE, submission_id="synthetic-submission")
    return source, owned


def test_submitted_source_receipt_grade_and_document_survive_delete_restore(published, tmp_path):
    admin, draft, release = published
    state = admin.state
    source, owned = admit_synthetic_submission(admin, tmp_path)
    state.transition_bundle_submission("synthetic-submission", "queued")
    state.transition_bundle_submission("synthetic-submission", "running")
    state.record_bundle_graded_result("synthetic-submission", result_id="synthetic-result", score=7, max_score=10)
    state.publish_bundle_result("synthetic-submission")
    before_receipt = state.get_owned_bundle_receipt(**owned)
    before_grade = state.get_owned_bundle_result(**owned)
    before_document = admin.get_assignment_document(COURSE, ASSIGNMENT)
    before_files = {path: path.read_bytes() for path in (source, tmp_path / "starter.txt", tmp_path / "assessment" / "grade.py")}
    admin.archive_release(COURSE, ASSIGNMENT)
    for operation in (admin.delete_release, admin.restore_release):
        operation(COURSE, ASSIGNMENT)
        assert state.get_owned_bundle_receipt(**owned) == before_receipt
        assert state.get_owned_bundle_result(**owned) == before_grade
        assert admin.get_assignment_document(COURSE, ASSIGNMENT) == before_document
        assert {path: path.read_bytes() for path in before_files} == before_files
        assert admin.get_draft(COURSE, draft["draft_id"])["published_assignment_id"] == release.assignment_id
    with state._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM bundle_submission_documents").fetchone()[0] == 1


@pytest.mark.parametrize("in_flight_state", ["queued", "running"])
def test_admitted_work_finishes_after_delete_but_new_submission_and_download_are_denied(
    published, tmp_path, in_flight_state,
):
    admin, draft, _ = published
    state = admin.state
    source, owned = admit_synthetic_submission(admin, tmp_path)
    state.record_bundle_download(download_id="synthetic-download-before-delete",
        access_token_hash=owned["access_token_hash"], course_key=COURSE, assignment_id=ASSIGNMENT)
    receipt = state.get_owned_bundle_receipt(**owned)
    state.transition_bundle_submission(owned["submission_id"], "queued")
    if in_flight_state == "running":
        state.transition_bundle_submission(owned["submission_id"], "running")

    admin.archive_release(COURSE, ASSIGNMENT)
    admin.delete_release(COURSE, ASSIGNMENT)
    assert admin.get_draft(COURSE, draft["draft_id"])["visibility"] == "deleted"
    with pytest.raises(PlatformAccessDenied):
        state.create_accepted_bundle_submission(submission_id="synthetic-new-submission", receipt_id="synthetic-new-receipt",
            access_token_hash=owned["access_token_hash"], course_key=COURSE, assignment_id=ASSIGNMENT,
            idempotency_key="synthetic-new-request", request_hash="7" * 64, source_path=str(source),
            source_digest="6" * 64, source_size_bytes=source.stat().st_size)
    with pytest.raises(PlatformAccessDenied):
        state.record_bundle_download(download_id="synthetic-new-download",
            access_token_hash=owned["access_token_hash"], course_key=COURSE, assignment_id=ASSIGNMENT)

    # Exercise the same durable state transitions used by the grading worker.
    # Existing receipts remain usable after admission has been closed.
    if in_flight_state == "queued":
        state.transition_bundle_submission(owned["submission_id"], "running")
    state.record_bundle_graded_result(owned["submission_id"], result_id="synthetic-post-delete-result", score=7, max_score=10)
    state.publish_bundle_result(owned["submission_id"])
    assert state.get_owned_bundle_submission(**owned).state == "published"
    assert state.get_owned_bundle_receipt(**owned) == receipt
    assert state.get_owned_bundle_result(**owned).score == 7
    assert source.read_text(encoding="utf-8") == "int main(){return 0;}\n"
    assert admin.get_draft(COURSE, draft["draft_id"])["visibility"] == "deleted"
    with state._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM bundle_submission_requests").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM bundle_download_events").fetchone()[0] == 1
