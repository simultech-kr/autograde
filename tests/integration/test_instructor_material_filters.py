"""Synthetic HTTP coverage for bounded material rosters and retained view state."""
from html import unescape
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from test_instructor_web import Forms
from autograde.instructor_upload import parse_instructor_upload
from test_instructor_upload_http import multipart, send
from test_student_material_http import (
    COURSE, PDF, download_path, material_http, material_multipart, publish_material,
    revision, student_login, zip_bytes,
)


def table_keys(html, caption):
    if f'<caption>{caption}</caption>' not in html:
        return []
    table = html.split(f'<caption>{caption}</caption>', 1)[1].split('</table>', 1)[0]
    return re.findall(r'<div class="cell-value">([0-9]+)</div></th>', table)


RECIPIENTS = '학생별 배정 파일 확인'
REQUESTS = '배정 학생별 승인된 자료 요청 기록'


@pytest.fixture
def populated_material(material_http):
    env = material_http
    extras = [f'202601{number:02}' for number in range(1, 21)]
    for number, key in enumerate(extras, 1):
        name = '특별 <script>학생</script>' if number == 1 else f'Filter Learner {number:02}'
        env.students.add_student(COURSE, student_key=key, name=name, password=str(500000 + number))
    keys = ['20260001', '20260002', '20260003', *extras[:18]]
    path = publish_material(env, batch=True, filename='distribution.zip',
                            content=zip_bytes([(key + '.pdf', PDF) for key in keys]))
    assert student_login(env).post(download_path(path))[0] == 200
    env.students.set_active(COURSE, '20260002', False, reason='synthetic inactive recipient')
    return env, path


def test_independent_bounded_pages_counts_and_current_file_download(populated_material):
    env, path = populated_material
    html = env.admin.get(path)[2].decode()
    assert len(table_keys(html, RECIPIENTS)) == len(table_keys(html, REQUESTS)) == 10
    assert '배정 대상: 전체 23명 · 필터 결과 23명 · 1–10명 표시 · 1/3페이지' in html
    assert '파일 요청: 전체 21명 · 필터 결과 21명 · 1–10명 표시 · 1/3페이지' in html
    assert '파일 미배정 2명' in html and '현재 파일 요청 기록 없음 19명' in html
    html = env.admin.get(path + '?recipients_page=2&requests_page=3')[2].decode()
    assert len(table_keys(html, RECIPIENTS)) == 10
    assert len(table_keys(html, REQUESTS)) == 1
    assert 'recipients_page=3&amp;requests_page=3#material-students' in html
    assert 'recipients_page=2&amp;requests_page=2#material-requests' in html
    assert '11–20명 표시 · 2/3페이지' in html
    assert '21–21명 표시 · 3/3페이지' in html
    second_request_page = env.admin.get(path + '?requests_page=2')[2].decode()
    assert '?requests_page=1#material-requests' in second_request_page
    html = env.admin.get(path + '?recipients_page=999999&requests_page=999999')[2].decode()
    assert len(table_keys(html, RECIPIENTS)) == 3 and len(table_keys(html, REQUESTS)) == 1
    # Paging/search never changes stored-file access or increments student requests.
    key = '20260001'
    student_id = env.students.get_student(COURSE, key)['student_id']
    assert env.admin.get(path + f'/download/{student_id}')[2] == PDF
    assert next(row for row in env.materials.recipients(COURSE, path.rsplit('/', 1)[1])
                if row['student_key'] == key)['request_count'] == 1


def test_shared_search_filters_summary_links_and_course_scope(populated_material):
    env, path = populated_material
    query = urlencode(dict(q='FILTER LEARNER', assignment='assigned', requested='unrequested', student_status='active'))
    html = env.admin.get(path + '?' + query)[2].decode()
    assert '배정 대상: 전체 23명 · 필터 결과 17명' in html
    assert '파일 요청: 전체 21명 · 필터 결과 17명' in html
    assert table_keys(html, RECIPIENTS) == table_keys(html, REQUESTS)
    assert '파일 미배정 2명' in html and '현재 파일 요청 기록 없음 19명' in html
    assert 'q=FILTER+LEARNER&amp;assignment=assigned&amp;requested=unrequested&amp;student_status=active&amp;recipients_page=2' in html
    # Summary links start from whole-material counts, clearing the current search.
    missing = next(unescape(url) for url in re.findall(r'href="([^"]+)"', html)
                   if 'assignment=unassigned' in url)
    missing_html = env.admin.get(missing.split('#')[0])[2].decode()
    assert table_keys(missing_html, RECIPIENTS) == ['20260119', '20260120']
    assert table_keys(missing_html, REQUESTS) == []
    assert '미배정 학생은 파일 요청 현황에 포함되지 않습니다.' in missing_html
    waiting = next(unescape(url) for url in re.findall(r'href="([^"]+)"', html)
                   if 'requested=unrequested' in url and 'q=' not in url)
    waiting_html = env.admin.get(waiting.split('#')[0])[2].decode()
    assert '필터 결과 19명' in waiting_html
    assert '20260001' not in table_keys(waiting_html, REQUESTS)
    assert '20260002' not in table_keys(waiting_html, REQUESTS)
    requested_html = env.admin.get(path + '?requested=requested')[2].decode()
    assert table_keys(requested_html, RECIPIENTS) == table_keys(requested_html, REQUESTS) == ['20260001']
    inactive_html = env.admin.get(path + '?student_status=inactive')[2].decode()
    assert table_keys(inactive_html, RECIPIENTS) == ['20260002']
    special_html = env.admin.get(path + '?' + urlencode({'q': '특별'}))[2].decode()
    assert table_keys(special_html, RECIPIENTS) == ['20260101']
    assert '&lt;script&gt;학생&lt;/script&gt;' in special_html and '<script>학생</script>' not in special_html
    foreign = env.admin.get(path + '?q=20260004')[2].decode()
    assert table_keys(foreign, RECIPIENTS) == table_keys(foreign, REQUESTS) == []
    assert '검색·필터 조건에 맞는 학생이 없습니다.' in foreign
    assert env.admin.get(path.replace('/come2201/', '/come3105/') + '?q=20260001')[0] == 404


def test_filter_context_survives_failed_and_successful_remove(populated_material):
    env, path = populated_material
    options = dict(q='Filter Learner', assignment='assigned', requested='unrequested',
                   student_status='active', recipients_page='2', requests_page='2')
    html = env.admin.get(path + '?' + urlencode(options))[2].decode()
    form = next(form for form in Forms(html).forms if '/remove/' in form['action'])
    assert all(form['fields'][key] == value for key, value in options.items())
    fields = {key: value for key, value in form['fields'].items() if key != 'csrf'}
    failed = env.admin.post(form['action'], **fields)
    assert failed[0] == 400
    recovered = next(item for item in Forms(failed[2].decode()).forms if item['action'] == form['action'])
    assert all(recovered['fields'][key] == value for key, value in options.items())
    assert '필터 결과 17명' in failed[2].decode()
    saved = env.admin.post(form['action'], **fields, confirm='yes', return_to='https://other.example/')
    assert saved[0] == 303
    target = urlsplit(saved[1]['Location'])
    assert target.path == path and not target.netloc
    assert parse_qs(target.query) == {key: [value] for key, value in options.items()}
    after = env.admin.get(saved[1]['Location'])[2].decode()
    assert '배정 대상: 전체 23명 · 필터 결과 16명 · 11–16명 표시 · 2/2페이지' in after


def test_context_survives_zip_review_and_apply(material_http):
    env = material_http
    path = publish_material(env)
    options = dict(q='Synthetic', assignment='assigned', requested='unrequested', student_status='active',
                   recipients_page='2', requests_page='2')
    mime, body = material_multipart(env.admin.csrf, revision(env, path), 'distribution.zip',
                                    zip_bytes([('20260002.pdf', PDF)]))
    boundary = mime.split('boundary=')[1]
    context = ''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
                      for key, value in options.items()).encode()
    response = env.admin._remember(send(env.server, path + '/upload-batch', context + body, mime, browser=env.admin))
    assert response[0] == 303, response[2]
    target = urlsplit(response[1]['Location'])
    assert parse_qs(target.query) == {key: [value] for key, value in options.items()}
    html = env.admin.get(response[1]['Location'])[2].decode()
    apply = next(form for form in Forms(html).forms if form['action'].endswith('/apply'))
    assert all(apply['fields'][key] == value for key, value in options.items())
    student_id = env.students.get_student(COURSE, '20260002')['student_id']
    assert env.admin.get(target.path + f'/file/{student_id}')[2] == PDF
    fields = {key: value for key, value in apply['fields'].items() if key != 'csrf'}
    failed = env.admin.post(apply['action'], **fields)
    assert failed[0] == 400
    recovered = next(form for form in Forms(failed[2].decode()).forms if form['action'].endswith('/apply'))
    assert all(recovered['fields'][key] == value for key, value in options.items())
    saved = env.admin.post(apply['action'], confirm='yes', **fields)
    assert saved[0] == 303 and urlsplit(saved[1]['Location']).path == path
    assert parse_qs(urlsplit(saved[1]['Location']).query) == {key: [value] for key, value in options.items()}
    assert table_keys(env.admin.get(saved[1]['Location'])[2].decode(), RECIPIENTS) == ['20260001', '20260002']


@pytest.mark.parametrize('query', [dict(q='x' * 201), dict(assignment='unknown'), dict(requested='unknown'),
                                  dict(student_status='unknown'), dict(recipients_page='0'), dict(requests_page='9999999')])
def test_invalid_filter_input_is_rejected_without_mutation(material_http, query):
    env = material_http
    path = publish_material(env)
    before = revision(env, path)
    assert env.admin.get(path + '?' + urlencode(query))[0] == 400
    assert revision(env, path) == before


def test_material_multipart_context_does_not_expand_other_upload_routes():
    fields = [('csrf', 'synthetic'), ('revision', '1'), ('file', PDF), ('q', 'student'),
              ('assignment', 'assigned'), ('requested', 'unrequested'), ('student_status', 'active'),
              ('recipients_page', '2'), ('requests_page', '3')]
    mime, body = multipart(fields)
    parsed = parse_instructor_upload(mime, body, file_limit=1024, allow_material_filters=True)
    assert parsed['file'] == PDF and parsed['requests_page'] == '3'
    with pytest.raises(ValueError):
        parse_instructor_upload(mime, body, file_limit=1024)
    for extra in [('q', 'duplicate'), ('return_to', 'https://other.example/')]:
        mime, body = multipart(fields + [extra])
        with pytest.raises(ValueError):
            parse_instructor_upload(mime, body, file_limit=1024, allow_material_filters=True)
