"""Real HTTP, durable queue, migration and process-launch integration coverage."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import base64
import json
import re
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import autograde.platform_state as state_module
from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.course_runtime import CourseRuntimeRegistry, SharedCourseWorker
from autograde.pilot_config import PilotConfigError, load_pilot_config
from autograde.pilot_roster import initialize_student_roster, initialize_web_roster, RosterBootstrapError
from autograde.platform_auth import hash_student_password
from autograde.platform_bundle import BundleStore
from autograde.platform_bundle_worker import BundleSubmissionProcessor
from autograde.platform_grader import PilotLocalGrader
from autograde.platform_portal import CourseAPI, CoursePortal
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths
from autograde.workspace import WorkspaceBuilder
from test_course_portal import API, WEB, SECRET, connect, cookie, csrf, login, request, serving


@pytest.fixture
def runtime(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    courses = CourseAdminService(state)
    students = EnrollmentAdminService(state, SECRET)
    bundles = BundleStore(paths.bundles)
    graders, created = [], []
    password_slots = threading.BoundedSemaphore(4)

    def factory(key):
        created.append(key)
        grader = PilotLocalGrader()
        graders.append(grader)
        processor = BundleSubmissionProcessor(state=state, course_key=key,
            workspace_builder=WorkspaceBuilder(paths.workspaces), grader=grader)
        service = StudentPlatformService(state=state, server_secret=SECRET, course_key=key,
            public_base_url=API, bundle_store=bundles, instructor_token='instructor' * 4,
            notify_bundle_submission=lambda value: worker.submission_available(value))
        service._password_verification_slots = password_slots
        return service, processor

    registry = CourseRuntimeRegistry(courses, factory)
    courses.before_activate = lambda key: registry[key]
    worker = SharedCourseWorker(state, registry, worker_count=4, recovery_interval_seconds=0.05)
    portal = CoursePortal(registry, SECRET, WEB, API, courses=courses)
    api = CourseAPI(registry, SECRET, courses=courses)
    worker.start()
    try:
        with ExitStack() as stack:
            web_server = stack.enter_context(serving(portal, WEB))
            api_server = stack.enter_context(serving(api, API))
            yield SimpleNamespace(paths=paths, state=state, courses=courses, students=students, bundles=bundles,
                                  registry=registry, worker=worker, web=web_server, api=api_server,
                                  created=created)
    finally:
        worker.stop(10)
        for grader in graders:
            grader.close()


def add_assignment(runtime, key):
    starter = runtime.paths.root / ('starter-' + key)
    starter.mkdir()
    (starter / 'answer.txt').write_text('answer', encoding='utf-8')
    bundle = runtime.bundles.create_from_directory(starter, kind='starter')
    assessment = runtime.paths.root / ('assessment-' + key)
    assessment.mkdir()
    (assessment / 'grade.py').write_text(
        'import json,os\nfrom pathlib import Path\n'
        'answer=(Path(os.environ["AUTOGRADE_SUBMISSION_DIR"])/"answer.txt").read_text()\n'
        'print(json.dumps({"score":10 if answer=="solution" else 0,"max_score":10,"rubric":{},"diagnostics":[]}))\n',
        encoding='utf-8')
    digest = WorkspaceBuilder(runtime.paths.workspaces).digest_instructor_tree(assessment).sha256
    now = datetime.now(timezone.utc)
    return runtime.state.register_bundle_assignment_release(assignment_id='asn_' + key, course_key=key,
        assignment_key='lab01', release_id='v1', title='Synthetic lab ' + key,
        starter_path=str(bundle.path), starter_digest=bundle.digest, starter_size_bytes=bundle.compressed_bytes,
        assessment_path=str(assessment), assessment_digest=digest, runner_image='pilot-local:v1',
        rubric_version='v1', max_score=10, result_policy='immediate',
        opens_at=now - timedelta(days=1), due_at=now + timedelta(days=1), ready=True)


def claim_assignment(runtime, key, student, password):
    status, headers, body = login(runtime.web, key, password, student)
    assert status == 200, body
    status, _, body = request(runtime.web, f'/courses/{key}/claims', method='POST', origin=WEB,
        cookie=cookie(headers), data={'csrf': csrf(body), 'assignment_id': 'asn_' + key})
    assert status == 200, body
    return re.search(rb'<code>(AK1-[A-Z0-9-]+)</code>', body)[1].decode()


def test_new_course_after_pool_started_real_http_acceptance_and_archive(runtime):
    original_threads = tuple(runtime.worker._threads)
    assert len(original_threads) == 4 and runtime.worker.healthy
    key = runtime.courses.create_course(code='new101', name='New course', year=2026, semester='2')['course_key']
    assert key not in runtime.created
    assert request(runtime.web, '/courses/' + key)[0] == 403
    assert key.encode() not in request(runtime.web, '/')[2]
    runtime.students.add_student(key, student_key='001', password='123456')
    runtime.courses.set_status(key, 'active')
    add_assignment(runtime, key)
    assert runtime.created.count(key) == 1
    assert key.encode() in request(runtime.web, '/')[2]
    _, tokens = connect(runtime.api, claim_assignment(runtime, key, '001', '123456'))
    status, _, body = request(runtime.api, '/v1/accepted-assignments', token=tokens['access_token'])
    assert status == 200 and key.encode() in body
    assert request(runtime.api, '/v1/assignments/asn_come2201/starter', token=tokens['access_token'])[0] in (403, 404)
    status, headers, body = login(runtime.web, key, '123456', '001')
    old_cookie, old_csrf = cookie(headers), csrf(body)
    runtime.courses.set_status(key, 'archived')
    assert request(runtime.web, '/courses/' + key, cookie=old_cookie)[0] == 403
    assert request(runtime.api, '/v1/me', token=tokens['access_token'])[0] == 401
    runtime.courses.set_status(key, 'active')
    assert request(runtime.web, f'/courses/{key}/claims', method='POST', origin=WEB, cookie=old_cookie,
                   data={'csrf': old_csrf, 'assignment_id': 'asn_' + key})[0] == 403
    assert request(runtime.api, '/v1/me', token=tokens['access_token'])[0] == 401
    assert request(runtime.api, '/v1/tokens/refresh', method='POST',
                   data={'refresh_token': tokens['refresh_token']})[0] == 401
    assert login(runtime.web, key, '123456', '001')[0] == 200
    assert tuple(runtime.worker._threads) == original_threads
    assert request(runtime.web, '/courses/unknown101')[0] == 404
    assert 'unknown101' not in runtime.created


def test_twenty_five_real_http_submissions_across_dynamic_courses_fixed_pool(runtime):
    keys = ['come2201', 'come3105']
    key = runtime.courses.create_course(code='extra', name='Third course', year=2026, semester='2')['course_key']
    runtime.courses.set_status(key, 'active')
    keys.append(key)
    for key in keys:
        add_assignment(runtime, key)
    for i in range(25):
        runtime.students.add_student(keys[i % 3], student_key=f'{i:06}', password=f'{1000 + i:06}')
    source = runtime.paths.root / 'solution'
    source.mkdir()
    (source / 'answer.txt').write_text('solution', encoding='utf-8')
    bundle = runtime.bundles.create_from_directory(source, kind='submission').path.read_bytes()
    threads = tuple(runtime.worker._threads)

    def submit(i):
        key = keys[i % 3]
        _, tokens = connect(runtime.api, claim_assignment(runtime, key, f'{i:06}', f'{1000 + i:06}'))
        status, _, body = request(runtime.api, f'/v1/assignments/asn_{key}/submissions', method='POST',
                                  token=tokens['access_token'], raw=bundle)
        assert status == 202, body
        submission_id = json.loads(body)['submission']['submission_id']
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status, _, body = request(runtime.api, f'/v1/submissions/{submission_id}/result', token=tokens['access_token'])
            result = json.loads(body).get('result')
            if status == 200 and result:
                assert result['score'] == 10, body
                return submission_id
            time.sleep(0.05)
        pytest.fail(f'submission did not finish: {submission_id}')

    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(submit, i) for i in range(25)]
        added = runtime.courses.create_course(code='during-load', name='Added during student load', year=2026, semester='2')
        runtime.courses.set_status(added['course_key'], 'active')
        runtime.students.add_student(added['course_key'], student_key='during-load', password='333333')
        ids = [future.result() for future in futures]
    assert len(set(ids)) == 25
    assert tuple(runtime.worker._threads) == threads
    assert len(threads) == 4 and all(thread.is_alive() for thread in threads)
    assert runtime.worker.healthy
    with runtime.state._connection() as db:
        assert db.execute("SELECT count(*) FROM bundle_submission_requests WHERE state='published'").fetchone()[0] == 25


@pytest.mark.parametrize('code', ['_lab', '-lab'])
def test_every_permitted_course_code_gets_a_reachable_student_url(runtime, code):
    key = runtime.courses.create_course(code=code, name='Allowed code', year=2026, semester='2')['course_key']
    runtime.students.add_student(key, student_key='0001', password='123456')
    runtime.courses.set_status(key, 'active')
    assert request(runtime.web, '/courses/' + key)[0] == 200
    assert login(runtime.web, key, '123456', '0001')[0] == 200


def test_metadata_edit_does_not_revoke_student_web_login(runtime):
    runtime.students.add_student('come2201', student_key='0001', password='123456')
    add_assignment(runtime, 'come2201')
    status, headers, body = login(runtime.web, 'come2201', '123456', '0001')
    assert status == 200
    runtime.courses.update_course('come2201', name='Updated course title', description='Corrected description')
    status, _, body = request(runtime.web, '/courses/come2201/claims', method='POST', origin=WEB,
        cookie=cookie(headers), data={'csrf': csrf(body), 'assignment_id': 'asn_come2201'})
    assert status == 200, body
    assert b'AK1-' in body


def test_v10_migration_preserves_identifiers_credentials_and_bootstrap(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(state_module, '_LATEST_SCHEMA_VERSION', 10)
        state = PlatformStateStore(tmp_path / 'state.sqlite3')
    student = state.upsert_local_student(student_key='001', auth_subject='local:001')
    state.upsert_enrollment(student_id=student.id, course_key='come2201')
    verifier = hash_student_password('123456')
    state.set_student_password_hash(student_id=student.id, course_key='come2201', password_hash=verifier)
    with state._write() as db:
        db.execute("INSERT INTO platform_roster_bootstrap VALUES(1,1,'2026-09-15T00:00:00Z')")
    with state._connection() as db:
        before = {table: [tuple(r) for r in db.execute(f'SELECT * FROM {table}')] for table in
                  ('platform_students', 'platform_enrollments', 'platform_student_passwords', 'platform_roster_bootstrap')}
    assert state.schema_version() == 10
    assert state.migrate() == 11
    with state._connection() as db:
        after = {table: [tuple(r) for r in db.execute(f'SELECT * FROM {table}')] for table in before}
        assert list(db.execute('PRAGMA foreign_key_check')) == []
    assert before == after
    assert CourseAdminService(state).get_course('come2201')['year'] is None
    assert initialize_web_roster(state)['status'] == 'already_initialized'


def test_empty_web_bootstrap_once_and_strict_csv_mode(tmp_path):
    state = PlatformStateStore(tmp_path / 'state.sqlite3')
    with pytest.raises(RosterBootstrapError):
        initialize_student_roster(state, tmp_path / 'missing.csv')
    assert state.roster_bootstrap_status() is None
    assert initialize_web_roster(state)['status'] == 'web_initialized'
    students = EnrollmentAdminService(state, SECRET)
    students.add_student('come2201', student_key='001', password='123456')
    students.set_active('come2201', '001', False)
    assert initialize_web_roster(state)['status'] == 'already_initialized'
    assert initialize_student_roster(state, tmp_path / 'missing.csv')['status'] == 'already_initialized'
    assert not students.get_student('come2201', '001')['active']
    assert state.roster_bootstrap_status()['enrollment_count'] == 0
    other = PlatformStateStore(tmp_path / 'other.sqlite3')
    EnrollmentAdminService(other, SECRET).add_student('come2201', student_key='001', password='123456')
    with pytest.raises(RosterBootstrapError):
        initialize_web_roster(other)


def test_v11_failed_migration_rolls_back_all_new_tables(tmp_path, monkeypatch):
    import autograde.instructor_schema as schema
    with monkeypatch.context() as patch:
        patch.setattr(state_module, '_LATEST_SCHEMA_VERSION', 10)
        state = PlatformStateStore(tmp_path / 'state.sqlite3')
    student = state.upsert_local_student(student_key='001', auth_subject='local:001')
    original = schema.initialize_instructor_runtime
    def fail_after_changes(connection):
        original(connection)
        raise RuntimeError('synthetic migration failure')
    with monkeypatch.context() as patch:
        patch.setattr(schema, 'initialize_instructor_runtime', fail_after_changes)
        with pytest.raises(RuntimeError, match='synthetic'):
            state.migrate()
    assert state.schema_version() == 10
    assert state.get_student_by_key('001').id == student.id
    with state._connection() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='admin_courses'").fetchone() is None
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='platform_roster_bootstrap'").fetchone()
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='platform_roster_bootstrap_v9'").fetchone() is None
    assert state.migrate() == 11


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_feature_enabled_real_cli_process_launch_and_readiness(tmp_path):
    api_port, web_port = free_port(), free_port()
    while web_port == api_port:
        web_port = free_port()
    config = tmp_path / 'pilot.csv'
    config.write_text('key,value\ncourse_key,come2201\ndata_root,data\nlisten,127.0.0.1\n'
        f'port,{api_port}\npublic_base_url,http://127.0.0.1:{api_port}\n'
        f'web_port,{web_port}\nweb_public_base_url,http://127.0.0.1:{web_port}\n'
        'grading_runtime,pilot-local\nbundle_worker_count,4\n'
        'instructor_assignment_web_enabled,true\nroster_bootstrap_mode,web\n', encoding='utf-8')
    config.chmod(0o600)
    assert load_pilot_config(config).values['roster_bootstrap_mode'] == 'web'
    process = subprocess.Popen([sys.executable, '-m', 'autograde.pilot_portal_cli', '--config', str(config)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    web = SimpleNamespace(server_address=('127.0.0.1', web_port))
    api = SimpleNamespace(server_address=('127.0.0.1', api_port))
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            try:
                if request(web, '/readyz')[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail('CLI process failed to become ready')
        assert request(api, '/readyz')[0] == 200
        assert request(web, '/instructor')[0] == 401
        token = (tmp_path / 'data' / 'platform-instructor-token').read_text().strip()
        auth = 'Basic ' + base64.b64encode(('instructor:' + token).encode()).decode()
        status, headers, body = request(web, '/instructor', authorization=auth)
        assert status == 200, body
        assert '수업 관리'.encode() in body
        instructor_cookie, instructor_csrf = cookie(headers), csrf(body)
        origin = f'http://127.0.0.1:{web_port}'
        status, headers, body = request(web, '/instructor/courses', method='POST', authorization=auth,
            origin=origin, cookie=instructor_cookie, data={'csrf': instructor_csrf, 'code': 'live101',
                'name': 'Live process course', 'year': '2026', 'semester': '2', 'section': '01',
                'description': 'Created after server start\r\nNo restart required'})
        assert status == 303, body
        base = headers['Location']
        key = base.split('/')[2]
        assert request(web, '/courses/' + key)[0] == 403
        status, _, body = request(web, base + '/status', method='POST', authorization=auth, origin=origin,
            cookie=instructor_cookie, data={'csrf': instructor_csrf, 'confirm': 'yes', 'status': 'active'})
        assert status == 303, body
        status, _, body = request(web, base + '/students', method='POST', authorization=auth, origin=origin,
            cookie=instructor_cookie, data={'csrf': instructor_csrf, 'student_key': '0001', 'name': 'Pilot student',
                                           'active': 'true', 'password': '123456'})
        assert status == 200, body
        status, headers, body = request(web, '/courses/' + key)
        assert status == 200, body
        status, _, body = request(web, '/courses/' + key + '/login', method='POST', origin=origin,
            cookie=cookie(headers), data={'csrf': csrf(body), 'student_key': '0001', 'password': '123456'})
        assert status == 200 and '학생 인증은 완료되었습니다'.encode() in body
        assert request(api, '/instructor', authorization=auth)[0] == 404
        assert not (tmp_path / 'student_roster.csv').exists()
    finally:
        process.terminate()
        try:
            output, error = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            output, error = process.communicate(timeout=5)
            pytest.fail('server process did not shut down')
    assert process.returncode == 0, (output, error)
    assert '"workers": 4' in output


def test_web_bootstrap_requires_explicit_admin_feature(tmp_path):
    config = tmp_path / 'pilot.csv'
    config.write_text('key,value\ncourse_key,come2201\ndata_root,data\nlisten,127.0.0.1\n'
                      'port,18080\npublic_base_url,http://127.0.0.1:18080\nroster_bootstrap_mode,web\n')
    with pytest.raises(PilotConfigError):
        load_pilot_config(config)
