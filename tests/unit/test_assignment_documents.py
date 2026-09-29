"""Published instructions evolve without changing accepted work or grading inputs."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from autograde.assignment_admin import AssignmentAdminService
from autograde.assignment_documents import digest
from autograde.course_admin import CourseAdminService
from autograde.platform_state import PlatformConflict, PlatformNotFound, PlatformStateStore
from autograde.settings import AppPaths
import test_bundle_platform_state as fixtures


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures, 'COURSE', 'come2201')
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    fixture = fixtures.BundleFixture(state)
    release = fixture.assignment(due_at=None)
    admin = AssignmentAdminService(state, paths)
    draft = admin.create_draft('come2201', description='# Original\nDo the work.')
    # Simulate a web release published by the preceding schema, without baseline rows.
    with state._write() as db:
        db.execute('UPDATE instructor_assignment_drafts SET published_assignment_id=? WHERE draft_id=?',
                   (release.assignment_id, draft['draft_id']))
    return state, fixture, release, admin, draft


def change(admin, release, revision=0, content='# Improved\nExtra explanation.'):
    return admin.update_assignment_document('come2201', release.assignment_id, revision, content, '예제 해설 보완')


def test_original_read_is_lazy_and_revisions_are_separate(published):
    state, fixture, release, admin, draft = published
    original = admin.get_assignment_document('come2201', release.assignment_id)
    assert original['revision'] == 0
    assert original['sha256'] == digest(original['content'])
    with state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM assignment_document_revisions').fetchone()[0] == 0
    edited = change(admin, release)
    assert edited['revision'] == 1 and edited['sha256'] == digest(edited['content'])
    assert [entry['revision'] for entry in edited['history']] == [1, 0]
    assert state.get_bundle_assignment(release.assignment_id) == release
    assert admin.get_draft('come2201', draft['draft_id'])['description'] == original['content']
    # Reopening the database must retain both documents, not rebuild from the old draft.
    reopened = AssignmentAdminService(PlatformStateStore(state.database), admin.paths)
    assert reopened.get_assignment_document('come2201', release.assignment_id) == edited
    copied = admin.copy_release('come2201', release.assignment_id)
    assert copied['description'] == edited['content']


def test_receipt_and_replay_keep_original_document_and_inputs(published):
    state, fixture, release, admin, _ = published
    before = fixture.submit()
    receipt = state.get_bundle_receipt(before.request.submission_id)
    change(admin, release)
    replay = fixture.submit()
    assert replay.replayed and replay.request.submission_id == before.request.submission_id
    later = fixture.submit(submission_id='bundle_sub_02', receipt_id='bundle_rcp_02', key='new-attempt')
    assert state.get_bundle_receipt(before.request.submission_id) == receipt
    assert state.get_bundle_receipt(later.request.submission_id).assessment_digest == receipt.assessment_digest
    with state._connection() as db:
        rows = list(db.execute('SELECT revision FROM bundle_submission_documents ORDER BY submission_id'))
        assert [r[0] for r in rows] == [0, 1]
    assert state.get_bundle_assignment(release.assignment_id) == release


def test_conflict_noop_and_concurrent_edits(published):
    state, _, release, admin, _ = published
    def edit(text):
        try:
            return change(admin, release, content=text)['revision']
        except PlatformConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(edit, ['Version A', 'Version B']))
    assert sorted(map(str, result)) == ['1', 'conflict']
    current = admin.get_assignment_document('come2201', release.assignment_id)
    assert change(admin, release, revision=1, content=current['content']) == current
    with pytest.raises(PlatformConflict):
        change(admin, release, revision=0)


@pytest.mark.parametrize('revision,content,note', [
    (-1, 'hello', 'reason'), (True, 'hello', 'reason'), ('0', 'hello', 'reason'),
    (0, '', 'reason'), (0, 'x' * 20001, 'reason'), (0, 'hello', ''),
    (0, 'hello', 'x' * 501), (0, 'bad\x00text', 'reason'), (0, '\ud800', 'reason'),
])
def test_invalid_edits_leave_no_rows(published, revision, content, note):
    state, _, release, admin, _ = published
    with pytest.raises(ValueError):
        admin.update_assignment_document('come2201', release.assignment_id, revision, content, note)
    with state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM assignment_document_revisions').fetchone()[0] == 0


def test_course_scope_and_archival_are_enforced(published):
    _, _, release, admin, _ = published
    with pytest.raises(PlatformNotFound):
        admin.get_assignment_document('come3105', release.assignment_id)
    with pytest.raises(PlatformNotFound):
        admin.update_assignment_document('come3105', release.assignment_id, 0, 'other', 'reason')
    CourseAdminService(admin.state).set_status('come2201', 'archived')
    with pytest.raises(PlatformConflict):
        change(admin, release)
    assert admin.get_assignment_document('come2201', release.assignment_id)['revision'] == 0


def test_immutable_storage_guards_and_cli_compatibility(published):
    state, fixture, release, admin, _ = published
    change(admin, release)
    fixture.submit()
    for table in ('assignment_document_revisions', 'bundle_submission_documents'):
        with pytest.raises(sqlite3.IntegrityError):
            with state._write() as db:
                db.execute(f'UPDATE {table} SET sha256=?', ('forged',))
        with pytest.raises(sqlite3.IntegrityError):
            with state._write() as db:
                db.execute(f'DELETE FROM {table}')
    cli = fixture.assignment(assignment_id='bundle_cli', assignment_key='cli')
    assert admin.get_assignment_document('come2201', cli.assignment_id) is None
    with pytest.raises(PlatformNotFound):
        change(admin, cli)


def test_history_is_bounded_but_old_content_is_preserved(published):
    state, _, release, admin, _ = published
    for revision in range(52):
        document = change(admin, release, revision=revision, content=f'Explanation {revision}')
    assert len(document['history']) == 50 and document['history_total'] == 53
    with state._connection() as db:
        assert db.execute('SELECT content FROM assignment_document_revisions WHERE revision=0').fetchone()[0].startswith('# Original')


def test_history_does_not_include_revision_newer_than_returned_body(published):
    from autograde.assignment_documents import current
    state, _, release, admin, _ = published
    change(admin, release)
    with state._connection() as db:
        class InterleavedReader:
            def execute(self, sql, parameters=()):
                if sql.startswith('SELECT revision,sha256,updated_at,change_note'):
                    change(admin, release, revision=1, content='Concurrent version 2')
                return db.execute(sql, parameters)
        document = current(InterleavedReader(), 'come2201', release.assignment_id)
    assert document['revision'] == 1 and document['history_total'] == 2
    assert [row['revision'] for row in document['history']] == [1, 0]


def test_schema_13_upgrade_preserves_receipts_without_fabricated_audit(tmp_path, monkeypatch):
    import autograde.platform_state as module
    with monkeypatch.context() as patch:
        patch.setattr(module, '_LATEST_SCHEMA_VERSION', 13)
        state = PlatformStateStore(tmp_path / 'legacy.sqlite3')
        fixture = fixtures.BundleFixture(state)
        release = fixture.assignment()
        receipt_id = fixture.submit().request.submission_id
        receipt = state.get_bundle_receipt(receipt_id)
    upgraded = PlatformStateStore(state.database)
    assert upgraded.schema_version() == PlatformStateStore.LATEST_SCHEMA_VERSION
    assert upgraded.get_bundle_assignment(release.assignment_id) == release
    assert upgraded.get_bundle_receipt(receipt_id) == receipt
    with upgraded._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM bundle_submission_documents').fetchone()[0] == 0
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
