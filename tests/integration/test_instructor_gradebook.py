"""Instructor Excel workflow, real multipart transport and course boundaries."""
import base64
from io import BytesIO
import re

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
    assert 'type="submit" disabled>선택 항목 학생 공개' in page.body
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
