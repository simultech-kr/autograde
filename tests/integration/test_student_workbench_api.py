"""Student task home, direct files and public feedback over real HTTP."""
from datetime import timedelta
import json

import pytest

from test_course_portal import portal, connect, claim, request
from test_student_browser_sessions import CookieBrowser
from test_legacy_student_portal_api import login, own, KEY, COURSE, OTHER
from test_student_membership_api import api
from test_student_portal_home import submit
from test_student_material_http import material_http, publish_material, PDF, ZIP, assert_file
from test_exam_paper_http import exam_http, publish_paper, assert_pdf


def read(browser, path):
    status, headers, raw = browser.request(path)
    assert status == 200, raw
    assert headers['Cache-Control'] == 'no-store'
    return json.loads(raw)['result']


def detail_path(sid, course=COURSE):
    return f'/student/api/course/{course}/submissions/{sid}'


def pin(material, csrf):
    return {'csrf': csrf, 'revision': str(material['revision']), 'file_revision': str(material['file_revision'])}


def make_result(env, tmp_path, *, policy='immediate', published=True):
    web, ide, services, _, _ = env
    state = services[COURSE].state
    assignment_id = 'asn_' + COURSE
    if policy != 'immediate':
        source = state.get_bundle_assignment(assignment_id)
        assignment_id = 'asn_workbench_' + policy
        fields = ('course_key', 'starter_path', 'starter_digest', 'starter_size_bytes', 'assessment_path',
                  'assessment_digest', 'runner_image', 'rubric_version', 'max_score', 'opens_at', 'due_at')
        state.register_bundle_assignment_release(assignment_id=assignment_id, assignment_key='workbench', release_id='v1',
            title='Public feedback exercise', result_policy=policy, ready=True,
            **{key: getattr(source, key) for key in fields})
    credential = state.get_student_password_credential(student_key=KEY, course_key=COURSE)
    _, code = services[COURSE].create_authenticated_assignment_claim(credential, assignment_id)
    token = connect(ide, code)[1]['access_token']
    sid = submit(env, tmp_path, number=1, token=token, assignment_id=assignment_id)
    state.transition_bundle_submission(sid, 'running')
    state.record_bundle_graded_result(sid, result_id='workbench-result', score=3, max_score=10,
        rubric={'output': {'title': '출력 형식', 'score': 3, 'max_score': 10, 'status': 'partial',
                          'feedback': '출력 마지막 줄을 확인하세요.', 'hint': '줄바꿈과 공백을 비교하세요.',
                          'path': 'main.cpp', 'line': 3, 'hidden_input': 'NOT_PUBLIC_INPUT'},
                'api_key': {'feedback': 'NOT_PUBLIC_SECRET'}},
        diagnostics=[{'message': '공개 테스트를 다시 실행하세요.', 'severity': 'info',
                      'path': '/private/grader/path', 'raw_stdout': 'NOT_PUBLIC_STDOUT'}])
    if published:
        state.publish_bundle_result(sid)
    return sid


def test_home_has_only_proven_courses_and_current_receipts_without_heavy_private_payload(portal, tmp_path):
    browser = CookieBrowser(portal[0])
    login(browser)
    _, auth = connect(portal[1], claim(portal[0], COURSE))
    previous = submit(portal, tmp_path, number=1, token=auth['access_token'], score=9, published=True)
    current = submit(portal, tmp_path, number=2, token=auth['access_token'])
    result = read(browser, '/student/api/home')
    assert result['student_key'] == KEY and not result['courses_truncated']
    assert [row['course_key'] for row in result['courses']] == [COURSE]
    overview = result['courses'][0]['overview']
    assert set(overview) == {'assignments', 'latest', 'assignments_truncated', 'latest_truncated', 'limit'}
    assert overview['latest'][0]['submission_id'] == current
    assert overview['latest'][0]['score'] is None
    assert overview['latest'][0]['previous_best_id'] == previous
    assert overview['latest'][0]['previous_best_score'] == 9
    assert all(word not in json.dumps(result) for word in ('PRIVATE_GRADER', 'gradebook', 'diagnostics', 'rubric', 'history'))
    assert browser.request('/student/api/home?student_key=20269999')[0] == 400


def test_home_never_falls_back_to_an_old_course_cookie_and_rechecks_authority(portal):
    browser = CookieBrowser(portal[0])
    assert browser.legacy_login()[0] == 200
    assert browser.request('/student/api/home')[0] == 401
    login(browser)
    portal[2][COURSE].set_student_password(student_key=KEY, password='123456')
    assert browser.request('/student/api/home')[0] == 401


def test_home_is_bounded_and_marks_omitted_courses(portal, monkeypatch):
    browser = CookieBrowser(portal[0])
    login(browser)
    import autograde.student_workbench_api as workbench
    monkeypatch.setattr(workbench, 'MAX_HOME_COURSES', 1)
    portal[2][OTHER].set_student_password(student_key=KEY, password='000001')
    login(browser)
    result = read(browser, '/student/api/home')
    assert len(result['courses']) == 1 and result['courses_truncated']


def test_home_limits_per_course_assignments_and_exposes_truncation(portal, tmp_path, monkeypatch):
    make_result(portal, tmp_path, policy='score_only')
    import autograde.student_workbench_api as workbench
    monkeypatch.setattr(workbench, 'MAX_HOME_ASSIGNMENTS', 1)
    browser = CookieBrowser(portal[0])
    login(browser)
    overview = read(browser, '/student/api/home')['courses'][0]['overview']
    assert len(overview['assignments']) == 1 and overview['assignments_truncated']
    assert overview['limit'] == 1


@pytest.mark.parametrize(('filename', 'content', 'mime'), [(KEY + '.pdf', PDF, 'application/pdf'), (KEY + '.zip', ZIP, 'application/zip')])
def test_material_catalog_downloads_actual_own_file_without_html_handoff(material_http, filename, content, mime):
    env = material_http
    publish_material(env, filename=filename, content=content)
    browser = CookieBrowser(env.server)
    login(browser, password='123456')
    result = own(browser, OTHER)
    item, = result['materials']['items']
    assert not result['materials']['truncated']
    assert item['filename'] == filename and item['size_bytes'] == len(content)
    assert item['availability'] == 'open' and item['kind'] == 'material'
    assert item['download_path'].startswith('/student/api/course/come2201/materials/mat_')
    assert all(key not in item for key in ('sha256', 'student_id', 'storage_path', 'password_hash'))
    assert_file(browser.request(item['download_path'], data=pin(item, result['csrf'])),
                content=content, content_type=mime, extension='.' + item['format'])
    other = CookieBrowser(env.server)
    other_login, _ = login(other, key='20260002', password='234567')
    assert read(other, '/student/api/course/' + OTHER)['materials']['items'] == []
    assert other.request(item['download_path'], data=pin(item, other_login['csrf']))[0] == 404


def test_material_metadata_has_only_schedule_before_open_and_after_close(material_http):
    env = material_http
    publish_material(env)
    browser = CookieBrowser(env.server)
    login(browser, password='123456')
    for delta, expected in ((-timedelta(seconds=1), 'scheduled'), (timedelta(hours=1), 'closed')):
        from test_student_material_http import NOW
        env.clock[0] = NOW + delta
        item, = own(browser, OTHER)['materials']['items']
        assert item['availability'] == expected
        assert item['filename'] is None and item['size_bytes'] is None and item['download_path'] is None
        assert not item['instructions'] and not item['change_note']


def test_material_download_rejects_csrf_origin_stale_revision_and_revoked_session(material_http):
    env = material_http
    publish_material(env)
    browser = CookieBrowser(env.server)
    login(browser, password='123456')
    course = own(browser, OTHER)
    item, = course['materials']['items']
    payload = pin(item, course['csrf'])
    path = item['download_path']
    assert browser.request(path)[0] == 405
    for fields, origin in (({**payload, 'csrf': 'wrong'}, None), (payload, 'https://other.invalid'),
                           ({**payload, 'student_key': '20260002'}, None)):
        options = {'origin': origin} if origin else {}
        status, headers, body = browser.request(path, data=fields, **options)
        assert status == 403 and headers['Content-Type'].startswith('application/json')
        assert b'%PDF' not in body
    for key in ('revision', 'file_revision'):
        status, _, raw = browser.request(path, data={**payload, key: str(int(payload[key]) + 1)})
        assert status == 409 and json.loads(raw)['error']['code'] == 'material_changed'
    assert env.materials.receipts(OTHER, item['id'])[0]['request_count'] == 0
    api(browser, 'logout', {'csrf': course['csrf']})
    assert browser.request(path, data=payload)[0] == 401


def test_material_catalog_bounds_large_unicode_guidance_without_cutting_kept_instructions(material_http, monkeypatch):
    env = material_http
    publish_material(env)
    browser = CookieBrowser(env.server)
    login(browser, password='123456')
    student_id = env.state.get_student_password_credential(student_key=KEY, course_key=OTHER).student_id
    row, = env.materials.list_for_student(OTHER, student_id)
    rows = [{**row, 'material_id': 'mat_' + str(number).zfill(32),
             'instructions': '가' * 5000, 'change_note': '나' * 5000} for number in range(201)]
    monkeypatch.setattr(env.materials, 'list_for_student', lambda *args, **kwargs: rows)
    result = own(browser, OTHER)['materials']
    assert result['truncated'] and 0 < len(result['items']) < 200
    assert all(item['instructions'] == '가' * 5000 for item in result['items'])
    assert len(json.dumps(result, ensure_ascii=False).encode('utf-8')) < 600 * 1024


def test_material_download_rechecks_password_after_file_verification(material_http, monkeypatch):
    env = material_http
    publish_material(env)
    browser = CookieBrowser(env.server)
    login(browser, password='123456')
    course = own(browser, OTHER)
    item, = course['materials']['items']
    original = env.materials.download_snapshot
    def rotated(*args, **kwargs):
        response = original(*args, **kwargs)
        env.services[OTHER].set_student_password(student_key=KEY, password='654321')
        return response
    monkeypatch.setattr(env.materials, 'download_snapshot', rotated)
    status, _, raw = browser.request(item['download_path'], data=pin(item, course['csrf']))
    assert status == 403 and b'%PDF' not in raw


def test_home_pending_signup_has_no_course_activity(portal):
    browser = CookieBrowser(portal[0])
    assert browser.signup()[0] == 303
    result = read(browser, '/student/api/home')
    assert result['courses'] == [] and not result['courses_truncated']


def test_exam_files_use_same_inline_download_contract(exam_http):
    env = exam_http
    publish_paper(env)
    browser = CookieBrowser(env.server)
    login(browser, password='123456')
    course = own(browser, OTHER)
    item, = course['materials']['items']
    assert item['kind'] == 'exam' and item['filename'] == item['id'] + '.pdf'
    assert item['size_bytes'] > 0 and item['format'] == 'pdf'
    assert_pdf(browser.request(item['download_path'], data=pin(item, course['csrf'])))
    assert browser.request(item['download_path'], data={**pin(item, course['csrf']), 'file_revision': '2'})[0] == 409


def test_published_feedback_uses_public_allowlist_and_exact_submission(portal, tmp_path):
    sid = make_result(portal, tmp_path)
    browser = CookieBrowser(portal[0])
    login(browser)
    body = read(browser, detail_path(sid))
    assert body['student_key'] == KEY and body['course_key'] == COURSE and body['submission_id'] == sid
    assert body['feedback_available'] and body['result']['score'] == 3
    row = body['result']['rubric']['output']
    assert row['hint'] == '줄바꿈과 공백을 비교하세요.' and row['status'] == 'partial'
    assert row['path'] == 'main.cpp' and row['line'] == 3
    assert 'NOT_PUBLIC' not in json.dumps(body) and '/private/grader' not in json.dumps(body)
    assert 'path' not in body['result']['diagnostics'][0]
    assert browser.request(detail_path(sid) + '?student_key=20269999')[0] == 400
    portal[2][OTHER].set_student_password(student_key=KEY, password='000001')
    login(browser)
    assert browser.request(detail_path(sid, OTHER))[0] == 404


@pytest.mark.parametrize('published', [False, True])
def test_feedback_never_exposes_unpublished_or_score_only_rubric(portal, tmp_path, published):
    sid = make_result(portal, tmp_path, policy='score_only', published=published)
    browser = CookieBrowser(portal[0])
    login(browser)
    status, _, raw = browser.request(detail_path(sid))
    assert b'\xec\xa4\x84\xeb\xb0\x94\xea\xbf\x88' not in raw
    if not published:
        assert status == 404 and json.loads(raw)['error']['code'] == 'result_not_available'
        assert b'score' not in raw
    else:
        result = json.loads(raw)['result']
        assert status == 200 and not result['feedback_available']
        assert result['result']['score'] == 3 and result['result']['rubric'] == {} and result['result']['diagnostics'] == []


def test_feedback_blocks_other_students_archived_assignments_and_revoked_password(portal, tmp_path):
    sid = make_result(portal, tmp_path)
    state = portal[2][COURSE].state
    other = state.upsert_local_student(student_key='20269999', auth_subject='local:20269999')
    state.upsert_enrollment(student_id=other.id, course_key=COURSE)
    portal[2][COURSE].set_student_password(student_key='20269999', password='123456')
    other_browser = CookieBrowser(portal[0])
    login(other_browser, key='20269999', password='123456')
    assert other_browser.request(detail_path(sid))[0] == 404
    browser = CookieBrowser(portal[0])
    login(browser)
    with state._write() as db:
        db.execute('UPDATE bundle_assignment_releases SET active=0 WHERE assignment_id=?', ('asn_' + COURSE,))
    assert browser.request(detail_path(sid))[0] == 404
    portal[2][COURSE].set_student_password(student_key=KEY, password='654321')
    assert browser.request(detail_path(sid))[0] == 401
