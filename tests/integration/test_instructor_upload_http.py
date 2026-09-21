"""Real HTTP multipart/form limits; synthetic credentials and private examples."""
import http.client
import io
import zipfile
from urllib.parse import urlencode

import pytest

from autograde.instructor_upload import parse_instructor_upload
from autograde.platform_portal import CoursePortal
from test_course_portal import serving
from test_instructor_web import setup, AUTH, WEB, BASE


def multipart(fields, *, ending=True):
    boundary = 'autograde-synthetic-boundary'
    content = bytearray()
    for name, value in fields:
        content.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'.encode())
        if isinstance(value, bytes):
            content.extend(b'; filename="untrusted-name"')
        content.extend(b'\r\n\r\n')
        content.extend(value if isinstance(value, bytes) else value.encode())
        content.extend(b'\r\n')
    if ending:
        content.extend(f'--{boundary}--\r\n'.encode())
    return f'multipart/form-data; boundary={boundary}', bytes(content)


def send(server, path, body, content_type, *, browser, authorization=AUTH, origin=WEB, length=None):
    headers = {'Content-Type': content_type,
               'Cookie': '; '.join(f'{key}={value}' for key, value in browser.cookies.items())}
    if authorization is not None:
        headers['Authorization'] = authorization
    if origin is not None:
        headers['Origin'] = origin
    if length is not None:
        headers['Content-Length'] = str(length)
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    try:
        connection.request('POST', path, body, headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def download(server, path, *, browser, authorization=AUTH):
    headers = {'Cookie': '; '.join(f'{key}={value}' for key, value in browser.cookies.items())}
    if authorization is not None:
        headers['Authorization'] = authorization
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    try:
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.fixture
def http_setup(setup):
    browser, *rest = setup
    portal = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=browser.web)
    with serving(portal, WEB) as server:
        yield server, browser, *rest


def test_actual_csv_multipart_preview_then_apply(http_setup):
    server, browser, state, courses, students, assignments = http_setup
    mime, body = multipart([('csrf', browser.csrf), ('auto_generate', 'yes'),
                            ('file', b'student_key,active,password\n001,true,\n')])
    status, headers, html = send(server, BASE + '/students/import/preview', body, mime, browser=browser)
    assert status == 200, html
    assert b'/students/import/apply' in html
    assert headers['Referrer-Policy'] == 'same-origin'
    assert headers['Cache-Control'] == 'no-store'
    assert not students.list_students('come2201')


def test_actual_direct_starter_upload_and_multiline_metadata(http_setup):
    server, browser, state, courses, students, assignments = http_setup
    draft = assignments.create_draft('come2201', mode='direct', language='c', negative_score=0)
    url = BASE + '/drafts/' + draft['draft_id']
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('main.c', 'int main(void){return 0;}\n')
    mime, body = multipart([('csrf', browser.csrf), ('revision', '1'), ('starter_confirm', 'yes'), ('file', stream.getvalue())])
    status, _, html = send(server, url + '/uploads/starter', body, mime, browser=browser)
    assert status == 303, html
    assert assignments.get_draft('come2201', draft['draft_id'])['revision'] == 2
    # Normal HTML textareas submit CRLF; the old transport rejected all newlines.
    form = {'csrf': browser.csrf, 'name': 'Course', 'description': 'First line\r\nSecond line',
            'revision': '1'}
    status, _, html = send(server, BASE + '/update', urlencode(form).encode(),
                           'application/x-www-form-urlencoded', browser=browser)
    assert status == 303, html
    assert courses.get_course('come2201')['description'] == 'First line\nSecond line'


def test_authenticated_default_and_draft_template_downloads(http_setup):
    server, browser, _, _, _, assignments = http_setup
    status, headers, content = download(server, BASE + '/assignment-templates/cpp/windows.zip', browser=browser)
    assert status == 200
    assert headers['Content-Type'] == 'application/zip'
    assert headers['Content-Disposition'] == 'attachment; filename="autograde-starter-cpp-windows.zip"'
    assert headers['Cache-Control'] == 'no-store'
    assert headers['X-Autograde-SHA256']
    with zipfile.ZipFile(io.BytesIO(content)) as generated:
        assert set(generated.namelist()) == {'main.cpp', 'README.md', 'CMakeLists.txt'}

    draft = assignments.create_draft('come2201', mode='direct', language='c', description='초안 전용 설명', negative_score=0)
    status, headers, content = download(server, BASE + '/drafts/' + draft['draft_id'] + '/starter-template.zip', browser=browser)
    assert status == 200
    with zipfile.ZipFile(io.BytesIO(content)) as generated:
        assert generated.read('README.md').decode() == '초안 전용 설명\n'

    status, _, _ = download(server, BASE + '/assignment-templates/c/linux.zip', browser=browser, authorization=None)
    assert status == 401


@pytest.mark.parametrize('authorization,origin,expected', [(None, WEB, 401), ('Bearer student', WEB, 401),
                                                         (AUTH, None, 403), (AUTH, 'null', 403)])
def test_reject_auth_before_reading_oversized_upload(http_setup, authorization, origin, expected):
    server, browser, *_ = http_setup
    status, _, _ = send(server, BASE + '/students/import/preview', b'', 'multipart/form-data; boundary=x',
                         browser=browser, authorization=authorization, origin=origin, length=9 * 1024 * 1024)
    assert status == expected


def test_limits_and_truncated_upload_leave_no_enrollment(http_setup):
    server, browser, state, courses, students, _ = http_setup
    status, _, _ = send(server, BASE + '/students/import/preview', b'', 'multipart/form-data; boundary=x',
                         browser=browser, length=2 * 1024 * 1024)
    assert status == 413
    mime, body = multipart([('csrf', browser.csrf), ('file', b'private-invalid-csv')], ending=False)
    status, _, html = send(server, BASE + '/students/import/preview', body, mime, browser=browser)
    assert status == 400 and b'private-invalid-csv' not in html
    assert not students.list_students('come2201')


@pytest.mark.parametrize('fields', [
    [('file', b'a'), ('file', b'b')], [('csrf', 'a'), ('csrf', 'b'), ('file', b'c')],
    [('evil', 'a'), ('file', b'b')], [('file', 'not-a-file')], [('file', b'a' * 1025)],
])
def test_parser_rejects_duplicate_unknown_and_oversized_parts(fields):
    mime, body = multipart(fields)
    with pytest.raises(ValueError):
        parse_instructor_upload(mime, body, file_limit=1024)


def test_plaintext_student_form_limits_unchanged(http_setup):
    server, browser, *_ = http_setup
    status, _, _ = send(server, '/courses/come2201/login', urlencode({'password': 'bad\nvalue'}).encode(),
                         'application/x-www-form-urlencoded', browser=browser)
    assert status == 400
