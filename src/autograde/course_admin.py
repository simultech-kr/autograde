"""Transactional course/enrollment administration shared by instructor controllers.

CSV previews contain password verifiers, never plaintext secrets. Expiring previews
are bound to an instructor session and an optimistic snapshot; applying a preview
is atomic and retrying it never returns a password a second time.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import sqlite3
import time
from contextlib import nullcontext
from typing import Any

from .platform_auth import hash_student_password, validate_student_password, verify_student_password
from .platform_state import (
    CourseRosterImportEntry, PlatformAccessDenied, PlatformConflict,
    PlatformNotFound, PlatformStateStore, StudentIdentityKind, utc_iso,
)


COURSE_ADMIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_courses (
 course_key TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
 year INTEGER, semester TEXT, section TEXT NOT NULL DEFAULT '',
 description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'preparation'
 CHECK(status IN ('preparation','active','archived')),
 legacy INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
 auth_revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(code,year,semester,section)
);
CREATE TABLE IF NOT EXISTS admin_student_profiles (
 student_id INTEGER PRIMARY KEY REFERENCES platform_students(id) ON DELETE RESTRICT,
 name TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_enrollment_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, course_key TEXT NOT NULL,
 student_key TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_roster_previews (
 preview_id TEXT PRIMARY KEY, course_key TEXT NOT NULL REFERENCES admin_courses(course_key),
 session_hash TEXT NOT NULL, input_hash TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL, expires_at REAL NOT NULL,
 applied_at TEXT, result_json TEXT
);
"""


def initialize_course_admin(state: PlatformStateStore, connection=None) -> None:
    """Called by the schema migration; safe on an existing transaction."""
    with (nullcontext(connection) if connection is not None else state._write()) as db:
        for statement in COURSE_ADMIN_SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)
        for key in ('come3105', 'come2201'):
            db.execute("INSERT OR IGNORE INTO admin_courses "
                       "(course_key,code,status,legacy,created_at,updated_at) "
                       "VALUES (?,?,'active',1,?,?)", (key, key, utc_iso(), utc_iso()))


def _text(value: Any, field: str, limit: int, *, required=True) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{field} must be text')
    value = value.strip()
    if field == 'description':
        value = value.replace('\r\n', '\n').replace('\r', '\n')
    controls = any((ord(c) < 32 and not (field == 'description' and c in '\n\t')) or ord(c) == 127 for c in value)
    if (required and not value) or len(value) > limit or controls:
        raise ValueError(f'{field} must contain {1 if required else 0} to {limit} text characters')
    return value


def _course(db, course_key, *, writable=False):
    row = db.execute('SELECT * FROM admin_courses WHERE course_key=?', (course_key,)).fetchone()
    if row is None:
        raise PlatformNotFound('course was not found')
    if writable and row['status'] == 'archived':
        raise PlatformConflict('archived course is read-only; reactivate it first')
    return dict(row)


class CourseAdminService:
    def __init__(self, state: PlatformStateStore, *, before_activate=None):
        self.state = state
        self.before_activate = before_activate

    def list_courses(self, *, active_only=False):
        with self.state._connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM admin_courses' + (" WHERE status='active'" if active_only else '') +
                ' ORDER BY code,year,semester,section,course_key')]

    def get_course(self, course_key):
        with self.state._connection() as db:
            return _course(db, course_key)

    def management_overview(self):
        """Credential-free offering summaries; latest active enrollment x release only.

        Read one SQLite snapshot without building a student x assignment matrix.
        A repeated submission replaces its previous status, not the denominator.
        """
        with self.state._connection() as db:
            db.execute('BEGIN')
            courses = [dict(row) for row in db.execute(
                'SELECT * FROM admin_courses ORDER BY code,year DESC,semester,section,course_key')]
            students = {row['course_key']: dict(row) for row in db.execute(
                'SELECT e.course_key,COUNT(*) enrolled_students,'
                'SUM(CASE WHEN e.active=1 AND s.active=1 THEN 1 ELSE 0 END) active_students '
                'FROM platform_enrollments e JOIN platform_students s ON s.id=e.student_id GROUP BY e.course_key')}
            assignments = {row['course_key']: row['count'] for row in db.execute(
                'SELECT course_key,COUNT(*) count FROM bundle_assignment_releases WHERE active=1 GROUP BY course_key')}
            latest = db.execute('''
                WITH ranked AS (
                  SELECT a.course_key,r.state,r.submission_id,
                    ROW_NUMBER() OVER (PARTITION BY r.student_id,r.assignment_id
                      ORDER BY r.received_at DESC,r.submission_id DESC) position
                  FROM bundle_submission_requests r
                  JOIN bundle_assignment_releases a ON a.assignment_id=r.assignment_id AND a.active=1
                  JOIN platform_enrollments e ON e.student_id=r.student_id AND e.course_key=a.course_key AND e.active=1
                  JOIN platform_students s ON s.id=r.student_id AND s.active=1
                )
                SELECT r.course_key,r.state,g.score,g.max_score,g.published_at
                FROM ranked r LEFT JOIN bundle_submission_results g ON g.submission_id=r.submission_id
                WHERE r.position=1
            ''').fetchall()
            totals = {}
            for row in latest:
                counts = totals.setdefault(row['course_key'], dict(submitted=0, completed=0, needs_work=0, waiting=0, errors=0, unknown=0))
                counts['submitted'] += 1
                state = row['state']
                if state in ('received', 'accepted', 'queued', 'running', 'graded'):
                    counts['waiting'] += 1
                elif state in ('rejected', 'infra_failed', 'assessment_failed', 'failed'):
                    counts['errors'] += 1
                elif state == 'published' and row['published_at'] and row['max_score'] is not None and row['max_score'] > 0 and row['score'] is not None and 0 <= row['score'] <= row['max_score']:
                    counts['completed' if row['score'] == row['max_score'] else 'needs_work'] += 1
                else:
                    counts['unknown'] += 1
            for course in courses:
                roster = students.get(course['course_key'], {})
                course['enrolled_students'] = roster.get('enrolled_students', 0)
                course['active_students'] = roster.get('active_students', 0)
                course['active_assignments'] = assignments.get(course['course_key'], 0)
                course.update(totals.get(course['course_key'], dict(submitted=0, completed=0, needs_work=0, waiting=0, errors=0, unknown=0)))
                course['expected'] = course['active_students'] * course['active_assignments']
                course['not_submitted'] = max(0, course['expected'] - course['submitted'])
            return courses

    def is_active(self, course_key):
        try:
            return self.get_course(course_key)['status'] == 'active'
        except PlatformNotFound:
            return False

    @staticmethod
    def _fields(code, name, year, semester, section, description):
        code = _text(code, 'code', 32)
        if re.fullmatch(r'[a-z0-9_-]+', code) is None:
            raise ValueError('code must use lowercase letters, digits, underscore or hyphen')
        if isinstance(year, bool) or not str(year).isascii() or not str(year).isdigit() or not 2000 <= int(year) <= 2200:
            raise ValueError('year must be between 2000 and 2200')
        if str(semester) not in ('1', '2', 'summer', 'winter'):
            raise ValueError('semester must be 1, 2, summer or winter')
        return dict(code=code, name=_text(name, 'name', 100), year=int(year),
                    semester=str(semester), section=_text(section, 'section', 16),
                    description=_text(description, 'description', 2000, required=False))

    def create_course(self, *, code, name, year, semester, section='01', description=''):
        fields = self._fields(code, name, year, semester, section, description)
        key_prefix = fields['code'] if fields['code'][0].isalnum() else 'course-' + fields['code']
        key = f"{key_prefix}-{fields['year']}-{fields['semester']}-{secrets.token_hex(4)}"
        now = utc_iso()
        with self.state._write() as db:
            try:
                db.execute('INSERT INTO admin_courses '
                           '(course_key,code,name,year,semester,section,description,created_at,updated_at) '
                           'VALUES (?,?,?,?,?,?,?,?,?)', (key, *fields.values(), now, now))
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict('this course, year, semester and section already exist') from exc
            return _course(db, key)

    def update_course(self, course_key, **fields):
        expected_revision = fields.pop('revision', None)
        if expected_revision is not None and (isinstance(expected_revision, bool) or
                                             not isinstance(expected_revision, int) or expected_revision < 1):
            raise ValueError('revision must be a positive integer')
        allowed = {'code', 'name', 'year', 'semester', 'section', 'description'}
        if not fields or set(fields) - allowed:
            raise ValueError('unsupported course fields')
        with self.state._write() as db:
            old = _course(db, course_key, writable=True)
            if expected_revision is not None and old['revision'] != expected_revision:
                raise PlatformConflict('course was changed in another page; refresh and review it')
            if old['status'] != 'preparation' and not old['legacy'] and any(
                k in fields and str(fields[k]) != str(old[k]) for k in ('code', 'year', 'semester', 'section')
            ):
                raise PlatformConflict('course offering identity is locked after activation')
            merged = {k: fields.get(k, old[k]) for k in allowed}
            # Legacy metadata is deliberately blank until the operator supplies it.
            if old['legacy'] and not any(k in fields for k in ('code', 'year', 'semester', 'section')):
                values = {k: _text(fields[k], k, 100 if k == 'name' else 2000,
                                  required=k == 'name') for k in fields}
            else:
                values = self._fields(**merged)
                values['legacy'] = 0
            try:
                db.execute('UPDATE admin_courses SET ' + ','.join(f'{k}=?' for k in values) +
                           ',revision=revision+1,updated_at=? WHERE course_key=?',
                           (*values.values(), utc_iso(), course_key))
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict('this course offering already exists') from exc
            return _course(db, course_key)

    def set_status(self, course_key, status):
        if status not in ('preparation', 'active', 'archived'):
            raise ValueError('unsupported course status')
        if status == 'active' and self.before_activate:
            self.get_course(course_key)
            self.before_activate(course_key)
        with self.state._write() as db:
            old = _course(db, course_key)
            if old['status'] == status:
                return old
            if status == 'preparation' and old['status'] != 'preparation':
                raise PlatformConflict('an activated course cannot return to preparation')
            if status == 'archived':
                self._require_idle(db, course_key)
                for row in db.execute('SELECT student_id FROM platform_enrollments WHERE course_key=?', (course_key,)):
                    self.state._revoke_course_credentials(db, student_id=row['student_id'],
                                                         course_key=course_key, now=utc_iso(), delete_password=False)
                # Deny unbound pending device connections as well as approved ones.
                db.execute("UPDATE device_authorizations SET state='denied',denied_at=?,updated_at=? "
                           "WHERE course_key=? AND state IN ('pending','approved')", (utc_iso(), utc_iso(), course_key))
            db.execute('UPDATE admin_courses SET status=?,revision=revision+1,auth_revision=auth_revision+1,updated_at=? WHERE course_key=?',
                       (status, utc_iso(), course_key))
            return _course(db, course_key)

    @staticmethod
    def _require_idle(db, key):
        for requests, assignments in (('bundle_submission_requests', 'bundle_assignment_releases'),
                                      ('submission_requests', 'platform_assignments')):
            if db.execute(f"SELECT 1 FROM {requests} r JOIN {assignments} a ON a.assignment_id=r.assignment_id "
                          "WHERE a.course_key=? AND r.state IN ('received','accepted','queued','running') LIMIT 1", (key,)).fetchone():
                raise PlatformConflict('course has unfinished submissions')
        if db.execute("SELECT 1 FROM bundle_release_checks c JOIN bundle_assignment_releases a "
                      "ON a.assignment_id=c.assignment_id WHERE a.course_key=? AND c.status='pending' LIMIT 1", (key,)).fetchone():
            raise PlatformConflict('course has unfinished validation')
        # New validation queues may be initialized after the course schema.
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('instructor_assignment_jobs',):
            if table in tables:
                columns = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
                if 'course_key' in columns and 'status' in columns and db.execute(
                    f"SELECT 1 FROM {table} WHERE course_key=? AND status IN ('queued','running') LIMIT 1", (key,)
                ).fetchone():
                    raise PlatformConflict('course has unfinished validation')


class EnrollmentAdminService:
    def __init__(self, state: PlatformStateStore, server_secret, *, clock=time.time):
        self.state = state
        self.secret = server_secret.encode() if isinstance(server_secret, str) else server_secret
        if not self.secret:
            raise ValueError('server secret is required')
        self.clock = clock

    @staticmethod
    def _rows(db, course_key):
        return [dict(row) for row in db.execute(
            "SELECT s.id student_id,e.id enrollment_id,s.student_key,COALESCE(p.name,'') name,"
            "e.active,s.active student_active,pw.password_hash,e.updated_at,s.auth_subject,s.identity_kind,"
            "s.github_user_id,s.github_login FROM platform_enrollments e JOIN platform_students s ON s.id=e.student_id "
            "LEFT JOIN admin_student_profiles p ON p.student_id=s.id "
            "LEFT JOIN platform_student_passwords pw ON pw.enrollment_id=e.id WHERE e.course_key=? ORDER BY s.student_key",
            (course_key,))]

    @staticmethod
    def _public(row):
        return {k: row[k] for k in ('student_id', 'enrollment_id', 'student_key', 'name')} | {
            'active': bool(row['active']), 'has_password': bool(row['password_hash'])}

    def list_students(self, course_key):
        with self.state._connection() as db:
            _course(db, course_key)
            return [self._public(row) for row in self._rows(db, course_key)]

    def get_student(self, course_key, student_key):
        for row in self.list_students(course_key):
            if row['student_key'] == student_key:
                return row
        raise PlatformNotFound('course enrollment was not found')

    def _snapshot(self, db, course_key):
        # Global names/identities can change in another course while this form is open.
        profiles = [tuple(row) for row in db.execute(
            "SELECT s.id,s.student_key,s.active,s.auth_subject,COALESCE(p.name,'') FROM platform_students s "
            'LEFT JOIN admin_student_profiles p ON p.student_id=s.id ORDER BY s.id')]
        value = [_course(db, course_key), self._rows(db, course_key), profiles]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _read(self, course_key):
        with self.state._connection() as db:
            # One read transaction, not a mixture of different concurrent snapshots.
            db.execute('BEGIN')
            _course(db, course_key, writable=True)
            snapshot = self._snapshot(db, course_key)
            rows = self._rows(db, course_key)
            identities = {r['student_key']: dict(r) for r in db.execute(
                "SELECT s.*,COALESCE(p.name,'') name FROM platform_students s "
                'LEFT JOIN admin_student_profiles p ON p.student_id=s.id')}
        return snapshot, rows, identities

    @staticmethod
    def _unique_password(password, rows, student_key):
        for row in rows:
            if row['student_key'] != student_key and row['password_hash'] and verify_student_password(password, row['password_hash']):
                raise PlatformConflict('password is already assigned in this course')

    def _new_password(self, requested, rows, student_key):
        for _ in range(100):
            value = validate_student_password(requested) if requested is not None else f'{secrets.randbelow(1000000):06d}'
            try:
                self._unique_password(value, rows, student_key)
            except PlatformConflict:
                if requested is not None:
                    raise
                continue
            return value, hash_student_password(value)
        raise PlatformConflict('could not allocate a unique password; retry')

    def _cas(self, db, key, snapshot):
        _course(db, key, writable=True)
        if self._snapshot(db, key) != snapshot:
            raise PlatformConflict('student or course state changed; review and retry')

    @staticmethod
    def _entry(student_key, identity, active, password_hash=None, expected=None):
        github = identity and identity['identity_kind'] == 'github'
        return CourseRosterImportEntry(student_key=student_key,
            auth_subject=identity['auth_subject'] if identity else f'local:{student_key}',
            identity_kind=StudentIdentityKind.GITHUB if github else StudentIdentityKind.LOCAL,
            github_user_id=identity['github_user_id'] if github else None,
            github_login=identity['github_login'] if github else None,
            active=active, password_managed=bool(active and (password_hash or expected)),
            expected_password_hash=expected, new_password_hash=password_hash)

    @staticmethod
    def _name(db, student_id, name):
        if name:
            db.execute('INSERT INTO admin_student_profiles VALUES (?,?,?) '
                       'ON CONFLICT(student_id) DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at',
                       (student_id, name, utc_iso()))

    @staticmethod
    def _audit(db, key, student_key, action, reason=''):
        reason = _text(reason, 'reason', 500, required=False)
        db.execute('INSERT INTO admin_enrollment_audit(course_key,student_key,action,actor,reason,created_at) '
                   "VALUES (?,?,?,'instructor',?,?)", (key, student_key, action, reason, utc_iso()))

    def add_student(self, course_key, *, student_key, name='', active=True, password=None):
        student_key = _text(student_key, 'student_key', 128)
        name = _text(name, 'name', 100, required=False)
        if not isinstance(active, bool):
            raise ValueError('active must be boolean')
        snapshot, rows, identities = self._read(course_key)
        if any(r['student_key'] == student_key for r in rows):
            raise PlatformConflict('student is already enrolled; use the student detail page')
        identity = identities.get(student_key)
        if identity and name and identity['name'] and name != identity['name']:
            raise PlatformConflict('student name conflicts with an existing student')
        if not active and password:
            raise ValueError('inactive students cannot receive a password')
        plain, verifier = self._new_password(password or None, rows, student_key) if active else (None, None)
        with self.state._write() as db:
            self._cas(db, course_key, snapshot)
            result = self.state._import_course_roster(course_key=course_key,
                entries=[self._entry(student_key, identity, active, verifier)], _connection=db)
            self._name(db, result[0][0].id, name)
            self._audit(db, course_key, student_key, 'added')
            row = next(r for r in self._rows(db, course_key) if r['student_key'] == student_key)
        return self._public(row) | ({'password': plain} if plain else {})

    def reset_password(self, course_key, student_key, *, password=None, reason=''):
        snapshot, rows, _ = self._read(course_key)
        row = next((r for r in rows if r['student_key'] == student_key), None)
        if row is None:
            raise PlatformNotFound('course enrollment was not found')
        if not row['active'] or not row['student_active']:
            raise PlatformConflict('reactivate the enrollment before resetting its password')
        plain, verifier = self._new_password(password or None, rows, student_key)
        with self.state._write() as db:
            self._cas(db, course_key, snapshot)
            self.state._set_student_password_hash_in_connection(db, student_id=row['student_id'],
                course_key=course_key, password_hash=verifier, now=utc_iso())
            # Even an explicit reset to the same numeric password revokes all tokens.
            self.state._revoke_course_credentials(db, student_id=row['student_id'],
                course_key=course_key, now=utc_iso(), delete_password=False)
            self._audit(db, course_key, student_key, 'password_reset', reason)
        return self.get_student(course_key, student_key) | {'password': plain}

    def update_name(self, course_key, student_key, *, name, expected_name, reason=''):
        """Edit global display information after the UI confirms its global scope."""
        name = _text(name, 'name', 100, required=False)
        expected_name = _text(expected_name, 'expected_name', 100, required=False)
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            row = next((r for r in self._rows(db, course_key) if r['student_key'] == student_key), None)
            if row is None:
                raise PlatformNotFound('course enrollment was not found')
            if row['name'] != expected_name:
                raise PlatformConflict('student name changed elsewhere; refresh and review it')
            db.execute('INSERT INTO admin_student_profiles VALUES (?,?,?) '
                       'ON CONFLICT(student_id) DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at',
                       (row['student_id'], name, utc_iso()))
            self._audit(db, course_key, student_key, 'global_name_updated', reason)
            row['name'] = name
            return self._public(row)

    def set_active(self, course_key, student_key, active, *, reason=''):
        if not isinstance(active, bool):
            raise ValueError('active must be boolean')
        snapshot, rows, identities = self._read(course_key)
        row = next((r for r in rows if r['student_key'] == student_key), None)
        if row is None:
            raise PlatformNotFound('course enrollment was not found')
        if bool(row['active']) == active:
            return self._public(row)
        plain, verifier = self._new_password(None, rows, student_key) if active else (None, None)
        with self.state._write() as db:
            self._cas(db, course_key, snapshot)
            self.state._import_course_roster(course_key=course_key, entries=[self._entry(
                student_key, identities[student_key], active, verifier,
                row['password_hash'] if active else None)], _connection=db)
            self._audit(db, course_key, student_key, 'reactivated' if active else 'deactivated', reason)
            updated = next(r for r in self._rows(db, course_key) if r['student_key'] == student_key)
        return self._public(updated) | ({'password': plain} if plain else {})

    def _session_hash(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise PlatformAccessDenied('instructor session is required')
        return hmac.new(self.secret, ('roster-preview:' + session_id).encode(), hashlib.sha256).hexdigest()

    def preview_csv(self, course_key, content: bytes, *, session_id, auto_generate=False):
        session_hash = self._session_hash(session_id)
        if not isinstance(content, bytes) or len(content) > 1024 * 1024:
            raise ValueError('CSV must be at most 1 MiB')
        try:
            reader = csv.reader(io.StringIO(content.decode('utf-8-sig'), newline=''), strict=True)
            header = next(reader)
            raw_rows = list(reader)
        except (UnicodeError, csv.Error, StopIteration) as exc:
            raise ValueError('CSV must contain a valid UTF-8 header and rows') from exc
        if set(header) not in ({'student_key', 'active', 'password'}, {'student_key', 'active', 'password', 'name'}) or len(set(header)) != len(header):
            raise ValueError('CSV columns must be student_key,active,password and optional name')
        if not raw_rows or len(raw_rows) > 1000:
            raise ValueError('CSV must contain 1 to 1000 student rows')
        snapshot, existing, identities = self._read(course_key)
        by_key = {r['student_key']: r for r in existing}
        seen_keys, seen_passwords = set(), set()
        payload, display = [], []
        for number, cells in enumerate(raw_rows, 2):
            view = {'row': number, 'student_key': '', 'name': '', 'active': False,
                    'status': 'error', 'password_action': '', 'errors': []}
            field = 'row'
            try:
                if len(cells) != len(header):
                    raise ValueError('column count does not match the header')
                item = dict(zip(header, cells))
                field = 'student_key'
                key = _text(item['student_key'], 'student_key', 128)
                view['student_key'] = key
                if key in seen_keys:
                    raise ValueError('student_key is duplicated')
                seen_keys.add(key)
                field = 'name'
                name = _text(item.get('name', ''), 'name', 100, required=False)
                view['name'] = name
                identity, old = identities.get(key), by_key.get(key)
                if identity and name and identity['name'] and identity['name'] != name:
                    raise ValueError('name conflicts with existing student')
                field = 'active'
                if item['active'].strip().lower() not in ('true', 'false', '1', '0'):
                    raise ValueError('active must be true, false, 1 or 0')
                active = item['active'].strip().lower() in ('true', '1')
                view['active'] = active
                if active and identity and not identity['active']:
                    raise ValueError('student identity is inactive')
                field = 'password'
                password = item['password'].strip()
                expected = old['password_hash'] if old else None
                verifier, generate = None, False
                if not active:
                    if password:
                        raise ValueError('inactive enrollment requires an empty password')
                    action = 'none'
                elif password:
                    validate_student_password(password)
                    if password in seen_passwords:
                        raise ValueError('password is duplicated in this CSV')
                    seen_passwords.add(password)
                    self._unique_password(password, existing, key)
                    if expected:
                        if not verify_student_password(password, expected):
                            raise ValueError('password differs; use explicit password reset')
                        action = 'keep'
                    else:
                        verifier = hash_student_password(password)
                        action = 'specified'
                elif expected and old['active']:
                    action = 'keep'
                elif auto_generate:
                    generate, action = True, 'generate'
                else:
                    raise ValueError('new active enrollment requires a password or automatic generation')
                view['password_action'] = action
                view['status'] = 'new' if old is None else (
                    'changed' if bool(old['active']) != active or (name and name != old['name']) else 'unchanged')
                payload.append(dict(student_key=key, name=name, active=active, expected=expected if active else None,
                                    verifier=verifier, generate=generate))
            except (ValueError, PlatformConflict) as exc:
                view['errors'].append({'field': field, 'message': str(exc)})
            display.append(view)
        valid = all(not row['errors'] for row in display)
        expires = self.clock() + 600
        preview_id = secrets.token_urlsafe(24) if valid else None
        if valid:
            normalized = json.dumps(payload, sort_keys=True)
            with self.state._write() as db:
                self._cas(db, course_key, snapshot)
                db.execute('DELETE FROM admin_roster_previews WHERE expires_at<? AND applied_at IS NULL', (self.clock(),))
                db.execute('INSERT INTO admin_roster_previews '
                           '(preview_id,course_key,session_hash,input_hash,snapshot_hash,payload_json,expires_at) VALUES (?,?,?,?,?,?,?)',
                           (preview_id, course_key, session_hash, hashlib.sha256(normalized.encode()).hexdigest(), snapshot, normalized, expires))
        return dict(preview_id=preview_id, valid=valid, rows=display, expires_at=expires)

    def apply_csv(self, course_key, preview_id, *, session_id):
        session_hash = self._session_hash(session_id)
        with self.state._connection() as db:
            preview = db.execute('SELECT * FROM admin_roster_previews WHERE preview_id=? AND course_key=?',
                                 (preview_id, course_key)).fetchone()
        if preview is None or not hmac.compare_digest(preview['session_hash'], session_hash):
            raise PlatformAccessDenied('roster preview does not belong to this session and course')
        if preview['applied_at']:
            return json.loads(preview['result_json']) | {'replayed': True, 'passwords': []}
        if self.clock() >= preview['expires_at']:
            raise PlatformConflict('roster preview expired; upload it again')
        snapshot, existing, identities = self._read(course_key)
        if snapshot != preview['snapshot_hash']:
            # Another copy of the same request may have committed between reads.
            with self.state._connection() as db:
                completed = db.execute('SELECT result_json FROM admin_roster_previews WHERE preview_id=? AND applied_at IS NOT NULL',
                                       (preview_id,)).fetchone()
            if completed:
                return json.loads(completed['result_json']) | {'replayed': True, 'passwords': []}
            raise PlatformConflict('student or course state changed; preview the CSV again')
        payload = json.loads(preview['payload_json'])
        issued, entries = [], []
        password_rows = list(existing)
        # Include all explicit new verifiers before allocating generated values.
        password_rows += [dict(student_key=p['student_key'], password_hash=p['verifier']) for p in payload if p['verifier']]
        for item in payload:
            verifier = item['verifier']
            if item['generate']:
                plain, verifier = self._new_password(None, password_rows, item['student_key'])
                issued.append({'student_key': item['student_key'], 'password': plain})
                password_rows.append(dict(student_key=item['student_key'], password_hash=verifier))
            entries.append(self._entry(item['student_key'], identities.get(item['student_key']),
                                       item['active'], verifier, item['expected']))
        with self.state._write() as db:
            latest = db.execute('SELECT * FROM admin_roster_previews WHERE preview_id=?', (preview_id,)).fetchone()
            if latest and latest['applied_at']:
                return json.loads(latest['result_json']) | {'replayed': True, 'passwords': []}
            if latest is None or self.clock() >= latest['expires_at']:
                raise PlatformConflict('roster preview expired; upload it again')
            self._cas(db, course_key, snapshot)
            imported = self.state._import_course_roster(course_key=course_key, entries=entries, _connection=db)
            for item, (student, _) in zip(payload, imported):
                self._name(db, student.id, item['name'])
                self._audit(db, course_key, item['student_key'], 'csv_applied')
            result = dict(applied=True, count=len(imported), replayed=False)
            db.execute("UPDATE admin_roster_previews SET applied_at=?,result_json=?,payload_json='[]' WHERE preview_id=?",
                       (utc_iso(), json.dumps(result), preview_id))
        return result | {'passwords': issued}
