"""Ten authored C++ labs through instructor validation and student HTTP delivery."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import io
import http.client
from html import escape
import json
from pathlib import Path
import re
import shutil
import tarfile

import pytest

from autograde.assignment_admin import AssignmentAdminService, _archive_files, _document
from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.platform_bundle import BundleStore
from autograde.platform_bundle_worker import BundleSubmissionProcessor
from autograde.platform_grader import PilotLocalGrader
from autograde.platform_portal import CourseAPI, CoursePortal
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformConflict, PlatformNotFound, PlatformStateStore
from autograde.settings import AppPaths
from autograde.workshop_catalog import load_workshops, register_workshop, run_catalog
from autograde.workspace import WorkspaceBuilder
from test_course_portal import API, WEB, SECRET, serving, request, login, cookie, csrf, connect

CATALOG = Path(__file__).resolve().parents[2] / 'examples' / 'come2201-2026f'
DATES = ['2026-09-23', '2026-09-30', '2026-10-07', '2026-10-14', '2026-10-28',
         '2026-11-04', '2026-11-11', '2026-11-18', '2026-11-25', '2026-12-02']


def synthetic_workshop():
    document = _document(dict(mode='direct', language='cpp', negative_score=0,
        tests=[dict(title='synthetic', input='', output='ok\n', weight=100, public=True)]))
    source = b'#include <iostream>\nint main(){std::cout << "ok\\n";}\n'
    return dict(key='problem99', document=document, starter=_archive_files({'main.cpp': b'int main(){}\n'}),
        grading=_archive_files({'solution/main.cpp': source, 'negative/main.cpp': b'int main(){}\n',
            'tests.json': json.dumps({key: document[key] for key in ('tests', 'negative_score')}).encode()}))


def submit_http(server, token, assignment_id, key, artifact):
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=30)
    try:
        connection.request('POST', f'/v1/assignments/{assignment_id}/submissions', artifact.path.read_bytes(),
            {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/gzip', 'Idempotency-Key': key})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.fixture(scope='module')
def workshops(tmp_path_factory):
    # Pin the teaching calendar so this test remains meaningful after the term.
    from autograde import assignment_admin, platform_portal
    patch = pytest.MonkeyPatch()
    real_utc = assignment_admin.utc_iso
    patch.setattr(assignment_admin, 'utc_iso', lambda value=None: real_utc(value) if value else '2026-09-22T04:00:00+00:00')
    patch.setattr(platform_portal, 'utc_iso', lambda value=None: real_utc(value) if value else '2026-09-22T04:00:00+00:00')
    paths = AppPaths.from_value(tmp_path_factory.mktemp('come2201-labs') / 'data').ensure()
    state = PlatformStateStore(paths.database)
    courses = CourseAdminService(state)
    admin = AssignmentAdminService(state, paths, course_status=courses.get_course)
    items = load_workshops(CATALOG)
    assert len(items) == 10
    # Lower-level published availability uses actual time; explicit service clock
    # below covers student boundary behavior independently of that check.
    try:
        rows = run_catalog(admin, 'come2201', items, validate=True, publish=True)
        yield paths, state, admin, items, rows
    finally:
        admin.stop()
        patch.undo()


def test_all_workshops_validate_schedule_privacy_and_idempotence(workshops):
    paths, state, admin, items, rows = workshops
    assert all(row['status'] == 'published' for row in rows)
    for index, (item, row) in enumerate(zip(items, rows)):
        assert datetime.fromisoformat(row['due_at'].replace('Z', '+00:00')) == datetime.fromisoformat(DATES[index] + 'T04:50:00+00:00')
        assert sum(case['weight'] for case in item['document']['tests']) == 100
        assert len(item['document']['tests']) >= 5
        draft = admin.get_draft('come2201', row['draft_id'])
        assert [case['score'] for case in draft['latest_check']['details']['cases']] == [100, 0]
        assert register_workshop(admin, 'come2201', item)['draft_id'] == row['draft_id']
        release = state.get_bundle_assignment(row['assignment_id'])
        target = paths.root / ('download-' + item['key'])
        BundleStore(paths.bundles).materialize_directory(release.starter_digest, target)
        assert (target / 'main.cpp').is_file() and (target / 'CMakeLists.txt').is_file()
        assert (target / 'README.md').read_text().rstrip() == item['document']['description']
        assert not any(file.name in {'solution.cpp', 'negative.cpp', 'tests.json', 'grade.py'} for file in target.rglob('*'))
    assert len(admin.list_drafts('come2201')) == 10
    (paths.root.parent / 'validation.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))


def test_import_preserves_instructor_edits(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    admin = AssignmentAdminService(PlatformStateStore(paths.database), paths)
    item = load_workshops(CATALOG)[0]
    draft = register_workshop(admin, 'come2201', item)
    admin.update_draft('come2201', draft['draft_id'], draft['revision'], title='교수자 수정 제목')
    with pytest.raises(PlatformConflict):
        register_workshop(admin, 'come2201', item)
    assert admin.get_draft('come2201', draft['draft_id'])['title'] == '교수자 수정 제목'


def test_import_resumes_partial_materials_without_new_revision_on_replay(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    admin = AssignmentAdminService(PlatformStateStore(paths.database), paths)
    item = synthetic_workshop()
    draft = admin.create_draft('come2201', creation_key='come2201-2026f-problem99', **item['document'])
    admin.upload_zip('come2201', draft['draft_id'], draft['revision'], 'starter', item['starter'])
    resumed = register_workshop(admin, 'come2201', item)
    assert {upload['role'] for upload in resumed['uploads']} == {'starter', 'solution', 'negative'}
    replay = register_workshop(admin, 'come2201', item)
    assert replay['draft_id'] == draft['draft_id'] and replay['revision'] == resumed['revision']
    assert len(admin.list_drafts('come2201')) == 1


def test_import_preserves_changed_code_and_deleted_draft(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    admin = AssignmentAdminService(PlatformStateStore(paths.database), paths)
    item = synthetic_workshop()
    draft = register_workshop(admin, 'come2201', item)
    edited = admin.upload_zip('come2201', draft['draft_id'], draft['revision'], 'solution',
        _archive_files({'main.cpp': b'int main(){return 1;}\n'}))
    with pytest.raises(PlatformConflict, match='파일이 변경'):
        register_workshop(admin, 'come2201', item)
    assert admin.get_draft('come2201', draft['draft_id'])['uploads'] == edited['uploads']
    admin.delete_draft('come2201', draft['draft_id'], edited['revision'])
    with pytest.raises(PlatformNotFound):
        register_workshop(admin, 'come2201', item)
    assert admin.list_drafts('come2201') == []


def test_republish_reports_extended_deadline_and_preserves_hidden_release(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    admin = AssignmentAdminService(PlatformStateStore(paths.database), paths)
    item = synthetic_workshop()
    item['document']['due_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    item['document'] = _document(item['document'])
    result = run_catalog(admin, 'come2201', [item], validate=True, publish=True)[0]
    extended = datetime.now(timezone.utc) + timedelta(days=2)
    admin.extend_deadline('come2201', result['assignment_id'], extended, 'synthetic extension')
    admin.hide_release('come2201', result['assignment_id'])
    replay = run_catalog(admin, 'come2201', [item], validate=True, publish=True)[0]
    assert replay['assignment_id'] == result['assignment_id']
    assert datetime.fromisoformat(replay['due_at'].replace('Z', '+00:00')) == extended
    assert replay['status'] == 'hidden'
    assert not admin.state.get_bundle_assignment(result['assignment_id']).ready
    assert len(admin.list_drafts('come2201')) == 1


def test_catalog_validation_failure_publishes_no_new_exercises(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    admin = AssignmentAdminService(PlatformStateStore(paths.database), paths)
    correct, wrong = synthetic_workshop(), synthetic_workshop()
    wrong['key'] = 'problem98'
    wrong['document']['tests'][0]['output'] = 'wrong expectation\n'
    wrong['grading'] = _archive_files({'solution/main.cpp': b'#include <iostream>\nint main(){std::cout << "ok\\n";}\n',
        'negative/main.cpp': b'int main(){}\n',
        'tests.json': json.dumps({key: wrong['document'][key] for key in ('tests', 'negative_score')}).encode()})
    with pytest.raises(RuntimeError, match='검증 실패'):
        run_catalog(admin, 'come2201', [correct, wrong], validate=True, publish=True)
    drafts = admin.list_drafts('come2201')
    assert len(drafts) == 2 and all(not draft['published_assignment_id'] for draft in drafts)


def test_existing_v12_database_gets_deleted_column(tmp_path):
    state = PlatformStateStore(tmp_path / 'upgrade.sqlite3')
    admin = AssignmentAdminService(state, AppPaths.from_value(tmp_path / 'artifacts').ensure())
    draft = admin.create_draft('come2201', title='Preserved')
    with state._write() as db:
        db.execute('ALTER TABLE instructor_assignment_drafts DROP COLUMN deleted_at')
        db.execute('DELETE FROM platform_schema_migrations WHERE version=13')
    upgraded = PlatformStateStore(state.database)
    assert upgraded.schema_version() == 13
    with upgraded._connection() as db:
        assert db.execute('SELECT deleted_at FROM instructor_assignment_drafts WHERE draft_id=?', (draft['draft_id'],)).fetchone()[0] is None
    assert AssignmentAdminService(upgraded, admin.paths).get_draft('come2201', draft['draft_id'])['title'] == 'Preserved'


@pytest.mark.parametrize('index', range(10))
def test_student_claim_download_submit_and_current_score(workshops, tmp_path, index):
    paths, state, admin, items, rows = workshops
    item, row = items[index], rows[index]
    store = BundleStore(paths.bundles)
    service = StudentPlatformService(state=state, server_secret=SECRET, course_key='come2201',
        public_base_url=API, bundle_store=store, instructor_token='synthetic-instructor-token-123456',
        now=lambda: datetime(2026, 9, 22, 4, tzinfo=timezone.utc))
    student = f'QA-{index:02d}'
    EnrollmentAdminService(state, SECRET).add_student('come2201', student_key=student, password=f'{index+1:06d}')
    portal = CoursePortal({'come2201': service}, SECRET, WEB, API)
    api = CourseAPI({'come2201': service}, SECRET)
    with ExitStack() as stack:
        web = stack.enter_context(serving(portal, WEB))
        api_server = stack.enter_context(serving(api, API))
        status, headers, html = login(web, 'come2201', f'{index+1:06d}', student)
        assert status == 200
        assert all(escape(other['title']).encode() in html for other in rows)
        status, _, html = request(web, '/courses/come2201/claims', method='POST', cookie=cookie(headers),
            origin=WEB, data={'csrf': csrf(html), 'assignment_id': row['assignment_id']})
        assert status == 200, html
        code = re.search(rb'<code>(AK1-[A-Z0-9-]+)</code>', html)[1].decode()
        _, tokens = connect(api_server, code)
        token = tokens['access_token']
        accepted = json.loads(request(api_server, '/v1/accepted-assignments', token=token)[2])
        assert [entry['assignment_id'] for entry in accepted['assignments']] == [row['assignment_id']]
        status, _, bundle = request(api_server, f'/v1/assignments/{row["assignment_id"]}/starter', token=token)
        assert status == 200
        with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
            assert not any('solution' in name or 'tests.json' in name for name in archive.getnames())
        grader = PilotLocalGrader()
        stack.callback(grader.close)
        processor = BundleSubmissionProcessor(state=state, course_key='come2201',
            workspace_builder=WorkspaceBuilder(paths.workspaces), grader=grader)
        for attempt, expected in [('solution', 100), ('negative', 0)]:
            source = tmp_path / attempt
            source.mkdir()
            shutil.copyfile(item['folder'] / 'instructor' / f'{attempt}.cpp', source / 'main.cpp')
            artifact = store.create_from_directory(source, kind='submission')
            # Both initial and repeat submissions exercise the actual HTTP contract.
            status, result = submit_http(api_server, token, row['assignment_id'], attempt + '-' + item['key'], artifact)
            assert status == 202, result
            sid = json.loads(result)['submission']['submission_id']
            if attempt == 'negative':
                status, _, result = request(api_server, f'/v1/submissions/{sid}', token=token)
                assert status == 200, result
                pending = json.loads(result)['submission']
                assert 'score' not in pending and pending['previous_best']['score'] == 100
            processor.process(sid)
            status, _, result = request(api_server, f'/v1/submissions/{sid}/result', token=token)
            assert status == 200, result
            result = json.loads(result)['result']
            assert result['score'] == expected and result['max_score'] == 100
            if attempt == 'negative':
                assert result['previous_best']['score'] == 100
        (paths.root.parent / f'{item["key"]}-student.html').write_bytes(html)
