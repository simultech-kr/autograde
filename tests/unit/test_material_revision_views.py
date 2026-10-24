"""Versioned personal-material UI preserves old editions and points to new ones."""
from html.parser import HTMLParser
from threading import RLock
from types import SimpleNamespace

import pytest

from autograde import instructor_student_materials as instructor
from autograde import student_material_portal as student
from autograde.instructor_web import InstructorWeb
from autograde.platform_service import PlatformResponse
from autograde.platform_state import PlatformConflict, PlatformNotFound


COURSE = {'course_key': 'come2201', 'status': 'active'}
BASE = '/courses/come2201/instructor/materials'
STUDENT_BASE = '/courses/come2201/materials'
FIRST = 'mat_' + 'a' * 32
SECOND = 'mat_' + 'b' * 32
SESSION = {'csrf': 'synthetic-csrf'}


class Elements(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def all(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]


def material(**changes):
    result = dict(material_id=FIRST, title='개별 자료 <제목>', instructions='사용 안내',
                  status='published', edition=1, revision=7, opens_at='2020-01-01T00:00:00Z',
                  closes_at='2099-01-01T00:00:00Z', published_at='2026-10-21T00:00:00Z',
                  change_note='', previous_material_id=None, revision_draft_id=None, superseded_by=None,
                  file_count=1, availability='open', filename='001.pdf', format='pdf', size_bytes=42,
                  request_count=0)
    result.update(changes)
    return result


def ui_for(row, *, history=None):
    recipients = [dict(student_id=1, student_key='001', name='학생', active=True,
                       filename='001.pdf', format='pdf', size_bytes=42, request_count=0)]
    service = SimpleNamespace(get=lambda *args: row, recipients=lambda *args: recipients,
                              list_for_instructor=lambda *args: [row])
    if history is not None:
        service.history = lambda *args: history
    ui = SimpleNamespace(student_materials=service, principal=None, web_url='https://grade.example.edu:20010',
                         _base=InstructorWeb._base, _form=InstructorWeb._form, _redirect=InstructorWeb._redirect)
    return ui


def test_current_published_material_has_confirmed_revision_form_and_distinct_versions():
    row = material()
    _, html = instructor.detail(ui_for(row), COURSE, row, SESSION)
    elements = Elements(html)
    assert any(form['action'] == BASE + '/' + FIRST + '/revise' and form['method'] == 'post'
               for form in elements.all('form'))
    assert any(field.get('name') == 'csrf' and field.get('value') == SESSION['csrf']
               for field in elements.all('input'))
    assert any(field.get('name') == 'revision' and field.get('value') == '7'
               for field in elements.all('input'))
    assert any(field.get('name') == 'change_note' and field.get('maxlength') == '1000' and 'required' in field
               for field in elements.all('textarea'))
    assert '학생에게 표시할 변경 내용' in html
    assert '수정본 만들기' in html and '배포본 v1' in html and '편집 번호 7' in html
    assert '수정본 공개 전까지 기존 파일은 원래 배포 기간에 계속 제공됩니다.' in html
    assert '공개 후에는 안내·파일·배포 기간·배정 대상을 변경할 수 없습니다.' not in html


def test_pending_revision_links_existing_draft_instead_of_creating_another():
    row = material(revision_draft_id=SECOND)
    _, html = instructor.detail(ui_for(row), COURSE, row, SESSION)
    assert f'href="{BASE}/{SECOND}">작성 중인 수정본 열기</a>' in html
    assert f'action="{BASE}/{FIRST}/revise"' not in html


def test_child_draft_reuses_upload_controls_and_explains_atomic_immediate_publication():
    row = material(material_id=SECOND, status='draft', edition=2, revision=1,
                   previous_material_id=FIRST, published_at=None, change_note='<script>정정</script>')
    _, html = instructor.detail(ui_for(row), COURSE, row, SESSION)
    assert '수정본 초안 · 배포 예정 v2' in html
    assert f'action="{BASE}/{SECOND}/upload"' in html
    assert f'action="{BASE}/{SECOND}/upload-batch"' in html
    assert f'href="{BASE}/{FIRST}">기존 배포본 확인</a>' in html
    assert '수정본 공개 · 기존 배포본 교체' in html
    assert '미래 시각으로 예약 전환할 수 없습니다.' in html
    assert '변경되지 않은 파일의 대상 학생에게도 새 버전 확인 안내가 표시됩니다.' in html
    assert '&lt;script&gt;정정&lt;/script&gt;' in html and '<script>정정</script>' not in html


def test_replaced_edition_keeps_version_history_links_without_mutation_controls():
    first = material(status='archived', superseded_by=SECOND)
    second = material(material_id=SECOND, edition=2, previous_material_id=FIRST, change_note='조건 정정')
    _, html = instructor.detail(ui_for(first, history=[first, second]), COURSE, first, SESSION)
    assert '이전 배포본 · 수정본으로 대체됨' in html
    assert f'href="{BASE}/{SECOND}">대체한 수정본 보기</a>' in html
    assert '배포본 v2 · 파일·요청 기록 보기' in html
    assert '이 배포본의 학생별 파일 요청 현황' in html
    assert f'action="{BASE}/{FIRST}/revise"' not in html
    assert f'action="{BASE}/{FIRST}/archive"' not in html


def test_archived_course_has_no_revision_form_even_for_current_published_material():
    row = material()
    _, html = instructor.detail(ui_for(row), {**COURSE, 'status': 'archived'}, row, SESSION)
    assert f'action="{BASE}/{FIRST}/revise"' not in html


def test_listing_displays_publication_edition_instead_of_internal_edit_number():
    row = material(edition=3, revision=47)
    _, html = instructor.listing(ui_for(row), COURSE, SESSION)
    assert '배포본 v3' in html and 'v47' not in html


def test_revision_route_dispatches_note_revision_actor_and_redirects_to_child():
    row = material()
    ui = ui_for(row)
    calls = []
    def revise(course, source, **fields):
        calls.append((course, source, fields))
        return material(material_id=SECOND, status='draft', edition=2)
    ui.student_materials.create_revision = revise
    ui.principal = SimpleNamespace(user_id='teacher-1')
    response = instructor.request(ui, 'POST', COURSE, f'materials/{FIRST}/revise',
                                  dict(revision='7', change_note='문제 조건 정정', confirm='yes'), SESSION)
    assert response.status == 303 and response.headers['Location'] == BASE + '/' + SECOND
    assert calls == [('come2201', FIRST, dict(revision=7, change_note='문제 조건 정정', actor='teacher-1'))]


@pytest.mark.parametrize('changes', [dict(confirm=''), dict(revision='bogus'), dict(change_note=['not text'])])
def test_revision_route_rejects_invalid_form_before_service_call(changes):
    row = material()
    ui = ui_for(row)
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid form must not create a revision')
    ui.student_materials.create_revision = forbidden
    form = dict(revision='7', change_note='수정 안내', confirm='yes')
    form.update(changes)
    with pytest.raises(ValueError):
        instructor.request(ui, 'POST', COURSE, f'materials/{FIRST}/revise', form, SESSION)


@pytest.mark.parametrize('method,route', [('GET', f'materials/{FIRST}/revise'),
                                         ('POST', f'materials/{FIRST}/revise/1')])
def test_revision_route_rejects_wrong_method_and_student_selector(method, route):
    with pytest.raises(PlatformNotFound):
        instructor.request(ui_for(material()), method, COURSE, route, {}, SESSION)


def portal_for(row):
    session = SimpleNamespace(csrf='student-csrf', credential=SimpleNamespace(student_id=1, student_key='001'))
    portal = SimpleNamespace(student_materials=SimpleNamespace(list_for_student=lambda *args: [row]),
                             _course_label=lambda course: 'COME2201',
                             _page=lambda title, body, status=200: PlatformResponse(status, body),
                             services={'come2201': object()}, courses=None, lock=RLock(),
                             web_url='https://grade.example.edu:20010', _session=lambda *args: ('session', session))
    return portal, session


def test_student_sees_current_revision_notice_and_safe_change_note():
    row = material(material_id=SECOND, edition=2, previous_material_id=FIRST, change_note='<b>정정</b>\n새 파일 확인')
    portal, session = portal_for(row)
    html = student._listing(portal, 'come2201', session).body
    assert '수정본 v2 · 다시 다운로드하세요' in html
    assert '&lt;b&gt;정정&lt;/b&gt;<br>새 파일 확인' in html
    assert f'action="{STUDENT_BASE}/{SECOND}/download"' in html
    assert FIRST not in html


@pytest.mark.parametrize('flags', [dict(request_count=1), dict(requested=True)])
def test_student_current_revision_request_record_suppresses_redownload_notice(flags):
    portal, session = portal_for(material(edition=2, **flags))
    html = student._listing(portal, 'come2201', session).body
    assert '수정본 v2 · 다시 다운로드하세요' not in html
    assert '실제 저장 여부는 브라우저 다운로드 목록에서 확인하세요.' in html


@pytest.mark.parametrize('availability', ['closed', 'scheduled'])
def test_student_unavailable_revision_has_no_download_or_redownload_prompt(availability):
    portal, session = portal_for(material(edition=2, availability=availability))
    html = student._listing(portal, 'come2201', session).body
    assert '다시 다운로드하세요' not in html
    assert f'action="{STUDENT_BASE}/{FIRST}/download"' not in html


def test_stale_student_download_conflict_links_latest_list_without_internal_detail():
    portal, session = portal_for(material())
    def fail(*args):
        raise PlatformConflict('private-storage-path secret')
    portal.student_materials.download_snapshot = fail
    response = student.request(portal, 'POST', STUDENT_BASE + '/' + FIRST + '/download',
                               {'csrf': session.csrf}, {}, portal.web_url)
    assert response.status == 409
    assert '최신 내 자료 목록을 열어 다시 다운로드하세요.' in response.body
    assert 'private-storage-path' not in response.body and 'secret' not in response.body
    assert f'href="{STUDENT_BASE}"' in response.body


def test_legacy_ui_fake_without_version_fields_or_history_still_renders():
    row = material()
    for name in ['edition', 'change_note', 'previous_material_id', 'revision_draft_id', 'superseded_by']:
        row.pop(name)
    _, html = instructor.detail(ui_for(row), COURSE, row, SESSION)
    assert '배포본 v1' in html
    portal, session = portal_for(row)
    assert '배포본 v1' in student._listing(portal, 'come2201', session).body
