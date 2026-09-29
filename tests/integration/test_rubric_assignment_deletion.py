"""Deleted releases retain rubric evidence but cannot start new authoring."""
import json

from test_instructor_rubric import catalog_setup, preview
from test_instructor_web import BASE, setup


def test_deleted_release_rejects_template_preview_and_stale_registration(catalog_setup):
    browser, _, _, document = catalog_setup
    _, fields = preview(browser, document)
    assignment_id = document['assignment_id']
    assignments = browser.web.assignments
    assignments.archive_release('come2201', assignment_id)
    assignments.delete_release('come2201', assignment_id)

    assert browser.get(BASE + '/rubrics/new/' + assignment_id).status == 409
    assert browser.post(BASE + '/rubrics/preview', document=json.dumps(document)).status == 409
    assert browser.post(BASE + '/rubrics/register', confirm='yes', **fields).status == 409
    assert browser.web.rubrics.store.list_versions('come2201')['count'] == 0

    assignments.restore_release('come2201', assignment_id)
    assert browser.get(BASE + '/rubrics/new/' + assignment_id).status == 200


def test_existing_rubric_evidence_remains_readable_after_assignment_deletion(catalog_setup):
    browser, _, _, document = catalog_setup
    _, fields = preview(browser, document)
    registered = browser.post(BASE + '/rubrics/register', confirm='yes', **fields)
    assignments = browser.web.assignments
    assignments.archive_release('come2201', document['assignment_id'])
    assignments.delete_release('come2201', document['assignment_id'])
    assert browser.get(registered.headers['Location']).status == 200
    assert browser.web.rubrics.store.list_versions('come2201')['count'] == 1
