"""Maintenance reset tests use synthetic data only."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from autograde.course_reset import DOCUMENT_GUARD, GUARD, _digest, reset_course
from autograde.exam_papers import ExamPaperService
from autograde.student_materials import StudentMaterialService
from autograde.pilot_roster import initialize_student_roster, initialize_web_roster
from autograde.platform_cli import _exclusive_course_service_lock
from autograde.platform_auth import verify_student_password
from autograde.platform_state import PlatformAccessDenied, PlatformStateStore
from autograde.settings import AppPaths
from test_course_portal import (portal, claim, connect, request, BundleSubmissionProcessor,
                               PilotLocalGrader, WorkspaceBuilder)
from test_instructor_web import setup


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


def test_reset_removes_only_target_course_classroom_metadata(records):
    paths, state, _ = records
    student = state.get_student_by_key('001')
    with state._write() as db:
        for course in ('come2201', 'come3105'):
            db.execute('''INSERT INTO classroom_clients(client_session_id,session_id,course_key,student_id,
                latest_sequence,latest_payload,first_seen_at,last_seen_at)
                VALUES (?,?,?,?,1,'{}','2026-10-25T00:00:00Z','2026-10-25T00:00:00Z')''',
                ('client-' + course, 'audit-session', course, student.id))
            db.execute('''INSERT INTO classroom_events(client_session_id,course_key,student_id,sequence,kind,received_at)
                VALUES (?,?,?,1,'workspace_opened','2026-10-25T00:00:00Z')''', ('client-' + course, course, student.id))
            db.execute('''INSERT INTO classroom_commands(command_id,course_key,student_id,request_key,request_json,kind,
                message,created_by,status,client_session_id,created_at,expires_at)
                VALUES (?,?,?,'test-request','{}','notice','Hello','teacher','received',?,'2026-10-25T00:00:00Z','2026-10-25T00:10:00Z')''',
                ('command-' + course, course, student.id, 'client-' + course))
            db.execute('INSERT INTO classroom_command_events(command_id,course_key,student_id,status,actor,recorded_at) '
                       "VALUES (?,?,?,'received','ide','2026-10-25T00:00:00Z')", ('command-' + course, course, student.id))
    plan = reset_course(paths, 'come2201')
    assert plan['counts']['classroom_clients'] == 1
    assert plan['counts']['classroom_events'] == 1
    assert plan['counts']['classroom_commands'] == 1
    assert plan['counts']['classroom_command_events'] == 1
    apply(paths, plan)
    with state._connection() as db:
        assert [row[0] for row in db.execute('SELECT course_key FROM classroom_clients')] == ['come3105']
        assert [row[0] for row in db.execute('SELECT course_key FROM classroom_events')] == ['come3105']
        assert [row[0] for row in db.execute('SELECT course_key FROM classroom_commands')] == ['come3105']
        assert [row[0] for row in db.execute('SELECT course_key FROM classroom_command_events')] == ['come3105']


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
                connection.execute("INSERT INTO platform_schema_migrations VALUES (?, 'synthetic')",
                                   (PlatformStateStore.LATEST_SCHEMA_VERSION + 1,))
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


@pytest.mark.parametrize('version', [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
def test_legacy_schema_reset_remains_supported(tmp_path, monkeypatch, version):
    import autograde.platform_state as module
    monkeypatch.setattr(module, '_LATEST_SCHEMA_VERSION', version)
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    student = state.upsert_local_student(student_key='SYN-LEGACY', auth_subject='local:SYN-LEGACY')
    for course in ('come2201', 'come3105'):
        state.upsert_enrollment(student_id=student.id, course_key=course)
    with state._write() as db:
        db.execute("INSERT INTO platform_roster_bootstrap VALUES (1,2,'synthetic')")
    plan = reset_course(paths, 'come2201')
    assert ('bundle_submission_documents' in plan['counts']) == (version >= 14)
    assert apply(paths, plan)['mode'] == 'reset_complete'
    assert state.schema_version() == version
    assert state.list_course_student_summaries(course_key='come2201') == []
    assert len(state.list_course_student_summaries(course_key='come3105')) == 1


@pytest.mark.parametrize('name', [GUARD, DOCUMENT_GUARD])
def test_missing_reset_delete_guard_refuses_even_preview(records, name):
    paths, state, _ = records
    with state._write() as db:
        db.execute(f'DROP TRIGGER "{name}"')
    before = fingerprint(paths)
    with pytest.raises(ValueError, match='immutability guard is missing'):
        reset_course(paths, 'come2201')
    assert fingerprint(paths) == before


def _course_session(state, course, index, now):
    student = state.upsert_local_student(student_key='SYN-' + course, auth_subject='local:SYN-' + course, at=now)
    state.upsert_enrollment(student_id=student.id, course_key=course, at=now)
    device_hash, user_hash, token = str(index + 2) * 64, str(index + 6) * 64, str(index) * 64
    state.create_device_authorization(authorization_id='dev_' + course,
        device_code_hash=device_hash, user_code_hmac=user_hash, course_key=course,
        device_label='Synthetic reset test', expires_at=now + timedelta(minutes=5), at=now)
    state.approve_device_authorization(user_code_hmac=user_hash,
        auth_subject=student.auth_subject, course_key=course, at=now)
    state.consume_device_authorization(device_code_hash=device_hash, course_key=course,
        session_id='ses_' + course, token_family_id='fam_' + course, access_token_hash=token,
        access_token_expires_at=now + timedelta(hours=1), refresh_token_hash=chr(ord('a') + index) * 64,
        refresh_token_expires_at=now + timedelta(days=1), at=now)
    return token


@pytest.fixture
def published_documents(setup, tmp_path):
    _, state, _, _, admin = setup
    initialize_web_roster(state)
    submissions = {}
    releases = {}
    # Publish through the actual instructor service, then receive submissions
    # under distinct course sessions. These are synthetic, terminal records.
    for index, course in enumerate(('come2201', 'come3105'), start=1):
        draft = admin.create_draft(course, language='c', description='# Original ' + course)
        admin.queue_check(course, draft['draft_id'], draft['revision'], True)
        assert admin.run_one()
        release = admin.publish(course, draft['draft_id'], draft['revision'])
        releases[course] = release
        now = datetime.now(timezone.utc)
        token = _course_session(state, course, index, now)
        submissions[course] = []
        for revision in (0, 1):
            if revision:
                admin.update_assignment_document(course, release.assignment_id, 0,
                    '# Improved ' + course, '명령 설명 보완')
            sid = f'sub_{course}_{revision}'
            state.create_accepted_bundle_submission(submission_id=sid, receipt_id='receipt_' + sid,
                access_token_hash=token, course_key=course, assignment_id=release.assignment_id,
                idempotency_key='key_' + sid, request_hash='f' * 64,
                source_path=str(tmp_path / (sid + '.tar.gz')), source_digest='d' * 64,
                source_size_bytes=1, at=now)
            state.transition_bundle_submission(sid, 'queued', at=now)
            state.transition_bundle_submission(sid, 'infra_failed', at=now,
                failure_code='synthetic_terminal', failure_message='Synthetic terminal test record')
            submissions[course].append(sid)
    return admin.paths, state, admin, releases, submissions


def test_web_submission_reset_preserves_documents_other_course_and_restores_guards(published_documents):
    paths, state, admin, releases, submissions = published_documents
    with state._connection() as db:
        documents = [tuple(row) for row in db.execute('SELECT * FROM assignment_document_revisions ORDER BY assignment_id,revision')]
        snapshots = [tuple(row) for row in db.execute('SELECT * FROM bundle_submission_documents ORDER BY submission_id')]
        guards = [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    assert len(documents) == len(snapshots) == 4
    other_receipts = [state.get_bundle_receipt(sid) for sid in submissions['come3105']]
    plan = reset_course(paths, 'come2201')
    assert plan['counts']['bundle_submission_documents'] == 2
    result = apply(paths, plan)
    assert result['mode'] == 'reset_complete'
    with sqlite3.connect(Path(result['backup_directory']) / 'state.sqlite3') as saved:
        assert saved.execute('SELECT * FROM assignment_document_revisions ORDER BY assignment_id,revision').fetchall() == documents
        assert saved.execute('SELECT * FROM bundle_submission_documents ORDER BY submission_id').fetchall() == snapshots
    with state._connection() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM assignment_document_revisions ORDER BY assignment_id,revision')] == documents
        assert [tuple(row) for row in db.execute('SELECT * FROM bundle_submission_documents ORDER BY submission_id')] == [row for row in snapshots if row[0] in submissions['come3105']]
        assert [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")] == guards
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert [state.get_bundle_receipt(sid) for sid in submissions['come3105']] == other_receipts
    for course, release in releases.items():
        assert state.get_bundle_assignment(release.assignment_id) == release
        assert admin.get_assignment_document(course, release.assignment_id)['revision'] == 1
    assert len(state.list_course_student_summaries(course_key='come2201')) == 0
    assert len(state.list_course_student_summaries(course_key='come3105')) == 1
    for table in ('bundle_submission_documents', 'assignment_document_revisions'):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            with state._write() as db:
                db.execute(f'DELETE FROM {table}')
    assert reset_course(paths, 'come2201')['counts']['bundle_submission_documents'] == 0


def test_failed_document_reset_rolls_back_references_and_both_guards(published_documents):
    paths, state, _, _, _ = published_documents
    with state._write() as db:
        db.execute("INSERT INTO platform_session_issuance_counters VALUES (1,'come3105','2026-09-15',1,'now')")
        db.execute('CREATE TRIGGER synthetic_bad_document_delete AFTER DELETE ON bundle_submission_documents '
                   'BEGIN DELETE FROM platform_session_issuance_counters; END')
    plan = reset_course(paths, 'come2201')
    with pytest.raises(ValueError, match='preservation'):
        apply(paths, plan)
    assert fingerprint(paths) == plan['expected_state']
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with state._write() as db:
            db.execute('DELETE FROM bundle_submission_documents')


def test_reset_preserves_deleted_assignments_audit_and_lifecycle_guards(published_documents):
    paths, state, admin, releases, _ = published_documents
    for course, release in releases.items():
        admin.archive_release(course, release.assignment_id)
        admin.delete_release(course, release.assignment_id)
    preserved_releases = {course: state.get_bundle_assignment(release.assignment_id)
                          for course, release in releases.items()}
    assignment_tables = ('instructor_assignment_deletions', 'instructor_assignment_events',
                         'instructor_assignment_drafts', 'assignment_document_revisions')
    with state._connection() as db:
        preserved_rows = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                          for table in assignment_tables}
        guards = [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
        assert db.execute("SELECT COUNT(*) FROM instructor_assignment_events WHERE action='release_deleted'").fetchone()[0] == 2
    assert {'instructor_assignment_deletion_scope', 'instructor_assignment_deleted_release_guard',
            'instructor_assignment_deleted_check_guard', 'instructor_assignment_deleted_document_guard'} <= {name for name, _ in guards}
    assignment_id = releases['come2201'].assignment_id
    # The same low-level lifecycle fence must remain effective before and after
    # student records are removed; reset must never lift assignment guards.
    with pytest.raises(sqlite3.IntegrityError, match='assignment_deleted'):
        with sqlite3.connect(paths.database) as db:
            db.execute('UPDATE bundle_assignment_releases SET active=1 WHERE assignment_id=?', (assignment_id,))
    plan = reset_course(paths, 'come2201')
    assert not (set(assignment_tables) & set(plan['counts']))
    result = apply(paths, plan)
    assert result['mode'] == 'reset_complete'
    assert state.list_course_student_summaries(course_key='come2201') == []
    assert len(state.list_course_student_summaries(course_key='come3105')) == 1
    with state._connection() as db:
        for table, expected in preserved_rows.items():
            assert [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')] == expected
        assert [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")] == guards
    with sqlite3.connect(Path(result['backup_directory']) / 'state.sqlite3') as saved:
        for table, expected in preserved_rows.items():
            assert saved.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() == expected
    assert {course: state.get_bundle_assignment(release.assignment_id)
            for course, release in releases.items()} == preserved_releases
    with pytest.raises(sqlite3.IntegrityError, match='assignment_deleted'):
        with sqlite3.connect(paths.database) as db:
            db.execute('UPDATE bundle_assignment_releases SET active=1 WHERE assignment_id=?', (assignment_id,))
    with pytest.raises(sqlite3.IntegrityError, match='assignment_deleted'):
        with sqlite3.connect(paths.database) as db:
            db.execute("INSERT INTO assignment_document_revisions "
                       "(assignment_id,revision,content,sha256,change_note,actor,updated_at) "
                       "VALUES (?,2,'changed','synthetic','test','test','now')", (assignment_id,))


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


def _exam_history(paths, state):
    now = datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
    service = ExamPaperService(state, paths.root / "exam-papers", clock=lambda: now)
    student_id = state.get_student_by_key("001").id
    papers = {}
    for course in ("come2201", "come3105"):
        paper = service.create(course, title="Synthetic " + course, opens_at=now,
                               closes_at=now + timedelta(hours=1), actor="synthetic-teacher")
        paper = service.upload(course, paper["paper_id"], revision=paper["revision"],
                               data=b"%PDF-1.4\nsynthetic exam\n%%EOF\n", actor="synthetic-teacher")
        paper = service.publish(course, paper["paper_id"], revision=paper["revision"], actor="synthetic-teacher")
        service.download(course, paper["paper_id"], student_id)
        papers[course] = paper
    service.download("come2201", papers["come2201"]["paper_id"], student_id)
    return service, papers, student_id


def test_migrated_v16_reset_preserves_exam_evidence_private_files_backup_and_other_course(tmp_path, monkeypatch):
    import autograde.platform_state as module
    # Start with the actual pre-exam schema and migrate a populated database.
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    with monkeypatch.context() as patch:
        patch.setattr(module, "_LATEST_SCHEMA_VERSION", 15)
        state = PlatformStateStore(paths.database)
        roster = tmp_path / "student_roster.csv"
        roster.write_text("course_key,student_key,active,password\n"
                          "come3105,001,true,183927\ncome2201,001,true,729461\n")
        roster.chmod(0o600)
        initialize_student_roster(state, roster)
        assert state.schema_version() == 15
    state = PlatformStateStore(paths.database)
    assert state.schema_version() == state.LATEST_SCHEMA_VERSION
    service, papers, student_id = _exam_history(paths, state)
    preserved_tables = ("exam_papers", "exam_paper_audit", "exam_paper_requests")
    with state._connection() as db:
        preserved = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                     for table in preserved_tables}
        guards = [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    files = {path.relative_to(paths.root): path.read_bytes() for path in service.root.iterdir()}
    other_receipts = service.receipts("come3105", papers["come3105"]["paper_id"])
    plan = reset_course(paths, "come2201")
    assert not (set(preserved_tables) & set(plan["counts"]))
    result = apply(paths, plan)
    backup = Path(result["backup_directory"])
    assert result["mode"] == "reset_complete"
    with state._connection() as db:
        for table, expected in preserved.items():
            assert [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] == expected
        assert [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")] == guards
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(backup / "state.sqlite3") as saved:
        for table, expected in preserved.items():
            assert saved.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == expected
    for relative, content in files.items():
        assert (paths.root / relative).read_bytes() == content
        assert (backup / relative).read_bytes() == content
        assert (backup / relative).stat().st_mode & 0o777 == 0o600
    assert service.receipts("come3105", papers["come3105"]["paper_id"]) == other_receipts
    receipt = service.receipts("come2201", papers["come2201"]["paper_id"])[0]
    assert receipt["request_count"] == 2 and not receipt["active"]
    with pytest.raises(PlatformAccessDenied):
        service.download("come2201", papers["come2201"]["paper_id"], student_id)
    # Existing reset semantics delete the course enrollment, not global identity.
    # Re-registration explicitly restores access without resetting exam evidence.
    state.upsert_enrollment(student_id=student_id, course_key="come2201")
    service.download("come2201", papers["come2201"]["paper_id"], student_id)
    receipt = service.receipts("come2201", papers["come2201"]["paper_id"])[0]
    assert receipt["request_count"] == 3 and receipt["active"]


def test_reset_rolls_back_incidental_exam_request_evidence_deletion(records):
    paths, state, _ = records
    service, papers, _ = _exam_history(paths, state)
    with state._write() as db:
        db.execute("CREATE TRIGGER synthetic_bad_exam_cleanup AFTER DELETE ON platform_student_passwords "
                   "BEGIN DELETE FROM exam_paper_requests; END")
    plan = reset_course(paths, "come2201")
    with pytest.raises(ValueError, match="preservation"):
        apply(paths, plan)
    assert fingerprint(paths) == plan["expected_state"]
    assert service.receipts("come2201", papers["come2201"]["paper_id"])[0]["request_count"] == 2
    assert service.receipts("come3105", papers["come3105"]["paper_id"])[0]["request_count"] == 1


@pytest.mark.parametrize('mode', ['original', 'edition', 'direct'])
def test_reset_preserves_personal_material_assignments_requests_and_backups(records, mode):
    paths, state, _ = records
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    service = StudentMaterialService(state, paths.root / 'student-materials', clock=lambda: now)
    student_id = state.get_student_by_key('001').id
    material = service.create('come2201', title='개별 피드백', opens_at=now,
                              closes_at=now + timedelta(hours=1), actor='teacher')
    material = service.upload('come2201', material['material_id'], revision=material['revision'],
                              filename='001.pdf', data=b'%PDF-1.4\nprivate feedback\n%%EOF\n', actor='teacher')
    material = service.publish('come2201', material['material_id'], revision=material['revision'], actor='teacher')
    artifact = service.download('come2201', material['material_id'], student_id)
    artifacts = [artifact]
    if mode != 'original':
        if mode == 'edition':
            material = service.create_revision('come2201', material['material_id'], revision=material['revision'],
                                               change_note='피드백 내용 보완', actor='teacher')
        material = service.upload('come2201', material['material_id'], revision=material['revision'],
                                  filename='001.pdf', data=b'%PDF-1.4\ncorrected feedback\n%%EOF\n', actor='teacher')
        if mode == 'edition':
            material = service.publish('come2201', material['material_id'], revision=material['revision'], actor='teacher')
        artifact = service.download('come2201', material['material_id'], student_id)
        artifacts.append(artifact)
    preserved_tables = ('student_materials', 'student_material_files', 'student_material_requests', 'student_material_audit',
                        'student_material_versions', 'student_material_file_history')
    with state._connection() as db:
        original = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in preserved_tables}
    result = apply(paths, reset_course(paths, 'come2201'))
    with state._connection() as db, sqlite3.connect(Path(result['backup_directory']) / 'state.sqlite3') as saved:
        for table, rows in original.items():
            assert [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')] == rows
            assert saved.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() == rows
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    for saved_artifact in artifacts:
        assert (Path(result['backup_directory']) / saved_artifact.relative_to(paths.root)).read_bytes() == saved_artifact.read_bytes()
    with pytest.raises(PlatformAccessDenied):
        service.download('come2201', material['material_id'], student_id)
    assert service.recipients('come2201', material['material_id'])[0]['request_count'] == 1
    state.upsert_enrollment(student_id=student_id, course_key='come2201')
    assert service.download('come2201', material['material_id'], student_id).read_bytes() == artifact.read_bytes()
    assert service.recipients('come2201', material['material_id'])[0]['request_count'] == 2


def test_reset_preserves_git_content_history_objects_and_backup(records):
    from autograde.content_store import ContentService
    paths, state, _ = records
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    service = ContentService(state, paths.root / 'content', clock=lambda: now)
    student = state.get_student_by_key('001')
    row = service.create('come2201', title='CMS reference', student_ids=[student.id], actor='teacher')
    row = service.upload('come2201', row['collection_id'], revision=row['revision'], path='README.md',
                         data=b'preserve content\n', actor='teacher')
    release = service.publish('come2201', row['collection_id'], revision=row['revision'],
                              idempotency_key='reset-preservation-test', actor='teacher')
    tables = ('content_collections', 'content_draft_files', 'content_releases', 'content_release_files',
              'content_publish_jobs', 'content_audit')
    with state._connection() as db:
        original = {table: [tuple(record) for record in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in tables}
    artifact = service.object_path('come2201', release['files'][0]['sha256'])
    result = apply(paths, reset_course(paths, 'come2201'))
    with state._connection() as db, sqlite3.connect(Path(result['backup_directory']) / 'state.sqlite3') as saved:
        for table, rows in original.items():
            assert [tuple(record) for record in db.execute(f'SELECT * FROM {table} ORDER BY rowid')] == rows
            assert saved.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() == rows
    assert (Path(result['backup_directory']) / artifact.relative_to(paths.root)).read_bytes() == artifact.read_bytes()
    with pytest.raises(PlatformAccessDenied):
        service.get_student_manifest('come2201', student.id, release['release_id'], subject_scope='a' * 64)
