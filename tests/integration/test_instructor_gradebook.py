"""Instructor Excel workflow, real multipart transport and course boundaries."""
import base64
from html import escape, unescape
from io import BytesIO
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from openpyxl import Workbook, load_workbook
import pytest

from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_upload import parse_instructor_upload
from autograde.platform_portal import CoursePortal
from autograde.platform_service import PlatformAPIError
from test_course_portal import serving
from test_instructor_upload_http import send, download
from test_instructor_web import setup, BASE, WEB, AUTH, Forms

ROOT = BASE + '/gradebook'


def excel(rows=None):
    book = Workbook()
    for row in rows or [['학번', '중간고사', '기말고사', '출석', '교수자 메모'],
                        ['00123', 0, 90.5, None, '<script>private</script>'],
                        ['00456', 88, 95, 10, 'second student']]:
        book.active.append(row)
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def multipart(browser, data, **fields):
    boundary = 'synthetic-gradebook-boundary'
    content = bytearray()
    for name, value in {'csrf': browser.csrf, 'title': '2026-2 성적표', 'orientation': 'rows',
                        'sheet_name': '', 'student_column': '학번', **fields, 'file': data}.items():
        content.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'.encode())
        if name == 'file':
            content.extend(b'; filename="grades.xlsx"')
        content.extend(b'\r\n\r\n')
        content.extend(value if isinstance(value, bytes) else str(value).encode())
        content.extend(b'\r\n')
    content.extend(f'--{boundary}--\r\n'.encode())
    return f'multipart/form-data; boundary={boundary}', bytes(content)


@pytest.fixture
def gradebook(setup):
    browser, state, courses, students, assignments = setup
    for index, key in enumerate(['00123', '00456'], 1):
        students.add_student('come2201', student_key=key, password=f'{index:06d}')
    browser.get(ROOT)
    return browser, state, courses, students


def upload(browser, rows=None, **fields):
    response = browser.post(ROOT + '/import', file=excel(rows), file_name='grades.xlsx', title='수업 성적표',
                            orientation='rows', sheet_name='', student_column='학번', **fields)
    assert response.status == 303, response.body
    path = response.headers['Location']
    return path, browser.web.gradebook.review('come2201', path.rsplit('/', 1)[-1])


def publish_fields(entry, keys=None):
    return {'revision': str(entry['revision']), 'confirm': 'yes',
            **{'column_' + column['key']: 'yes' for column in entry['columns']
               if keys is None or column['key'] in keys}}


def review_page(browser, path, **query):
    selected = [name.removeprefix('column_') for name, value in query.items() if name.startswith('column_') and value == 'yes']
    query = {name: value for name, value in query.items() if not name.startswith('column_')}
    if selected:
        query['columns'] = ','.join(selected)
    return browser.web.request('GET', path, query, browser.cookies, authorization=AUTH)


def test_upload_review_choose_columns_publish_replace_hide_and_delete(gradebook):
    browser, state, courses, students = gradebook
    path, entry = upload(browser)
    page = browser.get(path)
    assert page.status == 200 and '성적표 검토' in page.body
    assert '&lt;script&gt;private&lt;/script&gt;' in page.body and '<script>private</script>' not in page.body
    assert '미입력' in page.body and '>0<' in page.body
    assert '검토 중 · 학생 비공개' in page.body
    keys = [column['key'] for column in entry['columns'][:3]]
    published = browser.post(path + '/publish', **publish_fields(entry, keys))
    assert published.status == 303
    updated = browser.web.gradebook.review('come2201', entry['draft_id'])
    assert updated['status'] == 'published' and updated['selected_columns'] == keys
    rendered = browser.get(path).body
    assert '이 버전에서 선택한 공개 항목' in rendered and '교수자 메모 (학생 비공개)' in rendered
    second, draft = upload(browser, [['학번', '중간고사'], ['00123', 99], ['00456', 100]])
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'published'
    assert browser.post(second + '/delete', revision=str(draft['revision']), confirm='yes').status == 303
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'published'
    third, draft = upload(browser, [['학번', '중간고사'], ['00123', 99], ['00456', 100]])
    assert browser.post(third + '/publish', **publish_fields(draft)).status == 303
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'superseded'
    current = browser.web.gradebook.review('come2201', draft['draft_id'])
    assert current['version'] == 2
    assert browser.post(third + '/hide', revision=str(current['revision']), confirm='yes').status == 303
    assert browser.web.gradebook.review('come2201', draft['draft_id'])['status'] == 'hidden'


def test_unknown_students_and_empty_selected_columns_do_not_publish(gradebook):
    browser, *_ = gradebook
    path, entry = upload(browser, [['학번', '중간고사'], ['99999', 90]])
    page = browser.get(path)
    assert '수강 명단과 맞지 않는 학번' in page.body
    assert '/publish' not in page.body
    preview = review_page(browser, path, view='student', column_column_2='yes')
    assert 'type="submit" disabled>선택 항목 학생 공개' in preview.body
    assert browser.post(path + '/publish', **publish_fields(entry)).status in {400, 409}
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'draft'
    path, entry = upload(browser)
    assert browser.post(path + '/publish', revision='1', confirm='yes').status == 400
    assert browser.post(path + '/publish', **{**publish_fields(entry), 'column_foreign': 'yes'}).status == 400
    assert browser.post(path + '/publish', **{**publish_fields(entry), 'confirm': 'no'}).status == 400
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'draft'


def test_import_error_preserves_form_and_current_publication(gradebook):
    browser, *_ = gradebook
    path, entry = upload(browser)
    assert browser.post(path + '/publish', **publish_fields(entry)).status == 303
    response = browser.post(ROOT + '/import', file=b'not a spreadsheet', file_name='invalid.xlsx',
                            title='수정 성적표', sheet_name='2학기', student_column='학생번호', orientation='rows')
    assert response.status == 400
    assert '현재 공개본은 그대로 유지' in response.body
    assert 'value="수정 성적표"' in response.body and 'value="2학기"' in response.body
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'published'


def test_template_contains_this_courses_active_ids_and_real_http_upload(gradebook):
    browser, state, courses, students = gradebook
    facade = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=browser.web)
    with serving(facade, WEB) as server:
        status, headers, data = download(server, ROOT + '/template.xlsx', browser=browser)
        assert status == 200 and headers['Cache-Control'] == 'no-store'
        assert headers['Content-Type'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        book = load_workbook(BytesIO(data), data_only=True)
        assert [row[0] for row in list(book.active.values)[1:]] == ['00123', '00456']
        mime, body = multipart(browser, excel())
        status, headers, result = send(server, ROOT + '/import', body, mime, browser=browser)
        assert status == 303, result
        assert headers['Location'].startswith(ROOT + '/gbook_')
        assert headers['Cache-Control'] == 'no-store'
        assert headers['Referrer-Policy'] == 'same-origin'
        # Transport metadata is scoped to this one upload type.
        with pytest.raises(ValueError):
            parse_instructor_upload(mime, body, file_limit=5 * 1024 * 1024, capture_filename=True)
        assert parse_instructor_upload(mime, body, file_limit=5 * 1024 * 1024,
                                       capture_filename=True, allow_gradebook=True)['file_name'] == 'grades.xlsx'
        for authorization, origin, csrf in [(None, WEB, browser.csrf), (AUTH, 'https://other.example', browser.csrf), (AUTH, WEB, 'wrong')]:
            mime, body = multipart(browser, excel(), csrf=csrf)
            assert send(server, ROOT + '/import', body, mime, browser=browser, authorization=authorization, origin=origin)[0] in {401, 403}
        assert send(server, ROOT + '/import', b'', mime, browser=browser,
                    length=5 * 1024 * 1024 + 64 * 1024 + 1)[0] == 413
    assert len(browser.web.gradebook.list_for_instructor('come2201')) == 1


def test_course_scopes_apply_to_preview_template_and_mutations(gradebook, tmp_path):
    browser, *_ = gradebook
    path, entry = upload(browser)
    identities = InstructorIdentityStore(tmp_path / 'identities.sqlite3')
    identities.initialize()
    identities.create('teacher', 'Teacher', 'instructor', 'synthetic-password-12345')
    identities.set_grant('teacher', 'come3105', allowed=True)
    browser.web.identities = identities
    authorization = 'Basic ' + base64.b64encode(b'teacher:synthetic-password-12345').decode()
    for target in [ROOT, path, ROOT + '/template.xlsx']:
        assert browser.web.request('GET', target, {}, {}, authorization=authorization).status == 404
    with pytest.raises(PlatformAPIError) as denied:
        browser.web.authorize_upload(ROOT + '/import', {}, WEB, authorization)
    assert denied.value.status == 404


def test_navigation_and_stale_revision_cannot_change_the_publication(gradebook):
    browser, *_ = gradebook
    assert '성적표 관리' in browser.get(BASE).body
    path, entry = upload(browser)
    assert browser.post(path + '/publish', **publish_fields(entry)).status == 303
    assert browser.post(path + '/hide', revision=str(entry['revision']), confirm='yes').status == 409
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'published'


def test_new_upload_is_private_and_selected_preview_excludes_private_values(gradebook):
    browser, *_ = gradebook
    path, entry = upload(browser)
    assert entry['selected_columns'] == []
    page = browser.get(path).body
    assert not re.search(r'name="column_[^"]+"[^>]*checked', page)
    assert '/publish' not in page
    missing = review_page(browser, path, view='student')
    assert '공개할 항목을 한 개 이상 선택하세요' in missing.body and '/publish' not in missing.body
    selected = review_page(browser, path, view='student', column_column_2='yes')
    assert selected.status == 200
    assert '학생 공개 내용 미리보기' in selected.body
    assert '&lt;script&gt;private&lt;/script&gt;' not in selected.body
    assert 'second student' not in selected.body
    forms = Forms(selected.body).forms
    public = next(form for form in forms if form.get('action') == path + '/publish')
    assert public['fields']['column_column_2'] == 'yes'
    assert 'column_column_5' not in public['fields']
    assert '공개 대상: 2명 · 1개 항목' in selected.body
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'draft'


def test_all_rows_can_be_reviewed_and_filters_have_empty_recovery(gradebook):
    browser, *_ = gradebook
    rows = [['학번', '점수', '내부메모']] + [[f'{index:05d}', index, f'private-{index}'] for index in range(1, 251)]
    path, _ = upload(browser, rows)
    first = browser.get(path).body
    assert '전체 검토 대상 250명' in first and '1/5페이지' in first
    assert 'private-250' not in first
    last = review_page(browser, path, page='5').body
    assert 'private-250' in last and '5/5페이지' in last
    selected = review_page(browser, path, view='student', column_column_2='yes', page='5').body
    assert '>00250<' in selected and 'private-250' not in selected
    assert 'columns=column_2' in selected and '검색·페이지·검토 대상 필터는 공개 범위를 줄이지 않습니다' in selected
    searched = review_page(browser, path, q='00250').body
    assert '검색 결과 1명' in searched and 'private-250' in searched
    empty = review_page(browser, path, q='<missing>').body
    assert '이 조건에 해당하는 학생이 없습니다' in empty and '필터 초기화' in empty
    assert '&lt;missing&gt;' in empty


def test_replacement_preview_compares_public_labels_and_exposes_omitted_students(gradebook):
    browser, _, _, students = gradebook
    first, entry = upload(browser, [['학번', '중간', '비공개'], ['00123', 80, 'old memo'], ['00456', 90, 'old memo']])
    assert browser.post(first + '/publish', **publish_fields(entry, ['column_2'])).status == 303
    students.add_student('come2201', student_key='00789', password='000003')
    # Header positions change; changing private values does not change a public score.
    path, draft = upload(browser, [['학번', '비공개', '중간'], ['00123', 'new memo', 80], ['00789', 'new memo', 70]])
    options = dict(view='student', column_column_3='yes')
    page = review_page(browser, path, **options).body
    assert '<dt>신규</dt><dd>1명</dd>' in page and '<dt>유지</dt><dd>1명</dd>' in page
    assert '<dt>변경</dt><dd>0명</dd>' in page and '<dt>제외</dt><dd>1명</dd>' in page
    removed = review_page(browser, path, **options, change='removed').body
    assert '검색 결과 1명' in removed and '>00456<' in removed and '공개 대상 제외' in removed
    assert '>00123<' not in removed and 'new memo' not in removed
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'published'
    assert browser.web.gradebook.review('come2201', draft['draft_id'])['status'] == 'draft'


def test_replacement_preview_marks_public_value_change(gradebook):
    browser, *_ = gradebook
    first, entry = upload(browser, [['학번', '점수'], ['00123', 80], ['00456', 90]])
    assert browser.post(first + '/publish', **publish_fields(entry)).status == 303
    path, _ = upload(browser, [['학번', '점수'], ['00123', 81], ['00456', 90]])
    page = review_page(browser, path, view='student', column_column_2='yes', change='changed').body
    assert '<dt>변경</dt><dd>1명</dd>' in page and '>00123<' in page and '>00456<' not in page


def test_actual_http_preview_handles_one_hundred_columns_without_query_limit_failure(gradebook):
    browser, *_ = gradebook
    path, entry = upload(browser, [['학번', *(f'점수{index}' for index in range(100))],
                                  ['00123', *range(100)], ['00456', *range(100)]])
    facade = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=browser.web)
    with serving(facade, WEB) as server:
        fields = {'csrf': browser.csrf, 'revision': str(entry['revision']),
                  **{'column_' + column['key']: 'yes' for column in entry['columns']}}
        status, headers, body = send(server, path + '/preview', urlencode(fields).encode(),
                                    'application/x-www-form-urlencoded', browser=browser)
        assert status == 303, body
        assert 'columns=' in headers['Location'] and 'csrf=' not in headers['Location']
        status, _, body = download(server, headers['Location'], browser=browser)
        assert status == 200, body
        assert '공개 대상: 2명 · 100개 항목' in body.decode()
    assert browser.web.gradebook.review('come2201', entry['draft_id'])['status'] == 'draft'


@pytest.mark.parametrize('concurrent_action', ['first_publish', 'replace', 'hide', 'publish_then_hide'])
def test_reviewed_publication_cannot_overwrite_a_concurrent_publication_change(gradebook, concurrent_action):
    browser, state, *_ = gradebook
    current_path = None
    if concurrent_action in {'replace', 'hide'}:
        current_path, current = upload(browser)
        assert browser.post(current_path + '/publish', **publish_fields(current)).status == 303
    path, draft = upload(browser, [['학번', '점수'], ['00123', 81], ['00456', 90]])
    facade = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=browser.web)
    with serving(facade, WEB) as server:
        status, _, html = download(server, path + '?view=student&columns=column_2', browser=browser)
        assert status == 200
        reviewed_form = next(form for form in Forms(html.decode()).forms if form.get('action') == path + '/publish')
        if concurrent_action in {'first_publish', 'replace', 'publish_then_hide'}:
            current_path, current = upload(browser, [['학번', '점수'], ['00123', 100], ['00456', 100]])
            assert browser.post(current_path + '/publish', **publish_fields(current)).status == 303
        if concurrent_action in {'hide', 'publish_then_hide'}:
            current = browser.web.gradebook.review('come2201', current_path.rsplit('/', 1)[-1])
            assert browser.post(current_path + '/hide', revision=str(current['revision']), confirm='yes').status == 303
        with state._connection() as db:
            before = dict(db.execute("SELECT * FROM course_gradebook_state WHERE course_key='come2201'").fetchone())
        # The reviewed draft itself has not changed. The course publication CAS
        # must still reject replacement, hide and the null -> value -> null ABA.
        assert browser.web.gradebook.review('come2201', draft['draft_id'])['revision'] == draft['revision']
        fields = dict(reviewed_form['fields'], confirm='yes')
        status, _, body = send(server, path + '/publish', urlencode(fields).encode(),
                               'application/x-www-form-urlencoded', browser=browser)
        assert status == 409, body
        assert '다른 성적표가 공개되거나 비공개로 변경' in body.decode()
        with state._connection() as db:
            assert dict(db.execute("SELECT * FROM course_gradebook_state WHERE course_key='come2201'").fetchone()) == before
    assert browser.web.gradebook.review('come2201', draft['draft_id'])['status'] == 'draft'


def test_actual_http_large_gradebook_values_are_bounded_and_full_cell_preserves_context(gradebook):
    browser, *_ = gradebook
    long_value = '"' * 2000
    # Exercise maximum-length headers too: responsive table labels repeat them.
    headers = ['"' * 117 + f'{index:03d}' for index in range(100)]
    rows = [['학번', *headers]] + [[f'{index:05d}', *([long_value] * 100)] for index in range(1, 7)]
    path, entry = upload(browser, rows)
    facade = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=browser.web)
    selected = ','.join(column['key'] for column in entry['columns'])
    context = {'view': 'student', 'columns': selected, 'page': '2', 'change': 'all', 'q': ''}
    with serving(facade, WEB) as server:
        for query in ('', '?' + urlencode(context)):
            status, _, body = download(server, path + query, browser=browser)
            assert status == 200, body
            assert len(body) < 1024 * 1024
            html = body.decode()
            assert '페이지당 2명' in html and '전체 값 보기 (2000자)' in html
            assert escape(long_value) not in html
        status, _, body = download(server, path + '?' + urlencode(dict(context, cell='4:column_2')), browser=browser)
        assert status == 200
        html = body.decode()
        assert escape(long_value) in html
        assert '성적표 검토로 돌아가기' in html
        back = urlsplit(unescape(re.search(r'href="([^"]+)">성적표 검토로 돌아가기', html)[1]))
        assert back.path == path and back.fragment == 'gradebook-review'
        assert parse_qs(back.query, keep_blank_values=True) == {key: [value] for key, value in context.items()}
        assert '>00003<' in html
        # A selected-only preview cannot open a different, unselected cell.
        status, _, _ = download(server, path + '?view=student&columns=column_2&cell=4:column_3', browser=browser)
        assert status == 404
    stored = browser.web.gradebook.review('come2201', entry['draft_id'])
    assert stored['rows'][2]['values']['column_2'] == long_value
    assert stored['status'] == 'draft'
