"""Published, non-grading materials are directly editable with file audit history."""
from pathlib import Path

import pytest

from autograde import instructor_student_materials as instructor
from autograde import student_material_portal as student
from autograde.platform_state import PlatformNotFound
from test_material_revision_views import (
    BASE, COURSE, FIRST, SECOND, SESSION, STUDENT_BASE, Elements, material, portal_for, ui_for,
)


def test_published_material_edit_controls_are_primary_and_revision_flow_is_collapsed():
    row = material()
    _, html = instructor.detail(ui_for(row), COURSE, row, SESSION)
    elements = Elements(html)
    actions = {form['action'] for form in elements.all('form')}
    assert {BASE + '/' + FIRST + suffix for suffix in ['', '/upload', '/upload-batch', '/remove/1']} <= actions
    assert '안내·기간 수정 즉시 반영' in html
    assert '개별 파일 교체·즉시 반영' in html
    assert '자료 배포는 성적·과제 채점과 무관합니다.' in html
    assert '저장·파일 교체·배정 제외는 즉시 반영됩니다.' in html
    assert '이미 다운로드한 파일은 회수하거나 자동으로 바꿀 수 없습니다.' in html
    assert '<details><summary>고급: 검토 후 별도 수정본으로 배포</summary>' in html
    assert all('open' not in attrs for attrs in elements.all('details'))
    forms = elements.all('form')
    assert len([x for x in elements.all('input') if x.get('name') == 'csrf']) == len(forms)
    assert len([x for x in elements.all('input') if x.get('name') == 'revision']) == len(forms)


@pytest.mark.parametrize('changes', [dict(status='archived'), dict(status='archived', superseded_by=SECOND),
                                    dict(revision_draft_id=SECOND)])
def test_archived_superseded_and_pending_materials_hide_direct_mutations(changes):
    row = material(**changes)
    _, html = instructor.detail(ui_for(row), COURSE, row, SESSION)
    forms = Elements(html).all('form')
    assert all(form['action'] not in {BASE + '/' + FIRST + suffix
                                    for suffix in ['', '/upload', '/upload-batch', '/remove/1']} for form in forms)
    assert 'name="title"' not in html and 'type="file"' not in html
    if 'revision_draft_id' in changes:
        assert '수정본을 보관한 뒤 이 배포를 직접 수정하세요.' in html
        assert f'href="{BASE}/{SECOND}">작성 중인 수정본 열기</a>' in html


def test_archived_course_hides_all_mutation_controls():
    row = material()
    _, html = instructor.detail(ui_for(row), {**COURSE, 'status': 'archived'}, row, SESSION)
    assert Elements(html).all('form') == []


def test_file_change_history_shows_old_file_receipts_and_safe_download_link():
    row = material()
    ui = ui_for(row)
    ui.student_materials.file_history = lambda *args: [dict(
        student_id=1, student_key='001', name='<script>학생</script>', filename='001.zip', format='zip',
        size_bytes=40, file_revision=2, changed_at='2026-10-22T01:30:00Z', change_type='replaced',
        first_requested_at='2026-10-22T00:00:00Z', last_requested_at='2026-10-22T01:00:00Z', request_count=3,
    )]
    _, html = instructor.detail(ui, COURSE, row, SESSION)
    assert '파일 변경 이력' in html
    assert '파일 교체' in html and '파일 v2' in html
    assert f'href="{BASE}/{FIRST}/download/1/2"' in html
    assert '이전 파일 다운로드' in html
    assert '2026-10-22 10:30' in html
    assert '&lt;script&gt;학생&lt;/script&gt;' in html and '<script>학생</script>' not in html
    assert '이전 파일은 교수자만 검토할 수 있으며 학생은 현재 배정 파일만 받습니다.' in html


@pytest.mark.parametrize('revision', [None, 3])
def test_instructor_file_download_uses_atomic_path_metadata_snapshot(revision):
    row = material()
    ui = ui_for(row)
    calls = []
    def snapshot(course, mid, sid, *, file_revision=None):
        calls.append((course, mid, sid, file_revision))
        return Path('/tmp/immutable.zip'), dict(format='zip', filename='001.zip', sha256='f' * 64)
    ui.student_materials.file_snapshot_for_instructor = snapshot
    suffix = '' if revision is None else '/' + str(revision)
    response = instructor.request(ui, 'GET', COURSE, f'materials/{FIRST}/download/1{suffix}', {}, SESSION)
    assert response.status == 200 and response.path == Path('/tmp/immutable.zip')
    assert response.content_type == 'application/zip'
    assert response.headers['X-Autograde-SHA256'] == 'f' * 64
    assert calls == [('come2201', FIRST, 1, revision)]


@pytest.mark.parametrize('suffix', ['download/1/0', 'remove/1/2', 'publish/1/2', 'download/1/../../2',
                                   'download/1/2/3', 'upload/1/2'])
def test_history_download_rejects_invalid_or_mutating_history_routes(suffix):
    with pytest.raises(PlatformNotFound):
        instructor.request(ui_for(material()), 'GET', COURSE, f'materials/{FIRST}/{suffix}', {}, SESSION)


@pytest.mark.parametrize('flags', [dict(file_revision=2), dict(file_revision=2, file_updated=True)])
def test_student_direct_file_update_shows_notice_without_new_material_edition(flags):
    portal, session = portal_for(material(edition=1, **flags))
    html = student._listing(portal, 'come2201', session).body
    assert '배포본 v1 · 내 파일 v2' in html
    assert '파일 수정본 v2 · 다시 다운로드하세요' in html
    assert f'action="{STUDENT_BASE}/{FIRST}/download"' in html
    assert '/download/1/1' not in html


@pytest.mark.parametrize('flags', [dict(request_count=1), dict(requested=True)])
def test_student_current_file_request_hides_direct_update_notice(flags):
    portal, session = portal_for(material(file_revision=3, file_updated=True, **flags))
    html = student._listing(portal, 'come2201', session).body
    assert '다시 다운로드하세요' not in html
    assert '현재 파일의 요청 기록이 있습니다.' in html
    assert '실제 저장 여부는 브라우저 다운로드 목록에서 확인하세요.' in html


@pytest.mark.parametrize('availability', ['closed', 'scheduled'])
def test_student_updated_file_outside_window_has_no_download_prompt(availability):
    portal, session = portal_for(material(file_revision=2, file_updated=True, availability=availability))
    html = student._listing(portal, 'come2201', session).body
    assert '다시 다운로드하세요' not in html
    assert f'action="{STUDENT_BASE}/{FIRST}/download"' not in html


def test_student_metadata_only_update_does_not_claim_file_was_changed():
    portal, session = portal_for(material(revision=50, edition=1, file_revision=1, file_updated=False))
    html = student._listing(portal, 'come2201', session).body
    assert '다시 다운로드하세요' not in html
    assert '내 파일 v1' in html


def test_student_download_uses_one_snapshot_for_file_and_headers():
    portal, session = portal_for(material())
    calls = []
    def snapshot(course, mid, sid):
        calls.append((course, mid, sid))
        return Path('/tmp/replaced.zip'), dict(format='zip', filename='001.zip', sha256='c' * 64)
    portal.student_materials.download_snapshot = snapshot
    response = student.request(portal, 'POST', f'{STUDENT_BASE}/{FIRST}/download',
                               dict(csrf=session.csrf), {}, portal.web_url)
    assert response.status == 200 and response.path == Path('/tmp/replaced.zip')
    assert response.content_type == 'application/zip'
    assert response.headers['X-Autograde-SHA256'] == 'c' * 64
    assert calls == [('come2201', FIRST, 1)]
