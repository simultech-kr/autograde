"""Synthetic Excel marks follow course access and irreversible privacy closure."""
from io import BytesIO
import json
import sqlite3

from openpyxl import Workbook
import pytest

from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.course_deletion_runtime import configure_course_deletion
from autograde.course_gradebook import CourseGradebook
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, PlatformStateStore
from autograde.settings import AppPaths
from autograde.student_privacy import initialize as initialize_privacy, POLICY_VERSION
from autograde.student_registration import initialize as initialize_members


@pytest.fixture
def gradebook(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    initialize_privacy(state)
    initialize_members(state)
    courses = CourseAdminService(state)

    class Registry:
        def invalidate(self, _key):
            pass

    configure_course_deletion(paths, courses, Registry())
    students = EnrollmentAdminService(state, b'g' * 32)
    for course, key, password in [
        ('come2201', '20260001', '123456'),
        ('come2201', '20260002', '234567'),
        ('come3105', '20260001', '345678'),
        ('come3105', '20260003', '456789'),
    ]:
        students.add_student(course, student_key=key, password=password)
    service = CourseGradebook(state)  # Runtime schema added after course guards.
    return paths, state, courses, students, service


def workbook(rows):
    book = Workbook()
    sheet = book.active
    sheet.append(['학번', '중간고사', '출석점수', '교수자 내부 메모'])
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def upload(service, course='come2201', rows=None, title='합성 성적표'):
    rows = rows or [('20260001', 0, None, 'synthetic-private-one'), ('20260002', 88, 9, 'synthetic-private-two')]
    return service.import_workbook(course, workbook(rows), 'synthetic-gradebook.xlsx', title=title)


def publish(service, draft):
    return service.publish(draft['course_key'], draft['draft_id'], draft['revision'],
                           selected_columns=['column_2', 'column_3'])


def credential(state, key='20260001', course='come2201'):
    return state.get_student_password_credential(student_key=key, course_key=course)


def test_published_values_are_own_row_only_and_exclude_unselected_teacher_columns(gradebook):
    _paths, state, _courses, _students, service = gradebook
    draft = upload(service)
    assert service.student_view('come2201', credential(state)) is None
    publish(service, draft)
    first = service.student_view('come2201', credential(state))
    second = service.student_view('come2201', credential(state, '20260002'))
    assert [(item['label'], item['value']) for item in first['items']] == [('중간고사', 0), ('출석점수', None)]
    assert [(item['label'], item['value']) for item in second['items']] == [('중간고사', 88), ('출석점수', 9)]
    assert 'synthetic-private-' not in json.dumps([first, second])
    assert '20260002' not in json.dumps(first)
    with pytest.raises(PlatformAccessDenied):
        service.student_view('come3105', credential(state))
    assert service.student_view('come3105', credential(state, course='come3105')) is None


def test_retrieval_rechecks_enrollment_and_password_changes(gradebook):
    _paths, state, _courses, students, service = gradebook
    first = credential(state)
    second = credential(state, '20260002')
    publish(service, upload(service))
    students.set_active('come2201', '20260001', False, reason='Synthetic access revocation')
    with pytest.raises(PlatformAccessDenied):
        service.student_view('come2201', first)
    assert service.student_view('come2201', second)['items'][0]['value'] == 88
    students.reset_password('come2201', '20260002', password='654321', reason='Synthetic rotation')
    with pytest.raises(PlatformAccessDenied):
        service.student_view('come2201', second)
    current = credential(state, '20260002')
    assert service.student_view('come2201', current)['items'][0]['value'] == 88


def test_import_keeps_no_workbook_file_or_binary_blob(gradebook):
    paths, state, _courses, _students, service = gradebook
    before = {path.relative_to(paths.root) for path in paths.root.rglob('*') if path.is_file()}
    draft = upload(service)
    publish(service, draft)
    after = {path.relative_to(paths.root) for path in paths.root.rglob('*') if path.is_file()}
    assert {str(path) for path in after - before} <= {'state.sqlite3-wal', 'state.sqlite3-shm'}
    with state._connection() as db:
        for table in ('course_gradebooks', 'course_gradebook_rows', 'course_gradebook_state'):
            for row in db.execute('SELECT * FROM ' + table):
                assert not any(isinstance(value, bytes) for value in row)


@pytest.mark.parametrize('changed', ['active', 'verified', 'password_hash'])
def test_common_account_revocation_does_not_fall_back_to_course_password(gradebook, changed):
    _paths, state, _courses, _students, service = gradebook
    student = credential(state)
    publish(service, upload(service))
    with state._write() as db:
        db.execute('INSERT INTO student_accounts(student_key,password_hash,student_id,active,verified,created_at,updated_at) '
                   'VALUES(?,?,?,1,1,?,?)', ('20260001', 'synthetic-member-verifier', student.student_id, 'now', 'now'))
    verifier = ('20260001', 'synthetic-member-verifier')
    assert service.student_view('come2201', student, member_verifier=verifier)['items'][0]['value'] == 0
    with state._write() as db:
        db.execute('UPDATE student_accounts SET ' + changed + '=? WHERE student_key=?',
                   ('different-verifier' if changed == 'password_hash' else 0, '20260001'))
    with pytest.raises(PlatformAccessDenied):
        service.student_view('come2201', student, member_verifier=verifier)


def test_course_archive_erases_all_editions_and_drafts_without_retaining_individual_grades(gradebook):
    _paths, state, courses, _students, service = gradebook
    own_credential = credential(state)
    other_credential = credential(state, course='come3105')
    first = publish(service, upload(service, title='Private version one'))
    current = publish(service, upload(service, title='Private version two'))
    upload(service, rows=[('not-on-roster', 7, 8, 'unmatched-private')], title='Unpublished private draft')
    service.hide('come2201', current['draft_id'], current['revision'])
    other = publish(service, upload(service, 'come3105', [('20260001', 75, 8, 'other-private')], 'Other course'))
    with state._write() as db:
        db.execute('INSERT INTO student_privacy_consents VALUES(?,?,?,?,?,?)',
                   ('come2201', own_credential.student_id, 'granted', POLICY_VERSION, 1, '2026-11-01T00:00:00Z'))
    courses.privacy_archive_preview('come2201')
    assert len(service.list_for_instructor('come2201')) == 3
    courses.set_status('come2201', 'archived')
    report = courses.archive_status('come2201')
    assert report['cleanup_status'] == 'complete'
    assert report['aggregate']['assignments'] == []
    with state._connection() as db:
        for table in ('course_gradebooks', 'course_gradebook_rows', 'course_gradebook_state'):
            assert not db.execute(f'SELECT 1 FROM {table} WHERE course_key=?', ('come2201',)).fetchone()
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        archive = json.dumps(dict(db.execute('SELECT * FROM admin_course_privacy_archives WHERE course_key=?', ('come2201',)).fetchone()))
    assert all(value not in archive for value in ('20260001', 'not-on-roster', 'unmatched-private', first['draft_id']))
    assert service.student_view('come3105', other_credential)['publication_id'] == other['draft_id']
    with pytest.raises(PlatformAccessDenied):
        service.student_view('come2201', own_credential)
    with pytest.raises(PlatformConflict):
        upload(service)
    with pytest.raises(PlatformConflict, match='개인정보'):
        with state._write() as db:
            db.execute("INSERT INTO course_gradebook_state(course_key) VALUES('come2201')")


def test_course_deletion_erases_gradebook_only_in_confirmed_course_and_rejects_late_writes(gradebook):
    _paths, state, courses, _students, service = gradebook
    own = publish(service, upload(service))
    other = publish(service, upload(service, 'come3105', [('20260001', 90, 10, 'preserved')]))
    preview = courses.deletion_preview('come2201')
    assert preview['counts']['course_gradebooks'] == 1
    result = courses.delete_course('come2201', confirm_course='come2201', expected_state=preview['expected_state'])
    assert result['deleted'] and result['cleanup_status'] == 'complete'
    with state._connection() as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        for table in ('course_gradebooks', 'course_gradebook_rows', 'course_gradebook_state'):
            assert not db.execute(f'SELECT 1 FROM {table} WHERE course_key=?', ('come2201',)).fetchone()
    with pytest.raises(PlatformNotFound):
        service.review('come2201', own['draft_id'])
    assert service.student_view('come3105', credential(state, course='come3105'))['publication_id'] == other['draft_id']
    with pytest.raises(sqlite3.IntegrityError, match='course_permanently_deleted'):
        with state._write() as db:
            db.execute("INSERT INTO course_gradebook_state(course_key) VALUES('come2201')")
