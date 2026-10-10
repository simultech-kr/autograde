"""Historical acceptance metadata does not widen a claim token's assignment scope."""
from datetime import datetime, timedelta, timezone
import io
import json

import pytest

from autograde.assignment_admin import AssignmentAdminService
from autograde.platform_service import PlatformAPIError, StudentPlatformService
from autograde.platform_state import PlatformStateStore, utc_iso
from autograde.settings import AppPaths
from test_course_portal import portal, request, connect, csrf


CATALOG = "/v1/accepted-assignments/catalog"
COURSE = "come3105"
FIRST = "asn_come3105"
FIELDS = {"assignment_id", "assignment_key", "release_id", "title", "delivery_mode", "course_key",
          "accepted_at", "current_session_authorized", "can_reconnect", "can_submit", "status"}


def clone_release(portal, assignment_id, **overrides):
    state = portal[2][COURSE].state
    original = state.get_bundle_assignment(FIRST)
    names = ("course_key", "starter_path", "starter_digest", "starter_size_bytes", "assessment_path",
             "assessment_digest", "data_path", "dataset_digest", "runner_image", "rubric_version",
             "max_score", "result_policy", "opens_at", "due_at")
    fields = {name: getattr(original, name) for name in names}
    fields.update(overrides)
    return state.register_bundle_assignment_release(assignment_id=assignment_id,
        assignment_key=assignment_id, release_id="v1", title="Accepted <" + assignment_id + ">", ready=True, **fields)


def accept(portal, assignment_id=FIRST, *, course=COURSE, student="20260001", password=None):
    service = portal[2][course]
    credential = service.authenticate_student_password(student, password or ("000001" if course == COURSE else "000002"))
    _, code = service.create_authenticated_assignment_claim(credential, assignment_id)
    return connect(portal[1], code)[1]


def catalog(portal, tokens):
    status, headers, raw = request(portal[1], CATALOG, token=tokens["access_token"])
    assert status == 200, raw
    assert headers["Cache-Control"] == "no-store" and "Set-Cookie" not in headers
    response = json.loads(raw)
    assert set(response) == {"assignments"}
    assert all(set(item) == FIELDS for item in response["assignments"])
    return response["assignments"]


def audit_rows(state):
    with state._connection() as connection:
        return ([tuple(row) for row in connection.execute("SELECT * FROM platform_assignment_acceptances ORDER BY acceptance_id")],
                [tuple(row) for row in connection.execute("SELECT session_id, last_seen_at, updated_at, revoked_at FROM platform_sessions ORDER BY session_id")])


def test_catalog_deduplicates_same_student_course_acceptances_without_exposing_other_work(portal, tmp_path):
    service = portal[2][COURSE]
    now = datetime.now(timezone.utc)
    clock = [now]
    service._now = lambda: clock[0]
    clone_release(portal, "asn_second")
    clone_release(portal, "asn_never_accepted")
    first = accept(portal)
    source = tmp_path / "catalog-source"
    source.mkdir()
    (source / "answer.txt").write_text("my solution")
    bundle = portal[4].create_from_directory(source, kind="submission")
    receipt = service.submit_bundle(first["access_token"], "catalog-first", FIRST,
        io.BytesIO(bundle.path.read_bytes()), bundle.compressed_bytes)["submission"]["submission_id"]
    clock[0] += timedelta(seconds=1)
    repeated = accept(portal)
    repeated_at = utc_iso(clock[0])
    clock[0] += timedelta(seconds=1)
    second = accept(portal, "asn_second")
    accept(portal, "asn_come2201", course="come2201")
    other = service.state.upsert_local_student(student_key="20260002", auth_subject="local:catalog-other")
    service.state.upsert_enrollment(student_id=other.id, course_key=COURSE)
    service.set_student_password(student_key="20260002", password="900002")
    accept(portal, "asn_never_accepted", student="20260002", password="900002")
    before = audit_rows(service.state)
    clock[0] += timedelta(seconds=1)
    entries = catalog(portal, second)
    assert audit_rows(service.state) == before  # GET does not accept, renew, or revoke anything.
    assert [item["assignment_id"] for item in entries] == ["asn_second", FIRST]
    current, previous = entries
    assert current["current_session_authorized"] and current["can_submit"]
    assert previous["accepted_at"] == repeated_at
    assert previous["status"] == "available" and previous["can_reconnect"]
    assert not previous["current_session_authorized"] and not previous["can_submit"]
    assert all(item["course_key"] == COURSE and item["delivery_mode"] == "bundle" for item in entries)
    for tokens in (first, repeated):
        assert request(portal[1], CATALOG, token=tokens["access_token"])[0] == 401
        assert request(portal[1], "/v1/tokens/refresh", method="POST", data={"refresh_token": tokens["refresh_token"]})[0] in (400, 401)
    for endpoint in ("/v1/assignments", "/v1/accepted-assignments"):
        raw = request(portal[1], endpoint, token=second["access_token"])[2]
        assert [item["assignment_id"] for item in json.loads(raw)["assignments"]] == ["asn_second"]
    for suffix in ("starter", "history", "document"):
        assert request(portal[1], f"/v1/assignments/{FIRST}/{suffix}", token=second["access_token"])[0] in (403, 404)
    for suffix in ("", "/result", "/source"):
        assert request(portal[1], f"/v1/submissions/{receipt}{suffix}", token=second["access_token"])[0] in (403, 404)
    with pytest.raises(PlatformAPIError) as denied:
        service.submit_bundle(second["access_token"], "cannot-submit-other", FIRST,
            io.BytesIO(bundle.path.read_bytes()), bundle.compressed_bytes)
    assert denied.value.status == 403
    # A fresh claim restores only that old assignment, without downloading over local work.
    resumed = accept(portal)
    result = service.submit_bundle(resumed["access_token"], "intentional-old-resubmit", FIRST,
        io.BytesIO(bundle.path.read_bytes()), bundle.compressed_bytes)
    assert not result["replayed"] and result["submission"]["submission_id"] != receipt
    assert request(portal[1], CATALOG, token=second["access_token"])[0] == 401


def test_closed_current_assignment_metadata_does_not_enable_reconnect_or_new_submission(portal, tmp_path):
    service = portal[2][COURSE]
    now = datetime.now(timezone.utc)
    due = now + timedelta(minutes=1)
    service._now = lambda: now
    clone_release(portal, "asn_short", due_at=due)
    tokens = accept(portal, "asn_short")
    service._now = lambda: due
    entry, = catalog(portal, tokens)
    assert entry["status"] == "closed" and entry["current_session_authorized"]
    assert not entry["can_reconnect"] and not entry["can_submit"]
    assert request(portal[1], "/v1/assignments/asn_short/starter", token=tokens["access_token"])[0] == 200
    with pytest.raises(PlatformAPIError) as claim:
        accept(portal, "asn_short")
    assert claim.value.code == "assignment_claim_denied"
    source = tmp_path / "closed-source"
    source.mkdir()
    (source / "answer.txt").write_text("my solution")
    bundle = portal[4].create_from_directory(source, kind="submission")
    with pytest.raises(PlatformAPIError) as submission:
        service.submit_bundle(tokens["access_token"], "closed-new-submit", "asn_short",
            io.BytesIO(bundle.path.read_bytes()), bundle.compressed_bytes)
    assert submission.value.code == "assignment_closed"


@pytest.mark.parametrize("lifecycle", ["hidden", "archived", "deleted"])
def test_hidden_archived_deleted_acceptances_remain_metadata_only(portal, tmp_path, lifecycle):
    service = portal[2][COURSE]
    tokens = accept(portal)
    admin = AssignmentAdminService(service.state, AppPaths.from_value(tmp_path / "admin-data").ensure())
    if lifecycle == "hidden":
        # The web UI deliberately forbids hiding an accepted release. Model a
        # CLI availability change to cover the lower-level gate as well.
        service.state.set_bundle_assignment_availability(FIRST, course_key=COURSE, ready=False)
    else:
        admin.archive_release(COURSE, FIRST)
        if lifecycle == "deleted":
            admin.delete_release(COURSE, FIRST)
    entry, = catalog(portal, tokens)
    assert entry["status"] == "unavailable" and entry["current_session_authorized"]
    assert not entry["can_reconnect"] and not entry["can_submit"]
    assert request(portal[1], f"/v1/assignments/{FIRST}/starter", token=tokens["access_token"])[0] == 403
    with pytest.raises(PlatformAPIError) as denied:
        accept(portal)
    assert denied.value.code == "assignment_claim_denied"


def test_catalog_requires_accepted_authentication_and_is_get_only(portal):
    assert request(portal[1], CATALOG)[0] == 401
    assert request(portal[1], CATALOG, token="synthetic-invalid-token")[0] == 401
    service = portal[2][COURSE]
    issued = service.issue_student_activation(student_key="20260001")
    device = service.create_device_authorization({"device_name": "legacy"})
    page = service.activate_page({"user_code": device["user_code"]})
    cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    service.approve_activation({"user_code": device["user_code"], "csrf": csrf(page.body.encode()),
        "activation_code": issued["activation_code"]}, {"autograde_activation": cookie})
    legacy = service.exchange_device_authorization({"device_code": device["device_code"]})
    status, _, raw = request(portal[1], CATALOG, token=legacy["access_token"])
    assert status == 403 and json.loads(raw)["error"]["code"] == "assignment_acceptance_required"
    tokens = accept(portal)
    assert request(portal[1], CATALOG, method="POST", token=tokens["access_token"], data={})[0] == 405
    assert request(portal[1], CATALOG, token=tokens["access_token"], data={})[0] == 400
    now = service._aware_now()
    service._now = lambda: now + timedelta(minutes=16)
    assert request(portal[1], CATALOG, token=tokens["access_token"])[0] == 401


def test_git_acceptance_metadata_excludes_repository_and_grading_fields(portal):
    service = portal[2][COURSE]
    student = service.state.get_student_by_key("20260001")
    service.state.register_assignment(assignment_id="asn_git", student_id=student.id, course_key=COURSE,
        assignment_key="git-lab", release_id="git-v1", github_repository_id=99901,
        repository_owner="private-owner", repository_name="private-repository",
        clone_url="https://github.com/private-owner/private-repository.git", submission_mode="branch",
        target_ref="main", result_policy="immediate", assessment_path="/private/assessment",
        assessment_digest="a" * 64, runner_image="pilot-local:v1", rubric_version="private-rubric",
        max_score=10, due_at=datetime.now(timezone.utc) + timedelta(days=1), ready=True)
    accept(portal, "asn_git")
    tokens = accept(portal)
    entries = catalog(portal, tokens)
    git = next(item for item in entries if item["assignment_id"] == "asn_git")
    assert git["delivery_mode"] == "git" and git["title"] == "git-lab"
    assert git["can_reconnect"] and not git["can_submit"] and not git["current_session_authorized"]
    serialized = json.dumps(entries)
    assert all(value not in serialized for value in ("private-owner", "private-repository", "/private/assessment", "private-rubric"))


def test_catalog_rechecks_session_before_snapshot(portal, monkeypatch):
    service = portal[2][COURSE]
    tokens = accept(portal)
    original = service.state._active_bundle_session_row
    monkeypatch.setattr(service.state, "_active_bundle_session_row", lambda *args, **kwargs: None)
    assert request(portal[1], CATALOG, token=tokens["access_token"])[0] == 401
    monkeypatch.setattr(service.state, "_active_bundle_session_row", original)
    assert len(catalog(portal, tokens)) == 1


@pytest.mark.parametrize("revocation", ["student", "enrollment", "password", "logout"])
def test_catalog_does_not_outlive_identity_or_session_revocation(portal, revocation):
    service = portal[2][COURSE]
    tokens = accept(portal)
    student = service.state.get_student_by_key("20260001")
    if revocation == "student":
        service.state.set_student_active(student.id, False)
        service.state.set_student_active(student.id, True)
    elif revocation == "enrollment":
        service.state.upsert_enrollment(student_id=student.id, course_key=COURSE, active=False)
        service.state.upsert_enrollment(student_id=student.id, course_key=COURSE, active=True)
    elif revocation == "password":
        service.set_student_password(student_key="20260001", password="000003")
    else:
        service.revoke_current(tokens["access_token"])
    assert request(portal[1], CATALOG, token=tokens["access_token"])[0] == 401


@pytest.mark.parametrize("legacy_schema", [False, True])
def test_catalog_works_without_instructor_service_initialization(tmp_path, legacy_schema):
    state = PlatformStateStore(tmp_path / "api-only.sqlite3")
    student = state.upsert_local_student(student_key="20260001", auth_subject="local:api-only")
    state.upsert_enrollment(student_id=student.id, course_key=COURSE)
    state.register_bundle_assignment_release(assignment_id=FIRST, course_key=COURSE,
        assignment_key="api-only", release_id="v1", title="API-only release",
        starter_path="/private/starter.tar.gz", starter_digest="a" * 64, starter_size_bytes=100,
        assessment_path="/private/assessment", assessment_digest="b" * 64,
        runner_image="pilot-local:v1", rubric_version="v1", max_score=10,
        result_policy="immediate", ready=True)
    service = StudentPlatformService(state=state, server_secret=b"a" * 32,
        course_key=COURSE, public_base_url="https://grade.example.edu")
    service.set_student_password(student_key="20260001", password="000001")
    credential = service.authenticate_student_password("20260001", "000001")
    _, code = service.create_authenticated_assignment_claim(credential, FIRST)
    device = service.create_device_authorization({"device_name": "API-only", "claim_code": code})
    service.redeem_assignment_claim({"device_code": device["device_code"], "claim_code": code})
    tokens = service.exchange_device_authorization({"device_code": device["device_code"]})
    if legacy_schema:
        # Isolated synthetic DB only: emulate a historical maintenance schema.
        with state._write() as connection:
            connection.execute("DROP TABLE instructor_assignment_deletions")
    item, = service.list_accepted_assignment_catalog(tokens["access_token"])["assignments"]
    assert item["assignment_id"] == FIRST and item["status"] == "available"
    assert item["current_session_authorized"] and item["can_submit"]
