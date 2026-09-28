"""Read-only result updates use the same scoped, escaped view as the initial page."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import io
import json
import re

import pytest

from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_web import InstructorWeb
from autograde.platform_auth import sign_browser_value
from autograde.platform_service import StudentPlatformService
from test_course_portal import portal, request, cookie, login, serving, WEB, API, SECRET
from test_instructor_submission_review import submit, AUTH, BASE
from test_submission_timeline import grade


LIVE = BASE + '/live'


class ResultRows(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.rows = []
        self.current = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'tr' and 'data-result-key' in values:
            self.current = dict(attrs=values, text='', cells=0)
            self.rows.append(self.current)
        elif self.current is not None and tag in {'th', 'td'}:
            self.current['cells'] += 1

    def handle_endtag(self, tag):
        if tag == 'tr':
            self.current = None

    def handle_data(self, value):
        if self.current is not None:
            self.current['text'] += value


def install_admin(portal, *, identities=None):
    services = portal[2]
    service = services['come3105']
    portal[3].instructor = InstructorWeb(
        CourseAdminService(service.state), EnrollmentAdminService(service.state, SECRET), None,
        authorize=service._authorize_instructor, secret=SECRET, web_url=WEB,
        submissions=lambda key, auth: services[key].instructor_dashboard_page(auth, portal=True),
        submission_updates=lambda key, auth: services[key].instructor_dashboard_updates(auth),
        identities=identities)


@pytest.fixture(params=[False, True], ids=['legacy', 'admin'])
def results_portal(portal, request):
    if request.param:
        install_admin(portal)
    return portal


def open_results(portal, *, authorization=AUTH):
    status, headers, body = request(portal[0], BASE, authorization=authorization)
    assert status == 200, body
    session = cookie(headers) if 'Set-Cookie' in headers else None
    return session, headers, body.decode()


def updates(portal, session=None, *, authorization=AUTH, path=LIVE):
    status, headers, body = request(portal[0], path, authorization=authorization, cookie=session)
    assert status == 200, body
    return headers, json.loads(body)


def test_live_authentication_read_only_course_scope_and_method(results_portal, tmp_path):
    portal = results_portal
    sid, token, artifact = submit(portal, tmp_path)
    service = portal[2]['come3105']
    fixed = datetime.now(timezone.utc)
    service._now = lambda: fixed
    original = service.state.get_bundle_submission(sid)
    original_bytes = artifact.path.read_bytes()
    session, _, initial = open_results(portal)
    assert 'data-live-results' in initial and 'data-results-content' in initial
    for _ in range(2):
        headers, data = updates(portal, session)
        assert set(data) == {'course_key', 'generated_at', 'html'}
        assert data['course_key'] == 'come3105'
        assert data['html'] in initial
        assert headers['Content-Type'].startswith('application/json')
        assert headers['Cache-Control'] == 'no-store'
        assert 'Set-Cookie' not in headers
        assert 'script-src' not in headers['Content-Security-Policy']
        assert '<script' not in data['html']
        for private in (token, AUTH, 'instructor' * 4, 'password_hash', str(artifact.path), 'int main()'):
            assert private not in json.dumps(data)
    assert service.state.get_bundle_submission(sid) == original
    assert artifact.path.read_bytes() == original_bytes
    assert request(portal[0], LIVE, cookie=session)[0] == 401
    assert request(portal[0], LIVE, cookie=session, token=token)[0] == 401
    assert request(portal[0], LIVE, method='POST', cookie=session, authorization=AUTH,
                   data={}, origin=WEB)[0] == 405
    assert request(portal[0], LIVE.replace('come3105', 'missing'), cookie=session, authorization=AUTH)[0] == 404
    _, other = updates(portal, session, path=LIVE.replace('come3105', 'come2201'))
    assert other['course_key'] == 'come2201'
    assert sid not in other['html'] and 'asn_come3105' not in other['html']


def test_live_admin_cookie_required_and_never_renewed(portal):
    install_admin(portal)
    assert request(portal[0], LIVE, authorization=AUTH)[0] == 403
    _, student_headers, _ = login(portal[0], 'come3105')
    assert request(portal[0], LIVE, authorization=AUTH, cookie=cookie(student_headers))[0] == 403
    expired = sign_browser_value(SECRET, 'instructor-web', {'sid': 's' * 43, 'csrf': 'c' * 43},
                                 lifetime_seconds=1, now=1)
    for value in ('tampered', expired):
        assert request(portal[0], LIVE, authorization=AUTH,
                       cookie='autograde_instructor_web=' + value)[0] == 403
    session, headers, _ = open_results(portal)
    assert all(flag in headers['Set-Cookie'] for flag in ('Secure', 'HttpOnly', 'SameSite=Strict'))
    assert session.startswith('autograde_instructor_web=')
    assert 'Set-Cookie' not in updates(portal, session)[0]


@pytest.mark.parametrize('course_key', ['CSE101', 'cse.101', 'cse~101', 'c' * 97],
                         ids=['uppercase', 'dot', 'tilde', '97-characters'])
def test_legacy_live_route_accepts_existing_course_key_formats(portal, course_key):
    fixed = datetime.now(timezone.utc)
    portal[2][course_key] = StudentPlatformService(
        state=portal[2]['come3105'].state, server_secret=SECRET, course_key=course_key,
        public_base_url=API, bundle_store=portal[4], instructor_token='instructor' * 4,
        now=lambda: fixed)
    base = f'/courses/{course_key}/instructor'
    status, _, body = request(portal[0], base, authorization=AUTH)
    assert status == 200
    page = body.decode()
    live = base + '/submissions/live'
    assert f'data-live-results="{live}"' in page
    status, headers, body = request(portal[0], live, authorization=AUTH)
    assert status == 200
    data = json.loads(body)
    assert data['course_key'] == course_key and data['html'] in page
    assert headers['Cache-Control'] == 'no-store'


def test_live_resubmission_promotes_latest_pending_without_previous_score(results_portal, tmp_path):
    portal = results_portal
    service = portal[2]['come3105']
    unsubmitted = service.state.upsert_local_student(student_key='00000001', auth_subject='local:waiting')
    service.state.upsert_enrollment(student_id=unsubmitted.id, course_key='come3105')
    fixed = datetime.now(timezone.utc)
    service._now = lambda: fixed
    sid, token, original = submit(portal, tmp_path)
    grade(service.state, sid, 8)
    session, _, initial = open_results(portal)
    first = ResultRows(initial).rows
    assert len(first) == 2
    assert all(row['cells'] == 6 for row in first)
    assert '20260001' in first[0]['text'] and '8 / 10' in first[0]['text']
    assert '00000001' in first[1]['text']
    assert [row['attrs']['data-submitted'] for row in first] == ['yes', 'no']
    assert first[0]['attrs']['data-pending'] == 'no'
    assert first[0]['attrs']['data-attention'] == 'yes'
    service._now = lambda: fixed + timedelta(minutes=1)
    submitted = service.submit_bundle(token, 'live-intentional-resubmission', 'asn_come3105',
                                     io.BytesIO(original.path.read_bytes()), original.compressed_bytes)
    newer = submitted['submission']['submission_id']
    _, payload = updates(portal, session)
    current = ResultRows(payload['html']).rows
    assert [row['attrs']['data-result-key'] for row in current] == [row['attrs']['data-result-key'] for row in first]
    assert newer in payload['html'] and sid not in payload['html']
    assert current[0]['attrs']['data-pending'] == 'yes'
    assert '8 / 10' not in current[0]['text']
    assert 'accepted' not in current[0]['text'] and '제출 접수' in current[0]['text']
    assert '채점 중' in current[0]['text']
    fresh = open_results(portal)[2]
    assert payload['html'] in fresh
    grade(service.state, newer, 10)
    _, finished = updates(portal, session)
    final = ResultRows(finished['html']).rows[0]
    assert final['attrs']['data-pending'] == 'no'
    assert final['attrs']['data-attention'] == 'no'
    assert '10 / 10' in final['text'] and '채점 완료' in final['text']


def test_live_ordering_ties_and_untrusted_text_are_identical_to_initial_page(portal, monkeypatch):
    service = portal[2]['come3105']
    dashboard = deepcopy(service.instructor_dashboard(AUTH))
    template = dashboard['rows'][0]
    attack = '<img src=x onerror="alert(1)">'
    dashboard['rows'] = [
        dict(template, student_key='000-unsubmitted'),
        dict(template, student_key='100-older', latest_submission_id='bsub_older', state='queued',
             latest_received_at='2026-09-28T01:00:00+00:00', submission_count=1),
        dict(template, student_key='200-tied', latest_submission_id='bsub_tie_a', state='queued',
             latest_received_at='2026-09-28T02:00:00+00:00', submission_count=1),
        dict(template, student_key='300-tied', latest_submission_id='bsub_tie_b', state='queued',
             latest_received_at='2026-09-28T02:00:00+00:00', submission_count=1),
        dict(template, student_key='400-newest', title=attack, latest_submission_id='bsub_newest', state='queued',
             latest_received_at='2026-09-28T03:00:00+00:00', submission_count=1),
    ]
    monkeypatch.setattr(service, 'instructor_dashboard', lambda _: dashboard)
    _, _, page = open_results(portal)
    _, payload = updates(portal)
    assert payload['html'] in page
    rows = ResultRows(payload['html']).rows
    expected = ['400-newest', '200-tied', '300-tied', '100-older', '000-unsubmitted']
    assert len(rows) == len(expected)
    assert all(key in row['text'] for key, row in zip(expected, rows))
    assert len({row['attrs']['data-result-key'] for row in rows}) == len(rows)
    assert attack not in payload['html'] and '&lt;img src=x onerror=' in payload['html']
    assert attack in rows[0]['text']
    assert all('onerror' not in row['attrs'] for row in rows)
    assert rows[-1]['attrs']['data-submitted'] == 'no'


def test_live_csp_allows_only_fixed_results_script_on_results_html(results_portal):
    from autograde.instructor_results_browser import SCRIPT

    session, headers, page = open_results(results_portal)
    scripts = re.findall(r'<script\b[^>]*>(.*?)</script>', page, re.S)
    assert scripts.count(SCRIPT) == 1
    digest = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    policy = headers['Content-Security-Policy']
    assert f"'sha256-{digest}'" in policy
    directive = policy.split('script-src ', 1)[1].split(';', 1)[0]
    assert "'unsafe-inline'" not in directive and "'unsafe-eval'" not in directive
    assert 'script-src' not in updates(results_portal, session)[0]['Content-Security-Policy']
    status, student_headers, student_page = request(results_portal[0], '/courses/come3105')
    assert status == 200
    assert digest not in student_headers['Content-Security-Policy']
    assert SCRIPT.encode() not in student_page


def test_single_service_live_route_authentication_fragment_and_csp(portal):
    from autograde.instructor_results_browser import SCRIPT, SCRIPT_HASH

    service = portal[2]['come3105']
    fixed = datetime.now(timezone.utc)
    service._now = lambda: fixed
    path = '/v1/instructor/dashboard/live'
    with serving(service, API) as server:
        for protected in ('/instructor', path):
            assert request(server, protected)[0] == 401
            assert request(server, protected, token='synthetic-student-token')[0] == 401
        status, headers, body = request(server, '/instructor', authorization=AUTH)
        assert status == 200
        page = body.decode()
        assert f'data-live-results="{path}"' in page
        assert re.findall(r'<script\b[^>]*>(.*?)</script>', page, re.S).count(SCRIPT) == 1
        directive = headers['Content-Security-Policy'].split('script-src ', 1)[1].split(';', 1)[0]
        assert directive == f"'sha256-{SCRIPT_HASH}'"
        assert headers['Cache-Control'] == 'no-store'

        status, headers, body = request(server, path, authorization=AUTH)
        assert status == 200
        data = json.loads(body)
        assert set(data) == {'course_key', 'generated_at', 'html'}
        assert data['course_key'] == 'come3105' and data['html'] in page
        assert headers['Content-Type'].startswith('application/json')
        assert headers['Cache-Control'] == 'no-store'
        assert 'Set-Cookie' not in headers
        assert 'script-src' not in headers['Content-Security-Policy']
        assert '<script' not in data['html']


def test_live_personal_identity_and_grant_changes_are_rechecked(portal, tmp_path):
    identities = InstructorIdentityStore(tmp_path / 'live-identities.sqlite3')
    identities.initialize()
    password = 'synthetic-live-password-2026'
    for username in ('teacher', 'colleague'):
        identities.create(username, username, 'instructor', password)
        identities.set_grant(username, 'come3105', allowed=True)
    for service in portal[2].values():
        service._instructor_authorizer = identities.authorize_course
    install_admin(portal, identities=identities)

    def authorization(username):
        return 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()

    auth = authorization('teacher')
    session, _, _ = open_results(portal, authorization=auth)
    assert updates(portal, session, authorization=auth)[1]['course_key'] == 'come3105'
    assert request(portal[0], LIVE, cookie=session, authorization=authorization('colleague'))[0] == 403
    assert request(portal[0], LIVE.replace('come3105', 'come2201'), cookie=session, authorization=auth)[0] == 404
    identities.set_grant('teacher', 'come3105', allowed=False)
    assert request(portal[0], LIVE, cookie=session, authorization=auth)[0] in (403, 404)
    identities.set_active('teacher', False)
    assert request(portal[0], LIVE, cookie=session, authorization=auth)[0] == 401
