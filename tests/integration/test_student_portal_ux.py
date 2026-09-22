"""Student UX contracts through HTTP, using disposable synthetic students."""
from datetime import datetime, timedelta, timezone
import hashlib
import base64
import json
import re

from autograde.student_browser import SCRIPT, SCRIPT_CSP, SCRIPT_HASH
from autograde.student_portal_view import deadline, urgency
from test_course_portal import portal, request, login, cookie, csrf, WEB


def add_assignment(state, original, *, number, due_at, title=None, opens_at=None):
    fields = ('course_key', 'starter_path', 'starter_digest', 'starter_size_bytes',
              'assessment_path', 'assessment_digest', 'runner_image', 'rubric_version',
              'max_score', 'result_policy')
    return state.register_bundle_assignment_release(
        assignment_id=f'asn_ux_{number}', assignment_key=f'opaque-{20-number}', release_id='v1',
        title=title or f'Week{number:02} — 실습 과제', ready=True, due_at=due_at, opens_at=opens_at,
        **{field: getattr(original, field) for field in fields})


def test_initial_login_get_is_not_an_authentication_error_and_preserves_session(portal):
    web, _, _, _, _ = portal
    for path in ('/courses/come3105', '/courses/come3105/login', '/courses/come3105/assignments'):
        status, _, body = request(web, path)
        assert status == 200
        assert '학번과 전용 비밀번호를 확인' not in body.decode()
        assert b'role="alert"' not in body
    status, headers, _ = login(web, 'come3105')
    assert status == 200
    status, new_headers, body = request(web, '/courses/come3105/login', cookie=cookie(headers))
    assert status == 200 and '과제 선택' in body.decode()
    assert 'Set-Cookie' not in new_headers


def test_korean_time_and_urgency_boundary():
    assert deadline('2026-09-23T04:50:00.000000Z') == '2026년 9월 23일(수) 13:50 · 한국 시간'
    assert deadline('2026-09-23T13:50:00+09:00') == deadline('2026-09-23T04:50:00Z')
    assert deadline(None) == '미지정'
    assert '24시간 이내' in urgency('2026-09-23T04:50:00Z', '2026-09-22T04:50:00Z')
    assert '7일 이내' in urgency('2026-09-29T04:50:00Z', '2026-09-22T04:50:00Z')
    assert urgency('2026-09-22T04:50:00Z', '2026-09-22T04:50:00Z') == ''
    assert urgency(None, '2026-09-22T04:50:00Z') == ''


def test_deadline_order_no_deadline_last_and_unavailable_hidden(portal):
    web, _, services, _, _ = portal
    state = services['come3105'].state
    original = state.get_bundle_assignment('asn_come3105')
    now = datetime.now(timezone.utc)
    add_assignment(state, original, number=1, due_at=now + timedelta(hours=1), title='임박 <script>alert(1)</script>')
    add_assignment(state, original, number=2, due_at=now + timedelta(days=7))
    add_assignment(state, original, number=3, due_at=None)
    add_assignment(state, original, number=4, due_at=now - timedelta(seconds=1))
    add_assignment(state, original, number=5, due_at=None, opens_at=now + timedelta(days=1))
    _, headers, body = login(web, 'come3105')
    ids = re.findall(rb'name="assignment_id" value="([^"]+)"', body)
    assert ids == [b'asn_ux_1', b'asn_come3105', b'asn_ux_2', b'asn_ux_3']
    assert '한국 시간' in body.decode() and '마감 임박순' in body.decode()
    assert b'<script>alert(1)</script>' not in body
    assert b'&lt;script&gt;alert(1)&lt;/script&gt;' in body
    assert b'data-selected-assignment' in body and b'class="claim-action"' in body
    assert SCRIPT_CSP in headers['Content-Security-Policy']
    assert "'unsafe-inline'" not in headers['Content-Security-Policy'].split('script-src ')[1]
    assert base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode() == SCRIPT_HASH
    assert re.search(rb'<script data-student-enhancement>(.*?)</script>', body, re.S)[1] == SCRIPT.encode()


def test_claim_has_copy_expiry_ide_guidance_and_explicit_reauthentication(portal):
    web, _, _, portal_web, _ = portal
    _, headers, body = login(web, 'come3105')
    status, issued_headers, body = request(web, '/courses/come3105/claims', method='POST', origin=WEB,
        cookie=cookie(headers), data={'csrf': csrf(body), 'assignment_id': 'asn_come3105'})
    text = body.decode()
    assert status == 200 and 'Max-Age=0' in issued_headers['Set-Cookie']
    assert b'data-copy-target="claim-code"' in body and b'data-copy-target="api-address"' in body
    assert '웹 접속 주소와 다릅니다' in text and 'Visual Studio 2022 / 2026' in text
    assert '공용 PC 보호' in text and '다시 인증하여 새 코드 받기' in text
    expires_at = re.search(rb'data-claim-expires="([^"]+)"', body)[1].decode()
    assert 0 < (datetime.fromisoformat(expires_at.replace('Z', '+00:00')) - datetime.now(timezone.utc)).total_seconds() <= 600
    assert not portal_web.sessions
    assert SCRIPT_CSP in issued_headers['Content-Security-Policy']


def test_assignment_hidden_after_selection_explains_without_wrong_password_error(portal):
    web, _, services, _, _ = portal
    _, headers, body = login(web, 'come3105')
    services['come3105'].state.set_bundle_assignment_availability('asn_come3105', course_key='come3105', ready=False)
    status, result_headers, body = request(web, '/courses/come3105/claims', method='POST', origin=WEB,
        cookie=cookie(headers), data={'csrf': csrf(body), 'assignment_id': 'asn_come3105'})
    assert status == 403
    assert '선택한 실습을 지금 수령할 수 없습니다' in body.decode()
    assert '학번과 전용 비밀번호를 확인' not in body.decode()
    assert 'Set-Cookie' not in result_headers


def test_export_student_browser_fixtures(portal, tmp_path):
    web, _, services, _, _ = portal
    state = services['come3105'].state
    original = state.get_bundle_assignment('asn_come3105')
    for number in range(1, 10):
        add_assignment(state, original, number=number, due_at=datetime.now(timezone.utc) + timedelta(days=number + 1),
                       title=f'Week{number+4:02} — 문제상황: 여러 디자인 패턴을 조합하는 실습 과제 {number}')
    fixtures = {}
    status, headers, body = request(web, '/courses/come3105/login')
    fixtures['login'] = dict(html=body.decode(), csp=headers['Content-Security-Policy'])
    _, headers, body = login(web, 'come3105')
    fixtures['assignments'] = dict(html=body.decode(), csp=headers['Content-Security-Policy'])
    _, headers, body = request(web, '/courses/come3105/claims', method='POST', origin=WEB, cookie=cookie(headers),
                              data={'csrf': csrf(body), 'assignment_id': 'asn_come3105'})
    fixtures['claim'] = dict(html=body.decode(), csp=headers['Content-Security-Policy'])
    (tmp_path / 'student-browser-fixtures.json').write_text(json.dumps(fixtures), encoding='utf-8')
