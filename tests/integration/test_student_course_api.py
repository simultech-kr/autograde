"""Real HTTP student shell projection: scoped cookies and current receipt only."""
import json

from test_course_portal import portal, request, cookie, claim, connect, WEB
from test_student_registration import member_login, PASSWORD
from test_student_portal_home import submit


def member(portal):
    web, _, services, facade, _ = portal
    credential = services['come3105'].state.get_student_password_credential(student_key='20260001', course_key='come3105')
    facade.members.store.register('20260001', PASSWORD, credential=credential)
    status, headers, _ = member_login(web, key='20260001')
    assert status == 303
    return cookie(headers)


def course(web, auth, key='come3105'):
    status, headers, raw = request(web, f'/student/api/course/{key}', cookie=auth)
    return status, headers, json.loads(raw)


def test_json_course_requires_member_and_active_enrollment(portal):
    web, _, services, _, _ = portal
    assert course(web, None)[0] == 401
    auth = member(portal)
    status, headers, body = course(web, auth)
    assert status == 200 and body['ok']
    assert headers['Cache-Control'] == 'no-store'
    assert body['result']['course']['course_key'] == 'come3105'
    assert body['result']['links'] == {'assignments': '/courses/come3105/assignments'}
    assert body['result']['overview']['assignments'][0]['assignment_id'] == 'asn_come3105'
    state = services['come3105'].state
    with state._write() as db:
        db.execute("UPDATE platform_enrollments SET active=0 WHERE course_key='come3105'")
    assert course(web, auth)[0] == 403
    assert course(web, auth, 'missing')[0] == 404


def test_claim_checks_origin_csrf_scope_and_preserves_common_login(portal):
    web, api, services, _, _ = portal
    auth = member(portal)
    payload = {'csrf': course(web, auth)[2]['result']['csrf'], 'assignment_id': 'asn_come3105'}
    path = '/student/api/course/come3105/claims'
    for origin in [None, 'null', 'https://untrusted.invalid', WEB.replace('20010', '20000')]:
        status, headers, body = request(web, path, method='POST', origin=origin, cookie=auth, data=payload)
        assert status == 403 and 'Set-Cookie' not in headers and json.loads(body)['ok'] is False
    assert request(web, path, method='POST', origin=WEB, cookie=auth, data={**payload, 'csrf': 'bad'})[0] == 403
    assert request(web, path, method='POST', origin=WEB, cookie=auth, data={**payload, 'assignment_id': 'asn_come2201'})[0] == 404
    status, _, body = request(web, path, method='POST', origin=WEB, cookie=auth, data=payload)
    assert status == 200
    result = json.loads(body)['result']['claim']
    assert result['assignment_id'] == payload['assignment_id'] and result['code'].startswith('AK1-')
    assert connect(api, result['code'])[1]['access_token']
    assert course(web, auth)[0] == 200
    services['come3105'].state.set_bundle_assignment_availability('asn_come3105', course_key='come3105', ready=False)
    assert request(web, path, method='POST', origin=WEB, cookie=auth, data=payload)[0] == 403


def test_latest_queued_and_private_grades_are_not_previous_best(portal, tmp_path):
    web, api, services, _, _ = portal
    _, tokens = connect(api, claim(web, 'come3105'))
    first = submit(portal, tmp_path, number=1, token=tokens['access_token'], score=9, published=True)
    second = submit(portal, tmp_path, number=2, token=tokens['access_token'])
    auth = member(portal)
    latest = course(web, auth)[2]['result']['overview']['latest'][0]
    assert latest['submission_id'] == second and latest['state'] == 'queued' and latest['score'] is None
    assert latest['previous_best_id'] == first and latest['previous_best_score'] == 9
    state = services['come3105'].state
    state.transition_bundle_submission(second, 'running')
    state.record_bundle_graded_result(second, result_id='private-result', score=3, max_score=10,
                                    rubric={'private': 'PRIVATE'}, diagnostics=[{'message': 'SECRET'}])
    result = course(web, auth)[2]['result']['overview']
    assert result['latest'][0]['score'] is None and result['latest'][0]['state'] == 'graded'
    assert 'PRIVATE' not in json.dumps(result) and 'SECRET' not in json.dumps(result)
    state.publish_bundle_result(second)
    latest = course(web, auth)[2]['result']['overview']['latest'][0]
    assert latest['score'] == 3 and latest['previous_best_score'] == 9


def test_student_api_is_not_on_ide_listener_and_rejects_selector_injection(portal):
    web, api, _, _, _ = portal
    auth = member(portal)
    assert request(api, '/student/api/course/come3105', cookie=auth)[0] == 404
    assert request(web, '/student/api/course/come3105?student_key=20269999', cookie=auth)[0] == 400
    assert request(web, '/student/api/course/come3105', method='DELETE', cookie=auth)[0] == 405
    assert request(web, '/student/api/course/come3105/claims', cookie=auth)[0] == 405
