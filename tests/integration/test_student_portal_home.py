"""Role entry and credential-bound student portal privacy through the web listener."""
from io import BytesIO
import re

from autograde.student_portal_data import submission_overview
from test_course_portal import portal, request, login, cookie, csrf, claim, connect, WEB


def portal_login(web, section='portal', *, course='come3105', student='20260001', password='000001'):
    status, headers, body = request(web, f'/courses/{course}/{section}')
    assert status == 200
    status, headers, body = request(web, f'/courses/{course}/{section}/login', method='POST', origin=WEB,
        cookie=cookie(headers), data={'csrf': csrf(body), 'student_key': student, 'password': password})
    assert status == 303
    assert headers['Location'] == f'/courses/{course}/{section}'
    token = cookie(headers)
    return token, request(web, headers['Location'], cookie=token)


def submit(portal, tmp_path, *, number, token, course='come3105', assignment_id=None, score=None, published=False):
    _web, _api, services, _portal, store = portal
    source = tmp_path / f'source-{course}-{number}'
    source.mkdir()
    (source / 'answer.txt').write_text(f'answer number {number}')
    archive = store.create_from_directory(source, kind='submission').path.read_bytes()
    submission = services[course].submit_bundle(token, f'portal-{course}-{number}', assignment_id or f'asn_{course}', BytesIO(archive), len(archive))['submission']
    sid = submission['submission_id']
    services[course].state.transition_bundle_submission(sid, 'queued')
    if score is not None:
        state = services[course].state
        state.transition_bundle_submission(sid, 'running')
        state.record_bundle_graded_result(sid, result_id=f'result-{course}-{number}', score=score, max_score=10,
            rubric={'private_test': {'feedback': 'PRIVATE_GRADER_DIAGNOSTIC'}}, diagnostics=[{'message': 'PRIVATE_GRADER_PATH'}])
        if published:
            state.publish_bundle_result(sid)
    return sid


def test_public_entry_has_role_destinations_without_private_details(portal):
    web, _, _, portal_web, _ = portal
    status, _, body = request(web, '/')
    assert status == 200 and b'href="/student"' in body
    assert b'href="/instructor"' not in body  # This fixture does not enable instructor web.
    assert b'20260001' not in body and b'asn_come3105' not in body
    for path in ['/student', '/courses']:
        status, _, body = request(web, path)
        assert status == 200 and b'/courses/come3105/portal' in body
        assert b'20260001' not in body and b'asn_come3105' not in body


def test_portal_login_shared_navigation_empty_state_and_claim_logout(portal):
    web, _, _, _, _ = portal
    token, (status, headers, body) = portal_login(web)
    assert status == 200 and '내 수업' in body.decode() and '다가오는 마감' in body.decode()
    assert b'aria-current="page"' in body and b'href="/courses/come3105/results"' in body
    assert b'href="/courses/come3105/materials"' not in body
    assert b'href="/courses/come3105/exam-papers"' not in body
    assert headers['Cache-Control'] == 'no-store'
    status, _, body = request(web, '/courses/come3105/results', cookie=token)
    assert status == 200 and '표시할 공식 제출이 없습니다' in body.decode()
    status, _, body = request(web, '/courses/come3105/assignments', cookie=token)
    assert status == 200 and b'name="assignment_id"' in body
    status, _, _ = request(web, '/courses/come3105/claims', method='POST', origin=WEB, cookie=token,
        data={'csrf': csrf(body), 'assignment_id': 'asn_come3105'})
    assert status == 200
    assert b'name="password"' in request(web, '/courses/come3105/results', cookie=token)[2]


def test_cross_course_cookie_invalid_origin_and_destination_csrf_are_rejected(portal):
    web, _, _, _, _ = portal
    token, _ = portal_login(web)
    assert b'name="password"' in request(web, '/courses/come2201/results', cookie=token)[2]
    assert request(web, '/courses/come3105/results?student_id=999', cookie=token)[0] == 400
    _, headers, body = request(web, '/courses/come3105/portal')
    payload = {'csrf': csrf(body), 'student_key': '20260001', 'password': '000001'}
    assert request(web, '/courses/come3105/portal/login', method='POST', origin='https://other.invalid', cookie=cookie(headers), data=payload)[0] == 403
    assert request(web, '/courses/come3105/results/login', method='POST', origin=WEB, cookie=cookie(headers), data=payload)[0] == 403
    assert request(web, '/courses/come3105/portal', method='DELETE', cookie=token)[0] == 405


def test_latest_waiting_receipt_stays_current_and_previous_best_is_separate(portal, tmp_path):
    web, api, services, _, _ = portal
    _, auth = connect(api, claim(web, 'come3105'))
    first = submit(portal, tmp_path, number=1, token=auth['access_token'], score=9, published=True)
    second = submit(portal, tmp_path, number=2, token=auth['access_token'])
    token, (status, _, body) = portal_login(web, 'results')
    assert status == 200
    text = body.decode()
    latest_card = re.search(f'data-submission-id="{second}"(.*?)</article>', text, re.S)[1]
    assert '채점 대기' in latest_card
    assert '이번 제출 점수</dt><dd class="portal-score">공개된 점수 없음' in latest_card
    assert '이전 제출 중 최고점</dt><dd>9 / 10점' in latest_card
    assert text.index(second) < text.index(first)
    assert 'PRIVATE_GRADER' not in text
    state = services['come3105'].state
    state.transition_bundle_submission(second, 'running')
    state.record_bundle_graded_result(second, result_id='result-resubmitted', score=3, max_score=10)
    assert '3 / 10점' not in request(web, '/courses/come3105/results', cookie=token)[2].decode()
    state.publish_bundle_result(second)
    latest_card = re.search(f'data-submission-id="{second}"(.*?)</article>', request(web, '/courses/come3105/results', cookie=token)[2].decode(), re.S)[1]
    assert '이번 제출 점수</dt><dd class="portal-score">3 / 10점' in latest_card
    assert '이전 제출 중 최고점</dt><dd>9 / 10점' in latest_card


def test_score_only_never_selects_private_feedback_or_another_students_results(portal, tmp_path):
    web, api, services, _, _ = portal
    state = services['come3105'].state
    original = state.get_bundle_assignment('asn_come3105')
    assignment_id = 'asn_portal_score_only'
    fields = ('course_key', 'starter_path', 'starter_digest', 'starter_size_bytes', 'assessment_path',
              'assessment_digest', 'runner_image', 'rubric_version', 'max_score', 'opens_at', 'due_at')
    state.register_bundle_assignment_release(assignment_id=assignment_id, assignment_key='score-only', release_id='v1',
        title='Practice <script>alert(1)</script>', result_policy='score_only', ready=True,
        **{key: getattr(original, key) for key in fields})
    def accepted_token(student):
        credential = state.get_student_password_credential(student_key=student, course_key='come3105')
        _, code = services['come3105'].create_authenticated_assignment_claim(credential, assignment_id)
        return connect(api, code)[1]['access_token']
    own = submit(portal, tmp_path, number=1, token=accepted_token('20260001'), assignment_id=assignment_id, score=7, published=True)
    other = state.upsert_local_student(student_key='20269999', auth_subject='local:20269999')
    state.upsert_enrollment(student_id=other.id, course_key='come3105')
    services['come3105'].set_student_password(student_key='20269999', password='123456')
    others = submit(portal, tmp_path, number=2, token=accepted_token('20269999'), assignment_id=assignment_id, score=10, published=True)
    _, (status, _, body) = portal_login(web, 'results')
    assert status == 200 and own.encode() in body and others.encode() not in body
    assert b'20269999' not in body and b'PRIVATE_GRADER' not in body
    assert '7 / 10점' in body.decode()
    assert b'<script>alert(1)</script>' not in body and b'&lt;script&gt;alert(1)&lt;/script&gt;' in body
    assert str(tmp_path).encode() not in body


def test_fractional_published_scores_keep_precision_in_latest_best_and_history(portal, tmp_path):
    web, api, _, _, _ = portal
    _, auth = connect(api, claim(web, 'come3105'))
    submit(portal, tmp_path, number=1, token=auth['access_token'], score=9.999999, published=True)
    current = submit(portal, tmp_path, number=2, token=auth['access_token'], score=9.87654321, published=True)
    _, (status, _, body) = portal_login(web, 'results')
    assert status == 200
    text = body.decode()
    card = re.search(f'data-submission-id="{current}"(.*?)</article>', text, re.S)[1]
    assert '이번 제출 점수</dt><dd class="portal-score">9.87654321 / 10점' in card
    assert '이전 제출 중 최고점</dt><dd>9.999999 / 10점' in card
    assert '공개 점수</dt><dd class="portal-score">9.999999 / 10점' in text
    assert '>10 / 10점<' not in text


def test_invalid_projected_scores_render_controlled_message_over_http(portal, tmp_path, monkeypatch):
    web, api, _, _, _ = portal
    _, auth = connect(api, claim(web, 'come3105'))
    submit(portal, tmp_path, number=1, token=auth['access_token'], score=7, published=True)
    token, _ = portal_login(web, 'results')
    import autograde.student_portal as adapter
    original = adapter.submission_overview
    bad_values = [float('nan'), 10]
    def malformed_projection(state, credential):
        result = original(state, credential)
        for row in result['latest'] + result['history']:
            row['score'], row['max_score'] = bad_values
            if 'previous_best_score' in row:
                row['previous_best_score'], row['previous_best_max_score'] = bad_values
        return result
    monkeypatch.setattr(adapter, 'submission_overview', malformed_projection)
    for score, maximum in [(float('nan'), 10), (float('inf'), 10), (7, float('inf')),
                           (7, None), (True, 10), (-1, 10), (11, 10), ('<script>bad()</script>', 10)]:
        bad_values[:] = [score, maximum]
        status, _, body = request(web, '/courses/come3105/results', cookie=token)
        assert status == 200 and '점수 확인 필요' in body.decode()
        assert b'nan /' not in body and b'inf /' not in body and b'<script>bad()' not in body


def test_hidden_and_inactive_assignments_do_not_appear_in_history(portal, tmp_path):
    web, api, services, _, _ = portal
    _, auth = connect(api, claim(web, 'come3105'))
    sid = submit(portal, tmp_path, number=1, token=auth['access_token'], score=8, published=True)
    token, _ = portal_login(web, 'results')
    services['come3105'].state.set_bundle_assignment_availability('asn_come3105', course_key='come3105', ready=False)
    assert sid.encode() not in request(web, '/courses/come3105/results', cookie=token)[2]
    services['come3105'].state.set_bundle_assignment_availability('asn_come3105', course_key='come3105', ready=True, active=False)
    assert sid.encode() not in request(web, '/courses/come3105/results', cookie=token)[2]


def test_credentials_rechecked_after_read_and_expired_sessions_show_login(portal, monkeypatch):
    web, _, services, portal_web, _ = portal
    token, _ = portal_login(web)
    import autograde.student_portal as adapter
    original = adapter.submission_overview
    def revoke_after_read(state, credential):
        result = original(state, credential)
        services['come3105'].set_student_password(student_key='20260001', password='654321')
        return result
    monkeypatch.setattr(adapter, 'submission_overview', revoke_after_read)
    status, _, body = request(web, '/courses/come3105/results', cookie=token)
    assert status == 403 and b'name="password"' in body
    monkeypatch.setattr(adapter, 'submission_overview', original)
    token, _ = portal_login(web, password='654321')
    now = portal_web.clock()
    portal_web.clock = lambda: now + 601
    assert b'name="password"' in request(web, '/courses/come3105/portal', cookie=token)[2]


def test_bounded_read_model_keeps_best_outside_visible_history(portal, tmp_path):
    web, api, services, _, _ = portal
    _, auth = connect(api, claim(web, 'come3105'))
    first = submit(portal, tmp_path, number=1, token=auth['access_token'], score=10, published=True)
    second = submit(portal, tmp_path, number=2, token=auth['access_token'], score=4, published=True)
    third = submit(portal, tmp_path, number=3, token=auth['access_token'])
    state = services['come3105'].state
    # Equal timestamp tie-breaking must follow receipt insertion order.
    with state._write() as db:
        db.execute('UPDATE bundle_submission_requests SET received_at=? WHERE submission_id IN (?,?,?)', ('2026-10-28T01:00:00.000000Z', first, second, third))
    credential = state.get_student_password_credential(student_key='20260001', course_key='come3105')
    result = submission_overview(state, credential, limit=1)
    assert result['history_truncated'] and len(result['history']) == 1
    assert result['latest'][0]['submission_id'] == third
    assert result['latest'][0]['previous_best_id'] == first
    assert result['latest'][0]['version'] == 3


def test_student_home_distinguishes_issued_code_from_accepted_assignment(portal):
    web, api, _, _, _ = portal
    code = claim(web, 'come3105')
    token, (status, _, body) = portal_login(web)
    assert status == 200 and '아직 IDE에서 수락한 실습이 없습니다' in body.decode()
    connect(api, code)
    status, _, body = request(web, '/courses/come3105/portal', cookie=token)
    assert status == 200 and b'data-accepted-assignment="asn_come3105"' in body
    assert '공식 제출 전' in body.decode()
    assert '원격 실행공간은 아직 설정되지 않았습니다' in body.decode()


def test_student_runtime_status_uses_credential_and_only_visible_accepted_assignments(portal):
    web, api, services, _, _ = portal
    connect(api, claim(web, 'come3105'))
    service = services['come3105']
    owner = service.state.get_student_password_credential(student_key='20260001', course_key='come3105')
    calls = []
    def spaces(credential):
        calls.append(credential)
        return {'enabled': True, 'status': 'ready', 'languages': ['c', 'cpp'], 'token': 'PRIVATE_TOKEN',
                'sessions': [{'assignment_id': 'asn_come3105', 'status': 'ready', 'checkpoint_id': 'saved_1',
                              'expires_at': '2026-10-28T00:00:00Z', 'student_key': 'NEVER_RENDER_RAW_KEY'},
                             {'assignment_id': 'another_course', 'status': 'ready', 'checkpoint_id': 'OTHER_CHECKPOINT'}]}
    service.learning_runtime_student_overview = spaces
    token, (status, _, body) = portal_login(web)
    assert status == 200 and b'data-runtime-state="ready"' in body
    assert '사용 가능' in body.decode() and b'saved_1' in body
    assert b'PRIVATE' not in body and b'NEVER_RENDER_RAW_KEY' not in body and b'OTHER_CHECKPOINT' not in body
    assert calls[-1].student_id == owner.student_id and calls[-1].course_key == 'come3105'
    service.state.set_bundle_assignment_availability('asn_come3105', course_key='come3105', ready=False)
    status, _, body = request(web, '/courses/come3105/portal', cookie=token)
    assert status == 200 and b'saved_1' not in body and b'data-accepted-assignment=' not in body


def test_student_runtime_outage_keeps_portal_access_and_does_not_claim_deletion(portal):
    from autograde.platform_service import PlatformAPIError
    web, _, services, _, _ = portal
    def unavailable(credential):
        raise PlatformAPIError(503, 'learning_runtime_unavailable', '/PRIVATE/worker.sock')
    services['come3105'].learning_runtime_student_overview = unavailable
    token, (status, _, body) = portal_login(web)
    assert status == 200 and b'data-runtime-state="unavailable"' in body
    assert '실행공간이 삭제되었다는 뜻은 아닙니다' in body.decode()
    assert b'PRIVATE' not in body and b'name="password"' not in body
    assert request(web, '/courses/come3105/results', cookie=token)[0] == 200


def test_student_runtime_read_rechecks_password_revocation_before_render(portal):
    web, _, services, _, _ = portal
    token, _ = portal_login(web)
    def revoke(credential):
        services['come3105'].set_student_password(student_key='20260001', password='654321')
        return {'enabled': True, 'status': 'ready', 'sessions': []}
    services['come3105'].learning_runtime_student_overview = revoke
    status, _, body = request(web, '/courses/come3105/portal', cookie=token)
    assert status == 403 and b'name="password"' in body
    assert b'data-runtime-state="ready"' not in body


def test_home_shows_latest_waiting_result_and_previous_best(portal, tmp_path):
    web, api, _, _, _ = portal
    _, auth = connect(api, claim(web, 'come3105'))
    submit(portal, tmp_path, number=1, token=auth['access_token'], score=9, published=True)
    latest = submit(portal, tmp_path, number=2, token=auth['access_token'])
    _, (status, _, body) = portal_login(web)
    assert status == 200
    card = re.search(f'data-submission-id="{latest}"(.*?)</article>', body.decode(), re.S)[1]
    assert '이번 제출 점수</dt><dd class="portal-score">공개된 점수 없음' in card
    assert '이전 제출 중 최고점</dt><dd>9 / 10점' in card


def test_remote_status_read_does_not_hold_shared_student_login_lock(portal):
    import threading
    web, _, services, portal_web, _ = portal
    acquired = []
    def worker_status(credential):
        def concurrent_login():
            got_lock = portal_web.lock.acquire(timeout=.3)
            acquired.append(got_lock)
            if got_lock:
                portal_web.lock.release()
        thread = threading.Thread(target=concurrent_login)
        thread.start()
        thread.join(timeout=1)
        return {'enabled': True, 'status': 'ready', 'sessions': []}
    services['come3105'].learning_runtime_student_overview = worker_status
    _, (status, _, _) = portal_login(web)
    assert status == 200 and acquired == [True]
