"""Intentional resubmission, score history and read-only comparisons on synthetic data."""
import io
import json
from dataclasses import replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import http.client

import pytest

from test_course_portal import portal, claim, connect, request
from test_instructor_submission_review import AUTH, BASE
from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.instructor_web import InstructorWeb


def grade(state, sid, score, *, publish=True):
    state.transition_bundle_submission(sid, 'queued')
    state.transition_bundle_submission(sid, 'running')
    state.record_bundle_graded_result(sid, result_id='bres_' + sid, score=score, max_score=10)
    if publish:
        state.publish_bundle_result(sid)


def scenario(portal, tmp_path):
    web, api, services, _, store = portal
    _, session = connect(api, claim(web, 'come3105'))
    service = services['come3105']
    fixed = datetime.now(timezone.utc)
    service._now = lambda: fixed  # Exact timestamp ties must retain insertion order.
    def bundle(label, files):
        folder = tmp_path / label
        folder.mkdir()
        for name, data in files.items():
            (folder / name).write_bytes(data)
        return store.create_from_directory(folder, kind='submission')
    a = bundle('a', {'main.cpp': b'// original\nint main(){return 0;}\n', 'removed.h': b'// old'})
    b = bundle('b', {'main.cpp': b'// <script>inert</script>\nint main(){return 1;}\n', 'added.h': b'// new'})
    token = session['access_token']
    def send(artifact, key):
        return service.submit_bundle(token, key, 'asn_come3105', io.BytesIO(artifact.path.read_bytes()), artifact.compressed_bytes)
    return service, token, a, b, send, bundle


@pytest.mark.parametrize('admin_enabled', [False, True])
def test_a_b_a_latest_best_timeline_and_compare(portal, tmp_path, admin_enabled):
    service, token, a, b, send, _ = scenario(portal, tmp_path)
    state = service.state
    if admin_enabled:
        services = portal[2]
        portal[3].instructor = InstructorWeb(CourseAdminService(state), EnrollmentAdminService(state, b'p' * 32), None,
            authorize=service._authorize_instructor, secret=b'p' * 32, web_url='https://grade.example.edu:20010',
            submission_review=lambda key, auth, sid, index, **options: services[key].instructor_submission_page(auth, sid, index, **options))
    first = send(a, 'first')['submission']['submission_id']
    grade(state, first, 8)
    second = send(b, 'second')['submission']['submission_id']
    pending = service.get_submission(token, second)['submission']
    assert 'score' not in pending and pending['previous_best']['score'] == 8
    assert request(portal[1], f'/v1/submissions/{second}/result', token=token)[0] == 404
    grade(state, second, 3)
    third_response = send(a, 'third')
    third = third_response['submission']['submission_id']
    assert len({first, second, third}) == 3 and not third_response['replayed']
    assert send(a, 'third')['submission']['submission_id'] == third
    assert send(a, 'third')['replayed']
    assert service.list_assignments(token)['assignments'][0]['latest_submission']['submission_id'] == third
    assert service.get_bundle_history(token, 'asn_come3105')['submissions'][0]['submission_id'] == third
    grade(state, third, 10)
    assert service.get_result(token, third)['result']['score'] == 10
    assert service.get_result(token, third)['result']['previous_best']['score'] == 8
    assert service.get_result(token, second)['result']['previous_best']['score'] == 8  # Excludes newer 10.
    row, history = state.get_instructor_submission_review(course_key='come3105', submission_id=third)
    assert [h['version'] for h in history] == [3, 2, 1]
    assert history[0]['previous_score'] == 3 and history[1]['previous_score'] == 8
    assert row['source_digest'] == a.digest
    status, headers, html = request(portal[0], BASE + '/' + third, authorization=AUTH)
    assert status == 200 and 'no-store' in headers['Cache-Control']
    for expected in ('#3', '+7', '-5', '10 / 10', '직전 제출과 코드 비교', '이전 최고점'):
        assert expected.encode() in html
    (tmp_path / 'timeline.html').write_bytes(html)
    path = BASE + '/' + second + '/compare/' + first
    assert request(portal[0], path)[0] == 401
    assert request(portal[0], path, token=token)[0] == 401
    status, _, html = request(portal[0], path, authorization=AUTH)
    assert status == 200 and all(value.encode() in html for value in ('추가', '삭제', '수정'))
    # Sorted union is added.h, main.cpp, removed.h.
    status, _, html = request(portal[0], path + '/files/1', authorization=AUTH)
    assert status == 200 and b'&lt;script&gt;' in html and b'<script>' not in html
    assert b'-int main(){return 0;}' in html and b'+int main(){return 1;}' in html
    (tmp_path / 'comparison.html').write_bytes(html)
    assert request(portal[0], path.replace('come3105', 'come2201'), authorization=AUTH)[0] == 404
    assert request(portal[0], path + '/files/9999', authorization=AUTH)[0] == 404
    assert request(portal[0], BASE + '/' + first + '/compare/' + third, authorization=AUTH)[0] == 404
    # Same bytes, a fourth intentional attempt still appears in history.
    fourth = send(a, 'fourth')['submission']['submission_id']
    status, _, html = request(portal[0], BASE + '/' + fourth + '/compare/' + third, authorization=AUTH)
    assert status == 200 and '동일한 소스'.encode() in html


def test_unpublished_best_and_cross_student_compare_are_denied(portal, tmp_path):
    service, token, a, b, send, _ = scenario(portal, tmp_path)
    first = send(a, 'private')['submission']['submission_id']
    grade(service.state, first, 10, publish=False)
    second = send(b, 'new')['submission']['submission_id']
    assert service.get_submission(token, second)['submission']['previous_best'] is None
    other = service.state.upsert_local_student(student_key='other', auth_subject='local:other')
    service.state.upsert_enrollment(student_id=other.id, course_key='come3105')
    service.set_student_password(student_key='other', password='123456')
    _, session = connect(portal[1], claim(portal[0], 'come3105', '123456', 'other'))
    receipt = service.submit_bundle(session['access_token'], 'other-submit', 'asn_come3105', io.BytesIO(a.path.read_bytes()), a.compressed_bytes)
    sid = receipt['submission']['submission_id']
    assert service.get_submission(session['access_token'], sid)['submission']['previous_best'] is None
    assert request(portal[0], BASE + '/' + sid + '/compare/' + first, authorization=AUTH)[0] == 404


@pytest.mark.parametrize('data,notice', [(b'\x00binary', '바이너리'), (b'\xff', 'UTF-8'),
    (b'x' * (256 * 1024 + 1), '256 KiB'), (b'x\n' * 1001, '1,000줄')])
def test_comparison_limits(portal, tmp_path, data, notice):
    service, _, a, _, send, bundle = scenario(portal, tmp_path)
    first = send(a, 'first')['submission']['submission_id']
    second = send(bundle('large', {'main.cpp': data}), 'second')['submission']['submission_id']
    status, _, html = request(portal[0], BASE + '/' + second + '/compare/' + first + '/files/0', authorization=AUTH)
    assert status == 200 and notice.encode() in html
    assert len(html) < 1024 * 1024


def test_history_pagination_and_boundary_delta(portal, tmp_path):
    service, _, a, _, send, _ = scenario(portal, tmp_path)
    service.submission_policy = replace(service.submission_policy, max_daily_per_student=200)
    ids = []
    for index in range(102):
        sid = send(a, 'page-' + str(index))['submission']['submission_id']
        grade(service.state, sid, index % 11)
        ids.append(sid)
    row, history = service.state.get_instructor_submission_review(course_key='come3105', submission_id=ids[-1], offset=100)
    assert [h['version'] for h in history] == [2, 1]
    assert history[0]['previous_score'] == 0
    status, _, html = request(portal[0], BASE + '/' + ids[-1] + '/history/100', authorization=AUTH)
    assert status == 200 and b'#2' in html and b'#102' not in html
    assert request(portal[0], BASE + '/' + ids[-1] + '/history/100001', authorization=AUTH)[0] == 404


def test_concurrent_http_retry_one_receipt_then_new_key_new_receipt(portal, tmp_path):
    service, token, a, _, _, _ = scenario(portal, tmp_path)
    def post(key):
        connection = http.client.HTTPConnection(*portal[1].server_address[:2], timeout=20)
        try:
            connection.request('POST', '/v1/assignments/asn_come3105/submissions', a.path.read_bytes(),
                {'Authorization':'Bearer ' + token, 'Content-Type':'application/gzip', 'Idempotency-Key':key})
            response = connection.getresponse()
            assert response.status == 202
            return json.loads(response.read())
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(post, ['same-request'] * 8))
    assert len({r['submission']['submission_id'] for r in results}) == 1
    assert sum(not r['replayed'] for r in results) == 1
    assert post('intentional-new')['submission']['submission_id'] != results[0]['submission']['submission_id']
