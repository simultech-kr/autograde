"""Bulk lifecycle confirmations are bounded, scoped, stale-safe and atomic."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from autograde.assignment_admin import AssignmentAdminService
from autograde.platform_state import PlatformConflict, PlatformNotFound, PlatformStateStore, utc_iso
from autograde.settings import AppPaths


COURSE = "come2201"
IDS = ["synthetic-batch-a", "synthetic-batch-b", "synthetic-batch-cli"]


@pytest.fixture
def batch(tmp_path):
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    state = PlatformStateStore(paths.database)
    admin = AssignmentAdminService(state, paths)
    starter = tmp_path / "starter.cpp"
    starter.write_text("int main(){return 0;}\n", encoding="utf-8")
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("# Synthetic fixture, never executed.\n", encoding="utf-8")

    def release(assignment_id, *, course=COURSE, published=True, validation=False):
        draft = admin.create_draft(course, title=assignment_id) if published or validation else None
        state.register_bundle_assignment_release(
            assignment_id=assignment_id, course_key=course, assignment_key=assignment_id,
            release_id="v1", title=assignment_id, starter_path=str(starter), starter_digest="a" * 64,
            starter_size_bytes=starter.stat().st_size, assessment_path=str(assessment), assessment_digest="b" * 64,
            runner_image="pilot-local:v1", rubric_version="v1", max_score=10, result_policy="immediate",
            due_at="2098-01-01T00:00:00Z", ready=not validation,
        )
        with state._write() as connection:
            if published:
                connection.execute("UPDATE instructor_assignment_drafts SET published_assignment_id=? WHERE draft_id=?",
                                   (assignment_id, draft["draft_id"]))
            if validation:
                connection.execute(
                    "INSERT INTO instructor_assignment_jobs(job_id,draft_id,course_key,revision,status,assignment_id,created_at) "
                    "VALUES (?,?,?,1,'succeeded',?,?)",
                    ("check-" + assignment_id, draft["draft_id"], course, assignment_id, utc_iso()),
                )
        return draft

    for assignment_id in IDS:
        release(assignment_id, published=assignment_id != IDS[-1])
    return admin, release


def snapshot(admin):
    with admin.state._connection() as connection:
        return tuple(connection.iterdump())


def apply(admin, action, assignment_ids=IDS):
    preview = admin.preview_release_batch(COURSE, action, assignment_ids)
    return admin.apply_release_batch(COURSE, action, assignment_ids, preview["fingerprint"])


def lifecycle_events(admin):
    with admin.state._connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM instructor_assignment_events WHERE assignment_id IS NOT NULL ORDER BY id")]


@pytest.mark.parametrize("assignment_ids", [
    [], "synthetic-batch-a", None, [IDS[0], IDS[0]], ["../private"], [""], [True],
    ["과제"], ["a" * 256], [f"assignment-{index}" for index in range(21)],
])
def test_invalid_selection_has_no_side_effects(batch, assignment_ids):
    admin, _ = batch
    before = snapshot(admin)
    for method, extra in ((admin.preview_release_batch, ()), (admin.apply_release_batch, ("invalid",))):
        with pytest.raises(ValueError):
            method(COURSE, "archive", assignment_ids, *extra)
    assert snapshot(admin) == before


@pytest.mark.parametrize("action", ["restore", "hide", "", None, []])
def test_invalid_action_has_no_side_effects(batch, action):
    admin, _ = batch
    before = snapshot(admin)
    with pytest.raises(ValueError):
        admin.preview_release_batch(COURSE, action, IDS)
    assert snapshot(admin) == before


def test_preview_is_read_only_sorted_and_exposes_only_summary(batch):
    admin, _ = batch
    before = snapshot(admin)
    preview = admin.preview_release_batch(COURSE, "archive", list(reversed(IDS)))
    assert preview["assignment_ids"] == IDS
    assert preview["count"] == 3
    assert preview["action"] == "archive"
    assert len(preview["fingerprint"]) == 64
    assert preview == admin.preview_release_batch(COURSE, "archive", IDS)
    assert snapshot(admin) == before
    assert [item["assignment_id"] for item in preview["items"]] == IDS
    for item in preview["items"]:
        assert set(item) == {"assignment_id", "title", "active", "ready", "deleted_at",
                             "accepted_students", "submitted_students", "submission_count"}
        assert item["active"] and item["ready"] and item["deleted_at"] is None
        assert item["accepted_students"] == item["submitted_students"] == item["submission_count"] == 0


def test_twenty_targets_are_allowed(batch):
    admin, release = batch
    ids = [f"synthetic-limit-{index}" for index in range(20)]
    for assignment_id in ids:
        release(assignment_id, published=False)
    assert apply(admin, "archive", ids)["changed_count"] == 20


@pytest.mark.parametrize("action", ["archive", "delete"])
@pytest.mark.parametrize("kind", ["missing", "foreign", "deleted", "pending", "private", "draft"])
def test_one_ineligible_target_rejects_the_whole_batch(batch, action, kind):
    admin, release = batch
    bad_id = "zz-invalid"
    if kind == "foreign":
        release(bad_id, course="come3105")
    elif kind == "private":
        release(bad_id, published=False, validation=True)
    elif kind == "draft":
        bad_id = admin.create_draft(COURSE)["draft_id"]
    elif kind != "missing":
        release(bad_id)
        admin.archive_release(COURSE, bad_id)
        if kind == "deleted":
            admin.delete_release(COURSE, bad_id)
        else:
            admin.state.begin_bundle_release_check(bad_id, course_key=COURSE)
    if action == "delete":
        admin.archive_release(COURSE, IDS[0])
    before = snapshot(admin)
    error = PlatformConflict if kind in {"deleted", "pending"} else PlatformNotFound
    for method, extra in ((admin.preview_release_batch, ()), (admin.apply_release_batch, ("unused",))):
        with pytest.raises(error):
            method(COURSE, action, [IDS[0], bad_id], *extra)
    assert snapshot(admin) == before


def test_pending_published_draft_job_is_rejected_even_without_release_check(batch):
    admin, _ = batch
    with admin.state._write() as connection:
        connection.execute(
            "INSERT INTO instructor_assignment_jobs(job_id,draft_id,course_key,revision,status,created_at) "
            "SELECT 'pending-job',draft_id,course_key,revision,'queued',? FROM instructor_assignment_drafts "
            "WHERE published_assignment_id=?", (utc_iso(), IDS[0]),
        )
    before = snapshot(admin)
    with pytest.raises(PlatformConflict, match="검증"):
        admin.preview_release_batch(COURSE, "archive", IDS)
    assert snapshot(admin) == before


def test_archive_is_idempotent_and_delete_requires_every_target_archived(batch):
    admin, _ = batch
    admin.archive_release(COURSE, IDS[0])
    before = snapshot(admin)
    with pytest.raises(PlatformConflict, match="모두 보관"):
        admin.preview_release_batch(COURSE, "delete", IDS)
    assert snapshot(admin) == before
    assert apply(admin, "archive") == {"action": "archive", "count": 3, "changed_count": 2, "unchanged_count": 1}
    archived = snapshot(admin)
    assert apply(admin, "archive") == {"action": "archive", "count": 3, "changed_count": 0, "unchanged_count": 3}
    assert snapshot(admin) == archived
    assert apply(admin, "delete") == {"action": "delete", "count": 3, "changed_count": 3, "unchanged_count": 0}
    events = lifecycle_events(admin)
    assert [event["action"] for event in events] == ["archived"] * 3 + ["release_deleted"] * 3
    assert [event["assignment_id"] for event in events] == IDS * 2
    assert all(event["course_key"] == COURSE for event in events)
    assert all(event["draft_id"] is not None for event in events if event["assignment_id"] != IDS[-1])
    assert all(event["draft_id"] is None for event in events if event["assignment_id"] == IDS[-1])
    with pytest.raises(PlatformConflict, match="삭제"):
        admin.preview_release_batch(COURSE, "delete", IDS)


@pytest.mark.parametrize("action", ["archive", "delete"])
def test_failure_after_first_mutation_rolls_back_all_flags_deletions_and_audits(batch, monkeypatch, action):
    admin, _ = batch
    if action == "delete":
        apply(admin, "archive")
    preview = admin.preview_release_batch(COURSE, action, IDS)
    before = snapshot(admin)
    original = admin._event

    def fail_second(connection, course, draft_id, event_action, assignment_id=None):
        if assignment_id == IDS[1]:
            raise RuntimeError("synthetic audit failure")
        original(connection, course, draft_id, event_action, assignment_id)

    monkeypatch.setattr(admin, "_event", fail_second)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        admin.apply_release_batch(COURSE, action, IDS, preview["fingerprint"])
    assert snapshot(admin) == before


@pytest.mark.parametrize("action", ["archive", "delete"])
def test_updated_target_invalidates_confirmation_without_partial_changes(batch, action):
    admin, _ = batch
    if action == "delete":
        apply(admin, "archive")
    preview = admin.preview_release_batch(COURSE, action, IDS)
    with admin.state._write() as connection:
        connection.execute("UPDATE bundle_assignment_releases SET updated_at=? WHERE assignment_id=?",
                           ("2097-01-01T00:00:00Z", IDS[-1]))
    before = snapshot(admin)
    with pytest.raises(PlatformConflict, match="다시 선택"):
        admin.apply_release_batch(COURSE, action, IDS, preview["fingerprint"])
    assert snapshot(admin) == before


def test_confirmation_binds_action_and_selection(batch):
    admin, _ = batch
    apply(admin, "archive")
    preview = admin.preview_release_batch(COURSE, "archive", IDS)
    before = snapshot(admin)
    with pytest.raises(PlatformConflict, match="다시 선택"):
        admin.apply_release_batch(COURSE, "delete", IDS, preview["fingerprint"])
    with pytest.raises(PlatformConflict, match="다시 선택"):
        admin.apply_release_batch(COURSE, "archive", IDS[:1], preview["fingerprint"])
    assert snapshot(admin) == before


@pytest.mark.parametrize("fingerprint", [None, 123, [], {}, b"a" * 64, "", "a" * 63, "한" * 64])
def test_malformed_fingerprint_is_rejected_without_mutation(batch, fingerprint):
    admin, _ = batch
    before = snapshot(admin)
    with pytest.raises(PlatformConflict, match="다시 선택"):
        admin.apply_release_batch(COURSE, "archive", IDS, fingerprint)
    assert snapshot(admin) == before


@pytest.mark.parametrize("action", ["archive", "delete"])
def test_concurrent_confirmation_applies_once_and_rejects_the_stale_request(batch, action):
    admin, _ = batch
    if action == "delete":
        apply(admin, "archive")
    preview = admin.preview_release_batch(COURSE, action, IDS)
    barrier = Barrier(2)

    def confirm():
        barrier.wait(timeout=5)
        try:
            return admin.apply_release_batch(COURSE, action, IDS, preview["fingerprint"])
        except PlatformConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(confirm) for _ in range(2)]
        outcomes = [future.result(timeout=10) for future in futures]
    success = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    assert len(success) == 1 and success[0]["changed_count"] == 3
    assert sum(isinstance(outcome, PlatformConflict) for outcome in outcomes) == 1
    event_action = "archived" if action == "archive" else "release_deleted"
    assert [event["assignment_id"] for event in lifecycle_events(admin) if event["action"] == event_action] == IDS


def test_course_archival_is_rechecked_inside_write_even_with_stale_callback(batch):
    admin, _ = batch
    preview = admin.preview_release_batch(COURSE, "archive", IDS)
    admin.course_status = lambda _: "active"
    with admin.state._write() as connection:
        connection.execute("UPDATE admin_courses SET status='archived' WHERE course_key=?", (COURSE,))
    before = snapshot(admin)
    with pytest.raises(PlatformConflict):
        admin.apply_release_batch(COURSE, "archive", IDS, preview["fingerprint"])
    assert snapshot(admin) == before


def test_receipt_grade_source_and_instructions_survive_bulk_delete_and_restore(batch, tmp_path):
    admin, _ = batch
    state = admin.state
    now = datetime.now(timezone.utc)
    student = state.upsert_local_student(student_key="batch-student", auth_subject="school:batch-student")
    state.upsert_enrollment(student_id=student.id, course_key=COURSE)
    state.create_device_authorization(authorization_id="batch-device", device_code_hash="1" * 64,
        user_code_hmac="2" * 64, course_key=COURSE, device_label="Synthetic bulk test", expires_at=now + timedelta(hours=1))
    state.approve_device_authorization(user_code_hmac="2" * 64, auth_subject=student.auth_subject, course_key=COURSE)
    state.consume_device_authorization(device_code_hash="1" * 64, course_key=COURSE,
        session_id="batch-session", token_family_id="batch-family", access_token_hash="3" * 64,
        access_token_expires_at=now + timedelta(hours=1), refresh_token_hash="4" * 64,
        refresh_token_expires_at=now + timedelta(days=1))
    source = tmp_path / "submission.cpp"
    source.write_text("int main(){return 0;}\n", encoding="utf-8")
    preview = admin.preview_release_batch(COURSE, "archive", IDS)
    state.create_accepted_bundle_submission(submission_id="batch-submission", receipt_id="batch-receipt",
        access_token_hash="3" * 64, course_key=COURSE, assignment_id=IDS[0],
        idempotency_key="batch-request", request_hash="5" * 64, source_path=str(source),
        source_digest="6" * 64, source_size_bytes=source.stat().st_size)
    # The instructor must review changed impact counts before archiving.
    with pytest.raises(PlatformConflict, match="다시 선택"):
        admin.apply_release_batch(COURSE, "archive", IDS, preview["fingerprint"])
    summary = admin.preview_release_batch(COURSE, "archive", IDS)["items"][0]
    assert summary["submitted_students"] == summary["submission_count"] == 1
    state.transition_bundle_submission("batch-submission", "queued")
    state.transition_bundle_submission("batch-submission", "running")
    state.record_bundle_graded_result("batch-submission", result_id="batch-result", score=7, max_score=10)
    state.publish_bundle_result("batch-submission")
    owner = dict(access_token_hash="3" * 64, course_key=COURSE, submission_id="batch-submission")
    receipt = state.get_owned_bundle_receipt(**owner)
    grade = state.get_owned_bundle_result(**owner)
    document = admin.get_assignment_document(COURSE, IDS[0])
    files = {path: path.read_bytes() for path in (source, tmp_path / "starter.cpp", tmp_path / "assessment" / "grade.py")}
    apply(admin, "archive")
    apply(admin, "delete")
    assert state.get_owned_bundle_receipt(**owner) == receipt
    assert state.get_owned_bundle_result(**owner) == grade
    assert admin.get_assignment_document(COURSE, IDS[0]) == document
    assert {path: path.read_bytes() for path in files} == files
    for assignment_id in IDS:
        restored = admin.restore_release(COURSE, assignment_id)
        assert not restored.active and not restored.ready
    assert state.get_owned_bundle_receipt(**owner) == receipt
    assert state.get_owned_bundle_result(**owner) == grade
