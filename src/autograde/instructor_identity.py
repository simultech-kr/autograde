"""Optional local instructor identities and course grants; no student credentials."""
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid

from .platform_service import PlatformAPIError
from .platform_state import PlatformAccessDenied, PlatformNotFound


APPLICATION_ID = 0x41474944
USERNAME = re.compile(r'[a-z][a-z0-9_.-]{2,63}')
COURSE = re.compile(r'[a-z0-9_-]{1,96}')
CHALLENGE = {'WWW-Authenticate': 'Basic realm="Autograde personal instructor", charset="UTF-8"'}


def _password_bytes(value):
    if not isinstance(value, str):
        raise ValueError('비밀번호 형식을 확인하세요.')
    value = unicodedata.normalize('NFC', value)
    if not 12 <= len(value) <= 128 or len(value.encode('utf-8')) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('교수자 비밀번호는 12~128자, UTF-8 256바이트 이하이며 제어문자를 포함할 수 없습니다.')
    return value.encode('utf-8')


def _derive(password, salt):
    return hashlib.scrypt(password, salt=salt, n=32768, r=8, p=3, maxmem=64*1024*1024, dklen=32)


def hash_password(password):
    raw, salt = _password_bytes(password), secrets.token_bytes(16)
    return 'scrypt$instructor-v1$' + salt.hex() + '$' + _derive(raw, salt).hex()


def verify_password(password, encoded):
    valid = True
    try:
        raw = _password_bytes(password)
    except ValueError:
        valid, raw = False, b'invalid-password'
    try:
        prefix, version, salt, digest = encoded.split('$')
        salt, digest = bytes.fromhex(salt), bytes.fromhex(digest)
        if prefix != 'scrypt' or version != 'instructor-v1' or len(salt) != 16 or len(digest) != 32:
            raise ValueError()
    except (AttributeError, ValueError):
        valid, salt, digest = False, bytes(16), bytes(32)
    # Unknown/disabled accounts take the same bounded expensive verification path.
    return hmac.compare_digest(_derive(raw, salt), digest) and valid


@dataclass(frozen=True)
class InstructorPrincipal:
    user_id: str
    username: str
    display_name: str
    role: str
    revision: int
    courses: frozenset[str]

    @property
    def session_binding(self):
        return f'{self.user_id}:{self.revision}'

    def allows(self, course):
        return self.role == 'admin' or course in self.courses


class InstructorIdentityStore:
    def __init__(self, path, *, clock=time.monotonic):
        self.path = Path(path).absolute()
        self._slots = threading.BoundedSemaphore(2)
        self._lock = threading.Lock()
        self._failures = {}  # Known user IDs + one unknown-account bucket; bounded.
        self._clock = clock
        self._verified = {}  # HMAC credential fingerprints only, never raw credentials.
        self._cache_secret = secrets.token_bytes(32)

    def initialize(self):
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ValueError('교수자 계정 DB가 이미 존재합니다. 덮어쓰지 않습니다.') from exc
        os.close(descriptor)
        with self._connect(check=False) as db:
            db.executescript(f'''
                PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=1;
                CREATE TABLE instructors (
                    user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','instructor')),
                    password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE instructor_grants (
                    user_id TEXT NOT NULL REFERENCES instructors(user_id), course_key TEXT NOT NULL,
                    PRIMARY KEY(user_id,course_key));
                CREATE TABLE instructor_identity_audit (
                    sequence INTEGER PRIMARY KEY, action TEXT NOT NULL, user_id TEXT NOT NULL,
                    course_key TEXT, actor TEXT NOT NULL, occurred_at TEXT NOT NULL);
            ''')

    @contextmanager
    def _connect(self, *, check=True, read_only=False):
        if self.path.is_symlink():
            raise ValueError('교수자 계정 DB는 심볼릭 링크일 수 없습니다.')
        db = sqlite3.connect(self.path.as_uri() + ('?mode=ro' if read_only else '?mode=rw'),
                             uri=True, timeout=0.1 if read_only else 5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if check and (db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                          or db.execute('PRAGMA user_version').fetchone()[0] != 1):
                raise ValueError('지원하지 않는 교수자 계정 DB입니다.')
            with db:
                yield db
        finally:
            db.close()

    def check_ready(self):
        with self._connect(read_only=True) as db:
            db.execute('SELECT user_id,course_key FROM instructor_grants LIMIT 0')
            db.execute('SELECT action,actor FROM instructor_identity_audit LIMIT 0')
            if not db.execute("SELECT 1 FROM instructors WHERE role='admin' AND active=1 LIMIT 1").fetchone():
                raise ValueError('개인 인증을 켜기 전에 활성 관리자 계정을 생성하세요.')

    @staticmethod
    def _username(username):
        if not isinstance(username, str) or not USERNAME.fullmatch(username):
            raise ValueError('계정명은 소문자로 시작하는 3~64자 영문 소문자·숫자·밑줄·점·하이픈입니다.')
        return username

    @staticmethod
    def _audit(db, action, user_id, course=None):
        db.execute('INSERT INTO instructor_identity_audit(action,user_id,course_key,actor,occurred_at) VALUES(?,?,?,?,?)',
                   (action, user_id, course, 'local_operator', datetime.now(timezone.utc).isoformat()))

    @staticmethod
    def _user(db, username):
        row = db.execute('SELECT * FROM instructors WHERE username=?', (username,)).fetchone()
        if row is None:
            raise ValueError('등록된 교수자 계정이 없습니다.')
        return row

    def create(self, username, display_name, role, password):
        self._username(username)
        if role not in {'admin', 'instructor'} or not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 100 or any(ord(c) < 32 for c in display_name):
            raise ValueError('계정 역할과 표시 이름을 확인하세요.')
        encoded = hash_password(password)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM instructors WHERE username=?', (username,)).fetchone():
                raise ValueError('이미 등록된 계정명입니다.')
            user_id = 'ins_' + uuid.uuid4().hex
            db.execute('INSERT INTO instructors(user_id,username,display_name,role,password_hash) VALUES(?,?,?,?,?)',
                       (user_id, username, display_name.strip(), role, encoded))
            self._audit(db, 'created', user_id)
        return user_id

    def set_password(self, username, password):
        encoded = hash_password(password)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._user(db, username)
            db.execute('UPDATE instructors SET password_hash=?,revision=revision+1 WHERE user_id=?', (encoded, row['user_id']))
            self._audit(db, 'password_changed', row['user_id'])

    def set_active(self, username, active):
        if type(active) is not bool:
            raise ValueError('활성 상태는 boolean이어야 합니다.')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._user(db, username)
            if bool(row['active']) == active:
                return
            if not active and row['role'] == 'admin' and db.execute("SELECT COUNT(*) FROM instructors WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                raise ValueError('마지막 활성 관리자 계정은 비활성화할 수 없습니다.')
            db.execute('UPDATE instructors SET active=?,revision=revision+1 WHERE user_id=?', (active, row['user_id']))
            self._audit(db, 'enabled' if active else 'disabled', row['user_id'])

    def set_grant(self, username, course, *, allowed):
        if not isinstance(course, str) or not COURSE.fullmatch(course) or type(allowed) is not bool:
            raise ValueError('분반 코드와 권한을 확인하세요.')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._user(db, username)
            if row['role'] == 'admin':
                raise ValueError('관리자는 모든 수업에 접근합니다. 제한 계정은 instructor 역할로 만드세요.')
            if allowed:
                changed = db.execute('INSERT OR IGNORE INTO instructor_grants VALUES(?,?)', (row['user_id'], course)).rowcount
            else:
                changed = db.execute('DELETE FROM instructor_grants WHERE user_id=? AND course_key=?', (row['user_id'], course)).rowcount
            if changed:
                db.execute('UPDATE instructors SET revision=revision+1 WHERE user_id=?', (row['user_id'],))
                self._audit(db, 'course_granted' if allowed else 'course_revoked', row['user_id'], course)

    def list_accounts(self):
        with self._connect() as db:
            db.execute('BEGIN')
            result = []
            for row in db.execute('SELECT user_id,username,display_name,role,active,revision FROM instructors ORDER BY username'):
                result.append(dict(row, courses=[r[0] for r in db.execute('SELECT course_key FROM instructor_grants WHERE user_id=? ORDER BY course_key', (row['user_id'],))]))
            return result

    @staticmethod
    def _denied():
        return PlatformAPIError(401, 'instructor_auth_required', '교수자 계정과 비밀번호를 확인하세요.', headers=CHALLENGE)

    def _cached_principal(self, fingerprint):
        """Reuse hashing for 60s, but check active/revision/grants on EVERY request.

        This is not a browser login session. Entries expire absolutely and cannot
        bypass revocation or a missing identity store, even from another process.
        """
        now = self._clock()
        with self._lock:
            self._verified = {k: v for k, v in self._verified.items() if v[2] > now}
            cached = self._verified.get(fingerprint)
        if cached is None:
            return None
        user_id, revision, _ = cached
        try:
            with self._connect() as db:
                db.execute('BEGIN')
                row = db.execute('SELECT * FROM instructors WHERE user_id=?', (user_id,)).fetchone()
                if row and row['active'] and row['revision'] == revision:
                    courses = frozenset(r[0] for r in db.execute(
                        'SELECT course_key FROM instructor_grants WHERE user_id=?', (user_id,)))
                    return InstructorPrincipal(user_id, row['username'], row['display_name'], row['role'], revision, courses)
        except (sqlite3.Error, OSError, ValueError):
            raise PlatformAPIError(503, 'instructor_identity_unavailable', '교수자 인증 저장소를 확인해 주세요.') from None
        with self._lock:
            self._verified.pop(fingerprint, None)
        return None

    def authenticate(self, authorization):
        try:
            if not isinstance(authorization, str) or not authorization.startswith('Basic ') or len(authorization) > 1024:
                raise ValueError()
            decoded = base64.b64decode(authorization[6:], validate=True).decode('utf-8')
            username, separator, password = decoded.partition(':')
            if separator != ':' or not USERNAME.fullmatch(username):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise self._denied() from None
        fingerprint = hmac.digest(self._cache_secret, authorization.encode('utf-8'), 'sha256')
        cached = self._cached_principal(fingerprint)
        if cached is not None:
            return cached
        # Keep hashing concurrency bounded, but absorb small legitimate bursts.
        if not self._slots.acquire(timeout=2):
            raise PlatformAPIError(429, 'instructor_auth_busy', '인증 요청이 많습니다. 잠시 후 다시 시도하세요.', headers={'Retry-After':'5'})
        try:
            cached = self._cached_principal(fingerprint)
            if cached is not None:
                return cached
            with self._connect() as db:
                row = db.execute('SELECT * FROM instructors WHERE username=?', (username,)).fetchone()
            bucket = row['user_id'] if row else 'unknown'
            clock = self._clock()
            with self._lock:
                self._failures = {key: value for key, value in self._failures.items() if clock - value[0] < 60}
                attempts = self._failures.get(bucket, (clock, 0))
                if attempts[1] >= 5 or len(self._failures) >= 1024:
                    raise PlatformAPIError(429, 'instructor_auth_limited', '인증 시도가 많습니다. 1분 뒤 다시 시도하세요.', headers={'Retry-After':'60'})
                # Reserve under the lock so parallel failures cannot overshoot the cap.
                # A successful authentication clears this consecutive-failure budget.
                self._failures[bucket] = (attempts[0], attempts[1] + 1)
            verified = verify_password(password, row['password_hash'] if row else None)
            if not verified or not row or not row['active']:
                raise self._denied()
            # Recheck revision after slow hashing, then take grants in the same snapshot.
            with self._connect() as db:
                db.execute('BEGIN')
                current = db.execute('SELECT * FROM instructors WHERE user_id=?', (row['user_id'],)).fetchone()
                if not current or not current['active'] or current['revision'] != row['revision']:
                    raise self._denied()
                courses = frozenset(r[0] for r in db.execute('SELECT course_key FROM instructor_grants WHERE user_id=?', (row['user_id'],)))
            with self._lock:
                self._failures.pop(bucket, None)
                if len(self._verified) >= 1024:
                    self._verified.pop(next(iter(self._verified)))
                self._verified[fingerprint] = (row['user_id'], row['revision'], self._clock() + 60)
            return InstructorPrincipal(row['user_id'], row['username'], row['display_name'], row['role'], row['revision'], courses)
        except (sqlite3.Error, OSError, ValueError):
            raise PlatformAPIError(503, 'instructor_identity_unavailable', '교수자 인증 저장소를 확인해 주세요.') from None
        finally:
            self._slots.release()

    def authorize_course(self, authorization, course):
        principal = self.authenticate(authorization)
        if not principal.allows(course):
            raise PlatformAPIError(403, 'instructor_course_denied', '담당 교과목·분반이 아닙니다.')
        return principal


class ScopedCourses:
    """Request-local view of the course service; never mutate a shared controller."""
    def __init__(self, service, principal):
        self.service, self.principal = service, principal

    def get_course(self, key):
        if not self.principal.allows(key):
            raise PlatformNotFound()
        return self.service.get_course(key)

    def list_courses(self, **options):
        return [c for c in self.service.list_courses(**options) if self.principal.allows(c['course_key'])]

    def management_overview(self):
        return [c for c in self.service.management_overview() if self.principal.allows(c['course_key'])]

    def create_course(self, **fields):
        if self.principal.role != 'admin':
            raise PlatformAccessDenied()
        return self.service.create_course(**fields)

    def update_course(self, key, **fields):
        self.get_course(key)
        return self.service.update_course(key, **fields)

    def set_status(self, key, status):
        self.get_course(key)
        return self.service.set_status(key, status)
