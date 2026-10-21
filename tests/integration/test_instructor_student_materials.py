"""Instructor recovery, roster-matching presentation and synthetic browser pages."""
from datetime import timedelta
import json

import pytest

from autograde.student_material_portal import _listing
from test_student_material_http import (
    BASE, COURSE, PDF, STUDENT_BASE, ZIP, create_material, download_path,
    material_http, publish_material, revision, student_login, zip_bytes,
)
from test_instructor_web import Forms


def test_create_error_and_stale_revision_keep_draft_inputs(material_http):
    env = material_http
    assert env.admin.get(BASE)[0] == 200
    status, _, body = env.admin.post(BASE, title='<script>keep title</script>', instructions='Keep draft instructions',
                                    opens_at='invalid', closes_at='2026-10-21T10:00')
    assert status == 400
    assert '&lt;script&gt;keep title&lt;/script&gt;' in body.decode()
    assert 'Keep draft instructions' in body.decode()
    assert '배포 시작 일시를 올바르게 입력하세요.' in body.decode()
    path = create_material(env)
    before = revision(env, path)
    fields = dict(opens_at='2026-10-21T09:00', closes_at='2026-10-21T10:00',
                  instructions='Saved instructions', title='Saved title')
    assert env.admin.post(path, revision=str(before), **fields)[0] == 303
    status, _, body = env.admin.post(path, revision=str(before), **{**fields, 'title': 'Keep unsaved title'})
    assert status == 409 and 'Keep unsaved title' in body.decode()
    form = next(form for form in Forms(body.decode()).forms if form['action'] == path)
    assert form['fields']['revision'] == str(before)
    material = env.materials.get(COURSE, path.rsplit('/', 1)[1])
    assert material['title'] == 'Saved title'


def test_two_upload_forms_have_distinct_labels_and_recover_to_same_detail(material_http):
    env = material_http
    path = create_material(env)
    current = revision(env, path)
    html = env.admin.get(path)[2].decode()
    assert 'id="material-file"' in html and 'for="material-file"' in html
    assert 'id="material-batch"' in html and 'for="material-batch"' in html
    assert '파일 배정 0명 · 활성 수강 중 미배정 3명' in html
    for action in ['upload', 'upload-batch']:
        status, _, body = env.admin.upload(path + '/' + action, revision=current, content=b'invalid-file')
        assert status == 400
        html = body.decode()
        assert f'action="{path}/upload"' in html and f'action="{path}/upload-batch"' in html
        assert 'role="alert"' in html


def test_preparation_course_has_upload_controls_but_no_publish(material_http):
    env = material_http
    course = env.courses.create_course(code='come2202', name='준비 수업', year=2026, semester='2', section='01')
    env.students.add_student(course['course_key'], student_key='20260001', name='Synthetic 20260001', password='123456')
    base = f'/courses/{course["course_key"]}/instructor/materials'
    assert env.admin.get(base)[0] == 200
    status, headers, body = env.admin.post(base, title='준비 수업 자료', instructions='',
                                         opens_at='2026-10-21T09:00', closes_at='2026-10-21T10:00')
    assert status == 303, body
    path = headers['Location']
    assert env.admin.upload(path + '/upload', revision=revision(env, path))[0] == 303
    html = env.admin.get(path)[2].decode()
    assert '준비 중인 수업에서는 초안을 저장할 수 있습니다.' in html
    assert f'action="{path}/upload"' in html
    assert f'action="{path}/publish"' not in html


def test_remove_draft_recipient_requires_confirm_and_supports_corrected_upload(material_http):
    env = material_http
    path = create_material(env)
    assert env.admin.upload(path + '/upload', revision=revision(env, path))[0] == 303
    before = revision(env, path)
    student_id = env.students.get_student(COURSE, '20260001')['student_id']
    target = path + f'/remove/{student_id}'
    html = env.admin.get(path)[2].decode()
    assert f'action="{target}"' in html
    assert '원본 파일은 서버의 비공개 저장소에 남지만 학생에게 제공되지 않습니다.' in html
    assert env.admin.get(target)[0] == 404
    assert env.admin.post(target, revision=str(before))[0] == 400
    assert env.materials.get(COURSE, path.rsplit('/', 1)[1])['file_count'] == 1
    assert env.admin.post(target, revision=str(before), confirm='yes')[0] == 303
    assert env.materials.get(COURSE, path.rsplit('/', 1)[1])['file_count'] == 0
    html = env.admin.get(path)[2].decode()
    assert f'action="{target}"' not in html
    assert '파일 배정 0명 · 활성 수강 중 미배정 3명' in html
    assert env.admin.get(path + f'/download/{student_id}')[0] == 404
    # Corrected filename now assigns the material to another student.
    assert env.admin.upload(path + '/upload', revision=revision(env, path), filename='20260002.pdf')[0] == 303
    assert env.admin.post(path + '/publish', revision=str(revision(env, path)), confirm='yes')[0] == 303
    first = student_login(env)
    assert first.post(download_path(path))[0] == 404
    second = student_login(env, key='20260002', password='234567')
    assert second.post(download_path(path))[0] == 200
    recipient = env.students.get_student(COURSE, '20260002')['student_id']
    assert env.admin.post(path + f'/remove/{recipient}', revision=str(revision(env, path)), confirm='yes')[0] == 409


@pytest.mark.parametrize('fields', [{'csrf': 'wrong-csrf'}, {'origin': 'https://other.example'}])
def test_remove_draft_recipient_rejects_forged_requests(material_http, fields):
    env = material_http
    path = create_material(env)
    assert env.admin.upload(path + '/upload', revision=revision(env, path))[0] == 303
    before = revision(env, path)
    student_id = env.students.get_student(COURSE, '20260001')['student_id']
    assert env.admin.post(path + f'/remove/{student_id}', revision=str(before), confirm='yes', **fields)[0] == 403
    assert env.materials.get(COURSE, path.rsplit('/', 1)[1])['file_count'] == 1


def test_export_materials_browser_fixtures(material_http, tmp_path):
    env = material_http
    env.students.add_student(COURSE, student_key='20260005', name='미배정 긴이름' * 14, password='112233')
    bundle = zip_bytes([('20260001.pdf', PDF), ('20260002.zip', ZIP)])
    open_path = publish_material(env, title='학생별 실습자료·개별 피드백 ' + '긴제목' * 30,
                                 filename='distribution.zip', content=bundle, batch=True)
    publish_material(env, title='배포 시작 전 개인 자료', opens_at='2026-10-22T09:00', closes_at='2026-10-22T10:00')
    draft_path = create_material(env, title='학생별 자료 등록 초안')
    assert env.admin.upload(draft_path + '/upload', revision=revision(env, draft_path))[0] == 303
    browser = student_login(env)
    status, _, listing = browser.get(STUDENT_BASE)
    assert status == 200
    assert browser.post(download_path(open_path))[0] == 200
    pages = {
        'student-open': listing.decode(),
        'instructor-list': env.admin.get(BASE)[2].decode(),
        'instructor-draft': env.admin.get(draft_path)[2].decode(),
        'instructor-receipts': env.admin.get(open_path)[2].decode(),
    }
    with env.portal.lock:
        _, session = env.portal._session(COURSE, browser.cookies)
        env.clock[0] += timedelta(hours=2)
        pages['student-closed'] = _listing(env.portal, COURSE, session).body
    pages['student-login'] = env.portal._entry(COURSE, destination='materials').body
    target = tmp_path / 'materials-browser-fixtures.json'
    target.write_text(json.dumps(pages, ensure_ascii=False), encoding='utf-8')
    assert len(pages) == 6
