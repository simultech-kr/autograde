"""Permanent course deletion uses synthetic databases and no real files."""
import sqlite3
import json

import pytest

from autograde.course_admin import CourseAdminService, EnrollmentAdminService, initialize_course_admin
from autograde.course_runtime import CourseRuntimeRegistry
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, PlatformStateStore


class Files:
    summary = {'files': 2, 'bytes': 100, 'shared_preserved': 1, 'external_preserved': 0}

    def __init__(self, *, fail_stage=False, fail_purge=False):
        self.events = []
        self.fail_stage, self.fail_purge = fail_stage, fail_purge

    def stage(self, job_id):
        self.events.append('stage')
        if self.fail_stage:
            raise OSError('synthetic staging failure')

    def rollback(self):
        self.events.append('rollback')

    def purge(self):
        self.events.append('purge')
        if self.fail_purge:
            raise OSError('synthetic cleanup failure')


@pytest.fixture
def fixture(tmp_path):
    state = PlatformStateStore(tmp_path / 'state.sqlite3')
    courses = CourseAdminService(state)
    students = EnrollmentAdminService(state, b's' * 32)
    for key in ('come2201', 'come3105'):
        students.add_student(key, student_key='synthetic-shared', password='123456')
    return state, courses, students


def apply(courses, key='come2201', preview=None):
    preview = preview or courses.deletion_preview(key)
    return courses.delete_course(key, confirm_course=key, expected_state=preview['expected_state'])


@pytest.mark.parametrize('phase', ['verifying', 'pinning'])
def test_legacy_intake_cannot_be_deleted_mid_verification(phase):
    with sqlite3.connect(':memory:') as db:
        db.executescript('''CREATE TABLE bundle_submission_requests(assignment_id TEXT,state TEXT);
            CREATE TABLE bundle_assignment_releases(assignment_id TEXT,course_key TEXT);
            CREATE TABLE submission_requests(assignment_id TEXT,state TEXT);
            CREATE TABLE platform_assignments(assignment_id TEXT,course_key TEXT);''')
        db.execute('INSERT INTO platform_assignments VALUES(?,?)', ('legacy', 'come2201'))
        db.execute('INSERT INTO submission_requests VALUES(?,?)', ('legacy', phase))
        with pytest.raises(PlatformConflict, match='unfinished submissions'):
            CourseAdminService._require_idle(db, 'come2201')


@pytest.mark.parametrize('status', ['preparation', 'active', 'archived'])
def test_deletes_directly_without_archive_and_preserves_other_course(fixture, status):
    state, courses, students = fixture
    if status == 'preparation':
        key = courses.create_course(code='new', name='New', year=2026, semester='1')['course_key']
    else:
        key = 'come2201'
        courses.set_status(key, status)
    credential = state.get_student_password_credential(student_key='synthetic-shared', course_key='come3105')
    before_student = state.get_student_by_key('synthetic-shared')
    preview = courses.deletion_preview(key)
    assert courses.get_course(key)['status'] == status  # preview changes nothing
    result = apply(courses, key, preview)
    assert result['deleted'] and result['cleanup_status'] == 'complete'
    with pytest.raises(PlatformNotFound):
        courses.get_course(key)
    assert state.get_student_by_key('synthetic-shared') == before_student
    assert state.get_student_password_credential(student_key='synthetic-shared', course_key='come3105') == credential
    assert len(students.list_students('come3105')) == 1
    with state._connection() as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_restart_seed_and_late_writes_cannot_resurrect_deleted_course(fixture):
    state, courses, _ = fixture
    student = state.get_student_by_key('synthetic-shared')
    apply(courses)
    restarted = PlatformStateStore(state.database)
    initialize_course_admin(restarted)
    assert [c['course_key'] for c in CourseAdminService(restarted).list_courses()] == ['come3105']
    with pytest.raises(sqlite3.IntegrityError, match='course_permanently_deleted'):
        restarted.upsert_enrollment(student_id=student.id, course_key='come2201')
    with pytest.raises(sqlite3.IntegrityError, match='course_permanently_deleted'):
        with state._write() as db:
            db.execute("UPDATE platform_enrollments SET course_key='come2201' WHERE course_key='come3105'")


def test_wrong_confirmation_and_changed_data_do_not_stage_or_delete(fixture):
    state, courses, students = fixture
    files = Files()
    courses.file_planner = lambda *_: files
    preview = courses.deletion_preview('come2201')
    with pytest.raises(ValueError):
        courses.delete_course('come2201', confirm_course='come3105', expected_state=preview['expected_state'])
    students.add_student('come2201', student_key='late-student', password='234567')
    with pytest.raises(PlatformConflict, match='변경'):
        apply(courses, preview=preview)
    assert files.events == []
    assert len(students.list_students('come2201')) == 2


def test_failed_staging_restores_files_and_database(fixture):
    state, courses, _ = fixture
    files = Files(fail_stage=True)
    courses.file_planner = lambda *_: files
    with pytest.raises(OSError):
        apply(courses)
    assert files.events == ['stage', 'rollback']
    assert courses.get_course('come2201')['status'] == 'active'
    assert courses.deletion_history() == []


def test_failed_transaction_restores_exact_triggers_and_staged_files(fixture):
    state, courses, _ = fixture
    files = Files()
    courses.file_planner = lambda *_: files
    with state._write() as db:
        db.execute("CREATE TRIGGER synthetic_fail_tombstone BEFORE INSERT ON admin_course_deletions "
                   "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with state._connection() as db:
        before = [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    with pytest.raises(sqlite3.IntegrityError, match='synthetic failure'):
        apply(courses)
    with state._connection() as db:
        after = [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    assert before == after
    assert files.events == ['stage', 'rollback']
    assert courses.get_course('come2201')['status'] == 'active'


@pytest.mark.parametrize('failure', ['files', 'runtime'])
def test_cleanup_failure_is_durable_pending_without_reopening_access(fixture, failure):
    state, courses, _ = fixture
    files = Files(fail_purge=failure == 'files')
    courses.file_planner = lambda *_: files
    if failure == 'runtime':
        def fail(key):
            with pytest.raises(PlatformNotFound):
                courses.get_course(key)
            raise RuntimeError('worker unavailable')
        courses.after_delete = fail
    result = apply(courses)
    assert result['cleanup_status'] == 'pending'
    assert files.events == ['stage', 'purge']
    assert courses.deletion_history()[0]['cleanup_status'] == 'pending'
    with pytest.raises(PlatformNotFound):
        courses.get_course('come2201')


def test_registry_invalidates_deleted_course_without_constructing_it_again(fixture):
    _, courses, _ = fixture
    created = []
    registry = CourseRuntimeRegistry(courses, lambda key: (created.append(key) or object(), object()))
    original = registry['come3105']
    registry['come2201']
    courses.after_delete = registry.invalidate
    apply(courses)
    with pytest.raises(PlatformNotFound):
        registry['come2201']
    assert 'come2201' not in registry._services
    assert registry['come3105'] is original
    assert len(created) == 2


def test_new_fk_child_rows_deleted_but_global_accounts_preserved(fixture):
    state, courses, _ = fixture
    with state._write() as db:
        db.execute('CREATE TABLE synthetic_accounts(student_key TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE synthetic_course_registration(id INTEGER PRIMARY KEY,course_key TEXT REFERENCES admin_courses(course_key),student_key TEXT REFERENCES synthetic_accounts(student_key))')
        db.execute("INSERT INTO synthetic_accounts VALUES ('same-student')")
        for key in ('come2201', 'come3105'):
            db.execute('INSERT INTO synthetic_course_registration(course_key,student_key) VALUES (?,?)', (key, 'same-student'))
    assert courses.deletion_preview('come2201')['counts']['synthetic_course_registration'] == 1
    apply(courses)
    with state._connection() as db:
        assert [r[0] for r in db.execute('SELECT course_key FROM synthetic_course_registration')] == ['come3105']
        assert db.execute('SELECT COUNT(*) FROM synthetic_accounts').fetchone()[0] == 1


def test_cross_course_fk_relationship_refused_instead_of_removing_other_course(fixture):
    state, courses, _ = fixture
    with state._write() as db:
        db.execute('CREATE TABLE synthetic_cross_course(id INTEGER PRIMARY KEY,course_key TEXT,reference_course TEXT REFERENCES admin_courses(course_key))')
        db.execute("INSERT INTO synthetic_cross_course VALUES (1,'come3105','come2201')")
    with pytest.raises(PlatformConflict, match='다른 수업'):
        courses.deletion_preview('come2201')
    assert courses.is_active('come2201') and courses.is_active('come3105')


def test_authorization_revocation_after_staging_rolls_back(fixture):
    _, courses, _ = fixture
    files = Files()
    courses.file_planner = lambda *_: files
    preview = courses.deletion_preview('come2201')
    def authorize():
        if files.events:
            raise PlatformAccessDenied('revoked')
    with pytest.raises(PlatformAccessDenied):
        courses.delete_course('come2201', confirm_course='come2201', expected_state=preview['expected_state'],
                              reauthorize=authorize)
    assert files.events == ['stage', 'rollback']
    assert courses.is_active('come2201')
    assert courses.deletion_history() == []


def test_inflight_content_publish_cannot_be_deleted(fixture):
    state, courses, _ = fixture
    with state._write() as db:
        db.execute("INSERT INTO content_collections(collection_id,course_key,title,opens_at,created_at,updated_at) "
                   "VALUES ('cc_synthetic','come2201','Title','2026-10-01T00:00:00Z','now','now')")
        db.execute("INSERT INTO content_publish_jobs VALUES ('cc_synthetic','synthetic','hash','release','preparing','now','now')")
    with pytest.raises(PlatformConflict, match='진행 중'):
        courses.deletion_preview('come2201')
    assert courses.is_active('come2201')


def test_remote_cleanup_identity_is_captured_before_transaction_and_persisted(fixture):
    state, courses, _ = fixture
    context = {'worker_instance': 'synthetic-worker-A', 'socket_sha256': 'a' * 64}
    courses.remote_cleanup_required = True
    def capture(key):
        # This write would block if the callback held the deletion transaction.
        with state._write() as db:
            assert db.execute('SELECT course_key FROM admin_courses WHERE course_key=?', (key,)).fetchone()
        return context
    seen = []
    courses.cleanup_context = capture
    def cleanup(key, value):
        seen.append((key, value))
        raise RuntimeError('different worker is now configured')
    courses.after_delete_context = cleanup
    result = apply(courses)
    assert result['cleanup_status'] == 'pending'
    assert seen == [('come2201', context)]
    with state._connection() as db:
        saved = json.loads(db.execute('SELECT counts_json FROM admin_course_deletions').fetchone()[0])
    assert saved['remote_cleanup_required'] is True
    assert saved['remote_cleanup_context'] == context
