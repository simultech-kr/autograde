"""Real multipart requests for Git content authoring, using synthetic files only."""
import hashlib

import pytest

from autograde.content_store import ContentService, ContentStorageError
from test_instructor_upload_http import multipart, send
from test_student_material_http import COURSE, material_http, material_multipart, revision


BASE = f'/courses/{COURSE}/instructor/content'


@pytest.fixture
def content_http(material_http, tmp_path):
    env = material_http
    env.content = ContentService(env.state, tmp_path / 'content', clock=lambda: env.clock[0])
    env.instructor.content_service = env.content
    assert env.admin.get(BASE)[0] == 200
    response = env.admin.post(BASE, title='Synthetic content', instructions='Read first')
    assert response[0] == 303, response[2]
    env.path = response[1]['Location']
    env.collection_id = env.path.rsplit('/', 1)[1]
    yield env


def body_with_path(csrf, rev, filename, data, logical_path=None):
    mime, body = material_multipart(csrf, rev, filename, data)
    if logical_path is not None:
        boundary = mime.split('boundary=', 1)[1]
        field = (f'--{boundary}\r\nContent-Disposition: form-data; name="logical_path"\r\n\r\n'
                 f'{logical_path}\r\n').encode('utf-8')
        body = field + body
    return mime, body


def submit(env, *, logical_path=None, filename='README.md', data=b'# Read first\n', csrf=None, **options):
    mime, body = body_with_path(env.admin.csrf if csrf is None else csrf, revision(env, env.path),
                               filename, data, logical_path)
    return send(env.server, env.path + '/upload', body, mime, browser=env.admin, **options)


@pytest.mark.parametrize('logical_path,expected', [(None, 'README.md'), ('', 'README.md'), ('week04/README.md', 'week04/README.md')])
def test_actual_multipart_filename_fallback_and_relative_path(content_http, logical_path, expected):
    env = content_http
    data = b'# Synthetic 25 KiB material\n' + b'x' * (25 * 1024)
    before = revision(env, env.path)
    response = submit(env, logical_path=logical_path, data=data)
    assert response[0] == 303, response[2]
    collection = env.content.get(COURSE, env.collection_id)
    assert collection['revision'] == before + 1
    assert collection['files'] == [dict(path=expected, sha256=hashlib.sha256(data).hexdigest(),
                                        size_bytes=len(data), media_type='text/markdown')]
    assert env.content._read_object(COURSE, collection['files'][0]) == data
    assert not collection['current_release_id']  # Upload does not publish.


def test_actual_multipart_forged_csrf_does_not_store_bytes(content_http, monkeypatch):
    env = content_http
    calls = []
    original = env.content._store_object

    def spy(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(env.content, '_store_object', spy)
    response = submit(env, logical_path='week04/README.md', csrf='forged-csrf', data=b'private unaccepted input')
    assert response[0] == 403
    assert not calls and not env.content.root.exists()
    assert env.content.get(COURSE, env.collection_id)['files'] == []


@pytest.mark.parametrize('logical_path', ['../README.md', '/README.md', 'notes\\README.md'])
def test_actual_multipart_rejects_unsafe_path_without_storage(content_http, logical_path):
    env = content_http
    response = submit(env, logical_path=logical_path)
    assert response[0] == 400
    assert not env.content.root.exists()
    assert env.content.get(COURSE, env.collection_id)['files'] == []


def test_content_path_field_is_not_accepted_by_legacy_material_upload(content_http):
    env = content_http
    mime, body = multipart([('csrf', env.admin.csrf), ('revision', '1'), ('logical_path', 'README.md'), ('file', b'# Input')])
    legacy_path = f'/courses/{COURSE}/instructor/materials/mat_' + 'a' * 32 + '/upload'
    response = send(env.server, legacy_path, body, mime, browser=env.admin)
    assert response[0] == 400
    assert not env.content.root.exists()


def test_actual_upload_storage_fault_returns_safe_503(content_http, monkeypatch):
    env = content_http

    def broken(*args, **kwargs):
        raise ContentStorageError('synthetic private /teacher/secret')

    monkeypatch.setattr(env.content, 'upload', broken)
    response = submit(env, logical_path='README.md')
    assert response[0] == 503
    assert '콘텐츠 저장소 확인 필요'.encode() in response[2]
    assert b'/teacher/secret' not in response[2] and b'synthetic private' not in response[2]
    assert env.content.get(COURSE, env.collection_id)['files'] == []
