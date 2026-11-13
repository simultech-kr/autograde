"""Draft continuity uses the real HTTP/auth/upload/revision boundaries."""
import json
from urllib.parse import urlencode

from autograde.instructor_browser import SCRIPT_HASH
from test_instructor_web import setup, BASE, Forms
from test_instructor_upload_http import http_setup, send, download, multipart


def save(server, browser, path, **fields):
    return send(server, path, urlencode(dict(csrf=browser.csrf, inline_save='yes', **fields)).encode(),
                'application/x-www-form-urlencoded', browser=browser)


def test_inline_save_receipts_preserve_domains_and_fence_stale_writers(http_setup):
    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', mode='direct', language='c')
    path = BASE + '/drafts/' + draft['draft_id']
    status, headers, html = download(server, path, browser=browser)
    assert status == 200 and f"'sha256-{SCRIPT_HASH}'" in headers['Content-Security-Policy']
    forms = Forms(html.decode()).forms
    inline = [form for form in forms if 'data-inline-draft-save' in form]
    assert len(inline) == 7
    assert all('data-inline-draft-save' not in form for form in forms if form.get('action', '').endswith(('/checks', '/publish', '/delete')))
    status, headers, raw = save(server, browser, path, revision='1', title='Saved problem', description='Keep private test input separate')
    receipt = json.loads(raw)
    assert status == 200 and headers['Cache-Control'] == 'no-store'
    assert receipt['previous_revision'] == 1 and receipt['revision'] == 2 and receipt['path'] == path
    assert receipt['values']['title'] == 'Saved problem' and 'test_0_input' not in receipt['values']
    assert receipt['summary'][-1] == '자료 변경 · 재검증 필요'
    status, _, raw = save(server, browser, path, revision='2', tests_present='yes', negative_score='0',
                          test_0_title='Case', test_0_input='secret input', test_0_output='42', test_0_weight='100')
    assert status == 200 and json.loads(raw)['values']['test_0_input'] == 'secret input'
    assert assignments.get_draft('come2201', draft['draft_id'])['title'] == 'Saved problem'
    status, _, raw = save(server, browser, path, revision='2', title='stale overwrite')
    assert status == 409 and json.loads(raw)['error']['code'] == 'draft_conflict'
    assert assignments.get_draft('come2201', draft['draft_id'])['revision'] == 3


def test_inline_invalid_input_is_retryable_without_creating_a_new_version(http_setup):
    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', mode='direct')
    path = BASE + '/drafts/' + draft['draft_id']
    status, _, raw = save(server, browser, path, revision='1', tests_present='yes', test_0_weight='bad')
    assert status == 400 and json.loads(raw)['error']['code'] == 'draft_input'
    assert assignments.get_draft('come2201', draft['draft_id'])['revision'] == 1
    # No JavaScript still uses the existing ordinary form/redirect contract.
    assert browser.post(path, revision='1', title='Fallback save').status == 303


def test_inline_grading_upload_returns_registered_tests_not_validation_success(http_setup):
    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', mode='direct', language='c')
    path = BASE + '/drafts/' + draft['draft_id']
    _, _, archive = download(server, path + '/grading-template.zip', browser=browser)
    mime, body = multipart([('csrf', browser.csrf), ('revision', '1'), ('inline_save', 'yes'),
                           ('grading_confirm', 'yes'), ('file', archive)])
    status, _, raw = send(server, path + '/grading-template', body, mime, browser=browser)
    receipt = json.loads(raw)
    assert status == 200 and receipt['tests_replaced'] is True and receipt['revision'] == 2
    assert {'solution', 'negative'} == set(receipt['uploads'])
    assert receipt['values']['test_0_weight'] > 0 and receipt['can_check'] is False  # starter still missing
    assert assignments.get_draft('come2201', draft['draft_id'])['latest_check'] is None
    status, _, raw = save(server, browser, path + '/starter-template', revision='2', template_confirm='yes')
    assert status == 200 and json.loads(raw)['can_check'] is True
    assert assignments.get_draft('come2201', draft['draft_id'])['latest_check'] is None


def test_inline_receipt_never_advances_over_another_writers_commit(setup, monkeypatch):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='direct')
    path = BASE + '/drafts/' + draft['draft_id']
    original = assignments.update_draft

    def another_writer(*args, **kwargs):
        saved = original(*args, **kwargs)
        return original('come2201', draft['draft_id'], saved['revision'], description='concurrent writer')

    monkeypatch.setattr(assignments, 'update_draft', another_writer)
    response = browser.post(path, revision='1', inline_save='yes', title='first writer')
    assert response.status == 409 and response.body['error']['code'] == 'draft_conflict'
    assert assignments.get_draft('come2201', draft['draft_id'])['revision'] == 3


def test_inline_save_cannot_bypass_auth_origin_or_csrf(http_setup):
    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', mode='direct')
    path = BASE + '/drafts/' + draft['draft_id']
    body = urlencode(dict(csrf=browser.csrf, inline_save='yes', revision='1', title='Must not save')).encode()
    for overrides in ({'authorization': None}, {'origin': 'https://attacker.example'}):
        assert send(server, path, body, 'application/x-www-form-urlencoded', browser=browser, **overrides)[0] in (401, 403)
    bad = body.replace(browser.csrf.encode(), b'incorrect')
    assert send(server, path, bad, 'application/x-www-form-urlencoded', browser=browser)[0] == 403
    assert assignments.get_draft('come2201', draft['draft_id'])['revision'] == 1


def recovery(server, browser, path, revision, request_key, kind='problem'):
    return download(server, path + '/save-state?' + urlencode(dict(
        revision=revision, save_request=request_key, save_kind=kind)), browser=browser)


def test_save_recovery_identifies_before_commit_after_commit_and_concurrent_edit(http_setup):
    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', mode='direct', language='c')
    path = BASE + '/drafts/' + draft['draft_id']
    token = 'a' * 32
    status, headers, raw = recovery(server, browser, path, 1, token)
    body = json.loads(raw)
    assert status == 200 and body['status'] == 'not_saved'
    assert headers['Cache-Control'] == 'no-store' and 'Set-Cookie' not in headers
    assert body['receipt']['revision'] == 1 and body['request_saved'] is False
    status, _, _ = save(server, browser, path, revision='1', save_request=token, title='Committed before disconnect')
    assert status == 200
    # The response can be lost; recovery must rely on a transactional record,
    # not matching content, an optimistic revision or process memory.
    status, _, raw = recovery(server, browser, path, 1, token)
    body = json.loads(raw)
    assert status == 200 and body['status'] == 'saved' and body['request_saved'] is True
    assert body['receipt']['values']['title'] == 'Committed before disconnect'
    assert json.loads(recovery(server, browser, path, 1, 'b' * 32)[2])['status'] == 'conflict'
    assert json.loads(recovery(server, browser, path, 1, token, 'tests')[2])['request_saved'] is False
    assignments.update_draft('come2201', draft['draft_id'], 2, title='Later edit')
    body = json.loads(recovery(server, browser, path, 1, token)[2])
    assert body['status'] == 'conflict' and body['request_saved'] is True
    assert body['receipt']['revision'] == 3 and body['receipt']['values']['title'] == 'Later edit'


def test_save_recovery_upload_commit_and_rollback_are_atomic(http_setup):
    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', mode='direct', language='c')
    path = BASE + '/drafts/' + draft['draft_id']
    token = 'c' * 32
    _, _, archive = download(server, path + '/grading-template.zip', browser=browser)
    mime, body = multipart([('csrf', browser.csrf), ('revision', '1'), ('inline_save', 'yes'),
        ('save_request', token), ('grading_confirm', 'yes'), ('file', archive)])
    assert send(server, path + '/grading-template', body, mime, browser=browser)[0] == 200
    payload = json.loads(recovery(server, browser, path, 1, token, 'grading-template')[2])
    assert payload['status'] == 'saved' and payload['receipt']['tests_replaced'] is True
    assert {'solution', 'negative'} == set(payload['receipt']['uploads'])
    # Invalid evidence must roll back the content write as well.
    assert save(server, browser, path, revision='2', save_request='invalid', title='Must roll back')[0] == 400
    assert assignments.get_draft('come2201', draft['draft_id'])['revision'] == 2
    assert json.loads(recovery(server, browser, path, 2, 'd' * 32)[2])['status'] == 'not_saved'


def test_save_recovery_requires_signed_session_and_course_scope(http_setup):
    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', mode='direct')
    path = BASE + '/drafts/' + draft['draft_id']
    suffix = '/save-state?' + urlencode(dict(revision=1, save_request='e' * 32, save_kind='problem'))
    assert download(server, path + suffix, browser=browser, authorization=None)[0] == 401
    from types import SimpleNamespace
    assert download(server, path + suffix, browser=SimpleNamespace(cookies={}))[0] == 403
    other = path.replace('come2201', 'come3105')
    assert download(server, other + suffix, browser=browser)[0] == 404
    assert assignments.get_draft('come2201', draft['draft_id'])['revision'] == 1


def test_save_recovery_survives_service_restart_and_honors_edit_lock(setup):
    from autograde.assignment_admin import AssignmentAdminService
    browser, state, courses, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='template', language='c')
    token = 'f' * 32
    assignments.update_draft('come2201', draft['draft_id'], 1, title='Durable save', save_request=token)
    restarted = AssignmentAdminService(state, assignments.paths, course_status=courses.get_course)
    result = restarted.recover_save('come2201', draft['draft_id'], 1, token, 'problem')
    assert result['status'] == 'saved' and result['request_saved'] is True
    restarted.queue_check('come2201', draft['draft_id'], 2, trusted_code_confirmed=True)
    result = restarted.recover_save('come2201', draft['draft_id'], 1, token, 'problem')
    assert result['status'] == 'locked' and result['request_saved'] is True
