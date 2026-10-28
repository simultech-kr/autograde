"""Explicit, course-scoped permanent deletion of the SQLite aggregate.

The platform normally keeps immutable grading evidence. This one administrative
operation lifts the affected table triggers only inside its write transaction,
then restores their exact definitions before commit. Shared identities survive.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import secrets

from .platform_state import PlatformConflict, utc_iso


SCHEMA = """CREATE TABLE IF NOT EXISTS admin_course_deletions (
 job_id TEXT PRIMARY KEY, course_key TEXT NOT NULL UNIQUE,
 deleted_at TEXT NOT NULL, counts_json TEXT NOT NULL,
 cleanup_status TEXT NOT NULL CHECK(cleanup_status IN ('pending','complete'))
)"""
_MARKER = '__rowid__'
_EXCLUDED = {'admin_course_deletions'}


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def initialize(state, connection=None):
    if connection is None:
        with state._write() as db:
            initialize(state, db)
        return
    db = connection
    db.execute(SCHEMA)
    # Existing CLI-only schemas permit unregistered course keys. Preserve that
    # compatibility, but never allow a deleted key to recreate data after a
    # request that passed its authorization check before deletion committed.
    for table in _tables(db):
        if table in _EXCLUDED or 'course_key' not in _columns(db, table):
            continue
        for event in ('INSERT', 'UPDATE'):
            trigger = f'course_deleted_{event.lower()}_{table}'
            db.execute(f'CREATE TRIGGER IF NOT EXISTS {_q(trigger)} BEFORE {event} ON {_q(table)} '
                       'WHEN EXISTS (SELECT 1 FROM admin_course_deletions WHERE course_key=NEW.course_key) '
                       "BEGIN SELECT RAISE(ABORT,'course_permanently_deleted'); END")


def _tables(db):
    return [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _columns(db, table):
    return [row['name'] for row in db.execute(f'PRAGMA table_info({_q(table)})')]


def _foreign_keys(db, table):
    groups = defaultdict(list)
    for row in db.execute(f'PRAGMA foreign_key_list({_q(table)})'):
        groups[row['id']].append(row)
    for values in groups.values():
        values.sort(key=lambda item: item['seq'])
        parent = values[0]['table']
        primary = [row['name'] for row in sorted(db.execute(f'PRAGMA table_info({_q(parent)})'), key=lambda row: row['pk']) if row['pk']]
        yield parent, [(row['from'], row['to'] or primary[index]) for index, row in enumerate(values)]


def _targets(db, course_key):
    """Direct course rows plus their FK children, including composite keys."""
    tables = [table for table in _tables(db) if table not in _EXCLUDED]
    db.execute('DROP TABLE IF EXISTS temp.ag_course_delete_rows')
    db.execute('CREATE TEMP TABLE ag_course_delete_rows (table_name TEXT,row_id INTEGER,PRIMARY KEY(table_name,row_id))')
    for table in tables:
        if 'course_key' in _columns(db, table):
            db.execute(f'INSERT INTO ag_course_delete_rows SELECT ?,rowid FROM {_q(table)} WHERE course_key=?',
                       (table, course_key))
    previous = -1
    while previous != (count := db.execute('SELECT COUNT(*) FROM ag_course_delete_rows').fetchone()[0]):
        previous = count
        for table in tables:
            for parent, pairs in _foreign_keys(db, table):
                join = ' AND '.join(f'c.{_q(child)}=p.{_q(target)}' for child, target in pairs)
                db.execute(f'INSERT OR IGNORE INTO ag_course_delete_rows '
                           f'SELECT ?,c.rowid FROM {_q(table)} c JOIN {_q(parent)} p ON {join} '
                           'JOIN ag_course_delete_rows selected ON selected.table_name=? AND selected.row_id=p.rowid',
                           (table, parent))
    rows = {}
    for table in tables:
        selected = [dict(row) for row in db.execute(
            f'SELECT t.rowid AS {_q(_MARKER)},t.* FROM {_q(table)} t '
            'JOIN ag_course_delete_rows selected ON selected.table_name=? AND selected.row_id=t.rowid ORDER BY t.rowid', (table,))]
        if not selected:
            continue
        if any('course_key' in row and row['course_key'] != course_key for row in selected):
            raise PlatformConflict('다른 수업과 연결된 데이터가 있어 삭제를 중단했습니다. 운영자에게 연결 관계 확인을 요청하세요.')
        rows[table] = selected
    # A child without course_key can still link an origin in another course.
    # Refuse that unusual cross-course dependency instead of deleting its link.
    for table in rows:
        for parent, pairs in _foreign_keys(db, table):
            if 'course_key' not in _columns(db, parent):
                continue
            join = ' AND '.join(f'c.{_q(child)}=p.{_q(target)}' for child, target in pairs)
            if db.execute(f'SELECT 1 FROM {_q(table)} c JOIN {_q(parent)} p ON {join} '
                          'JOIN ag_course_delete_rows selected ON selected.table_name=? AND selected.row_id=c.rowid '
                          'WHERE p.course_key<>? LIMIT 1', (table, course_key)).fetchone():
                raise PlatformConflict('다른 수업의 데이터를 참조하고 있어 삭제를 중단했습니다.')
    return rows


def _idle(db, course_key):
    from .course_admin import CourseAdminService
    CourseAdminService._require_idle(db, course_key)
    if db.execute("SELECT 1 FROM content_publish_jobs j JOIN content_collections c USING(collection_id) "
                  "WHERE c.course_key=? AND j.state='preparing' LIMIT 1", (course_key,)).fetchone():
        raise PlatformConflict('강의자료 공개 작업이 진행 중입니다. 완료 후 다시 삭제해 주세요.')


def _digest(rows, files):
    def private_value(value):
        if isinstance(value, bytes):
            return {'binary_sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)}
        raise TypeError(type(value).__name__)
    data = json.dumps([rows, files], sort_keys=True, separators=(',', ':'), default=private_value)
    return hashlib.sha256(data.encode()).hexdigest()


def _review(service, db, key):
    from .course_admin import _course
    course = _course(db, key)
    _idle(db, key)
    rows = _targets(db, key)
    files = service.file_planner(db, key, rows) if service.file_planner else None
    if files is None and any(rows.get(table) for table in (
            'bundle_assignment_releases', 'platform_assignments', 'student_materials', 'exam_papers',
            'content_collections', 'ide_extension_releases')):
        raise PlatformConflict('교과목 파일 정리가 연결되지 않았습니다. 운영자가 서버 삭제 기능을 설정한 뒤 다시 시도해 주세요.')
    summary = files.summary if files else {'files': 0, 'bytes': 0, 'configured': False}
    counts = {table: len(values) for table, values in rows.items()}
    report = dict(course=course, counts=counts, files=summary, expected_state=_digest(rows, summary),
                  enrollments=counts.get('platform_enrollments', 0),
                  assignments=counts.get('bundle_assignment_releases', 0) + counts.get('platform_assignments', 0),
                  submissions=counts.get('bundle_submission_requests', 0) + counts.get('submission_requests', 0),
                  materials=counts.get('student_materials', 0) + counts.get('exam_papers', 0) + counts.get('content_collections', 0))
    return report, rows, files


def preview(service, course_key):
    with service.state._connection() as db:
        db.execute('BEGIN')
        report, _, _ = _review(service, db, course_key)
        return report


def delete(service, course_key, *, confirm_course, expected_state, reauthorize=None):
    if confirm_course != course_key or not isinstance(expected_state, str) or len(expected_state) != 64:
        raise ValueError('삭제할 교과목 코드를 정확히 입력하고 최신 삭제 미리보기를 확인해 주세요.')
    # A worker call must not hold SQLite's write lock. The authoritative course
    # and pending-work checks are repeated inside the transaction below.
    service.get_course(course_key)
    if service.before_delete:
        service.before_delete(course_key)
    context = service.cleanup_context(course_key) if service.cleanup_context else None
    if context is not None:
        if not isinstance(context, dict) or len(json.dumps(context, allow_nan=False)) > 4096:
            raise ValueError('삭제할 실행 관리자 식별 정보를 확인할 수 없습니다.')
    job_id = 'cdel_' + secrets.token_hex(16)
    staged = None
    try:
        with service.state._write() as db:
            report, rows, files = _review(service, db, course_key)
            if report['expected_state'] != expected_state:
                raise PlatformConflict('미리보기 후 수업 데이터가 변경되었습니다. 삭제 범위를 다시 확인해 주세요.')
            if reauthorize:
                reauthorize()
            if files:
                staged = files
                staged.stage(job_id)
            if reauthorize:
                reauthorize()
            guards = list(db.execute("SELECT name,sql,tbl_name FROM sqlite_master WHERE type='trigger' ORDER BY name"))
            affected = [row for row in guards if row['tbl_name'] in rows]
            for guard in affected:
                db.execute(f'DROP TRIGGER {_q(guard["name"])}')
            db.execute('PRAGMA defer_foreign_keys=ON')
            for table in rows:
                db.execute(f'DELETE FROM {_q(table)} WHERE rowid IN '
                           '(SELECT row_id FROM ag_course_delete_rows WHERE table_name=?)', (table,))
            for guard in affected:
                db.execute(guard['sql'])
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise PlatformConflict('연결 데이터 검증에 실패하여 삭제를 취소했습니다.')
            db.execute('INSERT INTO admin_course_deletions VALUES (?,?,?,?,?)',
                       (job_id, course_key, utc_iso(), json.dumps({'tables': report['counts'],
                            'remote_cleanup_required': service.remote_cleanup_required,
                            'remote_cleanup_context': context}, sort_keys=True),
                        'pending'))
    except BaseException:
        if staged:
            staged.rollback()
        raise
    cleanup_status = 'complete'
    if service.after_delete:
        try:
            service.after_delete(course_key)
        except Exception:
            # Authorization/data deletion committed; remote resources may still
            # exist. Keep the durable pending record for startup recovery.
            cleanup_status = 'pending'
    if service.after_delete_context:
        try:
            service.after_delete_context(course_key, context)
        except Exception:
            cleanup_status = 'pending'
    if staged:
        try:
            staged.purge()
        except (OSError, ValueError, RuntimeError):
            cleanup_status = 'pending'
    if cleanup_status == 'complete':
        with service.state._write() as db:
            db.execute("UPDATE admin_course_deletions SET cleanup_status='complete' WHERE job_id=?", (job_id,))
    return dict(course_key=course_key, job_id=job_id, deleted=True, cleanup_status=cleanup_status,
                counts=report['counts'], files=report['files'])


def history(service):
    with service.state._connection() as db:
        return [dict(row) for row in db.execute('SELECT job_id,course_key,deleted_at,cleanup_status FROM admin_course_deletions ORDER BY deleted_at DESC LIMIT 100')]
