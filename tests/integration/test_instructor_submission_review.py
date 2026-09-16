"""Source inspection uses real authenticated HTTP, immutable bundles and SQLite."""
import base64
import json
import io

import pytest

from test_course_portal import portal, claim, connect, request
from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.instructor_web import InstructorWeb
from autograde.submission_review import attention_reason

AUTH = 'Basic ' + base64.b64encode(('instructor:' + 'instructor' * 4).encode()).decode()
BASE = '/courses/come3105/instructor/submissions'


def submit(portal, tmp_path, files=None):
    web, api, _, _, store = portal
    _, session = connect(api, claim(web, 'come3105'))
    source = tmp_path / 'review-source'
    source.mkdir()
    for name, content in (files or {'main.cpp': b'int main() { return 0; }\n'}).items():
        (source / name).write_bytes(content)
    bundle = store.create_from_directory(source, kind='submission')
    status, _, raw = request(api, '/v1/assignments/asn_come3105/submissions', method='POST',
                            token=session['access_token'], raw=bundle.path.read_bytes())
    assert status == 202, raw
    return json.loads(raw)['submission']['submission_id'], session['access_token'], bundle


@pytest.mark.parametrize('admin_enabled', [False, True])
def test_review_auth_course_boundary_history_and_escaping(portal, tmp_path, admin_enabled):
    web, _, services, facade, store = portal
    if admin_enabled:
        service = services['come3105']
        facade.instructor = InstructorWeb(CourseAdminService(service.state), EnrollmentAdminService(service.state, b'p' * 32), None,
            authorize=service._authorize_instructor, secret=b'p' * 32, web_url='https://grade.example.edu:20010',
            submissions=lambda key, auth: services[key].instructor_dashboard_page(auth, portal=True),
            submission_review=lambda key, auth, sid, index: services[key].instructor_submission_page(auth, sid, index))
    sid, token, bundle = submit(portal, tmp_path, {'main.cpp': b'<script>alert("unsafe")</script>\n//\xe2\x80\xaeevil'})
    path = BASE + '/' + sid
    for endpoint in (path, path + '/files/0'):
        assert request(web, endpoint)[0] == 401
        assert request(web, endpoint, token=token)[0] == 401
        assert request(web, endpoint.replace('come3105', 'come2201'), authorization=AUTH)[0] == 404
    status, headers, raw = request(web, path, authorization=AUTH)
    assert status == 200 and b'main.cpp' in raw and bundle.digest.encode() in raw
    assert b'no-store' in headers['Cache-Control'].encode()
    assert b'grade.py' not in raw and str(store.root).encode() not in raw
    status, _, raw = request(web, path + '/files/0', authorization=AUTH)
    assert status == 200
    assert b'<script>' not in raw and b'&lt;script&gt;' in raw and b'\\u202e' in raw
    assert b'1 |' in raw
    assert request(web, path + '/files/42', authorization=AUTH)[0] == 404
    assert request(web, BASE + '/bsub_missing', authorization=AUTH)[0] == 404
    status, _, raw = request(web, BASE, authorization=AUTH)
    assert status == 200 and (path + '"').encode() in raw
    assert '확인 필요한 제출'.encode() in raw
    assert store.get(bundle.digest).path.read_bytes() == bundle.path.read_bytes()


@pytest.mark.parametrize('content,message', [
    (b'\x00binary', '바이너리'), (b'\xff\xfeinvalid', 'UTF-8'),
    (b'x' * (256 * 1024 + 1), '256 KiB'), (b'line\n' * 5001, '5,000줄'),
    (b'<' * (256 * 1024), '일부만 표시'),
    (b'', '줄 번호'),
])
def test_bounded_source_preview(portal, tmp_path, content, message):
    sid, _, _ = submit(portal, tmp_path, {'main.cpp': content})
    status, _, raw = request(portal[0], BASE + '/' + sid + '/files/0', authorization=AUTH)
    assert status == 200 and message.encode() in raw
    assert len(raw) < 1024 * 1024


def test_storage_failure_safe_and_auth_precedes_storage(portal, tmp_path, monkeypatch):
    sid, _, _ = submit(portal, tmp_path)
    calls = []
    def unavailable(digest):
        calls.append(digest)
        raise OSError('/private/location/must-not-leak')
    monkeypatch.setattr(portal[4], 'get', unavailable)
    path = BASE + '/' + sid + '/files/0'
    assert request(portal[0], path)[0] == 401 and calls == []
    status, _, raw = request(portal[0], path, authorization=AUTH)
    assert status == 503 and b'/private/location' not in raw
    assert '원본을 읽을 수 없습니다'.encode() in raw


def test_old_version_and_file_list_pagination(portal, tmp_path):
    sid, token, original = submit(portal, tmp_path)
    service = portal[2]['come3105']
    second = tmp_path / 'second-source'
    second.mkdir()
    for index in range(201):
        (second / f'file{index:03}.cpp').write_text(f'// version two {index}')
    artifact = portal[4].create_from_directory(second, kind='submission')
    newer = service.submit_bundle(token, 'second-review-submission', 'asn_come3105',
                                  io.BytesIO(artifact.path.read_bytes()), artifact.compressed_bytes)
    newer_id = newer['submission']['submission_id']
    status, _, raw = request(portal[0], BASE + '/' + newer_id, authorization=AUTH)
    assert status == 200 and sid.encode() in raw
    assert b'file199.cpp' in raw and b'file200.cpp' not in raw
    status, _, raw = request(portal[0], BASE + '/' + newer_id + '/files/200', authorization=AUTH)
    assert status == 200 and b'file200.cpp' in raw and b'// version two 200' in raw
    # Enrollment deactivation does not destroy instructor access to history.
    row = service.state.get_bundle_submission(sid)
    service.state.upsert_enrollment(student_id=row.student_id, course_key='come3105', active=False)
    status, _, raw = request(portal[0], BASE + '/' + sid + '/files/0', authorization=AUTH)
    assert status == 200 and b'int main()' in raw and b'version two' not in raw
    assert original.digest.encode() in raw


def test_corrupt_stored_source_is_not_displayed(portal, tmp_path):
    sid, _, artifact = submit(portal, tmp_path)
    artifact.path.chmod(0o600)  # Synthetic fixture: emulate storage corruption, not a normal write.
    artifact.path.write_bytes(b'not the original archive')
    status, _, raw = request(portal[0], BASE + '/' + sid + '/files/0', authorization=AUTH)
    assert status == 503 and b'int main()' not in raw


@pytest.mark.parametrize('state,score,maximum,expected', [
    ('infra_failed', None, 10, '처리 실패'), ('assessment_failed', None, 10, '처리 실패'),
    ('rejected', None, 10, '처리 실패'), ('published', 5, 10, '감점'),
    ('published', 10, 10, '채점 완료'), ('queued', None, 10, '처리 중'), (None, None, 10, '미제출'),
])
def test_attention_reasons_are_not_cheating_verdicts(state, score, maximum, expected):
    assert expected in attention_reason(state, score, maximum)
