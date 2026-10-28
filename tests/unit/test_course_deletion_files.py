import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from autograde.course_deletion_files import plan_course_files, recover_course_files
from autograde.platform_state import PlatformConflict
from autograde.settings import AppPaths


@pytest.fixture
def files(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    db = sqlite3.connect(paths.database)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE bundle_assignment_releases(assignment_id TEXT, course_key TEXT, starter_path TEXT, starter_digest TEXT, assessment_path TEXT);
        CREATE TABLE student_materials(material_id TEXT, course_key TEXT);
        CREATE TABLE exam_papers(paper_id TEXT, course_key TEXT);
        CREATE TABLE admin_course_deletions(job_id TEXT PRIMARY KEY, course_key TEXT);
    ''')
    yield paths, db
    db.close()


def put(paths, name, text='test data'):
    target = paths.root / name
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(text)
    return target


def snapshot(db):
    return {table: [dict(row) for row in db.execute(f'SELECT rowid AS __rowid__, * FROM {table} WHERE course_key=?', ('one',))]
            for table in ('bundle_assignment_releases', 'student_materials', 'exam_papers')}


def seed(files):
    paths, db = files
    shared, private = 'a' * 64, 'b' * 64
    common = put(paths, f'bundles/sha256/aa/{shared}/bundle.tar.gz')
    own = put(paths, f'bundles/sha256/bb/{private}/bundle.tar.gz')
    assessment = put(paths, f'instructor-inputs/assessment/{private}/grade.py')
    db.executemany('INSERT INTO bundle_assignment_releases VALUES(?,?,?,?,?)', [
        ('a1', 'one', str(common), 'sha256:' + shared, str(assessment.parent)),
        ('a2', 'one', str(own), 'sha256:' + private, str(assessment.parent)),
        ('a3', 'two', str(common), 'sha256:' + shared, '/outside/keep'),
    ])
    mid, paper = 'mat_' + '1' * 32, 'ep_' + '2' * 32
    db.execute('INSERT INTO student_materials VALUES(?,?)', (mid, 'one'))
    db.execute('INSERT INTO exam_papers VALUES(?,?)', (paper, 'one'))
    material = put(paths, f'student-materials/{mid}-5-{private}.zip')
    exam = put(paths, f'exam-papers/{paper}-{private}.pdf')
    content = put(paths, f'content/objects/{hashlib.sha256(b"one").hexdigest()}/{private}')
    checkpoint = paths.root / ('state.sqlite3.learning-checkpoints/' + 'c' * 64)
    put(paths, str(checkpoint.relative_to(paths.root) / 'index.json'), json.dumps({'checkpoints': [{'assignment_id': 'a1'}]}))
    put(paths, str(checkpoint.relative_to(paths.root) / 'lock'))
    source = put(paths, str(checkpoint.relative_to(paths.root) / 'archives/source.tar.gz'))
    db.commit()
    return common, own, assessment, material, exam, content, source


def test_stage_and_rollback_restore_all_private_files_without_touching_shared(files):
    paths, db = files
    common, *owned = seed(files)
    retained, owned = owned[:2], owned[2:]
    plan = plan_course_files(paths, db, 'one', snapshot(db))
    assert plan.summary['shared_objects_preserved'] == 3
    plan.stage('delete_test')
    assert common.exists()
    assert all(p.exists() for p in retained)
    assert not any(p.exists() for p in owned)
    plan.rollback()
    assert all(p.read_text() for p in (common, *owned))


def test_commit_purge_and_restart_keep_other_course_and_delete_only_owned_files(files):
    paths, db = files
    common, *owned = seed(files)
    retained, owned = owned[:2], owned[2:]
    plan = plan_course_files(paths, db, 'one', snapshot(db))
    plan.stage('delete_commit')
    db.execute('INSERT INTO admin_course_deletions VALUES(?,?)', ('delete_commit', 'one'))
    db.commit()
    plan._unlock()  # Simulate process exit before file purge.
    class State:
        @contextmanager
        def _connection(self):
            yield db
    assert recover_course_files(paths, State()) == 1
    assert common.exists()
    assert all(p.exists() for p in retained)
    assert not any(p.exists() for p in owned)
    assert not (paths.root / '.course-deletions/delete_commit').exists()
    assert recover_course_files(paths, State()) == 0


def test_uncommitted_restart_restores_staged_files(files):
    paths, db = files
    common, *owned = seed(files)
    plan = plan_course_files(paths, db, 'one', snapshot(db))
    plan.stage('delete_rollback')
    plan._unlock()
    class State:
        @contextmanager
        def _connection(self):
            yield db
    recover_course_files(paths, State())
    assert all(p.exists() for p in (common, *owned))


def test_symlink_inside_target_aborts_before_files_move(files, tmp_path):
    paths, db = files
    common, own, *_ = seed(files)
    outside = tmp_path / 'outside'
    outside.write_text('keep')
    # Global CAS is retained online; a course-owned content link must abort.
    content = paths.root / ('content/objects/' + hashlib.sha256(b'one').hexdigest())
    (content / 'linked').symlink_to(outside)
    with pytest.raises(PlatformConflict):
        plan_course_files(paths, db, 'one', snapshot(db))
    assert common.exists() and own.exists() and outside.read_text() == 'keep'


def test_partial_stage_failure_rolls_back_and_never_overwrites_replaced_target(files):
    paths, db = files
    seed(files)
    plan = plan_course_files(paths, db, 'one', snapshot(db))
    replaced = paths.root / plan.entries[-1]['relative']
    replaced.rename(replaced.with_name(replaced.name + '-old'))
    replaced.mkdir()
    (replaced / 'keep.txt').write_text('new content')
    with pytest.raises(PlatformConflict):
        plan.stage('delete_changed')
    plan.rollback()
    assert (replaced / 'keep.txt').read_text() == 'new content'


def test_manifest_cannot_delete_database_or_outside_paths(files):
    paths, db = files
    seed(files)
    plan = plan_course_files(paths, db, 'one', snapshot(db))
    plan.stage('delete_tampered')
    manifest = plan.folder / 'manifest.json'
    value = json.loads(manifest.read_text())
    value['entries'][-1]['relative'] = 'state.sqlite3'
    manifest.write_text(json.dumps(value))
    with pytest.raises(PlatformConflict):
        plan.purge()
    assert paths.database.exists()


def test_active_checkpoint_save_blocks_delete_and_releases_locks(files):
    import fcntl
    paths, db = files
    seed(files)
    lock = paths.root / ('state.sqlite3.learning-checkpoints/' + 'c' * 64 + '/lock')
    with lock.open('r+') as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = plan_course_files(paths, db, 'one', snapshot(db))
        with pytest.raises(PlatformConflict):
            plan.stage('delete_busy')
        plan.rollback()
        assert not plan._locks
        assert lock.exists()


def test_first_save_owner_without_published_index_is_deleted(files):
    paths, db = files
    seed(files)
    owner = 'state.sqlite3.learning-checkpoints/' + 'd' * 64
    put(paths, owner + '/owner.json', json.dumps({'course_key': 'one', 'assignment_id': 'a1'}))
    private = put(paths, owner + '/archives/unfinished.tar.gz')
    plan = plan_course_files(paths, db, 'one', snapshot(db))
    plan.stage('delete_first_save')
    plan.purge()
    assert not private.exists()


def test_late_file_change_invalidates_deletion_review_and_releases_course_guard(files):
    from autograde.course_file_guard import course_file_guard
    paths, db = files
    seed(files)
    plan = plan_course_files(paths, db, 'one', snapshot(db))
    put(paths, f'content/objects/{hashlib.sha256(b"one").hexdigest()}/late', 'late upload')
    with pytest.raises(PlatformConflict):
        plan.stage('delete_late')
    plan.rollback()
    with course_file_guard(paths.root, 'one'):
        pass


def test_legacy_submission_workspace_and_report_are_owned_deletion_targets(files):
    paths, db = files
    db.execute('CREATE TABLE submission_requests(submission_id TEXT, course_key TEXT)')
    db.execute('INSERT INTO submission_requests VALUES(?,?)', ('sub_legacy', 'one'))
    source = put(paths, 'workspaces/sub_legacy/source/main.c')
    report = put(paths, 'reports/sub_legacy.json')
    rows = snapshot(db)
    rows['submission_requests'] = [dict(row) for row in db.execute('SELECT rowid AS __rowid__, * FROM submission_requests')]
    plan = plan_course_files(paths, db, 'one', rows)
    plan.stage('delete_legacy')
    plan.purge()
    assert not source.exists() and not report.exists()
