from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from autograde.course_admin import CourseAdminService, EnrollmentAdminService, initialize_course_admin
from autograde.platform_auth import verify_student_password
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, PlatformStateStore


@pytest.fixture
def services(tmp_path):
    state = PlatformStateStore(tmp_path / 'state.sqlite3')
    initialize_course_admin(state)
    return state, CourseAdminService(state), EnrollmentAdminService(state, b's' * 32)


def test_course_seed_create_duplicate_and_locked_identity(services):
    state, courses, _ = services
    initialize_course_admin(state)
    assert [c['course_key'] for c in courses.list_courses()] == ['come2201', 'come3105']
    assert courses.get_course('come2201')['year'] is None
    course = courses.create_course(code='come2201', name='Systems', year=2026, semester='2')
    assert course['course_key'] != 'come2201'
    assert course['status'] == 'preparation'
    with pytest.raises(PlatformConflict):
        courses.create_course(code='come2201', name='Duplicate', year=2026, semester='2')
    assert courses.update_course(course['course_key'], section='02')['section'] == '02'
    courses.set_status(course['course_key'], 'active')
    with pytest.raises(PlatformConflict):
        courses.update_course(course['course_key'], year=2027)
    assert courses.update_course(course['course_key'], name='New title')['name'] == 'New title'
    assert not courses.is_active('nonexistent')


def test_failed_runtime_preparation_does_not_activate(services):
    state, courses, _ = services
    key = courses.create_course(code='x', name='X', year=2026, semester=1)['course_key']
    def fail(key):
        raise RuntimeError('worker unavailable')
    with pytest.raises(RuntimeError):
        CourseAdminService(state, before_activate=fail).set_status(key, 'active')
    assert courses.get_course(key)['status'] == 'preparation'


def test_course_update_stale_browser_revision_is_rejected(services):
    _, courses, _ = services
    initial = courses.get_course('come2201')
    courses.update_course('come2201', name='First edit', revision=initial['revision'])
    with pytest.raises(PlatformConflict, match='another page'):
        courses.update_course('come2201', name='Stale second edit', revision=initial['revision'])
    assert courses.get_course('come2201')['name'] == 'First edit'


def test_course_description_accepts_native_multiline_textarea(services):
    _, courses, _ = services
    key = courses.create_course(code='text', name='Description test', year=2026, semester='1',
                                description='First line\r\nSecond\tline')['course_key']
    assert courses.get_course(key)['description'] == 'First line\nSecond\tline'
    assert courses.update_course('come2201', description='A\r\nB')['description'] == 'A\nB'
    with pytest.raises(ValueError):
        courses.update_course(key, name='Name\nline')


@pytest.mark.parametrize('fields', [dict(code='../bad'), dict(year='abc'), dict(semester='3'),
                                 dict(name=''), dict(section='x' * 17), dict(description='\x00')])
def test_course_validation(services, fields):
    _, courses, _ = services
    defaults = dict(code='test', name='Title', year=2026, semester='1')
    defaults.update(fields)
    with pytest.raises(ValueError):
        courses.create_course(**defaults)


def test_student_identity_name_and_password_course_scope(services):
    state, _, students = services
    first = students.add_student('come2201', student_key='00123', name='Kim', password='123456')
    other = students.add_student('come3105', student_key='00123', name='Kim', password='123456')
    assert first['student_id'] == other['student_id']
    assert first['password'] == '123456'
    assert 'password' not in students.get_student('come2201', '00123')
    assert 'password_hash' not in json.dumps(students.list_students('come2201'))
    with pytest.raises(PlatformConflict):
        students.add_student('come2201', student_key='00123')
    with pytest.raises(PlatformConflict):
        students.add_student('come2201', student_key='00234', password='123456')
    with pytest.raises(PlatformConflict):
        students.add_student('come3105', student_key='00123', name='Other person')
    students.set_active('come2201', '00123', False)
    assert state.find_student_password_credential(student_key='00123', course_key='come2201') is None
    assert verify_student_password('123456', state.get_student_password_credential(student_key='00123', course_key='come3105').password_hash)
    activated = students.set_active('come2201', '00123', True)
    assert len(activated['password']) == 6
    assert students.get_student('come2201', '00123')['active']
    reset = students.reset_password('come2201', '00123', password='234567')
    assert reset['password'] == '234567'
    assert verify_student_password('234567', state.get_student_password_credential(student_key='00123', course_key='come2201').password_hash)
    with state._connection() as db:
        assert '234567' not in repr([tuple(r) for r in db.execute('SELECT * FROM admin_enrollment_audit')])


def test_cross_course_name_conflict_is_not_partial_registration(services):
    _, _, students = services
    students.add_student('come2201', student_key='001', name='Kim', password='123456')
    with pytest.raises(PlatformConflict):
        students.add_student('come3105', student_key='001', name='Park', password='234567')
    assert students.list_students('come3105') == []


def test_global_name_edit_requires_matching_previous_name_and_preserves_credentials(services):
    state, _, students = services
    for course in ('come2201', 'come3105'):
        students.add_student(course, student_key='001', name='Kim', password='123456')
    before = state.get_student_password_credential(student_key='001', course_key='come3105').password_hash
    students.update_name('come2201', '001', name='Kim Updated', expected_name='Kim')
    assert students.get_student('come3105', '001')['name'] == 'Kim Updated'
    assert state.get_student_password_credential(student_key='001', course_key='come3105').password_hash == before
    with pytest.raises(PlatformConflict):
        students.update_name('come3105', '001', name='Stale', expected_name='Kim')
    assert students.get_student('come2201', '001')['name'] == 'Kim Updated'
    with pytest.raises(PlatformNotFound):
        students.update_name('come2201', 'not-enrolled', name='Someone', expected_name='')
    students.update_name('come2201', '001', name='', expected_name='Kim Updated')
    assert students.get_student('come3105', '001')['name'] == ''


def test_archive_blocks_writes_and_preserves_records(services):
    _, courses, students = services
    students.add_student('come2201', student_key='001', password='123456')
    courses.set_status('come2201', 'archived')
    assert not courses.is_active('come2201')
    assert len(students.list_students('come2201')) == 1
    with pytest.raises(PlatformConflict):
        students.add_student('come2201', student_key='002')
    courses.set_status('come2201', 'active')
    assert courses.is_active('come2201')


def test_csv_atomic_preview_privacy_and_retry(services):
    state, _, students = services
    preview = students.preview_csv('come2201', b'\xef\xbb\xbfstudent_key,active,password,name\n0001,true,123456,Kim\n0002,true,,Lee\n',
                                   session_id='instructor-session', auto_generate=True)
    assert preview['valid']
    assert [r['status'] for r in preview['rows']] == ['new', 'new']
    assert '123456' not in json.dumps(preview)
    with state._connection() as db:
        stored = db.execute('SELECT payload_json FROM admin_roster_previews').fetchone()[0]
        assert '123456' not in stored
        assert 'scrypt$v2$' in stored
    assert students.list_students('come2201') == []
    result = students.apply_csv('come2201', preview['preview_id'], session_id='instructor-session')
    assert result['count'] == 2 and len(result['passwords']) == 1
    assert result['passwords'][0]['student_key'] == '0002'
    retry = students.apply_csv('come2201', preview['preview_id'], session_id='instructor-session')
    assert retry['replayed'] and not retry['passwords']
    assert len(students.list_students('come2201')) == 2
    with state._connection() as db:
        assert db.execute('SELECT payload_json FROM admin_roster_previews').fetchone()[0] == '[]'


@pytest.mark.parametrize('body', [
    b'good,true,123456\nbad,true,xyz\n',
    b'good,true,123456\ngood,true,234567\n',
    b'good,true,123456\nbad,true,123456\n',
    b'good,true,123456\nbad,false,234567\n',
    b'good,true,123456\nbad,maybe,234567\n',
    b'good,true,123456\nbad,true\n',
])
def test_csv_one_bad_row_means_no_preview_no_writes(services, body):
    state, _, students = services
    result = students.preview_csv('come2201', b'student_key,active,password\n' + body, session_id='operator')
    assert not result['valid'] and result['preview_id'] is None
    assert students.list_students('come2201') == []
    assert result['rows'][-1]['errors']
    with state._connection() as db:
        assert db.execute('SELECT count(*) FROM admin_roster_previews').fetchone()[0] == 0


def test_csv_bound_session_course_expiration_and_state(services):
    state, _, students = services
    now = [100.0]
    students = EnrollmentAdminService(state, b's' * 32, clock=lambda: now[0])
    content = b'student_key,active,password\n001,true,123456\n'
    preview = students.preview_csv('come2201', content, session_id='operator')
    for key, session in [('come3105', 'operator'), ('come2201', 'other')]:
        with pytest.raises(PlatformAccessDenied):
            students.apply_csv(key, preview['preview_id'], session_id=session)
    now[0] = 700
    with pytest.raises(PlatformConflict, match='expired'):
        students.apply_csv('come2201', preview['preview_id'], session_id='operator')
    preview = students.preview_csv('come2201', content, session_id='operator')
    students.add_student('come2201', student_key='002', password='234567')
    with pytest.raises(PlatformConflict, match='changed'):
        students.apply_csv('come2201', preview['preview_id'], session_id='operator')
    assert [s['student_key'] for s in students.list_students('come2201')] == ['002']


def test_csv_existing_password_unchanged_missing_students_preserved(services):
    state, _, students = services
    students.add_student('come2201', student_key='001', password='123456')
    students.add_student('come2201', student_key='002', password='234567')
    before = state.get_student_password_credential(student_key='001', course_key='come2201').password_hash
    bad = students.preview_csv('come2201', b'student_key,active,password\n001,true,999999\n', session_id='op')
    assert not bad['valid']
    preview = students.preview_csv('come2201', b'student_key,active,password\n001,true,\n', session_id='op')
    assert preview['rows'][0]['password_action'] == 'keep'
    students.apply_csv('come2201', preview['preview_id'], session_id='op')
    assert state.get_student_password_credential(student_key='001', course_key='come2201').password_hash == before
    assert len(students.list_students('come2201')) == 2


def test_csv_duplicate_apply_concurrently_returns_password_at_most_once(services):
    _, _, students = services
    preview = students.preview_csv('come2201', b'student_key,active,password\n001,true,\n', session_id='op', auto_generate=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(students.apply_csv, 'come2201', preview['preview_id'], session_id='op') for _ in range(2)]
        results = [f.result() for f in futures]
    assert sum(len(r['passwords']) for r in results) == 1
    assert len(students.list_students('come2201')) == 1


@pytest.mark.parametrize('content', [b'', b'bad,header\n', b'student_key,active,password\n',
                                     b'\xff', b'x' * (1024 * 1024 + 1)])
def test_csv_limits_and_headers(services, content):
    _, _, students = services
    with pytest.raises(ValueError):
        students.preview_csv('come2201', content, session_id='op')


@pytest.mark.parametrize('operation', ['reset', 'deactivate', 'archive'])
def test_course_only_extension_credential_revocation(services, operation):
    state, courses, students = services
    now = datetime.now(timezone.utc)
    for i, key in enumerate(('come2201', 'come3105')):
        students.add_student(key, student_key='001', password='123456')
        state.create_device_authorization(authorization_id=f'device{i}', device_code_hash=str(i + 1) * 64,
            user_code_hmac=str(i + 3) * 64, course_key=key, device_label='test', expires_at=now + timedelta(minutes=5))
        state.approve_device_authorization(user_code_hmac=str(i + 3) * 64, auth_subject='local:001', course_key=key)
        state.consume_device_authorization(device_code_hash=str(i + 1) * 64, course_key=key,
            session_id=f'session{i}', token_family_id=f'family{i}', access_token_hash=str(i + 5) * 64,
            access_token_expires_at=now + timedelta(minutes=15), refresh_token_hash=str(i + 7) * 64,
            refresh_token_expires_at=now + timedelta(days=1))
    if operation == 'reset':
        students.reset_password('come2201', '001', password='234567')
    elif operation == 'deactivate':
        students.set_active('come2201', '001', False)
    else:
        courses.set_status('come2201', 'archived')
    with state._connection() as db:
        sessions = {r['course_key']: r['revoked_at'] for r in db.execute('SELECT * FROM platform_sessions')}
        assert sessions['come2201'] is not None and sessions['come3105'] is None
        families = {r['course_key']: r['revoked_at'] for r in db.execute('SELECT * FROM platform_token_families')}
        assert families['come2201'] is not None and families['come3105'] is None
        refresh = {r['token_family_id']: r['state'] for r in db.execute('SELECT * FROM platform_refresh_tokens')}
        assert refresh == {'family0': 'revoked', 'family1': 'active'}


def test_course_archive_new_validation_queue_blocks(services):
    state, courses, _ = services
    with state._write() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='instructor_assignment_jobs'").fetchone():
            db.execute("INSERT INTO instructor_assignment_drafts(draft_id,course_key,revision,document_json,created_at,updated_at) "
                       "VALUES ('draft','come2201',1,'{}','now','now')")
            db.execute("INSERT INTO instructor_assignment_jobs(job_id,draft_id,course_key,revision,status,created_at) "
                       "VALUES ('job','draft','come2201',1,'queued','now')")
        else:
            db.execute('CREATE TABLE instructor_assignment_jobs (course_key TEXT,status TEXT)')
            db.execute("INSERT INTO instructor_assignment_jobs VALUES ('come2201','queued')")
    with pytest.raises(PlatformConflict, match='validation'):
        courses.set_status('come2201', 'archived')
    assert courses.is_active('come2201')
