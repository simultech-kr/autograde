"""Privacy closure uses synthetic identities and real isolated managed files."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import threading

import pytest

from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.course_deletion_runtime import configure_course_deletion
from autograde.course_file_guard import course_file_guard
from autograde.content_store import ContentService
from autograde.platform_bundle import BundleStore
from autograde.platform_state import PlatformConflict, PlatformNotFound, PlatformStateStore
from autograde.privacy_file_guard import privacy_file_guard
from autograde.settings import AppPaths
from autograde.student_privacy import initialize, POLICY_VERSION
from autograde.student_registration import initialize as initialize_members


@pytest.fixture
def setup(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    initialize(state)
    initialize_members(state)
    courses = CourseAdminService(state)
    class Registry:
        def invalidate(self, key):
            pass
    configure_course_deletion(paths, courses, Registry())
    return paths, state, courses, EnrollmentAdminService(state, b's' * 32)


def put(paths, relative, content):
    path = paths.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def student(setup, key, course='come2201'):
    _, state, _, students = setup
    password = str(100000 + int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 900000)
    students.add_student(course, student_key=key, password=password, name='Synthetic ' + key)
    return state.get_student_by_key(key).id


def release(setup, key='practice', course='come2201'):
    paths, state, _, _ = setup
    return state.register_bundle_assignment_release(assignment_id=key, course_key=course,
        assignment_key=key, release_id='v1', title='Public practice', starter_path=str(paths.bundles / 'public.tar.gz'),
        starter_digest='sha256:' + 'a' * 64, starter_size_bytes=1,
        assessment_path=str(paths.instructor_inputs / 'public'), assessment_digest='sha256:' + 'b' * 64,
        runner_image='pilot-local:v1', rubric_version='v1', max_score=100, result_policy='immediate', ready=True)


def receipt(setup, identifier, sid, assignment, *, score=76, digest='sha256:' + 'c' * 64):
    _, state, _, _ = setup
    now = datetime.now(timezone.utc)
    device = identifier + '-device'
    verifier = hashlib.sha256(device.encode()).hexdigest()
    state.create_device_authorization(authorization_id=device, device_code_hash=verifier,
        user_code_hmac=verifier, course_key=assignment.course_key, device_label='Synthetic', expires_at=now + timedelta(minutes=5))
    with state._connection() as db:
        key = db.execute('SELECT student_key FROM platform_students WHERE id=?', (sid,)).fetchone()[0]
    state.approve_device_authorization(user_code_hmac=verifier, auth_subject='local:' + key, course_key=assignment.course_key)
    session = identifier + '-session'
    state.consume_device_authorization(device_code_hash=verifier, course_key=assignment.course_key,
        session_id=session, token_family_id=identifier + '-family', access_token_hash=verifier,
        access_token_expires_at=now + timedelta(minutes=15), refresh_token_hash=verifier,
        refresh_token_expires_at=now + timedelta(days=1))
    with state._write() as db:
        db.execute('INSERT INTO bundle_submission_requests(submission_id,student_id,session_id,assignment_id,endpoint,idempotency_key,request_hash,source_digest,source_size_bytes,state,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (identifier,sid,session,assignment.assignment_id,'/bundle',identifier,verifier,digest,10,'published','2026-11-01T00:00:00Z','now'))
        db.execute('INSERT INTO bundle_submission_results VALUES(?,?,?,?,?,?,?,?,?,?)',
            (identifier + '-result',identifier,sid,digest,score,100,'{}','[]','now','now'))


def test_orphan_identity_password_events_and_consent_erased_shared_course_preserved(setup):
    _, state, courses, _ = setup
    own = student(setup, 'private-001')
    shared = student(setup, 'shared-002')
    student(setup, 'shared-002', 'come3105')
    with state._write() as db:
        for sid, key in [(own, 'private-001'), (shared, 'shared-002')]:
            db.execute('INSERT INTO student_accounts(student_key,password_hash,student_id,created_at,updated_at) VALUES(?,?,?,?,?)', (key,'synthetic-password',sid,'now','now'))
            db.execute("INSERT INTO student_membership_events(student_key,actor,action,detail,created_at) VALUES(?,'student','password-change','changed','now')", (key,))
            db.execute("INSERT INTO student_privacy_consents VALUES('come2201',?,'granted',?,1,'now')", (sid, POLICY_VERSION))
            db.execute("INSERT INTO student_privacy_events(course_key,student_id,decision,policy_version,revision,created_at) VALUES('come2201',?,'granted',?,1,'now')", (sid, POLICY_VERSION))
    courses.set_status('come2201', 'archived')
    with pytest.raises(PlatformNotFound):
        state.get_student_by_key('private-001')
    assert state.get_student_by_key('shared-002').id == shared
    with state._connection() as db:
        for table in ['student_privacy_events','student_privacy_consents','platform_enrollments']:
            assert not db.execute(f'SELECT 1 FROM {table} WHERE course_key=?', ('come2201',)).fetchone()
        assert [r[0] for r in db.execute('SELECT student_key FROM student_accounts')] == ['shared-002']
        assert [r[0] for r in db.execute('SELECT student_key FROM student_membership_events')] == ['shared-002']
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert courses.archive_status('come2201')['cleanup_status'] == 'complete'
    with pytest.raises(PlatformConflict):
        courses.set_status('come2201', 'active')
    courses.set_status('come2201', 'archived')


def test_current_consent_only_coarse_minimum_five_aggregate_and_no_individual_payload(setup):
    _, state, courses, _ = setup
    assignment = release(setup)
    small = release(setup, 'small')
    for i in range(8):
        sid = student(setup, f'private-{i}')
        receipt(setup, f'sub-{i}', sid, assignment, score=76 if i < 5 else 0)
        receipt(setup, f'small-{i}', sid, small, score=50) if i < 4 else None
        with state._write() as db:
            if i != 7:
                db.execute('INSERT INTO student_privacy_consents VALUES(?,?,?,?,?,?)',
                    ('come2201',sid,'declined' if i == 5 else 'granted', 'obsolete' if i == 6 else POLICY_VERSION,1,'now'))
    courses.set_status('come2201', 'archived')
    report = courses.archive_status('come2201')
    assert report['aggregate']['assignments'] == [{'assignment_id':'practice','cohort_size_floor':5,'mean_score_band':[70,80]}]
    with state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM bundle_submission_results').fetchone()[0] == 0
        raw = json.dumps(dict(db.execute('SELECT * FROM admin_course_privacy_archives').fetchone()))
        assert 'private-' not in raw and 'sha256:' not in raw and 'expected_state' not in raw
        assert db.execute('SELECT COUNT(*) FROM bundle_assignment_releases').fetchone()[0] == 2


def test_legacy_null_course_revoked_credentials_do_not_retain_last_student_identity(setup):
    _, state, courses, _ = setup
    own = student(setup,'legacy-one')
    shared = student(setup,'legacy-shared')
    student(setup,'legacy-shared','come3105')
    with state._write() as db:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='trg_platform_token_family_course_insert'").fetchone()[0]
        db.execute('DROP TRIGGER trg_platform_token_family_course_insert')
        for sid in (own, shared):
            db.execute('INSERT INTO platform_token_families(token_family_id,student_id,current_refresh_token_hash,refresh_token_expires_at,created_at,updated_at,revoked_at,course_key) VALUES(?,?,?,?,?,?,?,NULL)',
                       ('legacy-'+str(sid),sid,str(sid)*64,'past','past','past','past'))
        db.execute(sql)
    courses.set_status('come2201','archived')
    assert courses.archive_status('come2201')['cleanup_status'] == 'complete'
    with pytest.raises(PlatformNotFound):
        state.get_student_by_key('legacy-one')
    assert state.get_student_by_key('legacy-shared').id == shared
    with state._connection() as db:
        assert [r[0] for r in db.execute('SELECT student_id FROM platform_token_families')] == [shared]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_consent_without_remaining_enrollment_still_erases_final_identity(setup):
    _,state,courses,_ = setup
    sid = student(setup,'withdrawn-person')
    with state._write() as db:
        db.execute("INSERT INTO student_privacy_consents VALUES('come2201',?,'declined',?,1,'now')",(sid,POLICY_VERSION))
        db.execute('DELETE FROM platform_student_passwords WHERE enrollment_id IN (SELECT id FROM platform_enrollments WHERE student_id=?)',(sid,))
        db.execute('DELETE FROM platform_enrollments WHERE student_id=?',(sid,))
    courses.set_status('come2201','archived')
    with pytest.raises(PlatformNotFound):
        state.get_student_by_key('withdrawn-person')


def test_unconfigured_file_cleanup_refuses_before_personal_rows_change(setup):
    _,state,_,_ = setup
    sid = student(setup,'one')
    assignment = release(setup)
    receipt(setup,'personal-submission',sid,assignment)
    unconfigured = CourseAdminService(state)
    with pytest.raises(PlatformConflict,match='파일 파기'):
        unconfigured.set_status('come2201','archived')
    assert unconfigured.is_active('come2201')
    assert state.get_student_by_key('one').id == sid
    with state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM bundle_submission_results').fetchone()[0] == 1


def test_retry_without_managed_cleanup_hook_keeps_durable_pending(setup, monkeypatch):
    paths,state,courses,_ = setup
    student(setup,'one')
    put(paths,'state.sqlite3.learning-checkpoints/'+'d'*64+'/owner.json',json.dumps({'course_key':'come2201'}))
    from autograde.course_deletion_files import CourseFilePlan
    def fail(self):
        self._unlock()
        raise OSError('synthetic failure')
    monkeypatch.setattr(CourseFilePlan,'purge',fail)
    courses.set_status('come2201','archived')
    unconfigured = CourseAdminService(state)
    unconfigured.set_status('come2201','archived')
    result = unconfigured.archive_status('come2201')
    assert result['cleanup_status'] == 'pending' and 'managed_files' in result['pending_tasks']


def test_real_cas_checkpoint_material_cache_and_private_git_removed_public_shared_survive(setup, tmp_path):
    paths, state, courses, _ = setup
    own = student(setup, 'private-001')
    other = student(setup, 'other-002', 'come3105')
    a1, a2 = release(setup), release(setup, 'other', 'come3105')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'main.cpp').write_text('private code')
    store = BundleStore(paths.bundles)
    private = store.create_from_directory(source, kind='submission')
    (source / 'main.cpp').write_text('shared code')
    shared = store.create_from_directory(source, kind='submission')
    public = store.create_from_directory(source, kind='starter')
    receipt(setup, 'sub-own', own, a1, digest=private.archive_sha256)
    receipt(setup, 'sub-shared-own', own, a1, digest=shared.archive_sha256)
    receipt(setup, 'sub-other', other, a2, digest=shared.archive_sha256)
    checkpoint = put(paths, 'state.sqlite3.learning-checkpoints/' + 'd'*64 + '/index.json', json.dumps({'checkpoints':[{'assignment_id':'practice'}]}))
    code = put(paths, 'state.sqlite3.learning-checkpoints/' + 'd'*64 + '/archives/source.tar.gz', 'private code')
    material_id = 'mat_' + 'e'*32
    with state._write() as db:
        db.execute('INSERT INTO student_materials(material_id,course_key,title,opens_at,closes_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
            (material_id,'come2201','Private','2026-01-01','2027-01-01','now','now'))
    material = put(paths, f'student-materials/{material_id}-{own}-'+'e'*64+'.pdf', 'private report')
    cached = put(paths, 'cache/instructor-submission-downloads/' + 'f'*64+'.zip', 'private export')
    incomplete = [put(paths,'bundles/.incoming-orphan/upload.tar.gz','private upload'),
        put(paths,'cache/instructor-submission-downloads/.building-orphan.zip','private export'),
        put(paths,'student-materials/.upload-'+'f'*32,'private material')]
    content = ContentService(state, paths.root / 'content')
    releases = []
    for label, audience in [('public',None), ('private',[own])]:
        collection = content.create('come2201',title=label,student_ids=audience,actor='teacher')
        collection = content.upload('come2201',collection['collection_id'],revision=collection['revision'],path=label+'.txt',data=label.encode(),actor='teacher')
        releases.append(content.publish('come2201',collection['collection_id'],revision=collection['revision'],idempotency_key='publish-'+label,actor='teacher'))
    courses.set_status('come2201', 'archived')
    assert courses.archive_status('come2201')['cleanup_status'] == 'complete'
    assert not private.path.exists() and shared.path.exists() and public.path.exists()
    assert all(not p.exists() for p in [checkpoint,code,material,cached,*incomplete])
    repository = content.git_repository_path('come2201')
    assert content._git(repository,'show',releases[0]['git_commit']+':content/public.txt') == b'public'
    with pytest.raises(Exception):
        content._git(repository,'cat-file','-e',releases[1]['git_commit'])
    with state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM content_collections').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM bundle_submission_results').fetchone()[0] == 1


def test_stale_preview_and_active_publication_do_not_delete_data(setup):
    paths,state,courses,_ = setup
    student(setup,'one')
    preview = courses.privacy_archive_preview('come2201')
    student(setup,'two')
    with pytest.raises(PlatformConflict, match='변경'):
        courses.set_status('come2201','archived',expected_state=preview['expected_state'])
    ready, end = threading.Event(), threading.Event()
    def writer():
        with privacy_file_guard(paths.root):
            ready.set()
            assert end.wait(10)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(writer)
        assert ready.wait(10)
        try:
            with pytest.raises(PlatformConflict):
                courses.set_status('come2201','archived')
            assert courses.is_active('come2201')
        finally:
            end.set()
        future.result()
    with privacy_file_guard(paths.root,exclusive=True):
        with course_file_guard(paths.root,'come2201'):
            pass
    with privacy_file_guard(paths.root):
        with pytest.raises(PlatformConflict):
            with privacy_file_guard(paths.root,exclusive=True):
                pass


def test_committed_purge_failure_recovers_on_restart_and_does_not_reopen(setup, monkeypatch):
    paths,state,courses,_ = setup
    student(setup,'one')
    checkpoint = put(paths,'state.sqlite3.learning-checkpoints/'+'d'*64+'/owner.json',json.dumps({'course_key':'come2201'}))
    from autograde.course_deletion_files import CourseFilePlan
    original = CourseFilePlan.purge
    def fail(self):
        self._unlock()
        raise OSError('synthetic cleanup failure')
    monkeypatch.setattr(CourseFilePlan,'purge',fail)
    courses.set_status('come2201','archived')
    assert not checkpoint.exists()
    assert courses.archive_status('come2201')['pending_tasks'] == ['managed_files']
    with pytest.raises(PlatformConflict,match='정리가 남아'):
        courses.deletion_preview('come2201')
    monkeypatch.setattr(CourseFilePlan,'purge',original)
    restarted = CourseAdminService(PlatformStateStore(state.database))
    class Registry:
        def invalidate(self,key):
            pass
    configure_course_deletion(paths,restarted,Registry())
    assert restarted.archive_status('come2201')['cleanup_status'] == 'complete'
    assert not list((paths.root/'.course-deletions').iterdir())


def test_transaction_failure_restores_sources_and_personal_rows(setup):
    paths,state,courses,_ = setup
    student(setup,'one')
    checkpoint = put(paths,'state.sqlite3.learning-checkpoints/'+'d'*64+'/owner.json',json.dumps({'course_key':'come2201'}))
    with state._write() as db:
        db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON admin_course_privacy_archives BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='synthetic failure'):
        courses.set_status('come2201','archived')
    assert checkpoint.exists() and courses.is_active('come2201')
    assert state.get_student_by_key('one')
    assert courses.archive_status('come2201') is None
