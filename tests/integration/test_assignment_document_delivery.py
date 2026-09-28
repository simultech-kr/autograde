"""Instructor edits and accepted-student reads over the actual HTTP adapters."""
from contextlib import ExitStack
import io
import json
import re
import tarfile

from autograde.platform_bundle import BundleStore
from autograde.platform_bundle_worker import BundleSubmissionProcessor
from autograde.platform_grader import PilotLocalGrader
from autograde.platform_portal import CourseAPI, CoursePortal
from autograde.platform_service import StudentPlatformService
from autograde.workspace import WorkspaceBuilder
from test_instructor_web import setup, AUTH, BASE
from test_course_portal import API, WEB, request, serving, login, cookie, csrf, connect
from test_workshop_catalog import submit_http


def test_submitted_student_receives_new_explanation_without_reacceptance(setup, tmp_path):
    browser, state, courses, students, admin = setup
    draft = admin.create_draft('come2201', language='c', description='# Original\nPrint Hello, World!')
    admin.queue_check('come2201', draft['draft_id'], draft['revision'], True)
    assert admin.run_one()
    release = admin.publish('come2201', draft['draft_id'], draft['revision'])
    store = BundleStore(admin.paths.bundles)
    secret = b'w' * 32
    service = StudentPlatformService(state=state, server_secret=secret, course_key='come2201',
        public_base_url=API, bundle_store=store, instructor_token='synthetic-instructor-token-1234567890')
    students.add_student('come2201', student_key='QA-DOC', password='123456')
    portal = CoursePortal({'come2201': service}, secret, WEB, API, courses=courses, instructor=browser.web)
    api = CourseAPI({'come2201': service}, secret, courses=courses)
    doc_path = f'/v1/assignments/{release.assignment_id}/document'
    editor_path = BASE + f'/assignments/{release.assignment_id}/document'
    with ExitStack() as stack:
        web = stack.enter_context(serving(portal, WEB))
        api_server = stack.enter_context(serving(api, API))
        status, headers, body = login(web, 'come2201', '123456', 'QA-DOC')
        assert status == 200
        status, _, body = request(web, '/courses/come2201/claims', method='POST', origin=WEB,
            cookie=cookie(headers), data={'csrf': csrf(body), 'assignment_id': release.assignment_id})
        assert status == 200
        code = re.search(rb'<code>(AK1-[A-Z0-9-]+)</code>', body)[1].decode()
        _, tokens = connect(api_server, code)
        token = tokens['access_token']
        assert request(api_server, doc_path)[0] == 401
        status, doc_headers, body = request(api_server, doc_path, token=token)
        assert status == 200 and json.loads(body)['document']['revision'] == 0
        assert 'actor' not in json.loads(body)['document']
        assert 'no-store' in doc_headers['Cache-Control']
        starter_path = f'/v1/assignments/{release.assignment_id}/starter'
        status, _, starter = request(api_server, starter_path, token=token)
        assert status == 200
        source = tmp_path / 'answer'
        source.mkdir()
        (source / 'main.c').write_text('#include <stdio.h>\nint main(void){puts("Hello, World!");}\n')
        bundle = store.create_from_directory(source, kind='submission')
        status, result = submit_http(api_server, token, release.assignment_id, 'before-document-change', bundle)
        assert status == 202
        sid = json.loads(result)['submission']['submission_id']
        grader = PilotLocalGrader()
        stack.callback(grader.close)
        processor = BundleSubmissionProcessor(state=state, course_key='come2201',
            workspace_builder=WorkspaceBuilder(admin.paths.workspaces), grader=grader)
        processor.process(sid)
        receipt = state.get_bundle_receipt(sid)
        status, _, original_result = request(api_server, f'/v1/submissions/{sid}/result', token=token)
        assert status == 200 and json.loads(original_result)['result']['score'] == 10

        # Exercise actual instructor auth + CSRF + UTF-8 form sizing (180 KiB encoded).
        status, instructor_headers, html = request(web, editor_path, authorization=AUTH)
        assert status == 200
        content = '# Improved\n' + '설명' * 9900
        form = dict(csrf=csrf(html), revision='0', content=content, change_note='실행 안내 보완', confirm='yes')
        status, _, _ = request(web, editor_path, method='POST', authorization=AUTH, origin='https://other.example',
            cookie=cookie(instructor_headers), data=form)
        assert status == 403
        status, _, html = request(web, editor_path, method='POST', authorization=AUTH, origin=WEB,
            cookie=cookie(instructor_headers), data=form)
        assert status == 200, html
        assert state.get_bundle_receipt(sid) == receipt
        assert request(api_server, f'/v1/submissions/{sid}/result', token=token)[2] == original_result
        assert state.get_bundle_assignment(release.assignment_id) == release

        status, _, body = request(api_server, doc_path, token=token)
        assert status == 200
        updated = json.loads(body)['document']
        assert updated['revision'] == 1 and updated['content'] == content
        assert [entry['revision'] for entry in updated['history']] == [1, 0]
        listed = json.loads(request(api_server, '/v1/accepted-assignments', token=token)[2])['assignments']
        assert len(listed) == 1 and listed[0]['assignment_id'] == release.assignment_id
        assert listed[0]['document']['revision'] == 1
        assert request(api_server, starter_path, token=token)[2] == starter
        with tarfile.open(fileobj=io.BytesIO(starter), mode='r:gz') as archive:
            assert b'# Original' in archive.extractfile('README.md').read()
        # Existing student can resubmit with the same token/acceptance and same grading.
        status, result = submit_http(api_server, token, release.assignment_id, 'after-document-change', bundle)
        assert status == 202
        after_sid = json.loads(result)['submission']['submission_id']
        processor.process(after_sid)
        assert json.loads(request(api_server, f'/v1/submissions/{after_sid}/result', token=token)[2])['result']['score'] == 10
        with state._connection() as db:
            assert db.execute('SELECT revision FROM bundle_submission_documents WHERE submission_id=?', (sid,)).fetchone()[0] == 0
            assert db.execute('SELECT revision FROM bundle_submission_documents WHERE submission_id=?', (after_sid,)).fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM platform_assignment_acceptances WHERE assignment_id=?', (release.assignment_id,)).fetchone()[0] == 1
        other = admin.copy_release('come2201', release.assignment_id)
        admin.queue_check('come2201', other['draft_id'], other['revision'], True)
        admin.run_one()
        other_id = admin.get_draft('come2201', other['draft_id'])['latest_check']['assignment_id']
        assert request(api_server, f'/v1/assignments/{other_id}/document', token=token)[0] == 403
        assert request(api_server, doc_path, method='POST', token=token, data={})[0] == 405
        service.revoke_current(token)
        assert request(api_server, doc_path, token=token)[0] == 401
