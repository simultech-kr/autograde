from dataclasses import replace

import pytest

from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.course_gradebook import CourseGradebook
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, PlatformStateStore
from test_gradebook_excel import excel


@pytest.fixture
def setup(tmp_path):
    state = PlatformStateStore(tmp_path / 'state.sqlite')
    courses = CourseAdminService(state)
    students = EnrollmentAdminService(state, b's' * 32)
    for course in ('come2201', 'come3105'):
        for key in ('001', '002'):
            students.add_student(course, student_key=key, name='Synthetic ' + key, password='123' + key)
    return state, courses, students, CourseGradebook(state)


def credential(setup, key='001', course='come2201'):
    return setup[0].get_student_password_credential(student_key=key, course_key=course)


def upload(setup, rows=None, course='come2201'):
    return setup[3].import_workbook(course, excel(rows or [['학번', '중간', '기말', '출석'], ['001', 0, None, 9.5], ['002', 90, 80, 10]]), '성적.xlsx')


def publish(setup, draft, **kwargs):
    return setup[3].publish(draft['course_key'], draft['draft_id'], draft['revision'], **kwargs)


def test_preview_not_public_then_own_selected_values_only(setup):
    service = setup[3]
    draft = upload(setup)
    assert draft['student_count'] == draft['matched_count'] == 2
    assert draft['unmatched_keys'] == [] and draft['version'] == 0
    assert service.student_view('come2201', credential(setup)) is None
    book = publish(setup, draft, selected_columns=['column_2', 'column_3'])
    assert book['version'] == 1 and book['revision'] == 2
    own = service.student_view('come2201', credential(setup))
    assert own['items'] == [{'key': 'column_2', 'label': '중간', 'value': 0}, {'key': 'column_3', 'label': '기말', 'value': None}]
    assert 'student_key' not in own and 'rows' not in own and 'filename' not in own
    assert service.student_view('come2201', credential(setup, '002'))['items'][0]['value'] == 90
    assert service.student_view('come3105', credential(setup, course='come3105')) is None


def test_atomic_replacement_does_not_merge_missing_students_or_publish_draft(setup):
    service = setup[3]
    first = publish(setup, upload(setup))
    replacement = upload(setup, [['학번', '출석'], ['001', 8]])
    assert replacement['missing_student_count'] == 1
    assert service.student_view('come2201', credential(setup, '002'))['version'] == 1
    second = publish(setup, replacement)
    assert second['version'] == 2 and service.review('come2201', first['draft_id'])['status'] == 'superseded'
    assert service.student_view('come2201', credential(setup))['items'] == [{'key': 'column_2', 'label': '출석', 'value': 8}]
    assert service.student_view('come2201', credential(setup, '002')) is None


def test_unmatched_and_revoked_roster_are_rechecked_at_publish(setup):
    state, _, students, service = setup
    draft = upload(setup, [['학번', '기말'], ['unknown', 100]])
    assert draft['unmatched_keys'] == ['unknown']
    with pytest.raises(PlatformConflict, match='명단'):
        publish(setup, draft)
    students.add_student('come2201', student_key='unknown', password='123456')
    assert publish(setup, draft)['matched_count'] == 1
    another = upload(setup)
    second_id = credential(setup, '002').student_id
    with state._write() as db:
        db.execute("UPDATE platform_enrollments SET active=0 WHERE student_id=? AND course_key='come2201'", (second_id,))
    with pytest.raises(PlatformConflict, match='명단'):
        publish(setup, another)


def test_publication_cas_prevents_old_draft_overwriting_newer_gradebook(setup):
    service = setup[3]
    first, second = upload(setup), upload(setup)
    publish(setup, second)
    with pytest.raises(PlatformConflict, match='다른 성적표'):
        publish(setup, first)
    with pytest.raises(PlatformConflict):
        service.publish('come2201', second['draft_id'], second['revision'])
    assert service.student_view('come2201', credential(setup))['publication_id'] == second['draft_id']


def test_hide_and_delete_draft_are_scope_and_revision_checked(setup):
    service = setup[3]
    draft = upload(setup)
    with pytest.raises(PlatformNotFound):
        service.review('come3105', draft['draft_id'])
    with pytest.raises(PlatformConflict):
        service.delete_draft('come2201', draft['draft_id'], 999)
    service.delete_draft('come2201', draft['draft_id'], draft['revision'])
    with pytest.raises(PlatformNotFound):
        service.review('come2201', draft['draft_id'])
    book = publish(setup, upload(setup))
    with pytest.raises(PlatformConflict):
        service.delete_draft('come2201', book['draft_id'], book['revision'])
    assert service.hide('come2201', book['draft_id'], book['revision'])['status'] == 'hidden'
    assert service.student_view('come2201', credential(setup)) is None


@pytest.mark.parametrize('selected', [[], ['unknown'], ['column_2', 'column_2']])
def test_invalid_publication_column_selection(setup, selected):
    with pytest.raises(ValueError, match='점수 항목'):
        publish(setup, upload(setup), selected_columns=selected)


def test_reads_recheck_course_enrollment_password_and_current_member(setup):
    state, _, _, service = setup
    publish(setup, upload(setup))
    current = credential(setup)
    with pytest.raises(PlatformAccessDenied):
        service.student_view('come3105', current)
    with pytest.raises(PlatformAccessDenied):
        service.student_view('come2201', replace(current, password_hash='revoked'))
    from autograde.student_registration import initialize
    initialize(state)
    with state._write() as db:
        db.execute('INSERT INTO student_accounts(student_key,password_hash,student_id,verified,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                   ('001','synthetic-verifier',current.student_id,1,'now','now'))
    assert service.student_view('come2201', current, member_verifier=('001', 'synthetic-verifier'))
    with pytest.raises(PlatformAccessDenied):
        service.student_view('come2201', current, member_verifier=('001', 'revoked-verifier'))
    with state._write() as db:
        db.execute("UPDATE platform_enrollments SET active=0 WHERE course_key='come2201' AND student_id=?", (current.student_id,))
    with pytest.raises(PlatformAccessDenied):
        service.student_view('come2201', current)


def test_preparation_course_allows_instructor_draft_but_no_student_access(setup):
    course = setup[1].create_course(code='new', name='New course', year=2026, semester='2')['course_key']
    setup[2].add_student(course, student_key='001', password='123001')
    draft = upload(setup, [['학번', '점수'], ['001', 20]], course=course)
    publish(setup, draft)
    with pytest.raises(PlatformAccessDenied):
        setup[3].student_view(course, credential(setup, course=course))
