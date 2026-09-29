"""Bulk instructor actions use reviewed selections and synthetic releases only."""
import base64
from html.parser import HTMLParser
import time
from urllib.parse import urlencode

import pytest

from autograde.instructor_identity import InstructorIdentityStore
from autograde.platform_service import PlatformAPIError
from test_instructor_assignment_deletion import published
from test_instructor_upload_http import http_setup, send
from test_instructor_ux import release
from test_instructor_web import AUTH, BASE, WEB, Browser, Forms, setup


PREVIEW = BASE + '/assignments/bulk/preview'
APPLY = BASE + '/assignments/bulk/apply'


class Controls(HTMLParser):
    """Keep ordinary form controls, including the owning form, without running JS."""

    def __init__(self, body):
        super().__init__()
        self.controls = []
        self.action = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.action = attrs.get('action')
        elif tag in {'input', 'button'}:
            self.controls.append(dict(attrs, tag=tag, form_action=self.action))

    def handle_endtag(self, tag):
        if tag == 'form':
            self.action = None


def selections(*ids):
    return {'selected_' + assignment_id: 'yes' for assignment_id in ids}


def ticket(response):
    assert response.status == 200
    form = next(form for form in Forms(response.body).forms if form['action'] == APPLY)
    assert form['method'] == 'post'
    assert set(form['fields']) == {'csrf', 'ticket'}
    return form['fields']['ticket']


def visibility(browser, assignment_id, course='come2201'):
    return browser.web.catalog.get_release(course, assignment_id)['visibility']


def test_bulk_archive_then_delete_requires_review_and_preserves_unselected(setup):
    browser, state, _, _, assignments = setup
    web, draft = published(setup, assignment_id='web_selected')
    cli = release(state, assignment_id='cli_selected')
    kept = release(state, assignment_id='not_selected')
    selected = selections(web.assignment_id, cli.assignment_id)
    preview = browser.post(PREVIEW, action='archive', **selected)
    reviewed = ticket(preview)
    assert '웹 보관 삭제 &lt;과제&gt;' in preview.body
    assert 'CLI &lt;Lab&gt;' in preview.body
    assert '/synthetic/private' not in preview.body
    for item in (web, cli, kept):
        assert state.get_bundle_assignment(item.assignment_id).active

    assert browser.post(APPLY, ticket=reviewed).status == 400
    assert all(state.get_bundle_assignment(item.assignment_id).active for item in (web, cli))
    assert browser.post(APPLY, ticket=reviewed, confirm='yes').status in (200, 303)
    assert [visibility(browser, item.assignment_id) for item in (web, cli, kept)] == ['inactive', 'inactive', 'open']

    delete_preview = browser.post(PREVIEW, action='delete', **selected)
    assert [visibility(browser, item.assignment_id) for item in (web, cli)] == ['inactive', 'inactive']
    assert browser.post(APPLY, ticket=ticket(delete_preview), confirm='yes').status in (200, 303)
    assert [visibility(browser, item.assignment_id) for item in (web, cli, kept)] == ['deleted', 'deleted', 'open']
    assert assignments.get_draft('come2201', draft['draft_id'])['published_assignment_id'] == web.assignment_id
    assert state.get_bundle_assignment(web.assignment_id).due_at == web.due_at
    assert state.get_bundle_assignment(cli.assignment_id).assignment_id == cli.assignment_id


def test_selection_uses_normal_form_controls_without_javascript(setup):
    browser, state, _, _, assignments = setup
    active = release(state, assignment_id='ordinary_checkbox')
    archived = release(state, assignment_id='archived_checkbox')
    deleted = release(state, assignment_id='deleted_not_selectable')
    draft = assignments.create_draft('come2201', title='Draft is not a release')
    assignments.archive_release('come2201', archived.assignment_id)
    assignments.archive_release('come2201', deleted.assignment_id)
    assignments.delete_release('come2201', deleted.assignment_id)
    page = browser.get(BASE + '/assignments')
    form = next(form for form in Forms(page.body).forms if form['action'] == PREVIEW)
    assert form['method'] == 'post' and form['fields']['csrf'] == browser.csrf
    controls = [control for control in Controls(page.body).controls if control['form_action'] == PREVIEW]
    checkboxes = [control for control in controls if control.get('name', '').startswith('selected_')]
    assert {control['name'] for control in checkboxes} == set(selections(active.assignment_id, archived.assignment_id))
    assert all(control.get('type') == 'checkbox' and control.get('value') == 'yes' for control in checkboxes)
    assert all('checked' not in control and 'required' not in control for control in checkboxes)
    assert {control.get('value') for control in controls if control.get('name') == 'action'} == {'archive', 'delete'}
    assert not any(control.get('name') == 'selected_' + draft['draft_id'] for control in controls)
    selected = checkboxes[0]
    assert ticket(browser.post(form['action'], action='archive', **{selected['name']: selected['value']}))

    trash = browser.web.request('GET', BASE + '/assignments', {'visibility': 'deleted'}, browser.cookies, authorization=AUTH)
    assert not any(control.get('name', '').startswith('selected_') for control in Controls(trash.body).controls)


def test_selection_is_limited_to_current_page_and_cleared_on_navigation(setup):
    browser, state, _, _, _ = setup
    for index in range(21):
        release(state, assignment_id=f'page_item_{index:02d}')
    pages = [browser.web.request('GET', BASE + '/assignments', {'page': str(number)}, browser.cookies,
                                authorization=AUTH) for number in (1, 2, 1)]
    selected = [[control for control in Controls(page.body).controls
                 if control.get('name', '').startswith('selected_')] for page in pages]
    assert [len(controls) for controls in selected] == [20, 1, 20]
    assert {control['name'] for control in selected[0]}.isdisjoint(control['name'] for control in selected[1])
    assert all('checked' not in control for controls in selected for control in controls)


@pytest.mark.parametrize('fields', [
    {'action': 'archive'},
    {'action': 'unknown', 'selected_own_release': 'yes'},
    {'action': 'archive', 'selected_own_release': 'false'},
])
def test_invalid_bulk_selection_is_not_partially_applied(setup, fields):
    browser, state, _, _, _ = setup
    release(state, assignment_id='own_release')
    assert browser.post(PREVIEW, **fields).status == 400
    assert state.get_bundle_assignment('own_release').active


@pytest.mark.parametrize('foreign', ['unknown_release', 'other_course_release'])
def test_invalid_release_in_selection_rejects_whole_preview(setup, foreign):
    browser, state, _, _, _ = setup
    release(state, assignment_id='own_release')
    release(state, course='come3105', assignment_id='other_course_release')
    assert browser.post(PREVIEW, action='archive', **selections('own_release', foreign)).status == 404
    assert state.get_bundle_assignment('own_release').active
    assert state.get_bundle_assignment('other_course_release').active


def test_bulk_preview_rejects_more_than_one_page_of_releases(setup):
    browser, state, _, _, _ = setup
    ids = [f'too_many_{index:02d}' for index in range(21)]
    for assignment_id in ids:
        release(state, assignment_id=assignment_id)
    assert browser.post(PREVIEW, action='archive', **selections(*ids)).status == 400
    assert all(state.get_bundle_assignment(assignment_id).active for assignment_id in ids)


def test_delete_preview_rejects_mixed_active_and_archived_selection(setup):
    browser, state, _, _, assignments = setup
    release(state, assignment_id='already_archived')
    release(state, assignment_id='still_active')
    assignments.archive_release('come2201', 'already_archived')
    response = browser.post(PREVIEW, action='delete', **selections('already_archived', 'still_active'))
    assert response.status == 409
    assert visibility(browser, 'already_archived') == 'inactive'
    assert visibility(browser, 'still_active') == 'open'


@pytest.mark.parametrize('action', ['archive', 'delete'])
def test_changed_release_invalidates_entire_reviewed_selection(setup, action):
    browser, state, _, _, assignments = setup
    ids = ['unchanged_release', 'changed_release']
    for assignment_id in ids:
        release(state, assignment_id=assignment_id)
        if action == 'delete':
            assignments.archive_release('come2201', assignment_id)
    reviewed = ticket(browser.post(PREVIEW, action=action, **selections(*ids)))
    state.set_bundle_assignment_availability('changed_release', course_key='come2201', active=True, ready=False)
    response = browser.post(APPLY, ticket=reviewed, confirm='yes')
    assert response.status == 409
    assert visibility(browser, 'changed_release') == 'hidden'
    assert visibility(browser, 'unchanged_release') == ('open' if action == 'archive' else 'inactive')


def test_archived_course_invalidates_existing_bulk_review(setup):
    browser, state, courses, _, _ = setup
    release(state, assignment_id='course_became_archived')
    reviewed = ticket(browser.post(PREVIEW, action='archive', **selections('course_became_archived')))
    courses.set_status('come2201', 'archived')
    assert browser.post(APPLY, ticket=reviewed, confirm='yes').status == 409
    assert browser.post(PREVIEW, action='archive', **selections('course_became_archived')).status == 409
    assert not any(control.get('name', '').startswith('selected_')
                   for control in Controls(browser.get(BASE + '/assignments').body).controls)
    assert state.get_bundle_assignment('course_became_archived').active


@pytest.mark.parametrize('failure', ['missing', 'tampered', 'course', 'session'])
def test_bulk_review_ticket_cannot_be_forged_or_reused_in_another_scope(setup, failure):
    browser, state, _, _, _ = setup
    release(state, assignment_id='ticket_target')
    reviewed = ticket(browser.post(PREVIEW, action='archive', **selections('ticket_target')))
    endpoint = APPLY
    caller = browser
    if failure == 'missing':
        reviewed = ''
    elif failure == 'tampered':
        reviewed = 'x' + reviewed[1:]
    elif failure == 'course':
        endpoint = APPLY.replace('come2201', 'come3105')
    else:
        caller = Browser(browser.web)
        caller.get('/instructor')
    assert caller.post(endpoint, ticket=reviewed, confirm='yes').status == 400
    assert state.get_bundle_assignment('ticket_target').active


def test_bulk_ticket_expires_before_instructor_session(setup, monkeypatch):
    browser, state, _, _, _ = setup
    release(state, assignment_id='expired_ticket_target')
    reviewed = ticket(browser.post(PREVIEW, action='archive', **selections('expired_ticket_target')))
    later = time.time() + 601
    monkeypatch.setattr('autograde.platform_auth.time.time', lambda: later)
    assert browser.post(APPLY, ticket=reviewed, confirm='yes').status == 400
    assert state.get_bundle_assignment('expired_ticket_target').active


@pytest.mark.parametrize('malformed', ['unicode', 'overlong', 'empty', 'truncated'])
def test_malformed_bulk_ticket_fails_cleanly_without_changes(setup, malformed):
    browser, state, _, _, _ = setup
    ids = ('malformed_first', 'malformed_second')
    for assignment_id in ids:
        release(state, assignment_id=assignment_id)
    reviewed = ticket(browser.post(PREVIEW, action='archive', **selections(*ids)))
    invalid = {'unicode': 'x.한', 'overlong': 'a' * 16385, 'empty': '', 'truncated': reviewed[:-10]}[malformed]
    assert browser.post(APPLY, ticket=invalid, confirm='yes').status == 400
    assert all(state.get_bundle_assignment(assignment_id).active for assignment_id in ids)


@pytest.mark.parametrize('extra', [{'action': 'delete'}, {'selected_unreviewed': 'yes'}, {'assignment_ids': 'unreviewed'}])
def test_apply_rejects_unreviewed_selection_or_action_fields(setup, extra):
    browser, state, _, _, _ = setup
    for assignment_id in ('reviewed', 'unreviewed'):
        release(state, assignment_id=assignment_id)
    reviewed = ticket(browser.post(PREVIEW, action='archive', **selections('reviewed')))
    assert browser.post(APPLY, ticket=reviewed, confirm='yes', **extra).status == 400
    assert all(state.get_bundle_assignment(assignment_id).active for assignment_id in ('reviewed', 'unreviewed'))


def test_real_http_parses_multiple_checkboxes_and_applies_only_reviewed_releases(http_setup):
    server, browser, state, _, _, _ = http_setup
    for assignment_id in ('http_first', 'http_second', 'http_unselected'):
        release(state, assignment_id=assignment_id)
    fields = dict(csrf=browser.csrf, action='archive', **selections('http_first', 'http_second'))
    status, headers, html = send(server, PREVIEW, urlencode(fields).encode(),
                                 'application/x-www-form-urlencoded', browser=browser)
    assert status == 200, html
    assert headers['Cache-Control'] == 'no-store'
    form = next(form for form in Forms(html.decode()).forms if form['action'] == APPLY)
    assert all(state.get_bundle_assignment(assignment_id).active for assignment_id in ('http_first', 'http_second'))
    status, _, html = send(server, APPLY, urlencode(dict(form['fields'], confirm='yes')).encode(),
                           'application/x-www-form-urlencoded', browser=browser)
    assert status == 200, html
    assert [visibility(browser, assignment_id) for assignment_id in ('http_first', 'http_second', 'http_unselected')] == [
        'inactive', 'inactive', 'open']


def test_personal_instructor_bulk_actions_respect_course_grants_and_revocation(setup, tmp_path):
    browser, state, _, _, _ = setup
    release(state, assignment_id='personal_target')
    identities = InstructorIdentityStore(tmp_path / 'bulk-identities.sqlite3')
    identities.initialize()
    password = 'synthetic-bulk-instructor-password'
    identities.create('teacher', 'Bulk teacher', 'instructor', password)
    identities.set_grant('teacher', 'come2201', allowed=True)
    browser.web.identities = identities
    authorization = 'Basic ' + base64.b64encode(('teacher:' + password).encode()).decode()
    page = browser.web.request('GET', BASE + '/assignments', {}, {}, authorization=authorization)
    key, value = page.headers['Set-Cookie'].split(';', 1)[0].split('=', 1)
    cookies = {key: value}
    form = next(form for form in Forms(page.body).forms if form['action'] == PREVIEW)
    csrf = form['fields']['csrf']
    fields = dict(csrf=csrf, action='archive', **selections('personal_target'))
    reviewed = ticket(browser.web.request('POST', PREVIEW, fields, cookies, WEB, authorization))
    for endpoint, payload in ((PREVIEW, fields), (APPLY, dict(csrf=csrf, ticket=reviewed, confirm='yes'))):
        response = browser.web.request('POST', endpoint.replace('come2201', 'come3105'),
                                       payload, cookies, WEB, authorization)
        assert response.status == 404
    identities.set_grant('teacher', 'come2201', allowed=False)
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('POST', APPLY, dict(csrf=csrf, ticket=reviewed, confirm='yes'), cookies, WEB, authorization)
    assert error.value.status == 403
    assert state.get_bundle_assignment('personal_target').active


@pytest.mark.parametrize('endpoint', [PREVIEW, APPLY])
@pytest.mark.parametrize('authorization', [None, '', 'Bearer student-token', 'Basic invalid'])
def test_bulk_routes_require_instructor_authentication(setup, endpoint, authorization):
    browser, state, _, _, _ = setup
    release(state, assignment_id='auth_target')
    reviewed = ticket(browser.post(PREVIEW, action='archive', **selections('auth_target')))
    fields = dict(csrf=browser.csrf, action='archive', ticket=reviewed, confirm='yes', **selections('auth_target'))
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('POST', endpoint, fields, browser.cookies, WEB, authorization)
    assert error.value.status == 401
    assert state.get_bundle_assignment('auth_target').active


@pytest.mark.parametrize('endpoint', [PREVIEW, APPLY])
@pytest.mark.parametrize('failure', ['origin', 'csrf', 'student_cookie'])
def test_bulk_routes_require_same_origin_and_instructor_csrf(setup, endpoint, failure):
    browser, state, _, _, _ = setup
    release(state, assignment_id='csrf_target')
    reviewed = ticket(browser.post(PREVIEW, action='archive', **selections('csrf_target')))
    fields = dict(csrf='wrong' if failure == 'csrf' else browser.csrf,
                  action='archive', ticket=reviewed, confirm='yes', **selections('csrf_target'))
    cookies = {'autograde_portal_come2201': 'student'} if failure == 'student_cookie' else browser.cookies
    origin = 'https://attacker.example' if failure == 'origin' else WEB
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('POST', endpoint, fields, cookies, origin, AUTH)
    assert error.value.status == 403
    assert state.get_bundle_assignment('csrf_target').active


def test_get_bulk_routes_cannot_change_assignment_state(setup):
    browser, state, _, _, _ = setup
    release(state, assignment_id='get_target')
    reviewed = ticket(browser.post(PREVIEW, action='archive', **selections('get_target')))
    for endpoint in (PREVIEW, APPLY):
        response = browser.web.request('GET', endpoint, dict(action='archive', ticket=reviewed, confirm='yes',
                                       **selections('get_target')), browser.cookies, authorization=AUTH)
        assert response.status == 404
    assert state.get_bundle_assignment('get_target').active
