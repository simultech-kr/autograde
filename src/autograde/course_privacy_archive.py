"""Irreversible course closure: erase personal records, retain coarse opt-in totals.

An archive is not a reversible visibility switch. A durable, identity-free job
records unfinished managed-file/worker cleanup, independently of external
backups, CSV imports and exports which this server cannot recall.
"""
from __future__ import annotations

import json
import math
import secrets

from .course_deletion import _columns, _foreign_keys, _q, _tables
from .platform_state import PlatformConflict, utc_iso
from .privacy_file_guard import privacy_file_guard

MINIMUM_GROUP_SIZE = 5
EXTERNAL_COPY_TASKS = [
    '운영자가 별도 백업·복제본의 보관 기한과 파기 여부를 확인해야 합니다. 복원 시 이 수업의 파기 기록을 먼저 적용하세요.',
    '초기 명단 CSV와 교수자·학생이 내보낸 파일은 이 서버가 회수하지 못합니다. 해당 보관 위치에서 별도로 삭제하세요.',
    '별도 웹서버·접속 로그에 학번·IP·요청 주소가 남는지 확인하고 기관의 로그 보관·파기 절차를 적용하세요.',
]
SCHEMA = '''CREATE TABLE IF NOT EXISTS admin_course_privacy_archives (
 job_id TEXT PRIMARY KEY, course_key TEXT NOT NULL UNIQUE REFERENCES admin_courses(course_key),
 archived_at TEXT NOT NULL, aggregate_json TEXT NOT NULL, counts_json TEXT NOT NULL,
 cleanup_status TEXT NOT NULL CHECK(cleanup_status IN ('pending','complete')),
 pending_tasks_json TEXT NOT NULL, remote_context_json TEXT NOT NULL
)'''
_KEEP = {
    'admin_courses', 'admin_course_deletions', 'admin_course_privacy_archives',
    'bundle_assignment_releases', 'bundle_release_checks', 'bundle_deadline_changes',
    'assignment_document_revisions', 'exam_papers', 'exam_paper_audit',
    'instructor_assignment_create_requests', 'instructor_assignment_deletions',
    'instructor_assignment_drafts', 'instructor_assignment_events', 'instructor_assignment_jobs',
    'instructor_assignment_origins', 'instructor_assignment_uploads',
    'ide_extension_releases', 'ide_extension_desired', 'ide_extension_audit',
}
_CONTENT = {'content_collections', 'content_releases', 'content_release_files',
            'content_draft_files', 'content_audit', 'content_publish_jobs'}


def initialize(state, connection=None):
    if connection is None:
        with state._write() as db:
            initialize(state, db)
        return
    db = connection
    db.execute(SCHEMA)
    for table in _tables(db):
        if table in {'admin_course_deletions', 'admin_course_privacy_archives', 'admin_courses'} or 'course_key' not in _columns(db, table):
            continue
        for event in ('INSERT', 'UPDATE'):
            db.execute(f'CREATE TRIGGER IF NOT EXISTS {_q("privacy_closed_" + event.lower() + "_" + table)} '
                       f'BEFORE {event} ON {_q(table)} WHEN EXISTS '
                       '(SELECT 1 FROM admin_course_privacy_archives WHERE course_key=NEW.course_key) '
                       "BEGIN SELECT RAISE(ABORT,'course_privacy_closed'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS privacy_closed_course_reactivation BEFORE UPDATE ON admin_courses "
               "WHEN NEW.status!='archived' AND EXISTS(SELECT 1 FROM admin_course_privacy_archives WHERE course_key=NEW.course_key) "
               "BEGIN SELECT RAISE(ABORT,'course_privacy_closed'); END")


def _targets(db, key):
    tables = _tables(db)
    db.execute('DROP TABLE IF EXISTS temp.ag_privacy_rows')
    db.execute('CREATE TEMP TABLE ag_privacy_rows(table_name TEXT,row_id INTEGER,PRIMARY KEY(table_name,row_id))')
    for table in tables:
        if table not in _KEEP | _CONTENT and 'course_key' in _columns(db, table):
            db.execute(f'INSERT INTO ag_privacy_rows SELECT ?,rowid FROM {_q(table)} WHERE course_key=?', (table, key))
    # A previously targeted collection remains private even if its current
    # audience was changed to everyone; its complete history must be removed.
    db.execute("INSERT OR IGNORE INTO ag_privacy_rows SELECT 'content_collections',c.rowid FROM content_collections c "
               "WHERE c.course_key=? AND (c.student_ids_json IS NOT NULL OR EXISTS "
               "(SELECT 1 FROM content_releases r WHERE r.collection_id=c.collection_id AND r.student_ids_json IS NOT NULL))", (key,))
    previous = -1
    while previous != (count := db.execute('SELECT COUNT(*) FROM ag_privacy_rows').fetchone()[0]):
        previous = count
        for table in tables:
            if table in _KEEP:
                continue
            for parent, pairs in _foreign_keys(db, table):
                join = ' AND '.join(f'c.{_q(child)}=p.{_q(target)}' for child, target in pairs)
                db.execute(f'INSERT OR IGNORE INTO ag_privacy_rows SELECT ?,c.rowid FROM {_q(table)} c '
                           f'JOIN {_q(parent)} p ON {join} JOIN ag_privacy_rows selected '
                           'ON selected.table_name=? AND selected.row_id=p.rowid', (table, parent))
    # References through preserved assignment/paper parents still identify a
    # student's activity and must be erased as well.
    for table, parent, column in (
        ('bundle_submission_requests', 'bundle_assignment_releases', 'assignment_id'),
        ('bundle_download_events', 'bundle_assignment_releases', 'assignment_id'),
        ('exam_paper_requests', 'exam_papers', 'paper_id'),
    ):
        db.execute(f'INSERT OR IGNORE INTO ag_privacy_rows SELECT ?,c.rowid FROM {_q(table)} c '
                   f'JOIN {_q(parent)} p USING({_q(column)}) WHERE p.course_key=?', (table, key))
    # Repeat closure after those source records have been seeded.
    previous = -1
    while previous != (count := db.execute('SELECT COUNT(*) FROM ag_privacy_rows').fetchone()[0]):
        previous = count
        for table in tables:
            if table in _KEEP:
                continue
            for parent, pairs in _foreign_keys(db, table):
                join = ' AND '.join(f'c.{_q(child)}=p.{_q(target)}' for child, target in pairs)
                db.execute(f'INSERT OR IGNORE INTO ag_privacy_rows SELECT ?,c.rowid FROM {_q(table)} c '
                           f'JOIN {_q(parent)} p ON {join} JOIN ag_privacy_rows selected '
                           'ON selected.table_name=? AND selected.row_id=p.rowid', (table, parent))
    rows = {}
    for table in tables:
        selected = [dict(row) for row in db.execute(f'SELECT t.rowid AS __rowid__,t.* FROM {_q(table)} t '
            'JOIN ag_privacy_rows s ON s.table_name=? AND s.row_id=t.rowid', (table,))]
        if selected:
            if any(row.get('course_key', key) != key for row in selected):
                raise PlatformConflict('다른 수업과 연결된 자료가 있어 개인정보 보관을 중단했습니다.')
            rows[table] = selected
    for table in rows:
        for parent, pairs in _foreign_keys(db, table):
            if 'course_key' not in _columns(db, parent):
                continue
            join = ' AND '.join(f'c.{_q(child)}=p.{_q(target)}' for child, target in pairs)
            if db.execute(f'SELECT 1 FROM {_q(table)} c JOIN {_q(parent)} p ON {join} '
                          'JOIN ag_privacy_rows s ON s.table_name=? AND s.row_id=c.rowid '
                          'WHERE p.course_key<>? LIMIT 1', (table, key)).fetchone():
                raise PlatformConflict('다른 수업의 데이터를 참조하고 있어 보관을 중단했습니다.')
    return rows


def _aggregate(db, key):
    from .student_privacy import POLICY_VERSION
    result = {'schema': 'autograde.course-research-aggregate.v1', 'minimum_group_size': MINIMUM_GROUP_SIZE, 'assignments': []}
    if 'student_privacy_consents' not in _tables(db):
        return result
    values = db.execute('''WITH ranked AS (
        SELECT r.student_id,r.assignment_id,g.score,g.max_score,
          ROW_NUMBER() OVER(PARTITION BY r.student_id,r.assignment_id ORDER BY r.received_at DESC,r.rowid DESC) position
        FROM bundle_submission_requests r JOIN bundle_assignment_releases a USING(assignment_id)
        JOIN bundle_submission_results g USING(submission_id)
        JOIN student_privacy_consents c ON c.student_id=r.student_id AND c.course_key=a.course_key
        WHERE a.course_key=? AND c.decision='granted' AND c.policy_version=?
          AND r.state='published' AND g.published_at IS NOT NULL)
        SELECT assignment_id,COUNT(*) n,AVG(100.0*score/max_score) average FROM ranked
        WHERE position=1 AND max_score>0 AND score>=0 AND score<=max_score
        GROUP BY assignment_id HAVING COUNT(*)>=?''', (key, POLICY_VERSION, MINIMUM_GROUP_SIZE)).fetchall()
    for row in values:
        if not math.isfinite(row['average']):
            continue
        low = min(90, int(row['average'] // 10) * 10)
        result['assignments'].append({'assignment_id': row['assignment_id'],
            'cohort_size_floor': (row['n'] // MINIMUM_GROUP_SIZE) * MINIMUM_GROUP_SIZE,
            'mean_score_band': [low, low + 10]})
    return result


def _review(service, db, key):
    from .course_admin import _course
    from .course_deletion import _idle, _digest
    course = _course(db, key)
    _idle(db, key)
    rows = _targets(db, key)
    # Older repository-mode mirrors use an independent repository ledger and
    # require its explicit maintenance path, not a misleading partial erasure.
    if rows.get('platform_assignments'):
        raise PlatformConflict('이 수업에는 이전 Git 저장소 방식의 실습이 있습니다. 운영자의 저장소 원본 정리를 완료한 뒤 보관하세요.')
    files = service.privacy_file_planner(db, key, rows) if service.privacy_file_planner else None
    if files is None and any(rows.get(table) for table in ('bundle_submission_requests', 'student_materials', 'content_collections')):
        raise PlatformConflict('학생 파일 파기 기능이 연결되지 않았습니다. 운영 설정을 확인한 뒤 보관하세요.')
    summary = files.summary if files else {'files': 0, 'bytes': 0, 'configured': False}
    counts = {table: len(items) for table, items in rows.items()}
    return dict(course=course, enrollments=counts.get('platform_enrollments', 0),
                submissions=counts.get('bundle_submission_requests', 0), private_materials=counts.get('student_materials', 0),
                checkpoint_cleanup=files is not None, irreversible=True, retention='consented_aggregate_only',
                minimum_group_size=MINIMUM_GROUP_SIZE, files=summary, expected_state=_digest(rows, summary),
                external_copy_tasks=EXTERNAL_COPY_TASKS), rows, files


def preview(service, key):
    with service.state._connection() as db:
        db.execute('BEGIN')
        report, _, _ = _review(service, db, key)
        return report


def status(service, key):
    with service.state._connection() as db:
        row = db.execute('SELECT * FROM admin_course_privacy_archives WHERE course_key=?', (key,)).fetchone()
    if row is None:
        return None
    return {'course_key': key, 'archived_at': row['archived_at'], 'cleanup_status': row['cleanup_status'],
            'aggregate': json.loads(row['aggregate_json']), 'pending_tasks': json.loads(row['pending_tasks_json']),
            'external_copy_tasks': EXTERNAL_COPY_TASKS, 'irreversible': True}


def _drop_personal_rows(db, rows):
    guards = [r for r in db.execute("SELECT name,sql,tbl_name FROM sqlite_master WHERE type='trigger'") if r['tbl_name'] in rows]
    for guard in guards:
        db.execute(f'DROP TRIGGER {_q(guard["name"])}')
    db.execute('PRAGMA defer_foreign_keys=ON')
    for table in rows:
        db.execute(f'DELETE FROM {_q(table)} WHERE rowid IN (SELECT row_id FROM ag_privacy_rows WHERE table_name=?)', (table,))
    for guard in guards:
        db.execute(guard['sql'])


def _orphan_accounts(db, student_ids, student_keys):
    tables = _tables(db)
    for key in student_keys:
        if any('student_key' in _columns(db, table) and 'course_key' in _columns(db, table) and
               db.execute(f'SELECT 1 FROM {_q(table)} WHERE student_key=? AND course_key IS NOT NULL LIMIT 1', (key,)).fetchone()
               for table in tables):
            continue
        account = db.execute('SELECT student_id FROM student_accounts WHERE student_key=?', (key,)).fetchone() if 'student_accounts' in tables else None
        student_id = account['student_id'] if account else None
        if student_id and db.execute('SELECT 1 FROM platform_enrollments WHERE student_id=?', (student_id,)).fetchone():
            continue
        if 'student_accounts' in tables:
            db.execute('DELETE FROM student_accounts WHERE student_key=?', (key,))
        if 'student_account_attempts' in tables:
            db.execute('DELETE FROM student_account_attempts WHERE identity=?', (key,))
        if 'student_membership_events' in tables:
            db.execute('DELETE FROM student_membership_events WHERE course_key IS NULL AND student_key=?', (key,))
    for student_id in student_ids:
        _remove_unscoped_credentials(db, student_id)
        # Keep an identity needed by any other course, account or module.
        linked = False
        for table in tables:
            if table in {'admin_student_profiles', 'submission_admission_counters'}:
                continue
            for parent, pairs in _foreign_keys(db, table):
                if parent == 'platform_students' and any(db.execute(f'SELECT 1 FROM {_q(table)} WHERE {_q(child)}=? LIMIT 1', (student_id,)).fetchone() for child, _ in pairs):
                    linked = True
        if not linked:
            db.execute('DELETE FROM admin_student_profiles WHERE student_id=?', (student_id,))
            db.execute('DELETE FROM submission_admission_counters WHERE student_id=?', (student_id,))
            db.execute('DELETE FROM platform_students WHERE id=?', (student_id,))


def _remove_unscoped_credentials(db, student_id):
    """Migration-era revoked grants can have no course/session association.

    They may retain the final global identity after the enrollment disappears.
    Purge their FK children only when no surviving course references this
    student, and refuse an ambiguous link instead of touching another course.
    """
    tables = _tables(db)
    if any({'student_id', 'course_key'} <= set(_columns(db, table)) and db.execute(
            f'SELECT 1 FROM {_q(table)} WHERE student_id=? AND course_key IS NOT NULL LIMIT 1',
            (student_id,)).fetchone() for table in tables):
        return
    db.execute('DELETE FROM ag_privacy_rows')
    for table in ('platform_token_families', 'platform_sessions', 'platform_student_activations', 'device_authorizations'):
        if table in tables and {'student_id', 'course_key'} <= set(_columns(db, table)):
            db.execute(f'INSERT INTO ag_privacy_rows SELECT ?,rowid FROM {_q(table)} WHERE student_id=? AND course_key IS NULL', (table, student_id))
    previous = -1
    while previous != (count := db.execute('SELECT COUNT(*) FROM ag_privacy_rows').fetchone()[0]):
        previous = count
        for table in tables:
            for parent, pairs in _foreign_keys(db, table):
                join = ' AND '.join(f'c.{_q(child)}=p.{_q(target)}' for child, target in pairs)
                db.execute(f'INSERT OR IGNORE INTO ag_privacy_rows SELECT ?,c.rowid FROM {_q(table)} c '
                           f'JOIN {_q(parent)} p ON {join} JOIN ag_privacy_rows s ON s.table_name=? AND s.row_id=p.rowid', (table,parent))
    rows = {}
    for table in tables:
        selected = [dict(r) for r in db.execute(f'SELECT t.rowid AS __rowid__,t.* FROM {_q(table)} t JOIN ag_privacy_rows s ON s.table_name=? AND s.row_id=t.rowid', (table,))]
        if selected:
            if any(r.get('course_key') is not None or r.get('student_id', student_id) not in (None, student_id) for r in selected):
                raise PlatformConflict('이전 인증 기록이 다른 학생·수업과 연결되어 있어 보관을 중단했습니다.')
            rows[table] = selected
    _drop_personal_rows(db, rows)


def archive(service, key, *, expected_state=None, reauthorize=None):
    initialize(service.state)
    existing = status(service, key)
    if existing is not None:
        finish_cleanup(service, key)
        return service.get_course(key)
    service.get_course(key)
    context = service.cleanup_context(key) if service.cleanup_context else {'required': False}
    if not isinstance(context, dict) or len(json.dumps(context, allow_nan=False)) > 4096:
        raise ValueError('정리할 실행 관리자 식별 정보를 확인할 수 없습니다.')
    root = service.state.database.parent
    job_id = 'carc_' + secrets.token_hex(16)
    staged = None
    with privacy_file_guard(root, exclusive=True):
        try:
            with service.state._write() as db:
                db.execute('PRAGMA secure_delete=ON')
                report, rows, files = _review(service, db, key)
                if expected_state is not None and report['expected_state'] != expected_state:
                    raise PlatformConflict('확인 후 수업 자료가 변경되었습니다. 파기 범위를 다시 확인하세요.')
                if reauthorize:
                    reauthorize()
                aggregate = _aggregate(db, key)
                student_ids = {r['student_id'] for records in rows.values() for r in records
                               if isinstance(r.get('student_id'), int) and r['student_id'] > 0}
                student_keys = {r['student_key'] for table in rows.values() for r in table if isinstance(r.get('student_key'), str)}
                student_ids.update(r[0] for student_key in student_keys for r in db.execute(
                    'SELECT id FROM platform_students WHERE student_key=?', (student_key,)))
                student_keys.update(r[0] for student_id in student_ids for r in db.execute('SELECT student_key FROM platform_students WHERE id=?', (student_id,)))
                if files:
                    staged = files
                    staged.stage(job_id)
                if reauthorize:
                    reauthorize()
                _drop_personal_rows(db, rows)
                _orphan_accounts(db, student_ids, student_keys)
                db.execute("UPDATE admin_courses SET status='archived',revision=revision+1,auth_revision=auth_revision+1,updated_at=? WHERE course_key=?", (utc_iso(), key))
                # The preview's compare-and-swap digest covers personal data;
                # never retain it, personal counts or paths as research data.
                db.execute('INSERT INTO admin_course_privacy_archives VALUES(?,?,?,?,?,?,?,?)',
                    (job_id, key, utc_iso(), json.dumps(aggregate),
                     json.dumps({'managed_files': files is not None, 'secondary_ledger': service.privacy_after_archive is not None}),
                     'pending', '[]', json.dumps(context)))
                if db.execute('PRAGMA foreign_key_check').fetchone():
                    raise PlatformConflict('수업 개인정보 연결 검증에 실패하여 보관을 취소했습니다.')
        except BaseException:
            if staged:
                staged.rollback()
            raise
        finish_cleanup(service, key, staged=staged)
    return service.get_course(key)


def finish_cleanup(service, key, *, staged=None):
    with privacy_file_guard(service.state.database.parent, exclusive=True):
        return _finish_cleanup(service, key, staged=staged)


def _finish_cleanup(service, key, *, staged=None):
    pending = []
    if service.after_delete:
        try:
            service.after_delete(key)
        except Exception:
            pending.append('runtime_registry')
    with service.state._connection() as db:
        job = db.execute('SELECT * FROM admin_course_privacy_archives WHERE course_key=?', (key,)).fetchone()
    if job is None:
        return
    requirements = json.loads(job['counts_json'])
    if service.after_delete_context:
        try:
            service.after_delete_context(key, json.loads(job['remote_context_json']))
        except Exception:
            pending.append('remote_learning_space')
    elif json.loads(job['remote_context_json']).get('required'):
        pending.append('remote_learning_space')
    try:
        if staged:
            staged.purge()
        elif service.privacy_recover:
            service.privacy_recover()
        elif requirements.get('managed_files'):
            pending.append('managed_files')
    except Exception:
        pending.append('managed_files')
    if service.privacy_after_archive:
        try:
            service.privacy_after_archive(key)
        except Exception:
            pending.append('rubric_or_export_cache')
    elif requirements.get('secondary_ledger'):
        pending.append('rubric_or_export_cache')
    try:
        with service.state._connection() as db:
            checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint[0]:
                pending.append('database_checkpoint')
    except Exception:
        pending.append('database_checkpoint')
    with service.state._write() as db:
        db.execute('UPDATE admin_course_privacy_archives SET cleanup_status=?,pending_tasks_json=? WHERE course_key=?',
                   ('pending' if pending else 'complete', json.dumps(pending), key))
