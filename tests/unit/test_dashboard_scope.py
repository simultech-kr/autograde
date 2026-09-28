"""Current requirements exclude validation artifacts without losing submissions."""
from datetime import timedelta

import pytest

from autograde.assignment_admin import AssignmentAdminService
from autograde.course_admin import CourseAdminService
from autograde.platform_state import BundleSubmissionState, PlatformStateStore
from autograde.settings import AppPaths
from test_bundle_platform_state import BundleFixture, COURSE, NOW


@pytest.fixture
def dashboard(tmp_path):
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    state = PlatformStateStore(paths.database)
    with state._write() as db:
        db.execute(
            "INSERT INTO admin_courses "
            "(course_key,code,status,created_at,updated_at) VALUES (?,?,'active',?,?)",
            (COURSE, COURSE, NOW.isoformat(), NOW.isoformat()),
        )
    return BundleFixture(state), CourseAdminService(state), AssignmentAdminService(state, paths)


def overview(courses, course=COURSE):
    return next(item for item in courses.management_overview() if item["course_key"] == course)


def test_repeated_web_validation_creates_only_one_published_requirement(dashboard):
    student, courses, admin = dashboard
    draft = admin.create_draft(COURSE, language="c")
    first = admin.queue_check(COURSE, draft["draft_id"], draft["revision"], True)
    assert admin.run_one()
    assert admin.get_check(COURSE, first["job_id"])["status"] == "succeeded"
    assert student.store.list_bundle_dashboard_rows(course_key=COURSE) == []
    assert overview(courses)["expected"] == 0

    revised = admin.update_draft(COURSE, draft["draft_id"], draft["revision"], title="Rechecked")
    second = admin.queue_check(COURSE, draft["draft_id"], revised["revision"], True)
    assert admin.run_one()
    assert admin.get_check(COURSE, second["job_id"])["status"] == "succeeded"
    assert student.store.list_bundle_dashboard_rows(course_key=COURSE) == []
    assert overview(courses)["active_assignments"] == 0

    published = admin.publish(COURSE, draft["draft_id"], revised["revision"])
    rows = student.store.list_bundle_dashboard_rows(course_key=COURSE)
    assert [(row.student_key, row.assignment_id, row.submission_count) for row in rows] == [
        (student.student.student_key, published.assignment_id, 0)
    ]
    counts = overview(courses)
    assert (counts["active_assignments"], counts["expected"], counts["not_submitted"]) == (1, 1, 1)
    assert counts["submitted"] == counts["completed"] == 0


def test_hidden_assignment_keeps_only_actual_latest_submissions(dashboard):
    student, courses, _ = dashboard
    state = student.store
    assignment = student.assignment()
    BundleFixture(state, student_key="s002", token_character="2", suffix="2")
    first = student.submit().request
    state.transition_bundle_submission(first.submission_id, "queued", at=NOW)
    state.transition_bundle_submission(first.submission_id, "running", at=NOW)
    state.record_bundle_graded_result(first.submission_id, result_id="result", score=10, max_score=10, at=NOW)
    state.publish_bundle_result(first.submission_id, at=NOW)
    assert overview(courses)["completed"] == 1

    latest = student.submit(submission_id="latest", receipt_id="receipt-latest", key="latest",
                            at=NOW + timedelta(minutes=2)).request
    assert overview(courses)["submitted"] == 1
    assert overview(courses)["waiting"] == 1
    state.set_bundle_assignment_availability(assignment.assignment_id, course_key=COURSE, ready=False)

    rows = state.list_bundle_dashboard_rows(course_key=COURSE)
    assert len(rows) == 1
    assert rows[0].student_key == "s001"
    assert rows[0].submission_count == 2
    assert rows[0].latest_submission_id == latest.submission_id
    assert rows[0].latest_state == BundleSubmissionState.ACCEPTED
    assert state.get_bundle_submission(first.submission_id).state == BundleSubmissionState.PUBLISHED
    counts = overview(courses)
    assert all(counts[key] == 0 for key in (
        "active_assignments", "expected", "submitted", "completed", "waiting", "not_submitted"
    ))

    # A newly visible requirement does not inherit completion from hidden work.
    student.assignment(assignment_id="current", assignment_key="current")
    rows = state.list_bundle_dashboard_rows(course_key=COURSE)
    assert len(rows) == 3
    counts = overview(courses)
    assert (counts["active_assignments"], counts["expected"], counts["not_submitted"]) == (1, 2, 2)
    assert counts["submitted"] == counts["completed"] == 0


def test_assignment_selection_and_inactive_opt_in_preserve_review_scope(dashboard):
    student, _, _ = dashboard
    state = student.store
    assignment = student.assignment()
    BundleFixture(state, student_key="s002", token_character="2", suffix="2")
    student.submit()
    state.set_bundle_assignment_availability(assignment.assignment_id, course_key=COURSE, ready=False)
    selected = dict(course_key=COURSE, assignment_id=assignment.assignment_id)
    assert len(state.list_bundle_dashboard_rows(**selected)) == 2
    state.set_bundle_assignment_availability(assignment.assignment_id, course_key=COURSE, active=False)
    assert state.list_bundle_dashboard_rows(course_key=COURSE) == []
    assert state.list_bundle_dashboard_rows(**selected) == []
    assert len(state.list_bundle_dashboard_rows(course_key=COURSE, include_inactive_assignments=True)) == 1
    assert len(state.list_bundle_dashboard_rows(**selected, include_inactive_assignments=True)) == 2


def test_latest_submitted_students_precede_unsubmitted_before_limit(dashboard):
    student, _, _ = dashboard
    state = student.store
    student.assignment()
    earlier = BundleFixture(state, student_key="s002", token_character="2", suffix="2")
    latest = BundleFixture(state, student_key="s999", token_character="3", suffix="3")
    earlier.submit(submission_id="earlier", receipt_id="receipt-earlier", key="earlier",
                   at=NOW + timedelta(minutes=1))
    latest.submit(submission_id="latest", receipt_id="receipt-latest", key="latest",
                  at=NOW + timedelta(minutes=2))
    assert [row.student_key for row in state.list_bundle_dashboard_rows(course_key=COURSE)] == [
        "s999", "s002", "s001"
    ]
    assert [row.student_key for row in state.list_bundle_dashboard_rows(course_key=COURSE, limit=1)] == ["s999"]


def test_current_requirements_and_history_do_not_leak_across_courses(dashboard):
    student, courses, _ = dashboard
    state = student.store
    assignment = student.assignment()
    student.submit()
    state.set_bundle_assignment_availability(assignment.assignment_id, course_key=COURSE, ready=False)
    state.upsert_enrollment(student_id=student.student.id, course_key="come2201", at=NOW)
    fields = (
        "assignment_key", "title", "starter_path", "starter_digest", "starter_size_bytes",
        "assessment_path", "assessment_digest", "runner_image", "rubric_version", "max_score", "result_policy",
    )
    state.register_bundle_assignment_release(
        assignment_id="other-course", course_key="come2201", release_id="other-v1", ready=True,
        **{field: getattr(assignment, field) for field in fields},
    )
    rows = state.list_bundle_dashboard_rows(course_key="come2201")
    assert [(row.assignment_id, row.submission_count) for row in rows] == [("other-course", 0)]
    assert overview(courses)["expected"] == overview(courses)["submitted"] == 0
    other = overview(courses, "come2201")
    assert (other["expected"], other["not_submitted"], other["submitted"]) == (1, 1, 0)
    assert state.list_bundle_dashboard_rows(course_key="come2201", assignment_id=assignment.assignment_id) == []

    state.upsert_enrollment(student_id=student.student.id, course_key=COURSE, active=False)
    assert state.list_bundle_dashboard_rows(course_key=COURSE) == []
    assert len(state.list_bundle_dashboard_rows(course_key="come2201")) == 1
