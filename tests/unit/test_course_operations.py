import sqlite3
from datetime import timedelta

import pytest
import autograde.platform_state as state_module

from autograde.platform_state import PlatformStateStore, PlatformConflict, PlatformNotFound, PlatformInvalidTransition, BundleSubmissionState, ResultPolicy
from test_bundle_platform_state import BundleFixture, NOW, COURSE


@pytest.fixture
def fixture(tmp_path):
    return BundleFixture(PlatformStateStore(tmp_path / "state.sqlite3"))


def checked(fixture, assignment_id="bundle_asn_01", passed=True):
    check = fixture.store.begin_bundle_release_check(assignment_id, course_key=COURSE)
    fixture.store.finish_bundle_release_check(check, passed=passed, details={"test": True})
    return check


def test_upgrade_from_v9_preserves_public_release_and_receipt(tmp_path, monkeypatch):
    database = tmp_path / "legacy.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(state_module, "_LATEST_SCHEMA_VERSION", 9)
        legacy = BundleFixture(PlatformStateStore(database))
        assignment = legacy.assignment()
        submission = legacy.submit()
        receipt = legacy.store.get_bundle_receipt(submission.request.submission_id)
        assert legacy.store.schema_version() == 9
    upgraded = PlatformStateStore(database)
    assert upgraded.schema_version() == 13
    assert upgraded.get_bundle_assignment(assignment.assignment_id) == assignment
    assert upgraded.get_bundle_receipt(receipt.submission_id) == receipt
    assert upgraded.bundle_operation_history(assignment.assignment_id, course_key=COURSE)["checks"] == []


def test_only_newest_validation_can_authorize_publication(fixture):
    fixture.assignment(ready=False)
    older = fixture.store.begin_bundle_release_check("bundle_asn_01", course_key=COURSE)
    newer = fixture.store.begin_bundle_release_check("bundle_asn_01", course_key=COURSE)
    fixture.store.finish_bundle_release_check(newer, passed=False, details={})
    fixture.store.finish_bundle_release_check(older, passed=True, details={})
    with pytest.raises(PlatformConflict):
        fixture.store.publish_validated_bundle_assignment("bundle_asn_01", course_key=COURSE)
    with pytest.raises(PlatformNotFound):
        fixture.store.publish_validated_bundle_assignment("bundle_asn_01", course_key="other")


def test_publication_requires_latest_pass_and_prevents_two_ready_releases(fixture):
    fixture.assignment(ready=False)
    with pytest.raises(PlatformConflict):
        fixture.store.publish_validated_bundle_assignment("bundle_asn_01", course_key=COURSE)
    checked(fixture)
    pending = fixture.store.begin_bundle_release_check("bundle_asn_01", course_key=COURSE)
    with pytest.raises(PlatformConflict):
        fixture.store.publish_validated_bundle_assignment("bundle_asn_01", course_key=COURSE)
    fixture.store.finish_bundle_release_check(pending, passed=False, details={})
    with pytest.raises(PlatformConflict):
        fixture.store.publish_validated_bundle_assignment("bundle_asn_01", course_key=COURSE)
    checked(fixture)
    assert fixture.store.publish_validated_bundle_assignment("bundle_asn_01", course_key=COURSE).ready
    with pytest.raises(PlatformConflict):
        fixture.store.begin_bundle_release_check("bundle_asn_01", course_key=COURSE)
    # A separate release of the same assignment cannot be published alongside it.
    a = fixture.store.get_bundle_assignment("bundle_asn_01")
    fields = ("course_key", "assignment_key", "title", "starter_path", "starter_digest", "starter_size_bytes",
              "assessment_path", "assessment_digest", "data_path", "dataset_digest", "runner_image", "rubric_version", "max_score", "result_policy")
    fixture.store.register_bundle_assignment_release(assignment_id="bundle_asn_02", release_id="v2", ready=False,
        **{field: getattr(a, field) for field in fields})
    checked(fixture, "bundle_asn_02")
    with pytest.raises(PlatformConflict, match="another release"):
        fixture.store.publish_validated_bundle_assignment("bundle_asn_02", course_key=COURSE)
    assert fixture.store.get_bundle_assignment("bundle_asn_01").ready


def test_extension_keeps_submission_receipt_and_audits_change(fixture):
    assignment = fixture.assignment()
    submitted = fixture.submit()
    receipt = fixture.store.get_bundle_receipt(submitted.request.submission_id)
    new_due = NOW + timedelta(days=8)
    updated = fixture.store.extend_bundle_deadline(assignment.assignment_id, course_key=COURSE, due_at=new_due,
        reason="lab outage", actor="cli:instructor", at=NOW + timedelta(minutes=2))
    assert updated.assignment_id == assignment.assignment_id and updated.ready
    assert fixture.store.get_bundle_receipt(receipt.submission_id) == receipt
    history = fixture.store.bundle_operation_history(assignment.assignment_id, course_key=COURSE)
    assert history["deadline_changes"][0]["old_due_at"] == assignment.due_at
    assert history["deadline_changes"][0]["new_due_at"] == updated.due_at
    with pytest.raises(PlatformConflict):
        fixture.store.extend_bundle_deadline(assignment.assignment_id, course_key=COURSE, due_at=new_due,
            reason="repeat", actor="cli:test", at=NOW)
    with pytest.raises(PlatformNotFound):
        fixture.store.extend_bundle_deadline(assignment.assignment_id, course_key="other", due_at=NOW + timedelta(days=9),
            reason="wrong course", actor="cli:test", at=NOW)


def test_unlogged_deadline_and_grading_input_edits_remain_forbidden(fixture):
    fixture.assignment()
    with fixture.store._connection() as connection:
        for column, value in (("due_at", "2099-01-01T00:00:00.000000Z"), ("title", "tampered"), ("max_score", 99)):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE bundle_assignment_releases SET {column} = ?", (value,))


def test_extended_after_deadline_results_wait_and_public_results_block_extension(fixture):
    fixture.assignment(result_policy=ResultPolicy.AFTER_DEADLINE)
    submitted = fixture.submit()
    sid = submitted.request.submission_id
    fixture.store.transition_bundle_submission(sid, BundleSubmissionState.QUEUED, at=NOW)
    fixture.store.transition_bundle_submission(sid, BundleSubmissionState.RUNNING, at=NOW)
    fixture.store.record_bundle_graded_result(sid, result_id="bres_test", score=10, max_score=10, at=NOW)
    fixture.store.extend_bundle_deadline("bundle_asn_01", course_key=COURSE, due_at=NOW + timedelta(days=9),
        reason="extend", actor="cli:test", at=NOW)
    assert fixture.store.list_publishable_bundle_submissions(course_key=COURSE, at=NOW + timedelta(days=8)) == []
    with pytest.raises(PlatformInvalidTransition):
        fixture.store.publish_bundle_result(sid, at=NOW + timedelta(days=8))
    fixture.store.publish_bundle_result(sid, at=NOW + timedelta(days=10))
    with pytest.raises(PlatformConflict, match="public"):
        fixture.store.extend_bundle_deadline("bundle_asn_01", course_key=COURSE, due_at=NOW + timedelta(days=11),
            reason="too late", actor="cli:test", at=NOW + timedelta(days=10))
