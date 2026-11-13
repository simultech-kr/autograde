from datetime import timedelta

import pytest

from autograde.platform_state import PlatformConflict, PlatformNotFound
from autograde.student_material_batch import refresh
import autograde.student_material_batch as batches
import autograde.student_materials as materials
from test_student_materials import COURSE, PDF, ZIP, draft, published, setup, zip_bytes


def prepare(service, material, *, actor='teacher', entries=None):
    return service.prepare_batch(COURSE, material['material_id'], revision=material['revision'],
        data=zip_bytes(entries or [('001.pdf', PDF + b'\n'), ('002.zip', ZIP)]), actor=actor)


def test_review_is_private_and_apply_is_atomic_single_use(setup):
    _, service, _, students = setup
    material = published(service)
    review = prepare(service, material)
    assert [r['change'] for r in review['rows']] == ['replace', 'new']
    assert service.get(COURSE, material['material_id']) == material
    assert service.download(COURSE, material['material_id'], students[0].id).read_bytes() == PDF
    assert service.list_for_student(COURSE, students[1].id) == []
    assert service.review_batch_file(COURSE, material['material_id'], review['review_id'], students[1].id, actor='teacher')[0] == ZIP
    result = service.apply_batch(COURSE, material['material_id'], review['review_id'], actor='teacher')
    assert result['revision'] == material['revision'] + 1
    assert result['status'] == 'published' and result['file_count'] == 2
    assert service.download(COURSE, material['material_id'], students[0].id).read_bytes() == PDF + b'\n'
    assert len(service.file_history(COURSE, material['material_id'])) == 1
    with pytest.raises(PlatformNotFound):
        service.apply_batch(COURSE, material['material_id'], review['review_id'], actor='teacher')


@pytest.mark.parametrize('operation', ['review_batch', 'apply_batch', 'cancel_batch'])
def test_review_owner_and_course_scope(setup, operation):
    _, service, _, _ = setup
    material = draft(service)
    review = prepare(service, material)
    for course, actor in [(COURSE, 'another-teacher'), ('come3105', 'teacher')]:
        with pytest.raises(PlatformNotFound):
            getattr(service, operation)(course, material['material_id'], review['review_id'], actor=actor)
    assert service.get(COURSE, material['material_id']) == material


def test_invalid_batch_keeps_previous_review_and_current_publication(setup):
    _, service, _, _ = setup
    material = published(service)
    review = prepare(service, material)
    for entries in [[('001.pdf', PDF), ('999.pdf', PDF)], [('001.pdf', PDF), ('001.zip', ZIP)], [('001.pdf', b'bad')]]:
        with pytest.raises(ValueError):
            prepare(service, material, entries=entries)
        assert service.pending_batches(COURSE, material['material_id'], actor='teacher')['review_id'] == review['review_id']
        assert service.get(COURSE, material['material_id']) == material


def test_apply_storage_failure_rolls_back_all_assignments_and_keeps_review(setup, monkeypatch):
    _, service, _, students = setup
    material = published(service)
    review = prepare(service, material)
    original = service._store_file
    def fail_second(file, data):
        if file['student_id'] == students[1].id:
            raise OSError('synthetic disk failure')
        original(file, data)
    with monkeypatch.context() as context:
        context.setattr(service, '_store_file', fail_second)
        with pytest.raises(OSError, match='synthetic'):
            service.apply_batch(COURSE, material['material_id'], review['review_id'], actor='teacher')
    assert service.get(COURSE, material['material_id']) == material
    assert service.file_history(COURSE, material['material_id']) == []
    assert service.download(COURSE, material['material_id'], students[0].id).read_bytes() == PDF
    assert service.list_for_student(COURSE, students[1].id) == []
    assert service.pending_batches(COURSE, material['material_id'], actor='teacher')['review_id'] == review['review_id']
    assert service.apply_batch(COURSE, material['material_id'], review['review_id'], actor='teacher')['file_count'] == 2


def test_stale_review_requires_explicit_refresh_with_saved_zip(setup):
    _, service, _, _ = setup
    material = published(service)
    review = prepare(service, material)
    changed = service.update(COURSE, material['material_id'], revision=material['revision'], actor='teacher',
        title='changed', instructions='', opens_at=material['opens_at'], closes_at=material['closes_at'])
    assert service.review_batch(COURSE, material['material_id'], review['review_id'], actor='teacher')['stale']
    with pytest.raises(PlatformConflict):
        service.apply_batch(COURSE, material['material_id'], review['review_id'], actor='teacher')
    assert service.get(COURSE, material['material_id']) == changed
    reviewed = refresh(service, COURSE, material['material_id'], review['review_id'], actor='teacher')
    assert not reviewed['stale'] and reviewed['revision'] == changed['revision']
    assert reviewed['review_id'] != review['review_id']
    assert service.apply_batch(COURSE, material['material_id'], reviewed['review_id'], actor='teacher')['file_count'] == 2


def test_roster_change_between_review_and_apply_never_retargets(setup):
    state, service, _, students = setup
    material = draft(service)
    review = prepare(service, material)
    with state._write() as db:
        db.execute('UPDATE admin_student_profiles SET name=? WHERE student_id=?', ('changed name', students[0].id))
    with pytest.raises(PlatformConflict, match='학생 정보'):
        service.apply_batch(COURSE, material['material_id'], review['review_id'], actor='teacher')
    assert service.get(COURSE, material['material_id']) == material


def test_expiry_cancel_and_bounded_staging(setup, monkeypatch):
    state, service, clock, _ = setup
    material = draft(service)
    review = prepare(service, material)
    clock[0] += timedelta(hours=1)
    with pytest.raises(PlatformConflict, match='만료'):
        service.apply_batch(COURSE, material['material_id'], review['review_id'], actor='teacher')
    review2 = prepare(service, material)
    with state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM student_material_batch_reviews').fetchone()[0] == 1
    monkeypatch.setattr(batches, 'MAX_PENDING_BYTES', 1)
    with pytest.raises(PlatformConflict, match='임시 검토 공간'):
        prepare(service, material, actor='another')
    service.cancel_batch(COURSE, material['material_id'], review2['review_id'], actor='teacher')
    assert service.pending_batches(COURSE, material['material_id'], actor='teacher') is None
    assert not service.root.exists()


def test_nested_zip_budget_error_names_file_and_actual_limit(setup, monkeypatch):
    _, service, _, _ = setup
    inner = zip_bytes([('exercise.txt', b'a' * 1024)])
    bundle = zip_bytes([('001.zip', inner)])
    monkeypatch.setattr(materials, 'MAX_BATCH_BYTES', len(bundle) + 100)
    with pytest.raises(ValueError) as failure:
        service.prepare_batch(COURSE, draft(service)['material_id'], revision=1, data=bundle, actor='teacher')
    assert all(word in str(failure.value) for word in ('001.zip', 'exercise.txt', '누적 검증', 'MiB', '기존 배정'))


def test_course_deletion_and_privacy_closure_include_review_blob(setup):
    from autograde.course_deletion import _targets
    from autograde.course_privacy_archive import _targets as privacy_targets
    state, service, _, _ = setup
    material = draft(service)
    review = prepare(service, material)
    with state._connection() as db:
        for targets in (_targets, privacy_targets):
            assert targets(db, COURSE)['student_material_batch_reviews'][0]['review_id'] == review['review_id']
