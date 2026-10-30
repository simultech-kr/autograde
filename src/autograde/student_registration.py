"""SQLite student accounts and course-scoped enrollment roster matching.

The common account never overwrites an imported student's credential. Existing
students may activate an account only after presenting a current course session.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import csv
import io
import re
import secrets
import threading
import time

from .platform_auth import hash_member_password, verify_student_password
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, _local_identity_columns, utc_iso

SCHEMA = '''
CREATE TABLE IF NOT EXISTS student_accounts (
 student_key TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
 student_id INTEGER UNIQUE REFERENCES platform_students(id) ON DELETE RESTRICT,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS student_course_registrations (
 request_id TEXT PRIMARY KEY,
 student_key TEXT NOT NULL REFERENCES student_accounts(student_key) ON DELETE RESTRICT,
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 status TEXT NOT NULL CHECK(status IN ('pending','matched')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reviewed_by TEXT,
 UNIQUE(student_key,course_key)
);
CREATE TABLE IF NOT EXISTS student_course_eligibility (
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 student_key TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(course_key,student_key)
);
CREATE TABLE IF NOT EXISTS student_account_attempts (
 scope TEXT NOT NULL, identity TEXT NOT NULL, count INTEGER NOT NULL,
 expires_at REAL NOT NULL, PRIMARY KEY(scope,identity)
);
CREATE TABLE IF NOT EXISTS student_membership_tokens (
 token_hash TEXT PRIMARY KEY, student_key TEXT NOT NULL,
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 purpose TEXT NOT NULL CHECK(purpose IN ('activate','reset')),
 expires_at REAL NOT NULL, used_at TEXT, issued_by TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS student_membership_events (
 id INTEGER PRIMARY KEY, course_key TEXT REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 student_key TEXT, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS student_roster_changes (
 change_id TEXT PRIMARY KEY, course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 actor TEXT NOT NULL, mode TEXT NOT NULL, keys_json TEXT NOT NULL, before_digest TEXT NOT NULL,
 expires_at REAL NOT NULL, applied_at TEXT
);
'''
_SLOTS = threading.BoundedSemaphore(4)
_DUMMY = hash_member_password('dummy-member-password', salt=b'\0' * 16)


class RegistrationError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def initialize(state):
    with state._write() as db:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)
        columns = {row['name'] for row in db.execute('PRAGMA table_info(student_accounts)')}
        if 'verified' not in columns:
            # Existing pilot accounts remain usable. Newly created accounts
            # explicitly insert 0 until a teacher-issued code is redeemed.
            db.execute('ALTER TABLE student_accounts ADD COLUMN verified INTEGER NOT NULL DEFAULT 1 CHECK(verified IN (0,1))')


def student_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4,20}', value.strip()):
        raise RegistrationError('학번은 숫자 4~20자리로 입력하세요.')
    return value.strip()


class StudentRegistration:
    def __init__(self, state):
        self.state = state
        initialize(state)

    def _attempt(self, scope, identity, *, limit=5, seconds=900):
        now = time.time()
        with self.state._write() as db:
            db.execute('DELETE FROM student_account_attempts WHERE expires_at<=?', (now,))
            row = db.execute('SELECT count FROM student_account_attempts WHERE scope=? AND identity=?', (scope, identity)).fetchone()
            if row and row['count'] >= limit:
                raise RegistrationError('요청이 반복되었습니다. 잠시 후 다시 시도하세요.', 429)
            db.execute('INSERT INTO student_account_attempts VALUES(?,?,1,?) '
                       'ON CONFLICT(scope,identity) DO UPDATE SET count=count+1', (scope, identity, now + seconds))

    def register(self, key, password, *, course=None, credential=None):
        key = student_key(key)
        self._attempt('signup-global', '*', limit=60, seconds=60)
        self._attempt('signup', key)
        if not _SLOTS.acquire(timeout=2):
            raise RegistrationError('서버가 잠시 바쁩니다. 다시 시도하세요.', 503)
        try:
            try:
                verifier = hash_member_password(password)
            except (ValueError, TypeError) as exc:
                raise RegistrationError(str(exc)) from exc
        finally:
            _SLOTS.release()
        with self.state._write() as db:
            member = db.execute('SELECT * FROM student_accounts WHERE student_key=?', (key,)).fetchone()
            if member and (credential is None or member['student_id'] is not None or not member['active']):
                raise RegistrationError('이미 등록되었거나 등록을 진행할 수 없는 학번입니다. 로그인하거나 담당 교수자에게 문의하세요.', 409)
            if course is not None:
                if not db.execute("SELECT 1 FROM admin_courses WHERE course_key=? AND status='active'", (course,)).fetchone():
                    raise PlatformNotFound('신청할 수 있는 수업이 아닙니다.')
            existing = db.execute('SELECT * FROM platform_students WHERE student_key=?', (key,)).fetchone()
            if existing:
                current = db.execute('''SELECT pw.*,e.student_id,e.course_key,e.active AS enrolled
                    FROM platform_student_passwords pw JOIN platform_enrollments e ON e.id=pw.enrollment_id
                    WHERE e.student_id=? AND e.course_key=?''',
                    (existing['id'], credential.course_key if credential else '')).fetchone()
                eligible_without_credential = bool(course and existing['active'] and existing['identity_kind'] == 'local'
                    and db.execute('SELECT 1 FROM student_course_eligibility WHERE course_key=? AND student_key=?', (course, key)).fetchone()
                    and not db.execute('SELECT 1 FROM platform_student_passwords pw JOIN platform_enrollments e ON e.id=pw.enrollment_id WHERE e.student_id=?', (existing['id'],)).fetchone())
                if not eligible_without_credential and (credential is None or not current or not current['enrolled'] or not existing['active']
                        or credential.student_id != existing['id'] or credential.student_key != key
                        or current['enrollment_id'] != credential.enrollment_id
                        or not hmac.compare_digest(current['password_hash'], credential.password_hash)
                        or current['locked_until'] and current['locked_until'] > utc_iso()):
                    raise RegistrationError('기존 등록 학생은 수업의 전용 비밀번호로 로그인한 뒤 회원 계정을 만드세요.', 409)
            elif credential is not None:
                raise PlatformAccessDenied('학생 인증 상태가 변경되었습니다.')
            now = utc_iso()
            if member:
                # A current legacy credential may recover an unbound signup, but
                # cannot rotate an already activated member account.
                db.execute('UPDATE student_accounts SET password_hash=?,student_id=?,verified=1,updated_at=? WHERE student_key=?', (verifier, existing['id'], now, key))
            else:
                db.execute('INSERT INTO student_accounts(student_key,password_hash,student_id,active,created_at,updated_at,verified) VALUES(?,?,?,1,?,?,?)',
                    (key, verifier, existing['id'] if existing else None, now, now, int(credential is not None)))
            if course:
                db.execute("INSERT INTO student_course_registrations VALUES(?,?,?,'pending',?,?,NULL)", ('sr_' + secrets.token_hex(16), key, course, now, now))
                self._match_course(db, course)
            elif credential:
                for row in db.execute("SELECT DISTINCT course_key FROM student_course_registrations WHERE student_key=? AND status='pending'", (key,)).fetchall():
                    self._match_course(db, row['course_key'])
        return self.account(key)

    def account(self, key):
        with self.state._connection() as db:
            row = db.execute('SELECT a.* FROM student_accounts a LEFT JOIN platform_students s ON s.id=a.student_id '
                'WHERE a.student_key=? AND a.active=1 AND (a.student_id IS NULL OR s.active=1)', (key,)).fetchone()
        if not row:
            raise PlatformAccessDenied('회원 인증이 만료되었습니다. 다시 로그인하세요.')
        return dict(row)

    def authenticate(self, key, password):
        key = student_key(key)
        self._attempt('login-global', '*', limit=180, seconds=60)
        self._attempt('login', key)
        try:
            account = self.account(key)
        except PlatformAccessDenied:
            account = None
        if not _SLOTS.acquire(timeout=2):
            raise RegistrationError('서버가 잠시 바쁩니다. 다시 시도하세요.', 503)
        try:
            matched = verify_student_password(password, account['password_hash'] if account else _DUMMY)
        finally:
            _SLOTS.release()
        if not account or not matched:
            raise RegistrationError('학번과 비밀번호를 확인하세요.', 403)
        with self.state._write() as db:
            current = db.execute('SELECT active,password_hash FROM student_accounts WHERE student_key=?', (key,)).fetchone()
            if not current or not current['active'] or current['password_hash'] != account['password_hash']:
                raise RegistrationError('학번과 비밀번호를 확인하세요.', 403)
            db.execute("DELETE FROM student_account_attempts WHERE scope='login' AND identity=?", (key,))
        return account

    def request_course(self, account, course):
        with self.state._write() as db:
            self._current(db, account)
            row = db.execute('SELECT status FROM admin_courses WHERE course_key=?', (course,)).fetchone()
            if not row or row['status'] != 'active':
                raise PlatformNotFound('신청할 수 있는 수업이 아닙니다.')
            old = db.execute('SELECT * FROM student_course_registrations WHERE student_key=? AND course_key=?',
                (account['student_key'], course)).fetchone()
            if old:
                self._match_course(db, course)
                return dict(db.execute('SELECT * FROM student_course_registrations WHERE request_id=?', (old['request_id'],)).fetchone())
            now = utc_iso()
            request_id = 'sr_' + secrets.token_hex(16)
            db.execute('INSERT INTO student_course_registrations VALUES(?,?,?,\'pending\',?,?,NULL)',
                (request_id, account['student_key'], course, now, now))
            self._match_course(db, course)
            return dict(db.execute('SELECT * FROM student_course_registrations WHERE request_id=?', (request_id,)).fetchone())

    def request_courses(self, account, courses):
        if not isinstance(courses, list) or not 1 <= len(courses) <= 20 or any(not isinstance(k, str) for k in courses) or len(set(courses)) != len(courses):
            raise RegistrationError('수강할 수업을 1~20개 선택하세요.')
        with self.state._write() as db:
            self._current(db, account)
            for course in courses:
                if not db.execute("SELECT 1 FROM admin_courses WHERE course_key=? AND status='active'", (course,)).fetchone():
                    raise PlatformNotFound('신청할 수 있는 수업이 아닙니다.')
            now = utc_iso()
            for course in courses:
                db.execute("INSERT OR IGNORE INTO student_course_registrations VALUES(?,?,?,'pending',?,?,NULL)",
                           ('sr_' + secrets.token_hex(16), account['student_key'], course, now, now))
                self._match_course(db, course)
                self._event(db, course, account['student_key'], 'student', 'enroll', '수강 선택')

    def withdraw_course(self, account, course):
        with self.state._write() as db:
            self._current(db, account)
            row = db.execute('SELECT status FROM student_course_registrations WHERE student_key=? AND course_key=?', (account['student_key'], course)).fetchone()
            enrolled = db.execute('SELECT 1 FROM platform_enrollments WHERE student_id=? AND course_key=? AND active=1', (account['student_id'], course)).fetchone()
            if not row or row['status'] != 'pending' or enrolled:
                raise RegistrationError('확인 대기 중인 수강 신청만 취소할 수 있습니다. 수강 중지는 교수자에게 문의하세요.', 409)
            db.execute('DELETE FROM student_course_registrations WHERE student_key=? AND course_key=?', (account['student_key'], course))
            self._event(db, course, account['student_key'], 'student', 'withdraw', '확인 대기 신청 취소')

    @staticmethod
    def _event(db, course, key, actor, action, detail):
        db.execute('INSERT INTO student_membership_events(course_key,student_key,actor,action,detail,created_at) VALUES(?,?,?,?,?,?)',
                   (course, key, actor, action, detail, utc_iso()))

    def issue_token(self, course, key, purpose, *, actor, reason, reauthorize=lambda: None):
        key = student_key(key)
        if purpose not in {'activate', 'reset'} or not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
            raise RegistrationError('발급 목적과 본인 확인 사유(3~500자)를 입력하세요.')
        self._attempt('issue-global', '*', limit=120, seconds=60)
        self._attempt('issue-' + purpose, key, limit=5, seconds=900)
        raw = ('ACT1-' if purpose == 'activate' else 'RST1-') + secrets.token_urlsafe(24)
        expires = time.time() + 900
        with self.state._write() as db:
            reauthorize()
            if not db.execute("SELECT 1 FROM admin_courses WHERE course_key=? AND status='active'", (course,)).fetchone():
                raise PlatformNotFound()
            eligible = db.execute('SELECT 1 FROM student_course_eligibility WHERE course_key=? AND student_key=?', (course, key)).fetchone()
            enrolled = db.execute('SELECT 1 FROM platform_enrollments e JOIN platform_students s ON s.id=e.student_id WHERE e.course_key=? AND s.student_key=? AND e.active=1 AND s.active=1', (course,key)).fetchone()
            account = db.execute('SELECT * FROM student_accounts WHERE student_key=? AND active=1', (key,)).fetchone()
            # Admin-confirmed recovery also repairs a pre-activation account
            # registered by the wrong person. Recovery never activates it.
            if not (eligible or enrolled) or (purpose == 'reset' and not account):
                raise RegistrationError('수강 명단과 회원 상태를 확인하세요.', 409)
            db.execute('DELETE FROM student_membership_tokens WHERE expires_at<=? OR (student_key=? AND purpose=? AND used_at IS NULL)', (time.time(), key, purpose))
            db.execute('INSERT INTO student_membership_tokens VALUES(?,?,?,?,?,NULL,?,?,?)',
                       (hashlib.sha256(raw.encode()).hexdigest(), key, course, purpose, expires, str(actor), reason.strip(), utc_iso()))
            self._event(db, course, key, str(actor), 'issue-' + purpose, reason.strip())
        return {'token': raw, 'expires_at': expires, 'purpose': purpose, 'student_key': key}

    def _token(self, db, key, token, purpose):
        if not isinstance(token, str) or not 16 <= len(token) <= 128:
            raise RegistrationError('코드와 학번을 확인하거나 새 코드를 요청하세요.', 403)
        row = db.execute('SELECT t.* FROM student_membership_tokens t JOIN admin_courses c ON c.course_key=t.course_key WHERE token_hash=? AND student_key=? AND purpose=? AND used_at IS NULL AND expires_at>? AND c.status=\'active\'',
                         (hashlib.sha256(token.encode()).hexdigest(), key, purpose, time.time())).fetchone()
        if not row:
            raise RegistrationError('코드와 학번을 확인하거나 새 코드를 요청하세요.', 403)
        eligible = db.execute('SELECT 1 FROM student_course_eligibility WHERE course_key=? AND student_key=?', (row['course_key'],key)).fetchone()
        enrolled = db.execute('SELECT 1 FROM platform_enrollments e JOIN platform_students s ON s.id=e.student_id WHERE e.course_key=? AND s.student_key=? AND e.active=1 AND s.active=1', (row['course_key'],key)).fetchone()
        if not (eligible or enrolled):
            raise RegistrationError('코드와 학번을 확인하거나 새 코드를 요청하세요.', 403)
        return row

    def activate_token(self, account, token):
        self._attempt('activate', account['student_key'])
        with self.state._write() as db:
            self._current(db, account)
            row = self._token(db, account['student_key'], token, 'activate')
            if account['verified']:
                raise RegistrationError('이미 본인 확인이 완료된 계정입니다.', 409)
            db.execute('UPDATE student_accounts SET verified=1,updated_at=? WHERE student_key=?', (utc_iso(), account['student_key']))
            db.execute('UPDATE student_membership_tokens SET used_at=? WHERE token_hash=?', (utc_iso(), row['token_hash']))
            for course in db.execute('SELECT course_key FROM student_course_registrations WHERE student_key=? AND status=\'pending\'', (account['student_key'],)).fetchall():
                self._match_course(db, course['course_key'])
            self._event(db, row['course_key'], account['student_key'], 'student', 'activate', '일회용 코드 본인 확인')
        return self.account(account['student_key'])

    def change_password(self, account, current_password, new_password):
        authenticated = self.authenticate(account['student_key'], current_password)
        if not hmac.compare_digest(authenticated['password_hash'], account['password_hash']):
            raise PlatformAccessDenied()
        return self._replace_password(account['student_key'], new_password, account=account)

    def reset_password(self, key, token, new_password):
        key = student_key(key)
        self._attempt('reset-global', '*', limit=60, seconds=60)
        self._attempt('reset', key)
        return self._replace_password(key, new_password, token=token)

    def _replace_password(self, key, password, *, account=None, token=None):
        if not _SLOTS.acquire(timeout=2):
            raise RegistrationError('서버가 잠시 바쁩니다. 다시 시도하세요.', 503)
        try:
            try:
                verifier = hash_member_password(password)
            except (TypeError, ValueError) as exc:
                raise RegistrationError(str(exc)) from exc
        finally:
            _SLOTS.release()
        with self.state._write() as db:
            current = db.execute('SELECT * FROM student_accounts WHERE student_key=? AND active=1', (key,)).fetchone()
            if not current:
                raise RegistrationError('코드와 학번을 확인하거나 새 코드를 요청하세요.', 403)
            if account is not None:
                self._current(db, account)
            else:
                row = self._token(db, key, token, 'reset')
                db.execute('UPDATE student_membership_tokens SET used_at=? WHERE token_hash=?', (utc_iso(), row['token_hash']))
            # Password-hash checks invalidate every old common session. Course
            # passwords and IDE tokens remain separate credentials.
            db.execute('UPDATE student_accounts SET password_hash=?,updated_at=? WHERE student_key=?', (verifier, utc_iso(), key))
            db.execute('UPDATE student_membership_tokens SET used_at=? WHERE student_key=? AND used_at IS NULL', (utc_iso(), key))
            self._event(db, None, key, 'student', 'password-change', '회원 비밀번호 변경 · 공통 웹 세션 만료')
        return self.account(key)

    def _current(self, db, account):
        current = db.execute('SELECT * FROM student_accounts WHERE student_key=?', (account['student_key'],)).fetchone()
        if (not current or not current['active'] or current['student_id'] != account['student_id']
                or not hmac.compare_digest(current['password_hash'], account['password_hash'])):
            raise PlatformAccessDenied('회원 인증 상태가 변경되었습니다.')
        if current['student_id'] is not None and not db.execute('SELECT 1 FROM platform_students WHERE id=? AND active=1', (current['student_id'],)).fetchone():
            raise PlatformAccessDenied('학생 이용 상태가 변경되었습니다.')

    def requests(self, course):
        with self.state._connection() as db:
            return [dict(row) for row in db.execute('SELECT r.request_id,r.student_key,r.status,r.created_at,r.updated_at,a.verified,a.active AS account_active '
                'FROM student_course_registrations r JOIN student_accounts a ON a.student_key=r.student_key WHERE r.course_key=? ORDER BY CASE r.status WHEN \'pending\' THEN 0 ELSE 1 END,r.created_at DESC LIMIT 1000', (course,))]

    def roster_overview(self, course):
        """Read membership and enrollment facts for this course's students only.

        Callers must authorize the course before and after this read. A global
        account alone does not make a student visible to a course instructor.
        Passwords, password hashes and other courses' enrollments are excluded.
        """
        with self.state._connection() as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM admin_courses WHERE course_key=?', (course,)).fetchone():
                raise PlatformNotFound()
            return [dict(row) for row in db.execute('''
                WITH scoped(student_key) AS (
                    SELECT student_key FROM student_course_eligibility WHERE course_key=:course
                    UNION SELECT student_key FROM student_course_registrations WHERE course_key=:course
                    UNION SELECT s.student_key FROM platform_enrollments e
                        JOIN platform_students s ON s.id=e.student_id WHERE e.course_key=:course
                )
                SELECT scoped.student_key,COALESCE(p.name,'') AS name,
                    a.active AS account_active,a.verified,a.student_id AS account_student_id,
                    s.id AS student_id,s.active AS student_active,
                    e.id AS enrollment_id,e.active AS enrollment_active,
                    CASE WHEN pw.enrollment_id IS NULL THEN 0 ELSE 1 END AS has_password,
                    CASE WHEN pw.password_hash LIKE 'scrypt$v2$%' THEN 1 ELSE 0 END AS has_course_pin,
                    pw.locked_until,r.status AS request_status,r.created_at AS requested_at,
                    CASE WHEN allowed.student_key IS NULL THEN 0 ELSE 1 END AS eligible
                FROM scoped
                LEFT JOIN student_accounts a ON a.student_key=scoped.student_key
                LEFT JOIN platform_students s ON s.student_key=scoped.student_key
                LEFT JOIN admin_student_profiles p ON p.student_id=s.id
                LEFT JOIN platform_enrollments e ON e.student_id=s.id AND e.course_key=:course
                LEFT JOIN platform_student_passwords pw ON pw.enrollment_id=e.id
                LEFT JOIN student_course_registrations r ON r.student_key=scoped.student_key AND r.course_key=:course
                LEFT JOIN student_course_eligibility allowed ON allowed.student_key=scoped.student_key AND allowed.course_key=:course
                ORDER BY scoped.student_key
                ''', {'course': course})]

    @staticmethod
    def parse_roster(content):
        if not isinstance(content, bytes) or len(content) > 1024 * 1024:
            raise ValueError('CSV는 1 MiB 이하로 등록하세요.')
        try:
            rows = csv.reader(io.StringIO(content.decode('utf-8-sig')))
            header = next(rows)
            if header not in [['student_key'], ['학번'], ['student_key', 'name'], ['학번', '이름']]:
                raise ValueError('CSV 첫 행은 student_key 또는 학번이어야 합니다. 이름 열은 선택입니다.')
            keys = set()
            duplicates = 0
            for index, row in enumerate(rows, 2):
                if len(row) != len(header):
                    raise ValueError(f'CSV {index}행의 열 개수를 확인하세요.')
                key = student_key(row[0])
                duplicates += int(key in keys)
                keys.add(key)
                if index > 10001:
                    raise ValueError('CSV는 10,000명 이하로 등록하세요.')
            if not keys:
                raise ValueError('명단에 학번을 한 개 이상 넣으세요.')
        except (UnicodeError, StopIteration, csv.Error) as exc:
            raise ValueError('UTF-8 CSV 형식을 확인하세요.') from exc
        return keys, duplicates

    def upload_eligibility(self, course, content, *, reauthorize=lambda: None):
        keys, _ = self.parse_roster(content)
        with self.state._write() as db:
            reauthorize()
            if not db.execute("SELECT 1 FROM admin_courses WHERE course_key=? AND status='active'", (course,)).fetchone():
                raise PlatformNotFound('운영 중인 수업이 아닙니다.')
            now = utc_iso()
            db.executemany('INSERT OR IGNORE INTO student_course_eligibility VALUES(?,?,?)', [(course,key,now) for key in sorted(keys)])
            matched = self._match_course(db, course)
        return {'uploaded': len(keys), 'matched': matched}

    def preview_eligibility(self, course, content, mode, *, actor, reauthorize=lambda: None):
        if mode not in {'add', 'replace'}:
            raise RegistrationError('명단 추가 또는 교체를 선택하세요.')
        keys, duplicates = self.parse_roster(content)
        with self.state._write() as db:
            reauthorize()
            if not db.execute("SELECT 1 FROM admin_courses WHERE course_key=? AND status='active'", (course,)).fetchone():
                raise PlatformNotFound()
            old = {r['student_key'] for r in db.execute('SELECT student_key FROM student_course_eligibility WHERE course_key=?', (course,))}
            pending = {r['student_key'] for r in db.execute("SELECT r.student_key FROM student_course_registrations r JOIN student_accounts a ON a.student_key=r.student_key WHERE r.course_key=? AND r.status='pending' AND a.verified=1 AND a.active=1", (course,))}
            change_id = 'rc_' + secrets.token_hex(16)
            db.execute('DELETE FROM student_roster_changes WHERE expires_at<=?', (time.time(),))
            db.execute('INSERT INTO student_roster_changes VALUES(?,?,?,?,?,?,?,NULL)',
                       (change_id, course, str(actor), mode, json.dumps(sorted(keys)), self._roster_digest(old), time.time()+900))
        return {'change_id': change_id, 'mode': mode, 'total': len(keys), 'added': len(keys-old),
                'existing': len(keys&old), 'removed': len(old-keys) if mode == 'replace' else 0,
                'duplicates': duplicates, 'pending_verified': len(keys&pending), 'sample': sorted(keys)[:20]}

    @staticmethod
    def _roster_digest(keys):
        return hashlib.sha256(json.dumps(sorted(keys)).encode()).hexdigest()

    def apply_eligibility(self, course, change_id, *, actor, reauthorize=lambda: None):
        with self.state._write() as db:
            reauthorize()
            row = db.execute('SELECT * FROM student_roster_changes WHERE change_id=? AND course_key=? AND actor=? AND expires_at>? AND applied_at IS NULL', (change_id, course, str(actor), time.time())).fetchone()
            if not row or not db.execute("SELECT 1 FROM admin_courses WHERE course_key=? AND status='active'", (course,)).fetchone():
                raise RegistrationError('검토 화면이 만료되었거나 이미 처리되었습니다. 파일을 다시 확인하세요.', 409)
            old = {r['student_key'] for r in db.execute('SELECT student_key FROM student_course_eligibility WHERE course_key=?', (course,))}
            if not hmac.compare_digest(self._roster_digest(old), row['before_digest']):
                raise RegistrationError('다른 작업에서 명단이 변경되었습니다. 파일을 다시 검토하세요.', 409)
            keys = set(json.loads(row['keys_json']))
            if row['mode'] == 'replace':
                db.execute('DELETE FROM student_course_eligibility WHERE course_key=?', (course,))
            now = utc_iso()
            db.executemany('INSERT OR IGNORE INTO student_course_eligibility VALUES(?,?,?)', [(course,key,now) for key in sorted(keys)])
            matched = self._match_course(db, course)
            result = {'added': len(keys-old), 'removed': len(old-keys) if row['mode'] == 'replace' else 0, 'matched': matched}
            db.execute('UPDATE student_roster_changes SET applied_at=? WHERE change_id=?', (now, change_id))
            self._event(db, course, None, str(actor), 'roster-' + row['mode'], json.dumps(result))
        return result

    def eligibility(self, course):
        with self.state._connection() as db:
            return [row['student_key'] for row in db.execute('SELECT student_key FROM student_course_eligibility WHERE course_key=? ORDER BY student_key LIMIT 10000', (course,))]

    def _match_course(self, db, course):
        rows = db.execute("SELECT r.* FROM student_course_registrations r JOIN student_course_eligibility e ON e.course_key=r.course_key AND e.student_key=r.student_key WHERE r.course_key=? AND r.status='pending'", (course,)).fetchall()
        matched = 0
        for row in rows:
            account = db.execute('SELECT * FROM student_accounts WHERE student_key=? AND active=1', (row['student_key'],)).fetchone()
            if not account or not account['verified']:
                continue
            existing = db.execute('SELECT * FROM platform_students WHERE student_key=?', (row['student_key'],)).fetchone()
            # A concurrently imported or blocked identity is never adopted.
            if existing and (existing['id'] != account['student_id'] or not existing['active']):
                continue
            now = utc_iso()
            if not existing:
                if account['student_id'] is not None:
                    continue
                github_id, github_login = _local_identity_columns(row['student_key'])
                cursor = db.execute("INSERT INTO platform_students(student_key,auth_subject,identity_kind,github_user_id,github_login,active,created_at,updated_at) VALUES(?,?,'local',?,?,1,?,?)", (row['student_key'], 'member:' + secrets.token_hex(24), github_id, github_login, now, now))
                student_id = cursor.lastrowid
                db.execute('UPDATE student_accounts SET student_id=?,updated_at=? WHERE student_key=?', (student_id, now, row['student_key']))
            else:
                student_id = existing['id']
            previous = db.execute('SELECT * FROM platform_enrollments WHERE student_id=? AND course_key=?', (student_id, course)).fetchone()
            if previous and not previous['active']:
                continue
            if not previous:
                db.execute('INSERT INTO platform_enrollments(student_id,course_key,active,created_at,updated_at) VALUES(?,?,1,?,?)', (student_id, course, now, now))
            if not previous or not db.execute('SELECT 1 FROM platform_student_passwords WHERE enrollment_id=?', (previous['id'],)).fetchone():
                self.state._set_student_password_hash_in_connection(db, student_id=student_id, course_key=course, password_hash=account['password_hash'], now=now)
            db.execute("UPDATE student_course_registrations SET status='matched',updated_at=?,reviewed_by='roster-match' WHERE request_id=?", (now, row['request_id']))
            matched += 1
        return matched

    def summary(self, account):
        with self.state._connection() as db:
            self._current(db, account)
            rows = db.execute('''SELECT c.*,r.status AS request_status,
                CASE WHEN EXISTS (SELECT 1 FROM student_course_eligibility x WHERE x.course_key=c.course_key AND x.student_key=?) THEN 1 ELSE 0 END AS eligible,
                CASE WHEN e.active=1 AND pw.enrollment_id IS NOT NULL THEN 1 ELSE 0 END AS enrolled
                FROM admin_courses c
                LEFT JOIN student_course_registrations r ON r.course_key=c.course_key AND r.student_key=?
                LEFT JOIN platform_enrollments e ON e.course_key=c.course_key AND e.student_id=?
                LEFT JOIN platform_student_passwords pw ON pw.enrollment_id=e.id
                WHERE c.status='active' ORDER BY c.code,c.year,c.semester,c.section''',
                (account['student_key'], account['student_key'], account['student_id'])).fetchall()
            return [dict(row) for row in rows]
