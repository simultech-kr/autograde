"""Synthetic source/evaluation records cannot race or undo privacy erasure."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from autograde.course_privacy_files import _cleanup_rubric
from autograde.course_admin import CourseAdminService
from autograde.platform_state import PlatformConflict, PlatformStateStore
from autograde.privacy_file_guard import privacy_file_guard
from autograde.rubric_engine import RubricConflict
from autograde.rubric_store import RubricStore
from test_course_portal import portal
from test_connected_rubric_assessment import connected, approve as approve_web, basic
from test_rubric_local_workflow import setup, approve, assess


def catalog_path(store):
    target = store.path.parent / 'rubric-catalog.sqlite3'
    store.path.rename(target)
    store.path = target
    return SimpleNamespace(root=target.parent)


def test_offline_import_fences_calculation_through_commit(setup, monkeypatch):
    store, rubric, evidence, profile, row = setup
    approve(store, row)
    import autograde.rubric_store as module
    calculate, started, resume = module.evaluate, Event(), Event()

    def paused(*args):
        started.set()
        assert resume.wait(5)
        return calculate(*args)

    monkeypatch.setattr(module, 'evaluate', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(assess, store, rubric, evidence, profile)
        try:
            assert started.wait(5)
            with pytest.raises(PlatformConflict):
                with privacy_file_guard(store.path.parent, exclusive=True):
                    pytest.fail('erasure must not overlap imported evidence publication')
        finally:
            resume.set()
        result = saving.result(timeout=5)
    assert store.get_assessment(rubric['course_key'], result['assessment_id']) == result


def test_erasure_removes_real_import_and_blocks_api_and_sql_recreation(setup):
    store, rubric, evidence, profile, row = setup
    approve(store, row)
    assess(store, rubric, evidence, profile)
    paths = catalog_path(store)
    with store._connect() as db:
        db.execute("INSERT INTO source_receipts VALUES('other-course','other-submission','other-assignment','other-source','other-files')")
    with privacy_file_guard(paths.root, exclusive=True):
        _cleanup_rubric(paths, rubric['course_key'])
    with pytest.raises(RubricConflict, match='파기'):
        assess(store, rubric, evidence, profile, key='late-import')
    with store._connect() as db:
        for table in ('source_receipts', 'rubric_assessments', 'test_evidence_runs', 'rubric_audit'):
            assert db.execute(f'SELECT COUNT(*) FROM {table} WHERE course_key=?', (rubric['course_key'],)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM source_receipts WHERE course_key='other-course'").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO source_receipts VALUES('come2201','late','assignment','digest','files')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE source_receipts SET course_key='come2201' WHERE course_key='other-course'")
    # A second cleanup after restart remains idempotent and keeps the tombstone.
    _cleanup_rubric(paths, rubric['course_key'])
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM rubric_course_privacy_archives').fetchone()[0] == 1


def test_connected_snapshot_is_fenced_and_stale_course_cannot_restore_erased_evidence(connected, monkeypatch):
    c = connected
    approve_web(c)
    evaluator, store = c['catalog'].assessments, c['catalog'].store
    course = c['courses'].get_course('come3105')
    principal = c['identities'].authenticate(basic())
    session = {'sid': 'synthetic-privacy-review'}
    row, _, ticket = evaluator.prepare(course, principal, c['row']['rubric_id'], 1, c['sid'], session)
    snapshot, started, resume = evaluator.source.snapshot, Event(), Event()

    def paused(*args):
        value = snapshot(*args)
        started.set()
        assert resume.wait(5)
        return value

    monkeypatch.setattr(evaluator.source, 'snapshot', paused)
    arguments = (course, principal, row['rubric_id'], 1, c['sid'], session, ticket, [])
    with ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(evaluator.save, *arguments)
        try:
            assert started.wait(5)
            with pytest.raises(PlatformConflict):
                with privacy_file_guard(c['state'].database.parent, exclusive=True):
                    pytest.fail('erasure must not overlap a connected source snapshot')
        finally:
            resume.set()
        result = saving.result(timeout=5)
    assert result['submission_id'] == c['sid']
    _cleanup_rubric(SimpleNamespace(root=store.path.parent), 'come3105')
    with pytest.raises(RubricConflict, match='파기'):
        evaluator.save(*arguments)  # Same stale active-course object and signed ticket.
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM rubric_web_assessments').fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO rubric_web_assessments VALUES('late','come3105','submission','rubric',1,'key','digest','{}','{}','actor','now')")


def test_new_catalog_honors_course_erased_before_catalog_existed(setup, tmp_path):
    _, rubric, evidence, profile, _ = setup
    state = PlatformStateStore(tmp_path / 'state.sqlite3')
    courses = CourseAdminService(state)
    courses.set_status('come2201', 'archived')
    _cleanup_rubric(SimpleNamespace(root=tmp_path), 'come2201')
    assert not (tmp_path / 'rubric-catalog.sqlite3').exists()
    store = RubricStore(tmp_path / 'rubric-catalog.sqlite3')
    store.initialize()
    row = store.register(rubric, 'operator')
    approve(store, row)
    with pytest.raises(RubricConflict, match='파기'):
        assess(store, rubric, evidence, profile)
    with store._connect() as db:
        for table in ('source_receipts', 'rubric_assessments', 'test_evidence_runs'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    with store.privacy_guard('come3105'):
        pass  # An unrelated active course is still usable.


def test_standalone_assessment_does_not_create_platform_database(setup):
    store, rubric, evidence, profile, row = setup
    approve(store, row)
    assess(store, rubric, evidence, profile)
    assert not (store.path.parent / 'state.sqlite3').exists()


def test_unreadable_platform_journal_cannot_be_ignored(setup):
    store, rubric, evidence, profile, row = setup
    approve(store, row)
    platform = store.path.parent / 'state.sqlite3'
    platform.write_bytes(b'synthetic damaged state')
    with pytest.raises(RubricConflict, match='확인하지 못해'):
        assess(store, rubric, evidence, profile)
    assert platform.read_bytes() == b'synthetic damaged state'
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM rubric_assessments').fetchone()[0] == 0
