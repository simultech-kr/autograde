"""Recoverable instructor deletion using synthetic releases, without compiling code."""
from datetime import datetime, timedelta, timezone

import pytest

from autograde.assignment_documents import ensure_original
from autograde.platform_service import PlatformAPIError
from test_instructor_ux import release
from test_instructor_web import AUTH, BASE, WEB, Forms, setup


def published(setup, origin='web', *, assignment_id='delete_target', course='come2201'):
    """Link a normal web draft to a synthetic release; validation is covered elsewhere."""
    _, state, _, _, assignments = setup
    due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    item = release(state, course, assignment_id, due_at=due)
    draft = None
    if origin == 'web':
        draft = assignments.create_draft(course, title='웹 보관 삭제 <과제>',
            description='학생에게 공개된 원본 설명', due_at=due)
        with state._write() as connection:
            connection.execute('UPDATE instructor_assignment_drafts SET published_assignment_id=? WHERE draft_id=?',
                               (assignment_id, draft['draft_id']))
            ensure_original(connection, course, assignment_id)
    return item, draft


def post_actions(response):
    return {form['action'] for form in Forms(response.body).forms if form.get('method') == 'post'}


def filtered(browser, **values):
    return browser.web.request('GET', BASE + '/assignments', values, browser.cookies, authorization=AUTH)


@pytest.mark.parametrize('origin', ['web', 'cli'])
def test_legacy_inactive_ready_release_can_finish_archiving_before_deletion(setup, origin):
    browser, state, _, _, _ = setup
    item, _ = published(setup, origin)
    state.set_bundle_assignment_availability(item.assignment_id, course_key='come2201', active=False, ready=True)
    path = BASE + '/assignments/' + item.assignment_id
    actions = post_actions(browser.get(path))
    assert path + '/archive' in actions
    assert path + '/delete' not in actions
    assert browser.post(path + '/archive', confirm='yes').status == 303
    assert path + '/delete' in post_actions(browser.get(path))
    assert browser.post(path + '/delete', confirm='yes').status == 303


@pytest.mark.parametrize('origin', ['web', 'cli'])
def test_instructor_archives_deletes_and_restores_without_republishing(setup, origin):
    browser, state, _, _, assignments = setup
    item, draft = published(setup, origin)
    path = BASE + '/assignments/' + item.assignment_id
    active = browser.get(path)
    assert path + '/archive' in post_actions(active)
    assert path + '/delete' not in post_actions(active)
    assert browser.post(path + '/delete', confirm='yes').status == 409
    assert state.get_bundle_assignment(item.assignment_id).active

    archived = browser.post(path + '/archive', confirm='yes')
    assert archived.status == 303 and archived.headers['Location'] == path
    page = browser.get(path)
    assert path + '/delete' in post_actions(page)
    assert '과제 삭제' in page.body
    assert '코드' in page.body and '점수' in page.body
    assert browser.post(path + '/delete').status == 400
    assert browser.web.catalog.get_release('come2201', item.assignment_id)['visibility'] == 'inactive'

    deleted = browser.post(path + '/delete', confirm='yes')
    assert deleted.status == 303
    assert deleted.headers['Location'] == BASE + '/assignments?visibility=deleted'
    assert browser.web.catalog.list('come2201')['count'] == 0
    trash = browser.web.catalog.list('come2201', visibility='deleted')
    assert trash['count'] == 1
    assert trash['items'][0]['assignment_id'] == item.assignment_id
    assert trash['items'][0]['origin'] == origin
    assert browser.web.catalog.get_release('come2201', item.assignment_id)['visibility'] == 'deleted'
    if draft:
        assert draft['draft_id'] not in {row['draft_id'] for row in assignments.list_drafts('come2201')}
    deleted_page = browser.get(path)
    assert deleted_page.status == 200 and '보관 상태로 복원' in deleted_page.body
    assert post_actions(deleted_page) == {path + '/restore'}

    assert browser.post(path + '/restore').status == 400
    assert browser.web.catalog.list('come2201', visibility='deleted')['count'] == 1
    restored = browser.post(path + '/restore', confirm='yes')
    assert restored.status == 303 and restored.headers['Location'] == path
    current = state.get_bundle_assignment(item.assignment_id)
    assert not current.active and not current.ready
    assert current.due_at == item.due_at
    assert browser.web.catalog.list('come2201', visibility='deleted')['count'] == 0
    listed = browser.web.catalog.list('come2201')
    assert listed['count'] == 1
    assert listed['items'][0]['visibility'] == 'inactive'
    assert listed['items'][0]['origin'] == origin
    if draft:
        assert assignments.get_draft('come2201', draft['draft_id'])['published_assignment_id'] == item.assignment_id


@pytest.mark.parametrize('origin', ['web', 'cli'])
def test_deleted_catalog_search_does_not_leak_or_create_phantom_cli_rows(setup, origin):
    browser, state, _, _, assignments = setup
    item, _ = published(setup, origin)
    release(state, assignment_id='retained')
    assignments.archive_release('come2201', item.assignment_id)
    assignments.delete_release('come2201', item.assignment_id)
    catalog = browser.web.catalog
    assert [row['assignment_id'] for row in catalog.list('come2201')['items']] == ['retained']
    assert catalog.list('come2201', visibility='inactive')['count'] == 0
    search = '웹 보관' if origin == 'web' else 'CLI'
    trash = catalog.list('come2201', visibility='deleted', search=search, page=10000)
    assert trash['count'] == 1 and trash['page'] == 1
    assert trash['items'][0]['assignment_id'] == item.assignment_id
    assert catalog.list('come2201', visibility='deleted', search='%')['count'] == 0
    assert catalog.list('come3105', visibility='deleted')['count'] == 0
    response = filtered(browser, visibility='deleted', q=search)
    assert response.status == 200
    assert BASE + '/assignments/' + item.assignment_id in response.body
    assert BASE + '/assignments/retained' not in response.body
    assert '/synthetic/private' not in response.body
    if origin == 'web':
        assert '웹 보관 삭제 &lt;과제&gt;' in response.body
    assignments.restore_release('come2201', item.assignment_id)
    assert catalog.list('come2201')['count'] == 2
    assert catalog.list('come2201', visibility='deleted')['count'] == 0


def test_deleted_web_draft_and_document_are_read_only_even_from_stale_forms(setup):
    browser, _, _, _, assignments = setup
    item, draft = published(setup)
    path = BASE + '/assignments/' + item.assignment_id
    draft_path = BASE + '/drafts/' + draft['draft_id']
    document = assignments.get_assignment_document('come2201', item.assignment_id)
    assignments.archive_release('come2201', item.assignment_id)
    assignments.delete_release('come2201', item.assignment_id)
    for view in (draft_path, path + '/document'):
        page = browser.get(view)
        assert page.status == 200
        assert post_actions(page) <= {path + '/restore'}
        assert '삭제' in page.body
    future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%dT%H:%M')
    for suffix, fields in (
        ('/copy', {}), ('/hide', {}), ('/archive', {}),
        ('/extend', {'due_at': future, 'reason': 'stale form'}),
        ('/document', {'revision': str(document['revision']), 'content': 'stale edit', 'change_note': 'test'}),
    ):
        assert browser.post(path + suffix, confirm='yes', **fields).status == 409
    assert browser.post(draft_path + '/publish', revision=str(draft['revision']), confirm='yes').status == 409
    assert assignments.get_assignment_document('come2201', item.assignment_id) == document
    assert browser.web.catalog.list('come2201', visibility='deleted')['count'] == 1
    assert browser.web.catalog.list('come2201')['count'] == 0


@pytest.mark.parametrize('action', ['delete', 'restore'])
@pytest.mark.parametrize('authorization', [None, '', 'Bearer student-token', 'Basic invalid'])
def test_deletion_and_restoration_require_instructor_authentication(setup, action, authorization):
    browser, _, _, _, assignments = setup
    item, _ = published(setup, 'cli')
    assignments.archive_release('come2201', item.assignment_id)
    if action == 'restore':
        assignments.delete_release('come2201', item.assignment_id)
    path = BASE + '/assignments/' + item.assignment_id + '/' + action
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('POST', path, {'csrf': browser.csrf, 'confirm': 'yes'},
                            browser.cookies, WEB, authorization)
    assert error.value.status == 401
    expected = 'deleted' if action == 'restore' else 'inactive'
    assert browser.web.catalog.get_release('come2201', item.assignment_id)['visibility'] == expected


@pytest.mark.parametrize('action', ['delete', 'restore'])
@pytest.mark.parametrize('failure', ['origin', 'csrf', 'student_cookie'])
def test_deletion_and_restoration_require_same_origin_and_instructor_csrf(setup, action, failure):
    browser, _, _, _, assignments = setup
    item, _ = published(setup, 'cli')
    assignments.archive_release('come2201', item.assignment_id)
    if action == 'restore':
        assignments.delete_release('come2201', item.assignment_id)
    path = BASE + '/assignments/' + item.assignment_id + '/' + action
    cookies = {'autograde_portal_come2201': 'student'} if failure == 'student_cookie' else browser.cookies
    csrf = 'invalid' if failure == 'csrf' else browser.csrf
    origin = 'https://attacker.example' if failure == 'origin' else WEB
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('POST', path, {'csrf': csrf, 'confirm': 'yes'}, cookies, origin, AUTH)
    assert error.value.status == 403
    expected = 'deleted' if action == 'restore' else 'inactive'
    assert browser.web.catalog.get_release('come2201', item.assignment_id)['visibility'] == expected


def test_release_deletion_is_course_scoped_and_get_does_not_mutate(setup):
    browser, _, _, _, assignments = setup
    item, _ = published(setup, 'cli', course='come3105')
    assignments.archive_release('come3105', item.assignment_id)
    path = BASE + '/assignments/' + item.assignment_id
    assert browser.post(path + '/delete', confirm='yes').status == 404
    assignments.delete_release('come3105', item.assignment_id)
    assert browser.post(path + '/restore', confirm='yes').status == 404
    assert browser.get(path).status == 404
    own_path = '/courses/come3105/instructor/assignments/' + item.assignment_id
    assert browser.get(own_path + '/restore').status == 404
    assert browser.get(own_path + '/delete').status == 404
    assert browser.web.catalog.get_release('come3105', item.assignment_id)['visibility'] == 'deleted'


def test_archived_course_prevents_deleting_and_restoring_releases(setup):
    browser, state, courses, _, assignments = setup
    archived, _ = published(setup, 'cli', assignment_id='archived_target')
    deleted = release(state, assignment_id='deleted_target')
    for item in (archived, deleted):
        assignments.archive_release('come2201', item.assignment_id)
    assignments.delete_release('come2201', deleted.assignment_id)
    courses.set_status('come2201', 'archived')
    for item, action, visibility in ((archived, 'delete', 'inactive'), (deleted, 'restore', 'deleted')):
        path = BASE + '/assignments/' + item.assignment_id
        page = browser.get(path)
        assert page.status == 200 and not post_actions(page)
        assert browser.post(path + '/' + action, confirm='yes').status == 409
        assert browser.web.catalog.get_release('come2201', item.assignment_id)['visibility'] == visibility
