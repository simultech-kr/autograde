"""Course deletion web flow, SQLite graph, and private filesystem integration."""
import base64
import sqlite3

import pytest

from autograde.content_store import ContentService
from autograde.course_admin import CourseAdminService, initialize_course_admin
from autograde.course_deletion import _targets
from autograde.course_deletion_files import plan_course_files
from autograde.instructor_identity import InstructorIdentityStore
from autograde.platform_service import PlatformAPIError
from autograde.platform_state import PlatformConflict, PlatformNotFound
from test_instructor_web import setup, AUTH, BASE, Forms, WEB
from test_course_reset import published_documents


def wire(courses, assignments):
    courses.file_planner = lambda db, key, rows: plan_course_files(assignments.paths, db, key, rows)


def test_web_reviews_counts_and_deletes_active_course_without_archiving(setup):
    browser, state, courses, students, assignments = setup
    wire(courses, assignments)
    students.add_student('come2201', student_key='synthetic-001', password='123456')
    settings = browser.get(BASE + '/settings')
    assert f'href="{BASE}/delete"' in settings.body
    page = browser.get(BASE + '/delete')
    assert page.status == 200 and '보관을 먼저 할 필요는 없습니다' in page.body
    assert '수강 등록</dt><dd>1</dd>' in page.body
    assert 'synthetic-001' not in page.body and '123456' not in page.body
    fields = next(form['fields'] for form in Forms(page.body).forms if form['action'] == BASE+'/delete')
    result = browser.post(BASE+'/delete', confirm_course='come2201', expected_state=fields['expected_state'])
    assert result.status == 200 and '교과목 영구 삭제 완료' in result.body
    assert browser.get(BASE).status == 404
    home = browser.get('/instructor/admin').body
    assert '영구 삭제 기록' in home and '정리 완료' in home
    assert f'href="{BASE}"' not in home
    initialize_course_admin(state)
    assert browser.get(BASE).status == 404


def test_web_wrong_confirmation_and_stale_preview_refresh_require_retyping(setup):
    browser, _, courses, students, _ = setup
    page = browser.get(BASE+'/delete')
    fields = next(form['fields'] for form in Forms(page.body).forms if form['action'] == BASE+'/delete')
    wrong = browser.post(BASE+'/delete', confirm_course='come3105', expected_state=fields['expected_state'])
    assert wrong.status == 400 and '정확히' in wrong.body
    assert 'name="confirm_course" value=' not in wrong.body
    students.add_student('come2201', student_key='later', password='123456')
    stale = browser.post(BASE+'/delete', confirm_course='come2201', expected_state=fields['expected_state'])
    assert stale.status == 409 and '변경' in stale.body
    assert '수강 등록</dt><dd>1</dd>' in stale.body
    assert courses.is_active('come2201')


def test_personal_teacher_cannot_delete_and_admin_cannot_skip_csrf(setup, tmp_path):
    browser, _, courses, _, _ = setup
    identities = InstructorIdentityStore(tmp_path/'identities.sqlite3')
    identities.initialize()
    identities.create('teacher', 'Teacher', 'instructor', 'synthetic-password-123456')
    identities.set_grant('teacher', 'come2201', allowed=True)
    browser.web.identities = identities
    auth = 'Basic ' + base64.b64encode(b'teacher:synthetic-password-123456').decode()
    request = lambda path: browser.web.request('GET', path, {}, {}, authorization=auth)
    assert request(BASE+'/delete').status == 403
    assert request('/courses/come3105/instructor/delete').status == 404
    assert f'href="{BASE}/delete"' not in request(BASE+'/settings').body
    browser.web.identities = None
    with pytest.raises(PlatformAPIError) as denied:
        browser.web.request('POST', BASE+'/delete', {}, {}, WEB, AUTH)
    assert denied.value.status == 403
    assert courses.is_active('come2201')


def test_real_assignment_history_and_content_cycle_delete_without_other_course_changes(published_documents):
    paths, state, admin, releases, submissions = published_documents
    courses = CourseAdminService(state)
    wire(courses, admin)
    content = ContentService(state, paths.root/'content')
    created = {}
    for key in ('come2201', 'come3105'):
        collection = content.create(key, title='Synthetic notes', actor='instructor')
        collection = content.upload(key, collection['collection_id'], revision=collection['revision'],
                                    path='notes.txt', data=b'synthetic notes', actor='instructor')
        content.publish(key, collection['collection_id'], revision=collection['revision'],
                        idempotency_key='synthetic-publish', actor='instructor')
        created[key] = collection['collection_id']
    with state._connection() as db:
        db.execute('BEGIN')
        targets = _targets(db, 'come2201')
        before = {}
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            table = row[0]
            if table == 'admin_course_deletions':
                continue
            remove = {r['__rowid__'] for r in targets.get(table, [])}
            before[table] = [tuple(row) for row in db.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid') if row[0] not in remove]
        guards = [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    preview = courses.deletion_preview('come2201')
    assert preview['submissions'] == 2
    assert preview['counts']['bundle_submission_documents'] == 2
    assert preview['counts']['content_releases'] == 1
    result = courses.delete_course('come2201', confirm_course='come2201', expected_state=preview['expected_state'])
    assert result['cleanup_status'] == 'complete'
    with state._connection() as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        for table, expected in before.items():
            assert [tuple(row) for row in db.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid')] == expected, table
        assert [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")] == guards
    assert content.get('come3105', created['come3105'])['status'] == 'published'
    assert content.git_repository_path('come3105').is_dir()
    assert not content.git_repository_path('come2201').exists()
    assert state.get_bundle_assignment(releases['come3105'].assignment_id) == releases['come3105']
    with pytest.raises(PlatformNotFound):
        state.get_bundle_assignment(releases['come2201'].assignment_id)
    # Immutable evidence guards remain active for the surviving course.
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with state._write() as db:
            db.execute('DELETE FROM assignment_document_revisions')
