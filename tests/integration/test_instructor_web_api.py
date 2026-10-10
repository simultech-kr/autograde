"""Read-only instructor API contracts, real HTTP, auth, and snapshot semantics."""
import base64
import json
from urllib.parse import urlencode

import pytest

from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_results_query import CourseResultsQuery
from autograde.instructor_web import InstructorWeb
from autograde.platform_auth import sign_browser_value
from test_course_portal import portal, request, cookie, SECRET, WEB
from test_instructor_submission_review import AUTH
from test_submission_timeline import scenario, grade


ROOT = '/instructor/api/v1'
RESULTS = ROOT + '/courses/come3105/results'
PASSWORD = 'synthetic-next-instructor-password'


@pytest.fixture
def api(portal):
    service = portal[2]['come3105']
    web = InstructorWeb(CourseAdminService(service.state), EnrollmentAdminService(service.state, SECRET), None,
                        authorize=service._authorize_instructor, secret=SECRET, web_url=WEB,
                        next_ui_policy=lambda: frozenset({'come3105', 'come2201'}))
    portal[3].instructor = web
    return portal, web


def read(api, path=RESULTS, *, authorization=AUTH, session=None, **query):
    suffix = '?' + urlencode(query) if query else ''
    status, headers, content = request(api[0][0], path + suffix, authorization=authorization, cookie=session)
    return status, headers, json.loads(content) if content else None


def assert_json(status, headers, payload, expected=200):
    assert status == expected, payload
    assert headers['Cache-Control'] == 'no-store'
    assert headers['Content-Type'].startswith('application/json')
    assert payload['contract_version'] == '1'
    assert payload['request_id'] == headers['X-Request-ID']
    assert len(payload['request_id']) == 32 and payload['server_time']


def test_entry_basic_cookie_not_login_safe_redirect_and_bootstrap(api):
    missing = read(api, ROOT + '/entry', authorization=None)
    assert_json(*missing, expected=401)
    assert missing[1]['WWW-Authenticate'].startswith('Basic realm=')
    status, headers, _ = request(api[0][0], ROOT + '/entry?screen=results&course_key=come3105', authorization=AUTH)
    assert status == 303 and headers['Location'] == '/app?course_key=come3105'
    assert headers['Cache-Control'] == 'no-store' and headers['X-Request-ID']
    assert all(value in headers['Set-Cookie'] for value in ('Path=/', 'Secure', 'HttpOnly', 'SameSite=Strict'))
    session = cookie(headers)
    assert read(api, ROOT + '/bootstrap', authorization=None, session=session)[0] == 401
    status, headers, data = read(api, ROOT + '/bootstrap', session=session)
    assert_json(status, headers, data)
    assert data['principal'] == {'id': 'shared', 'display_name': '공유 교수자', 'mode': 'shared'}
    assert len(data['csrf']) == 43 and data['capabilities'] == ['results.read']
    assert {c['course_key'] for c in data['courses']} == {'come3105', 'come2201'}
    assert {c['course_key'] for c in read(api, ROOT + '/courses')[2]['courses']} == {'come3105', 'come2201'}
    assert AUTH not in json.dumps(data) and 'password' not in json.dumps(data)
    for query in ({'next': 'https://attacker.invalid'}, {'return_to': '//attacker.invalid'},
                  {'screen': 'https://attacker.invalid'}, {'course_key': '//attacker.invalid'},
                  {'course_key': 'come3105\r\nLocation: https://attacker.invalid'}):
        rejected = read(api, ROOT + '/entry', **query)
        # Unknown fields/control characters may be rejected by the HTTP parser
        # before the application contract; either boundary must fail closed.
        assert rejected[0] == 400 and rejected[1]['Cache-Control'] == 'no-store'
        assert 'error' in rejected[2]
        assert 'Location' not in rejected[1]
    assert read(api, ROOT + '/entry', course_key='missing')[0] == 404
    # Read APIs authenticate Basic every time; no signed cookie can replace it.
    forged = 'autograde_instructor_web=' + sign_browser_value(SECRET, 'instructor-web',
        {'sid': 's' * 43, 'csrf': 'c' * 43}, lifetime_seconds=3600)
    assert read(api, authorization=None, session=forged)[0] == 401
    assert 'Set-Cookie' not in read(api, session=session)[1]


def test_disabled_by_default_every_request_rollback_and_get_only(api):
    portal, web = api
    web.next_ui_policy = lambda: frozenset()
    for path in (ROOT + '/entry', ROOT + '/bootstrap', ROOT + '/courses', RESULTS):
        disabled = read(api, path)
        assert_json(*disabled, expected=404)
        assert disabled[2]['error']['code'] == 'next_ui_disabled'
    default = InstructorWeb(web.courses, web.students, None, authorize=web.authorize, secret=SECRET, web_url=WEB)
    assert default.request('GET', ROOT + '/bootstrap', {}, {}, authorization=AUTH).body['error']['code'] == 'next_ui_disabled'
    allowed = {'come3105'}
    web.next_ui_policy = lambda: frozenset(allowed)
    session = cookie(read(api, ROOT + '/bootstrap')[1])
    assert read(api, session=session)[0] == 200
    assert read(api, RESULTS.replace('come3105', 'come2201'))[0] == 404
    allowed.clear()
    assert read(api, session=session)[2]['error']['code'] == 'next_ui_disabled'
    allowed.add('come3105')
    status, headers, body = request(portal[0], RESULTS, method='POST', authorization=AUTH,
                                    cookie=session, origin=WEB, data={})
    assert status == 405 and headers['Cache-Control'] == 'no-store'
    assert json.loads(body)['error']['code'] == 'method_not_allowed'
    assert headers['Allow'] == 'GET'
    # Internal callers receive the same safe denial for all unsupported methods.
    for method in ('PUT', 'PATCH', 'DELETE'):
        response = web.request(method, RESULTS, {}, {}, authorization=AUTH)
        assert response.status == 405 and response.body['error']['code'] == 'method_not_allowed'


def test_personal_acl_revocation_disable_and_password_change(api, tmp_path):
    identities = InstructorIdentityStore(tmp_path / 'next-identities.sqlite3')
    identities.initialize()
    identities.create('teacher', 'Scoped professor', 'instructor', PASSWORD)
    identities.set_grant('teacher', 'come3105', allowed=True)
    api[1].identities = identities
    auth = 'Basic ' + base64.b64encode(('teacher:' + PASSWORD).encode()).decode()
    status, headers, bootstrap = read(api, ROOT + '/bootstrap', authorization=auth)
    assert_json(status, headers, bootstrap)
    assert bootstrap['principal']['mode'] == 'personal'
    assert [c['course_key'] for c in bootstrap['courses']] == ['come3105']
    session = cookie(headers)
    assert read(api, RESULTS.replace('come3105', 'come2201'), authorization=auth, session=session)[0] == 404
    assert read(api, ROOT + '/entry', authorization=auth, course_key='come2201')[0] == 404
    identities.set_grant('teacher', 'come3105', allowed=False)
    assert read(api, authorization=auth, session=session)[0] == 404
    identities.set_active('teacher', False)
    denied = read(api, ROOT + '/bootstrap', authorization=auth, session=session)
    assert_json(*denied, expected=401)
    assert 'Autograde personal instructor' in denied[1]['WWW-Authenticate']
    identities.set_active('teacher', True)
    identities.set_grant('teacher', 'come3105', allowed=True)
    identities.set_password('teacher', PASSWORD + '-changed')
    assert read(api, authorization=auth, session=session)[0] == 401


def test_current_score_previous_published_best_and_no_sensitive_fields(api, tmp_path):
    service, token, a, b, send, _ = scenario(api[0], tmp_path)
    first = send(a, 'first')['submission']['submission_id']
    grade(service.state, first, 8)
    second = send(b, 'second')['submission']['submission_id']
    grade(service.state, second, 10, publish=False)
    provisional = read(api)[2]['rows'][0]
    assert provisional['state'] == 'graded' and provisional['score'] == 10
    assert provisional['previous_best']['score'] == 8
    third = send(a, 'third')['submission']['submission_id']
    response = read(api)
    assert_json(*response)
    row = response[2]['rows'][0]
    assert row['submission_id'] == third and row['state'] == 'accepted'
    assert row['score'] is None and row['max_score'] is None
    assert row['previous_best'] == {'submission_id': first, 'received_at': row['received_at'], 'score': 8.0, 'max_score': 10.0}
    assert row['detail_url'] == '/courses/come3105/instructor/submissions/' + third
    assert row['submission_count'] == 3
    assert response[2]['summary']['pending'] == 1 and response[2]['summary']['submission_attempts'] == 3
    for private in (token, AUTH, str(a.path), 'source_digest', 'assessment_path', 'password', 'diagnostics', 'rubric'):
        assert private not in json.dumps(response[2])
    grade(service.state, third, 3)
    graded = read(api)[2]
    latest = graded['rows'][0]
    assert latest['score'] == 3 and latest['previous_best']['score'] == 8
    assert latest['row_key'] == row['row_key'] and latest['attention'] is True
    assert graded['summary']['completed'] == 1 and graded['summary']['attention'] == 1
    assert graded['version'] != response[2]['version']
    # The latest published result never becomes its own previous best.
    assert latest['previous_best']['submission_id'] != third
    # Publication of an earlier provisional grade must invalidate the version.
    service.state.publish_bundle_result(second)
    assert read(api, version=graded['version'])[0] == 409
    assert read(api)[2]['rows'][0]['previous_best']['submission_id'] == second


def test_hidden_history_error_status_and_course_isolation(api, tmp_path):
    service, _, a, _, send, _ = scenario(api[0], tmp_path)
    sid = send(a, 'failed')['submission']['submission_id']
    service.state.transition_bundle_submission(sid, 'queued')
    service.state.transition_bundle_submission(sid, 'running')
    service.state.transition_bundle_submission(sid, 'infra_failed',
                                                failure_code='synthetic_failure', failure_message='Synthetic fixture')
    other_student = service.state.upsert_local_student(student_key='waiting', auth_subject='local:waiting')
    service.state.upsert_enrollment(student_id=other_student.id, course_key='come3105')
    before = read(api)[2]
    assert before['total'] == 2 and before['summary']['unsubmitted'] == 1
    failed = read(api, status='attention')[2]
    assert failed['total'] == 1 and failed['rows'][0]['state'] == 'infra_failed'
    assert failed['rows'][0]['score'] is None and failed['summary']['submitted'] == 1
    other = read(api, RESULTS.replace('come3105', 'come2201'))[2]
    assert sid not in json.dumps(other) and other['summary']['submitted'] == 0
    with service.state._write() as db:
        db.execute("UPDATE bundle_assignment_releases SET ready=0 WHERE assignment_id='asn_come3105'")
    hidden = read(api)[2]
    assert hidden['total'] == 1 and hidden['rows'][0]['assignment_ready'] is False
    assert hidden['assignments'][0]['unsubmitted'] is None
    assert hidden['summary']['unsubmitted'] == 0
    # Another course's visibility change has no effect on this course's version.
    assert read(api, RESULTS.replace('come3105', 'come2201'), version=other['version'])[0] == 200
    with service.state._write() as db:
        db.execute("UPDATE bundle_assignment_releases SET active=0 WHERE assignment_id='asn_come3105'")
    assert read(api, version=hidden['version'])[0] == 409
    assert read(api)[2]['rows'] == []


def test_auth_precedes_reader_and_unexpected_error_does_not_disclose_data(api, monkeypatch, caplog):
    calls = []

    def broken(*args):
        calls.append(True)
        raise RuntimeError('synthetic-sensitive-source-and-password')

    monkeypatch.setattr(CourseResultsQuery, 'read', broken)
    assert read(api, authorization=None)[0] == 401 and not calls
    failed = read(api)
    assert_json(*failed, expected=500)
    assert calls and failed[2]['error']['code'] == 'internal_error'
    assert 'synthetic-sensitive' not in json.dumps(failed[2])
    assert 'synthetic-sensitive' not in caplog.text
    assert failed[2]['request_id'] in caplog.text and 'RuntimeError' in caplog.text


def test_full_roster_pagination_filters_assignment_census_and_rollback(api):
    state = api[0][2]['come3105'].state
    for index in range(105):
        student = state.upsert_local_student(student_key=f'roster-{index:03}', auth_subject=f'local:roster-{index}')
        state.upsert_enrollment(student_id=student.id, course_key='come3105', active=index != 104)
    first = read(api, page_size=40)[2]
    assert first['total'] == 105 and len(first['rows']) == 40
    assert first['summary']['active_students'] == 105 and first['summary']['unsubmitted'] == 105
    assert first['assignments'][0]['students'] == 105 and first['assignments'][0]['unsubmitted'] == 105
    assert all(r['state'] == 'unsubmitted' and r['score'] is None and r['detail_url'] is None for r in first['rows'])
    pages = [first] + [read(api, page_size=40, page=page, version=first['version'])[2] for page in (2, 3)]
    keys = [row['row_key'] for page in pages for row in page['rows']]
    assert len(keys) == len(set(keys)) == 105
    assert read(api, page=4, page_size=40)[2]['rows'] == []
    filtered = read(api, student='roster-00', status='unsubmitted', sort='student')[2]
    assert filtered['total'] == 10 and filtered['summary']['active_students'] == 105
    assert read(api, student='%')[2]['total'] == 0
    assert read(api, status='submitted')[2]['total'] == 0
    assert read(api, assignment_id='asn_come2201')[0] == 404
    with state._write() as db:
        db.execute("UPDATE bundle_assignment_releases SET ready=0 WHERE assignment_id='asn_come3105'")
    assert read(api, version=first['version'])[2]['error']['code'] == 'result_set_changed'
    hidden = read(api)[2]
    assert hidden['rows'] == [] and hidden['assignments'] == [] and hidden['summary']['active_students'] == 105


@pytest.mark.parametrize('query', [
    {'page': '0'}, {'page': '-1'}, {'page': '1.5'}, {'page': '1000001'}, {'page_size': '101'},
    {'page_size': '0'}, {'sort': 'received_at; DROP TABLE'}, {'status': 'bogus'},
    {'student': 's' * 101}, {'version': 'broken'},
])
def test_query_bounds(api, query):
    rejected = read(api, **query)
    assert_json(*rejected, expected=400)
    assert rejected[2]['error']['code'] == 'invalid_query'


def test_one_snapshot_for_version_summary_rows_and_detect_later_row_shift(api, monkeypatch):
    state = api[0][2]['come3105'].state
    original = CourseResultsQuery._version
    inserted = []

    def mutate_after_snapshot(db, key):
        version = original(db, key)
        student = state.upsert_local_student(student_key='000-first', auth_subject='local:000-first')
        state.upsert_enrollment(student_id=student.id, course_key=key)
        inserted.append(student.id)
        return version

    monkeypatch.setattr(CourseResultsQuery, '_version', staticmethod(mutate_after_snapshot))
    before = read(api, page_size=1)[2]
    assert inserted and before['total'] == before['summary']['active_students'] == 1
    assert before['rows'][0]['student_key'] == '20260001'
    monkeypatch.setattr(CourseResultsQuery, '_version', staticmethod(original))
    conflict = read(api, page=2, page_size=1, version=before['version'])
    assert_json(*conflict, expected=409)
    assert conflict[2]['error']['code'] == 'result_set_changed'
    after = read(api, page_size=1)[2]
    assert after['total'] == after['summary']['active_students'] == 2
    assert after['rows'][0]['student_key'] == '000-first'
