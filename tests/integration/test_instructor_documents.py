"""Document editor HTML/authentication contracts without grading or live student data."""
import base64
from copy import deepcopy
import hashlib
import re
from types import SimpleNamespace

import pytest

from autograde.instructor_identity import InstructorPrincipal
from autograde.instructor_web import InstructorWeb
from autograde.platform_service import PlatformAPIError, StudentPlatformService
from autograde.platform_state import PlatformConflict, PlatformNotFound
from test_instructor_web import setup


WEB = 'https://grade.example.edu:20010'
AUTH = 'Basic ' + base64.b64encode(b'instructor:synthetic-instructor-token-1234567890').decode()
BASE = '/courses/come2201/instructor'
PATH = BASE + '/assignments/basn_one/document'


class Courses:
    def __init__(self):
        self.entries = {key: dict(course_key=key, code=key, name='Synthetic course', year=2026,
                                 semester='2', section='01', status='active')
                        for key in ('come2201', 'come3105')}

    def get_course(self, key):
        if key not in self.entries:
            raise PlatformNotFound()
        return dict(self.entries[key])

    def list_courses(self):
        return list(self.entries.values())


class AssignmentDocuments:
    """The service seam is replaced; persistent snapshot invariants are service tests."""
    state = None

    def __init__(self):
        self.reads, self.writes = [], []
        self.document = dict(assignment_id='basn_one', revision=0, content='# Original\nKeep the contract.\n',
                             sha256='0' * 64, updated_at='2026-09-22T00:00:00Z', change_note='', history=[])

    def get_assignment_document(self, course, assignment_id):
        self.reads.append((course, assignment_id))
        if course != 'come2201' or assignment_id not in {'basn_one', 'basn_cli'}:
            raise PlatformNotFound()
        return None if assignment_id == 'basn_cli' else deepcopy(self.document)

    def update_assignment_document(self, course, assignment_id, revision, content, change_note):
        if course != 'come2201' or assignment_id != 'basn_one':
            raise PlatformNotFound()
        if revision != self.document['revision']:
            raise PlatformConflict('다른 창에서 설명이 변경되었습니다. 최신 버전을 확인하세요.')
        self.writes.append((course, assignment_id, revision, content, change_note))
        entry = dict(revision=revision + 1, sha256=hashlib.sha256(content.encode()).hexdigest(),
                     updated_at='2026-09-22T01:00:00Z', change_note=change_note)
        self.document = dict(entry, assignment_id=assignment_id, content=content,
                             history=[entry, *self.document['history']])
        return deepcopy(self.document)

    def get_draft(self, course, draft_id):
        if course != 'come2201' or draft_id != 'draft_one':
            raise PlatformNotFound()
        return dict(draft_id=draft_id, revision=1, title='Synthetic lab', language='cpp', platform='windows',
                    mode='template', published_assignment_id='basn_one', description='# Original',
                    updated_at='2026-09-22T00:00:00Z', due_at=None, opens_at=None, result_policy='immediate',
                    max_score=100, negative_score=0, uploads=[], visibility='open', accepted_count=5,
                    submission_count=7, latest_check={})


class Catalog:
    def get_release(self, course, assignment_id):
        if course != 'come2201' or assignment_id not in {'basn_one', 'basn_cli'}:
            raise PlatformNotFound()
        return dict(assignment_id=assignment_id, title='Synthetic lab', visibility='open',
                    origin='cli' if assignment_id == 'basn_cli' else 'web', due_at=None,
                    accepted_students=5, submitted_students=4, submission_count=7,
                    draft_id=None if assignment_id == 'basn_cli' else 'draft_one')


class Browser:
    def __init__(self, web):
        self.web, self.cookies, self.csrf = web, {}, ''

    def get(self, path=PATH):
        response = self.web.request('GET', path, {}, self.cookies, authorization=AUTH)
        if response.headers.get('Set-Cookie'):
            key, value = response.headers['Set-Cookie'].split(';', 1)[0].split('=', 1)
            self.cookies[key] = value
        if isinstance(response.body, str):
            match = re.search('name="csrf" value="([^"]+)"', response.body)
            if match:
                self.csrf = match[1]
        return response

    def post(self, path=PATH, **overrides):
        fields = dict(csrf=self.csrf, revision='0', content='# Clarified\nSame grading contract.\n',
                      change_note='예제 설명 보완', confirm='yes')
        fields.update(overrides)
        return self.web.request('POST', path, fields, self.cookies, WEB, AUTH)


@pytest.fixture
def editor():
    courses, assignments = Courses(), AssignmentDocuments()
    auth = object.__new__(StudentPlatformService)
    auth._instructor_token = 'synthetic-instructor-token-1234567890'
    students = SimpleNamespace(for_instructor=lambda principal: None)
    web = InstructorWeb(courses, students, assignments, authorize=auth._authorize_instructor,
                        secret=b'w' * 32, web_url=WEB)
    web.catalog = Catalog()
    browser = Browser(web)
    browser.get()
    return browser, courses, assignments


def test_editor_is_available_with_existing_acceptances_and_submissions(editor):
    browser, _, assignments = editor
    response = browser.get()
    assert response.status == 200
    assert '설명 수정·이력' in response.body
    assert '같은 과제의 수락·제출·점수·채점 기준은 유지됩니다' in response.body
    assert 'name="revision" value="0"' in response.body
    assert 'name="content" rows="24" maxlength="20000" required' in response.body
    assert 'maxlength="500"' in response.body
    assert 'data-dirty-guard' in response.body
    assert '학생에게 공개되는 안내문' in response.body
    assert '정답 코드·비공개 테스트·개인정보·비밀번호를 넣지 마세요' in response.body
    assert '자동으로 덮어쓰지 않습니다' in response.body
    assert assignments.writes == []


def test_release_and_published_draft_link_to_same_assignment_document(editor):
    browser, _, _ = editor
    for path in (BASE + '/assignments/basn_one', BASE + '/drafts/draft_one'):
        page = browser.get(path)
        assert page.status == 200
        assert f'href="{PATH}">설명 수정·이력</a>' in page.body
    assert '최초 공개 시 저장한 설명' in page.body


def test_save_keeps_assignment_identity_and_displays_escaped_diff_and_history(editor):
    browser, _, assignments = editor
    content = '# Revised\n</textarea><script>alert(1)</script>\n'
    note = '<img src=x onerror=alert(1)>'
    response = browser.post(content=content, change_note=note)
    assert response.status == 200
    assert assignments.writes == [('come2201', 'basn_one', 0, content, note)]
    assert '설명 버전 1 저장 완료' in response.body
    assert 'name="revision" value="1"' in response.body
    assert '방금 저장한 변경 전후 비교' in response.body
    assert '--- 저장 전 · 버전 0' in response.body and '+++ 저장 후 · 버전 1' in response.body
    assert '-# Original' in response.body and '+# Revised' in response.body
    assert '&lt;/textarea&gt;&lt;script&gt;alert(1)&lt;/script&gt;' in response.body
    assert '<script>alert(1)</script>' not in response.body
    assert '&lt;img src=x onerror=alert(1)&gt;' in response.body and '<img src=x' not in response.body
    assert assignments.document['sha256'] in response.body
    assert '설명 변경 이력' in response.body
    assert '과거 본문 복원이나 삭제를 제공하지 않습니다' in response.body
    assert 'action="' + PATH + '"' in response.body


@pytest.mark.parametrize('fields', [dict(confirm=''), dict(confirm='no'), dict(content=''),
    dict(content=' '), dict(content='가' * 20001), dict(change_note=''), dict(change_note=' '),
    dict(change_note='가' * 501), dict(revision='-1'), dict(revision='NaN')])
def test_invalid_or_unconfirmed_changes_do_not_reach_writer_and_recover_form(editor, fields):
    browser, _, assignments = editor
    response = browser.post(**fields)
    assert response.status == 400
    assert assignments.writes == []
    assert 'data-recovered' in response.body
    assert 'name="revision" value="' + fields.get('revision', '0') + '"' in response.body


def test_stale_revision_preserves_unsaved_edit_without_upgrading_write_fence(editor):
    browser, _, assignments = editor
    assert browser.post(content='Saved elsewhere').status == 200
    response = browser.post(content='My unsaved text', change_note='My reason')
    assert response.status == 409
    assert len(assignments.writes) == 1
    assert assignments.document['content'] == 'Saved elsewhere'
    assert '현재 학생 안내문 · 버전 1' in response.body
    assert 'name="revision" value="0"' in response.body
    assert '>My unsaved text</textarea>' in response.body
    assert 'value="My reason"' in response.body
    assert '이전 버전 번호를 유지했습니다' in response.body


def test_bounded_history_explains_that_older_records_are_still_preserved(editor):
    browser, _, assignments = editor
    assignments.document.update(revision=52, history_total=53,
        history=[dict(revision=n, sha256='a' * 64, updated_at='2026-09-22T01:00:00Z', change_note='보완')
                 for n in range(52, 2, -1)])
    page = browser.get()
    assert page.status == 200
    assert '전체 53건 중 최근 50건을 표시합니다' in page.body
    assert '이전 기록도 서버에 보존' in page.body
    assert assignments.writes == []


@pytest.mark.parametrize('authorization', [None, '', 'Bearer student-token', 'Basic invalid'])
def test_authentication_precedes_document_read_and_write(editor, authorization):
    browser, _, assignments = editor
    count = len(assignments.reads)
    for method in ('GET', 'POST'):
        with pytest.raises(PlatformAPIError) as error:
            browser.web.request(method, PATH, {}, browser.cookies, WEB, authorization)
        assert error.value.status == 401
    assert len(assignments.reads) == count and assignments.writes == []


@pytest.mark.parametrize('origin', [None, 'null', 'https://attacker.example', 'https://grade.example.edu'])
def test_exact_origin_is_required_before_any_document_read(editor, origin):
    browser, _, assignments = editor
    count = len(assignments.reads)
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('POST', PATH, dict(csrf=browser.csrf), browser.cookies, origin, AUTH)
    assert error.value.status == 403
    assert len(assignments.reads) == count and assignments.writes == []


@pytest.mark.parametrize('kind', ['missing-cookie', 'wrong-csrf', 'student-cookie'])
def test_csrf_and_instructor_cookie_are_required_before_document_read(editor, kind):
    browser, _, assignments = editor
    count = len(assignments.reads)
    cookies = browser.cookies if kind == 'wrong-csrf' else {} if kind == 'missing-cookie' else {'autograde_portal_come2201': 'student'}
    token = 'wrong' if kind == 'wrong-csrf' else browser.csrf
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('POST', PATH, dict(csrf=token), cookies, WEB, AUTH)
    assert error.value.status == 403
    assert len(assignments.reads) == count and assignments.writes == []


def test_course_and_assignment_scopes_are_not_interchangeable(editor):
    browser, _, assignments = editor
    for path in ('/courses/come3105/instructor/assignments/basn_one/document',
                 BASE + '/assignments/basn_missing/document'):
        assert browser.get(path).status == 404
        assert browser.post(path).status == 404
    assert assignments.writes == []


def test_personal_instructor_cannot_read_or_edit_unassigned_course(editor):
    browser, _, assignments = editor
    principal = InstructorPrincipal('teacher', 'teacher', 'Teacher', 'instructor', 1, frozenset({'come2201'}))
    browser.web.identities = SimpleNamespace(authenticate=lambda authorization: principal)
    browser.get()  # Rebind the signed cookie to the individual instructor identity.
    count = len(assignments.reads)
    foreign = '/courses/come3105/instructor/assignments/basn_one/document'
    assert browser.get(foreign).status == 404
    assert browser.post(foreign).status == 404
    assert len(assignments.reads) == count and assignments.writes == []


def test_cli_document_none_does_not_offer_or_create_blank_baseline(editor):
    browser, _, assignments = editor
    path = BASE + '/assignments/basn_cli/document'
    page = browser.get(path)
    assert page.status == 200 and '별도 설명 편집 미지원' in page.body
    assert 'name="content"' not in page.body and '설명 새 버전 저장' not in page.body
    assert browser.post(path).status == 400
    assert assignments.writes == []


def test_archived_course_is_read_only_for_documents(editor):
    browser, courses, assignments = editor
    courses.entries['come2201']['status'] = 'archived'
    page = browser.get()
    assert page.status == 200 and '설명과 이력만 조회' in page.body
    assert 'name="content"' not in page.body
    assert browser.post().status == 409
    assert assignments.writes == []


def test_real_published_service_document_save_preserves_release_and_original_draft(setup):
    browser, state, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='template', language='c', description='Original student guidance')
    job = assignments.queue_check('come2201', draft['draft_id'], draft['revision'], trusted_code_confirmed=True)
    assert assignments.run_one()
    assert assignments.get_check('come2201', job['job_id'])['status'] == 'succeeded'
    release = assignments.publish('come2201', draft['draft_id'], draft['revision'])
    before = assignments.get_draft('come2201', draft['draft_id'])
    path = BASE + '/assignments/' + release.assignment_id + '/document'
    page = browser.get(path)
    assert page.status == 200 and 'Original student guidance' in page.body
    response = browser.post(path, revision='0', content='Original student guidance\n\nA clearer example.',
                            change_note='풀이 안내 보완', confirm='yes')
    assert response.status == 200 and '설명 버전 1 저장 완료' in response.body
    assert state.get_bundle_assignment(release.assignment_id) == release
    current_draft = assignments.get_draft('come2201', draft['draft_id'])
    for field in ('revision', 'description', 'tests', 'published_assignment_id'):
        assert current_draft[field] == before[field]
    document = assignments.get_assignment_document('come2201', release.assignment_id)
    assert document['revision'] == 1
    assert document['content'].endswith('A clearer example.')
    stale = browser.post(path, revision='0', content='Do not overwrite', change_note='stale', confirm='yes')
    assert stale.status == 409
    assert assignments.get_assignment_document('come2201', release.assignment_id)['content'] == document['content']
