"""Student-material view boundaries independent of persistence/HTTP integration."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from autograde import instructor_student_materials as instructor
from autograde import student_material_portal as student
from autograde.instructor_web import InstructorWeb
from autograde.platform_portal import CoursePortal
from autograde.platform_service import PlatformAPIError, PlatformFileResponse, PlatformResponse
from autograde.platform_state import PlatformNotFound


ID = 'mat_' + 'a' * 32
BASE = '/courses/come2201/materials'
WEB = 'https://grade.example.edu:20010'
COURSE = {'course_key': 'come2201', 'status': 'active'}
MATERIAL = dict(material_id=ID, title='Individual <script>paper</script>', instructions='Read <b>carefully</b>',
                opens_at='2026-10-17T01:00:00Z', closes_at='2026-10-17T03:00:00Z', status='draft',
                revision=3, file_count=1)
RECIPIENT = dict(student_id=7, student_key='0001', name='<script>Alice</script>', filename='0001.pdf',
                 format='pdf', size_bytes=100, sha256='a' * 64, active=True, request_count=0,
                 first_requested_at=None, last_requested_at=None)


class MaterialService:
    def __init__(self):
        self.calls = []
        self.material = deepcopy(MATERIAL)
        self.recipient = deepcopy(RECIPIENT)

    def get(self, *args):
        return self.material

    def list_for_instructor(self, *args):
        return [self.material]

    def recipients(self, *args):
        return [self.recipient, dict(student_id=8, student_key='0002', name='Bob', active=True)]

    def list_for_student(self, course, student_id):
        self.calls.append(('list_for_student', course, student_id))
        return [{**self.material, **self.recipient, 'availability': 'open'}]

    def file_metadata(self, course, material, student_id):
        assert student_id == 7
        return self.recipient

    def file_for_instructor(self, course, material, student_id):
        assert student_id == 7
        return Path('/tmp/synthetic-material.pdf')

    def file_snapshot_for_instructor(self, course, material, student_id, file_revision=None):
        assert file_revision is None
        return self.file_for_instructor(course, material, student_id), self.file_metadata(course, material, student_id)

    def download(self, course, material, student_id):
        self.calls.append(('download', course, material, student_id))
        assert student_id == 7
        return Path('/tmp/synthetic-material.pdf')

    def download_snapshot(self, course, material, student_id):
        return self.download(course, material, student_id), self.file_metadata(course, material, student_id)

    def upload(self, *args, **kwargs):
        self.calls.append(('upload', args, kwargs))

    def upload_batch(self, *args, **kwargs):
        self.calls.append(('upload_batch', args, kwargs))

    def publish(self, *args, **kwargs):
        self.calls.append(('publish', args, kwargs))

    def remove_recipient(self, *args, **kwargs):
        self.calls.append(('remove_recipient', args, kwargs))


@pytest.fixture
def ui():
    return SimpleNamespace(student_materials=MaterialService(), web_url=WEB, principal=None,
                           _base=InstructorWeb._base, _form=InstructorWeb._form,
                           _redirect=InstructorWeb._redirect)


@pytest.fixture
def portal():
    service = MaterialService()
    session = SimpleNamespace(credential=SimpleNamespace(student_id=7, student_key='0001'),
                              csrf='csrf-token', member_session=False)
    obj = SimpleNamespace(student_materials=service, web_url=WEB, services={'come2201': object()},
                          courses=None, lock=nullcontext(), exam_papers=object(), sessions={'session': session},
                          members=None, secret=b'synthetic-cookie-secret',
                          _page=CoursePortal._page, _course_label=lambda key: 'COME2201',
                          _session=lambda course, cookies: ('session', session))
    obj._cookie = lambda course, raw, **kwargs: CoursePortal._cookie(obj, course, raw, **kwargs)
    obj._login_headers = lambda course, raw: CoursePortal._login_headers(obj, course, raw)
    obj._revoke_browser_sessions = lambda cookies: CoursePortal._revoke_browser_sessions(obj, cookies)
    obj._logout_session = lambda *args, **kwargs: CoursePortal._logout_session(obj, *args, **kwargs)
    obj._entry = lambda course, message='', status=200, **kwargs: PlatformResponse(status, {'entry': kwargs, 'message': message}, {})
    obj._login_session = lambda course, form, cookies, **kwargs: ('session-token', session)
    obj._student_nav = lambda course, current: CoursePortal._student_nav(obj, course, current)
    return obj


def test_instructor_detail_guides_mapping_batch_partial_delivery_and_escapes(ui):
    _, body = instructor.detail(ui, COURSE, MATERIAL, {'csrf': 'csrf-token'})
    assert '학번.pdf' in body and '학번.zip' in body
    assert '25 MiB' in body and '100 MiB' in body and '최대 200명' in body
    assert '최상위' in body and '앞자리 0' in body
    assert '파일 배정 1명 · 활성 수강 중 미배정 1명' in body
    assert '미배정 · 학생에게 표시 안 됨' in body
    assert '자료 배포는 성적·과제 채점과 무관합니다.' in body
    assert '공개 후에도 안내·기간과 학생별 파일을 직접 수정할 수 있습니다.' in body
    assert '다운로드 완료·열람을 증명하지 않습니다.' in body
    assert '&lt;script&gt;Alice&lt;/script&gt;' in body
    assert '<script>Alice</script>' not in body
    assert f'/instructor/materials/{ID}/download/7' in body
    assert body.count('id="material-file"') == body.count('id="material-batch"') == 1
    assert '학생용 ZIP 내부 파일을 서버에 풀거나 실행하지 않습니다.' in body
    assert f'action="/courses/come2201/instructor/materials/{ID}/remove/7"' in body
    assert f'/remove/8' not in body
    assert '0001 학생의 파일 배정을 제외합니다.' in body


def test_instructor_listing_links_student_destination_and_draft(ui):
    _, body = instructor.listing(ui, COURSE, {'csrf': 'csrf-token'})
    assert f'{WEB}{BASE}' in body
    assert '&lt;script&gt;paper&lt;/script&gt;' in body
    assert '초안 저장 · 학생별 파일 등록으로' in body


def test_published_detail_allows_direct_file_and_metadata_edit_forms(ui):
    material = dict(MATERIAL, status='published')
    _, body = instructor.detail(ui, COURSE, material, {'csrf': 'csrf-token'})
    assert 'type="file"' in body
    assert 'name="title"' in body
    assert '학생별 자료 공개' not in body
    assert '/archive' in body
    assert '/remove/' in body
    assert '안내·기간 수정 즉시 반영' in body
    assert '개별 파일 교체·즉시 반영' in body


@pytest.mark.parametrize('action', ['upload', 'upload-batch'])
def test_upload_delegates_bytes_revision_and_original_filename(ui, action):
    form = {'revision': '3', 'file': b'private-file', 'file_name': '0001.pdf'}
    response = instructor.request(ui, 'POST', COURSE, f'materials/{ID}/{action}', form, {'csrf': 'x'})
    assert response.status == 303
    call = ui.student_materials.calls[0]
    assert call[0] == action.replace('-', '_')
    assert call[1] == ('come2201', ID)
    assert call[2]['revision'] == 3 and call[2]['data'] == b'private-file'
    if action == 'upload':
        assert call[2]['filename'] == '0001.pdf'
    else:
        assert 'filename' not in call[2]


@pytest.mark.parametrize('form', [{}, {'file': b''}, {'file': 'path'}, {'file': b'private'}])
def test_upload_requires_bytes_and_original_filename(ui, form):
    with pytest.raises(ValueError):
        instructor.request(ui, 'POST', COURSE, f'materials/{ID}/upload', {'revision': '3', **form}, {'csrf': 'x'})
    assert not ui.student_materials.calls


@pytest.mark.parametrize('action', ['upload', 'upload-batch', 'publish', 'archive', 'download', 'remove', 'remove/7'])
def test_get_mutations_and_incomplete_download_do_not_run(ui, action):
    with pytest.raises(PlatformNotFound):
        instructor.request(ui, 'GET', COURSE, f'materials/{ID}/{action}', {}, {'csrf': 'x'})
    assert not ui.student_materials.calls


def test_publish_requires_confirmation(ui):
    with pytest.raises(ValueError):
        instructor.request(ui, 'POST', COURSE, f'materials/{ID}/publish', {'revision': '3'}, {'csrf': 'x'})
    response = instructor.request(ui, 'POST', COURSE, f'materials/{ID}/publish', {'revision': '3', 'confirm': 'yes'}, {'csrf': 'x'})
    assert response.status == 303
    assert ui.student_materials.calls[0][0] == 'publish'


def test_remove_requires_confirmation_and_revision_then_passes_numeric_recipient(ui):
    route = f'materials/{ID}/remove/7'
    for form in ({'revision': '3'}, {'revision': 'not-a-number', 'confirm': 'yes'}):
        with pytest.raises(ValueError):
            instructor.request(ui, 'POST', COURSE, route, form, {'csrf': 'x'})
    assert ui.student_materials.calls == []
    response = instructor.request(ui, 'POST', COURSE, route, {'revision': '3', 'confirm': 'yes'}, {'csrf': 'x'})
    assert response.status == 303
    assert ui.student_materials.calls == [('remove_recipient', ('come2201', ID, 7), {'revision': 3, 'actor': 'shared-instructor'})]


def test_archived_course_never_shows_remove_controls(ui):
    _, body = instructor.detail(ui, dict(COURSE, status='archived'), MATERIAL, {'csrf': 'csrf-token'})
    assert '/remove/' not in body


@pytest.mark.parametrize('suffix', ['remove', 'remove/../7', 'remove/not-a-number', 'publish/7'])
def test_remove_requires_valid_target_route(ui, suffix):
    with pytest.raises(PlatformNotFound):
        instructor.request(ui, 'POST', COURSE, f'materials/{ID}/{suffix}', {'revision': '3', 'confirm': 'yes'}, {'csrf': 'x'})
    assert ui.student_materials.calls == []


def test_instructor_download_uses_numeric_recipient_and_safe_attachment_filename(ui):
    ui.student_materials.recipient.update(filename='학번0001.zip', format='zip')
    response = instructor.request(ui, 'GET', COURSE, f'materials/{ID}/download/7', {}, {'csrf': 'x'})
    assert isinstance(response, PlatformFileResponse)
    assert response.content_type == 'application/zip'
    assert response.headers['Content-Disposition'].startswith('attachment; filename="material.zip"; filename*=UTF-8\'\'%')


def test_student_listing_uses_authenticated_student_and_escape(portal):
    response = student.request(portal, 'GET', BASE, {}, {}, None)
    assert response.status == 200
    assert portal.student_materials.calls == [('list_for_student', 'come2201', 7)]
    assert '0001.pdf' in response.body
    assert '0002' not in response.body
    assert '&lt;script&gt;paper&lt;/script&gt;' in response.body and '<script>paper</script>' not in response.body
    assert f'{BASE}/{ID}/download' in response.body
    assert 'name="student_id"' not in response.body and 'name="student_key"' not in response.body
    assert '/exam-papers' in response.body and '공용 PC' in response.body
    assert f'href="{BASE}" aria-current="page"' in response.body
    assert 'href="/courses/come2201/portal"' in response.body


def test_student_download_receives_own_file_and_session_identity(portal):
    response = student.request(portal, 'POST', f'{BASE}/{ID}/download', {'csrf': 'csrf-token'}, {}, WEB)
    assert isinstance(response, PlatformFileResponse)
    assert response.content_type == 'application/pdf'
    assert portal.student_materials.calls == [('download', 'come2201', ID, 7)]
    assert 'filename*=UTF-8\'\'0001.pdf' in response.headers['Content-Disposition']


@pytest.mark.parametrize('form', [{'csrf': 'wrong'}, {'csrf': 'csrf-token', 'student_id': '8'}, {'csrf': ['csrf-token']}])
def test_student_download_rejects_csrf_or_student_selector(portal, form):
    response = student.request(portal, 'POST', f'{BASE}/{ID}/download', form, {}, WEB)
    assert response.status == 403
    assert not portal.student_materials.calls


@pytest.mark.parametrize('origin', [None, 'null', 'https://grade.example.edu', 'https://attacker.example'])
def test_student_download_rejects_wrong_origin(portal, origin):
    response = student.request(portal, 'POST', f'{BASE}/{ID}/download', {'csrf': 'csrf-token'}, {}, origin)
    assert response.status == 403
    assert not portal.student_materials.calls


def test_student_get_download_denied_and_post_login_redirects(portal):
    response = student.request(portal, 'GET', f'{BASE}/{ID}/download', {}, {}, None)
    assert response.status == 405
    response = student.request(portal, 'POST', BASE + '/login', {}, {}, WEB)
    assert response.status == 303 and response.headers['Location'] == BASE
    assert response.headers['Set-Cookie'] == (
        'autograde_portal_come2201=; Path=/courses/come2201; Max-Age=0; HttpOnly; SameSite=Lax; Secure',
        'autograde_portal_come2201=session-token; Path=/; Max-Age=600; HttpOnly; SameSite=Lax; Secure',
    )


def test_expired_session_returns_material_login_and_logout_invalidates(portal):
    response = student.request(portal, 'POST', BASE + '/logout', {'csrf': 'csrf-token'}, {}, WEB)
    assert response.status == 200 and response.body['entry']['destination'] == 'materials'
    assert portal.sessions == {}
    def expired(*args):
        raise PlatformAPIError(403, 'expired', 'expired')
    portal._session = expired
    response = student.request(portal, 'POST', f'{BASE}/{ID}/download', {'csrf': 'csrf-token'}, {}, WEB)
    assert response.status == 403 and response.body['entry']['destination'] == 'materials'
    assert not portal.student_materials.calls
