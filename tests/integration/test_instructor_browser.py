"""Browser enhancement contracts using synthetic courses and real HTTP transport."""
import base64
import hashlib
import json
import re

from autograde.instructor_browser import SCRIPT, SCRIPT_CSP, SCRIPT_HASH
from autograde.platform_auth import sign_browser_value
from test_instructor_web import setup, AUTH, BASE, WEB, Forms
from test_instructor_upload_http import http_setup
from test_course_portal import request
from test_instructor_ux import Fields


def queued(assignments):
    draft = assignments.create_draft('come2201')
    job = assignments.queue_check('come2201', draft['draft_id'], draft['revision'], trusted_code_confirmed=True)
    return draft, job


def test_poll_is_scoped_read_only_and_does_not_renew_session(http_setup):
    server, browser, _, _, _, assignments = http_setup
    draft, job = queued(assignments)
    path = BASE + '/checks/' + job['job_id'] + '/status'
    cookie = '; '.join(f'{k}={v}' for k, v in browser.cookies.items())
    before = assignments.get_draft('come2201', draft['draft_id'])
    for _ in range(3):
        status, headers, body = request(server, path, authorization=AUTH, cookie=cookie)
        assert status == 200
        assert json.loads(body) == dict(job_id=job['job_id'], status='queued', revision=1, draft_revision=1)
        assert 'Set-Cookie' not in headers
        assert headers['Cache-Control'] == 'no-store'
        assert 'script-src' not in headers['Content-Security-Policy']
    assert assignments.get_draft('come2201', draft['draft_id']) == before
    assert request(server, path, cookie=cookie)[0] == 401
    assert request(server, path, authorization=AUTH)[0] == 403
    assert request(server, path.replace('come2201', 'come3105'), authorization=AUTH, cookie=cookie)[0] == 404
    expired = sign_browser_value(browser.web.secret, 'instructor-web', {'sid': 's'*43, 'csrf': 'c'*43}, lifetime_seconds=1, now=1)
    assert request(server, path, authorization=AUTH, cookie='autograde_instructor_web='+expired)[0] == 403


def test_csp_allows_only_fixed_instructor_script(http_setup):
    server, _, *_ = http_setup
    status, headers, body = request(server, BASE+'/assignments/new', authorization=AUTH)
    assert status == 200
    assert SCRIPT_CSP in headers['Content-Security-Policy']
    embedded = re.search(rb'<script data-instructor-enhancement>(.*?)</script>', body, re.S)[1]
    assert embedded == SCRIPT.encode()
    assert base64.b64encode(hashlib.sha256(embedded).digest()).decode() == SCRIPT_HASH
    script_directive = headers['Content-Security-Policy'].split('script-src ')[1].split(';')[0]
    assert script_directive == f"'sha256-{SCRIPT_HASH}'"
    _, student_headers, _ = request(server, '/courses/come2201/login')
    assert 'script-src' not in student_headers['Content-Security-Policy']


def test_guard_only_edit_forms_and_no_js_fallback(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='direct', negative_score=0, tests=[dict(title='one', input='', output='ok', weight=2, public=False)])
    path = BASE+'/drafts/'+draft['draft_id']
    forms = Forms(browser.get(path+'/step/3').body).forms
    assert len([form for form in forms if 'data-dirty-guard' in form]) == 3
    page = browser.get(path+'/step/3').body
    assert page.count('<details data-case ') == 50
    assert 'name="test_49_weight"' in page
    assert 'data-case-add hidden' in page
    assert all('data-dirty-guard' not in form for form in Forms(browser.get(path+'/step/4').body).forms)
    # Invalid negative score restores attempted deletion, not removed saved tests.
    result = browser.post(path, revision='1', tests_present='yes', negative_score='bad')
    assert result.status == 400 and 'data-recovered' in result.body
    assert Fields(result.body).inputs['test_0_title']['value'] == ''


def test_emit_actual_browser_fixtures(http_setup, tmp_path):
    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', mode='direct', negative_score=0, tests=[dict(title='one', input='input', output='ok', weight=2, public=False)])
    path = BASE+'/drafts/'+draft['draft_id']
    fixtures = {}
    for name, url in [('cases', path+'/step/3'), ('edit', path+'/step/1'), ('new', BASE+'/assignments/new')]:
        status, headers, body = request(server, url, authorization=AUTH)
        assert status == 200
        fixtures[name] = dict(url=WEB+url, html=body.decode(), csp=headers['Content-Security-Policy'])
    _, job = queued(assignments)
    url = BASE+'/checks/'+job['job_id']
    status, headers, body = request(server, url, authorization=AUTH)
    assert status == 200
    fixtures['poll'] = dict(url=WEB+url, html=body.decode(), csp=headers['Content-Security-Policy'],
                            data=dict(job_id=job['job_id'], revision=1, draft_revision=1, status='queued'))
    recovered = browser.post(path, revision='1', tests_present='yes', test_0_title='edited',
        test_0_input='', test_0_output='new', test_0_weight='4', negative_score='bad')
    assert recovered.status == 400
    fixtures['recovered'] = dict(url=WEB+path, html=recovered.body, csp=headers['Content-Security-Policy'])
    (tmp_path/'browser-fixtures.json').write_text(json.dumps(fixtures), encoding='utf-8')
