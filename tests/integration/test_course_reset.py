"""Maintenance reset tests use synthetic data only."""
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from autograde.course_reset import _digest, reset_course
from autograde.pilot_roster import initialize_student_roster
from autograde.platform_cli import _exclusive_course_service_lock
from autograde.platform_auth import verify_student_password
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths
from test_course_portal import (portal, claim, connect, request, BundleSubmissionProcessor,
                               PilotLocalGrader, WorkspaceBuilder)


@pytest.fixture
def records(tmp_path):
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    state = PlatformStateStore(paths.database)
    roster = tmp_path / "student_roster.csv"
    roster.write_text("course_key,student_key,active,password\n"
                      "come3105,001,true,183927\ncome2201,001,true,729461\n"
                      "come2201,002,false,\n")
    roster.chmod(0o600)
    initialize_student_roster(state, roster)
    return paths, state, roster


def fingerprint(paths):
    with sqlite3.connect(paths.database) as connection:
        return _digest(connection)


def apply(paths, plan):
    return reset_course(paths, "come2201", apply=True,
                        expected_state=plan["expected_state"], confirm_course="come2201")


def test_preview_reset_backup_and_reregister(records):
    paths, state, roster = records
    before = fingerprint(paths)
    plan = reset_course(paths, "come2201")
    assert plan["counts"]["platform_enrollments"] == 2
    assert fingerprint(paths) == before
    result = apply(paths, plan)
    backup = Path(result["backup_directory"])
    with sqlite3.connect(backup / "state.sqlite3") as connection:
        assert _digest(connection) == before
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (backup / "state.sqlite3").stat().st_mode & 0o777 == 0o600
    assert len(state.list_course_student_summaries(course_key="come2201")) == 0
    assert verify_student_password("183927", state.get_student_password_credential(
        student_key="001", course_key="come3105").password_hash)
    assert initialize_student_roster(state, roster)["status"] == "already_initialized"
    student = state.get_student_by_key("001")
    state.upsert_enrollment(student_id=student.id, course_key="come2201")
    with pytest.raises(ValueError, match="changed"):
        apply(paths, plan)  # Stale apply must not delete the re-registered student.


def test_live_service_and_confirmation_refused(records):
    paths, _, _ = records
    with _exclusive_course_service_lock(paths, "come3105"):
        with pytest.raises(Exception, match="active"):
            reset_course(paths, "come2201")
    with pytest.raises(ValueError, match="requires"):
        reset_course(paths, "come2201", apply=True)
    with pytest.raises(ValueError, match="already be registered"):
        reset_course(paths, "all")


def test_backup_failure_cannot_delete(records, monkeypatch):
    paths, _, _ = records
    plan = reset_course(paths, "come2201")
    def fail(*args):
        raise OSError("synthetic disk error")
    monkeypatch.setattr("autograde.course_reset._backup", fail)
    with pytest.raises(OSError):
        apply(paths, plan)
    assert fingerprint(paths) == plan["expected_state"]


@pytest.mark.parametrize("case", ["wrong_confirmation", "stale_preview", "schema", "bootstrap", "symlink"])
def test_unsafe_apply_refused_without_deletion(records, case, tmp_path):
    paths, _, _ = records
    plan = reset_course(paths, "come2201")
    if case in {"schema", "bootstrap"}:
        with sqlite3.connect(paths.database) as connection:
            if case == "schema":
                connection.execute("INSERT INTO platform_schema_migrations VALUES (13, 'synthetic')")
            else:
                connection.execute("DELETE FROM platform_roster_bootstrap")
    if case == "symlink":
        (paths.root / "unsafe-link").symlink_to(tmp_path / "student_roster.csv")
    before = fingerprint(paths)
    with pytest.raises(ValueError):
        reset_course(paths, "come2201", apply=True,
                     confirm_course="come3105" if case == "wrong_confirmation" else "come2201",
                     expected_state="0" * 64 if case == "stale_preview" else plan["expected_state"])
    assert fingerprint(paths) == before


def test_unexpected_cross_course_change_rolls_back_and_restores_guard(records):
    paths, _, _ = records
    with sqlite3.connect(paths.database) as connection:
        connection.execute("CREATE TRIGGER synthetic_bad_trigger AFTER DELETE ON platform_student_passwords "
                           "BEGIN DELETE FROM platform_session_issuance_counters; END")
        connection.execute("INSERT INTO platform_session_issuance_counters VALUES (1, 'come3105', '2026-09-15', 1, 'now')")
    plan = reset_course(paths, "come2201")
    with pytest.raises(ValueError, match="preservation"):
        apply(paths, plan)
    assert fingerprint(paths) == plan["expected_state"]


def test_reset_actual_claims_submissions_preserves_other_course(portal, tmp_path):
    web, api, services, _, store = portal
    state = services["come2201"].state
    paths = AppPaths.from_value(tmp_path)
    with state._write() as connection:
        connection.execute("INSERT INTO platform_roster_bootstrap VALUES (1, 2, 'now')")
    tokens = {}
    for course, password in (("come3105", "000001"), ("come2201", "000002")):
        _, tokens[course] = connect(api, claim(web, course, password))
        token = tokens[course]["access_token"]
        assert request(api, f"/v1/assignments/asn_{course}/starter", token=token)[0] == 200
        services[course].report_download_diagnostic(token, 'asn_' + course, dict(
            schema_version=1, attempt_id=str(uuid4()), seq=0, stage='files_ready', outcome='succeeded',
            open_outcome='not_attempted', ide='visualstudio', extension_version='0.5.2', os='windows', remote_kind='none'))
        source = tmp_path / (course + "-solution")
        source.mkdir()
        (source / "answer.txt").write_text("my solution")
        artifact = store.create_from_directory(source, kind="submission")
        status, _, body = request(api, f"/v1/assignments/asn_{course}/submissions",
                                  method="POST", token=token, raw=artifact.path.read_bytes())
        assert status == 202
        submission = json.loads(body)["submission"]["submission_id"]
        grader = PilotLocalGrader()
        try:
            BundleSubmissionProcessor(state=state, course_key=course,
                workspace_builder=WorkspaceBuilder(tmp_path / "graded"), grader=grader).process(submission)
        finally:
            grader.close()
    # Maintenance begins only after the test HTTP listeners are stopped.
    web.shutdown()
    api.shutdown()
    plan = reset_course(paths, "come2201")
    assert plan["counts"]["bundle_submission_results"] == 1
    assert plan["counts"]["platform_assignment_acceptances"] == 1
    assert plan['counts']['download_diagnostic_attempts'] == 1
    assert plan['counts']['download_diagnostic_events'] == 1
    apply(paths, plan)
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bundle_assignment_releases").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM bundle_submission_results").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM platform_assignment_acceptances").fetchone()[0] == 1
        assert connection.execute("SELECT course_key FROM download_diagnostic_attempts").fetchall() == [('come3105',)]
        assert connection.execute("SELECT COUNT(*) FROM download_diagnostic_events").fetchone()[0] == 1
    assert services["come3105"].get_me(tokens["come3105"]["access_token"])
    with pytest.raises(Exception):
        services["come2201"].get_me(tokens["come2201"]["access_token"])
    with pytest.raises(Exception):
        services["come2201"].refresh_tokens({"refresh_token": tokens["come2201"]["refresh_token"]})
    assert artifact.path.exists()  # No shared source/artifact cleanup.
