"""Revocable instructor browser sessions and RFC 6238 second factors.

Bearer secrets/recovery codes are persisted only as digests. TOTP seeds use
Fernet authenticated encryption under a separate mode-0600 installation key.
The identity revision remains the authority for every authenticated request.
"""
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import struct
import time

from cryptography.fernet import Fernet, InvalidToken

from .platform_auth import create_or_load_auth_secret
from .platform_service import PlatformAPIError
from .platform_state import PlatformAccessDenied


COOKIE = 'autograde_instructor_session'
MAX_AGE = 8 * 3600
IDLE_AGE = 30 * 60
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')


def totp(secret, counter):
    """RFC 4226/6238 dynamic truncation, 6 digits, 30-second time step."""
    digest = hmac.digest(base64.b32decode(secret), struct.pack('>Q', counter), 'sha1')
    offset = digest[-1] & 15
    return f'{(struct.unpack(">I", digest[offset:offset+4])[0] & 0x7fffffff) % 1000000:06d}'


def _digest(value):
    return hashlib.sha256(('instructor-session-v1:' + value).encode()).hexdigest()


class InstructorBrowserAuth:
    def __init__(self, identities, *, clock=time.time):
        self.identities, self.clock = identities, clock
        self.key_path = identities.path.parent / 'instructor-mfa.key'
        self._fernet = None

    def prepare(self):
        if self.identities.path.stat().st_mode & 0o077:
            raise ValueError('교수자 계정 DB 권한은 0600이어야 합니다.')
        with self.identities._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS instructor_browser_sessions (
                    digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES instructors(user_id),
                    revision INTEGER NOT NULL, csrf TEXT NOT NULL, level TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, touched REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS instructor_browser_user ON instructor_browser_sessions(user_id);
                CREATE TABLE IF NOT EXISTS instructor_mfa (
                    user_id TEXT PRIMARY KEY REFERENCES instructors(user_id), secret TEXT,
                    pending TEXT, pending_expires REAL, last_counter INTEGER NOT NULL DEFAULT -1);
                CREATE TABLE IF NOT EXISTS instructor_recovery_codes (
                    user_id TEXT NOT NULL REFERENCES instructors(user_id), digest TEXT NOT NULL,
                    PRIMARY KEY(user_id,digest));
                CREATE TABLE IF NOT EXISTS instructor_factor_failures (
                    user_id TEXT PRIMARY KEY REFERENCES instructors(user_id), started REAL NOT NULL, attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS instructor_access_roles (
                    user_id TEXT PRIMARY KEY REFERENCES instructors(user_id), role TEXT NOT NULL CHECK(role IN ('assistant','evaluator')));
            ''')
            encrypted = db.execute('SELECT 1 FROM instructor_mfa WHERE secret IS NOT NULL OR pending IS NOT NULL LIMIT 1').fetchone()
        # Never silently replace a lost encryption key and strand existing MFA.
        if encrypted and not self.key_path.exists():
            raise ValueError('교수자 MFA 암호화 키를 백업에서 복원하세요.')
        key = create_or_load_auth_secret(self.key_path)
        self._key_fingerprint = hashlib.sha256(key).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def check_ready(self):
        if self.identities.path.stat().st_mode & 0o077:
            raise ValueError('교수자 계정 DB 권한은 0600이어야 합니다.')
        with self.identities._connect(read_only=True) as db:
            db.execute('SELECT digest FROM instructor_browser_sessions LIMIT 0')
            for row in db.execute('SELECT secret,pending FROM instructor_mfa'):
                for encrypted in row:
                    if encrypted:
                        self._decrypt(encrypted)
        # Verify on disk too; cached crypto objects must not hide a missing key.
        if not self.key_path.is_file() or self.key_path.is_symlink() or self.key_path.stat().st_mode & 0o077:
            raise ValueError('교수자 MFA 암호화 키를 확인하세요.')
        if self.key_path.stat().st_size != 32 or not hmac.compare_digest(hashlib.sha256(self.key_path.read_bytes()).digest(), self._key_fingerprint):
            raise ValueError('교수자 MFA 암호화 키가 변경되었습니다. 백업 복원 후 재시작하세요.')

    def _decrypt(self, encrypted):
        try:
            return self._fernet.decrypt(encrypted.encode('ascii')).decode('ascii')
        except (InvalidToken, ValueError, UnicodeError):
            raise PlatformAPIError(503, 'instructor_mfa_unavailable', '교수자 인증 키의 복원이 필요합니다.') from None

    @staticmethod
    def _denied():
        return PlatformAPIError(401, 'instructor_login_required', '계정·비밀번호·인증 코드를 확인하고 다시 로그인하세요.')

    def _new_session(self, db, principal, level):
        now = self.clock()
        db.execute('DELETE FROM instructor_browser_sessions WHERE expires<=? OR touched<=?', (now, now-IDLE_AGE))
        # Bound concurrent sessions for one user; old sessions are revoked.
        db.execute('DELETE FROM instructor_browser_sessions WHERE digest IN (SELECT digest FROM instructor_browser_sessions WHERE user_id=? ORDER BY created DESC LIMIT -1 OFFSET 4)', (principal.user_id,))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        lifetime = 600 if level == 'enroll' else MAX_AGE
        db.execute('INSERT INTO instructor_browser_sessions VALUES(?,?,?,?,?,?,?,?)',
                   (_digest(token), principal.user_id, principal.revision, csrf, level, now, now+lifetime, now))
        return token

    def _session(self, db, token, *, allow_enrollment=False):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise self._denied()
        row = db.execute('SELECT * FROM instructor_browser_sessions WHERE digest=?', (_digest(token),)).fetchone()
        now = self.clock()
        if not row or row['expires'] <= now or row['touched'] <= now-IDLE_AGE:
            raise self._denied()
        account = db.execute('SELECT * FROM instructors WHERE user_id=?', (row['user_id'],)).fetchone()
        if not account or not account['active'] or account['revision'] != row['revision']:
            raise self._denied()
        if row['level'] != 'full' and not allow_enrollment:
            raise PlatformAPIError(403, 'instructor_mfa_enrollment_required', '관리자 2단계 인증 등록을 완료하세요.')
        if row['touched'] <= now-60:
            db.execute('UPDATE instructor_browser_sessions SET touched=? WHERE digest=?', (now, row['digest']))
        return {'principal': self.identities._principal(db, account), 'csrf': row['csrf'], 'level': row['level'], 'sid': row['digest']}

    def session(self, token, *, allow_enrollment=False):
        try:
            with self.identities._connect() as db:
                return self._session(db, token, allow_enrollment=allow_enrollment)
        except (sqlite3.Error, OSError, ValueError):
            raise PlatformAPIError(503, 'instructor_identity_unavailable', '교수자 인증 저장소를 확인하세요.') from None

    def logout(self, token):
        if isinstance(token, str) and TOKEN.fullmatch(token):
            with self.identities._connect() as db:
                db.execute('DELETE FROM instructor_browser_sessions WHERE digest=?', (_digest(token),))

    def _reserve_factor(self, user_id):
        """Commit attempts before verification so failed transactions retain limits."""
        with self.identities._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            row = db.execute('SELECT * FROM instructor_factor_failures WHERE user_id=?', (user_id,)).fetchone()
            if row and row['started'] > now-60 and row['attempts'] >= 5:
                raise PlatformAPIError(429, 'instructor_mfa_limited', '인증 코드 시도가 많습니다. 1분 뒤 다시 시도하세요.', headers={'Retry-After': '60'})
            started, attempts = (row['started'], row['attempts']+1) if row and row['started'] > now-60 else (now, 1)
            db.execute('INSERT OR REPLACE INTO instructor_factor_failures VALUES(?,?,?)', (user_id, started, attempts))

    def _consume_factor(self, db, user_id, supplied, *, pending=False):
        row = db.execute('SELECT * FROM instructor_mfa WHERE user_id=?', (user_id,)).fetchone()
        encrypted = row['pending' if pending else 'secret'] if row else None
        if not encrypted or (pending and (row['pending_expires'] or 0) <= self.clock()):
            raise self._denied()
        secret = self._decrypt(encrypted)
        if isinstance(supplied, str) and re.fullmatch(r'[0-9]{6}', supplied):
            current = int(self.clock()) // 30
            match = next((counter for counter in (current, current-1, current+1)
                          if counter > row['last_counter'] and hmac.compare_digest(totp(secret, counter), supplied)), None)
            if match is None:
                raise self._denied()
            db.execute('UPDATE instructor_mfa SET last_counter=? WHERE user_id=?', (match, user_id))
        elif not pending and isinstance(supplied, str) and re.fullmatch(r'RC-[A-F0-9]{32}', supplied):
            changed = db.execute('DELETE FROM instructor_recovery_codes WHERE user_id=? AND digest=?', (user_id, _digest(user_id+':'+supplied))).rowcount
            if not changed:
                raise self._denied()
            self.identities._audit(db, 'mfa_recovery_used', user_id)
        else:
            raise self._denied()
        db.execute('DELETE FROM instructor_factor_failures WHERE user_id=?', (user_id,))

    def verify_credentials(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 64 or len(password) > 128:
            raise self._denied()
        header = 'Basic ' + base64.b64encode((username+':'+password).encode()).decode()
        try:
            return self.identities._authenticate_basic(header)
        except PlatformAPIError as exc:
            if exc.status == 401:
                raise self._denied() from None
            raise

    def login(self, username, password, factor=''):
        principal = self.verify_credentials(username, password)
        with self.identities._connect() as db:
            row = db.execute('SELECT secret FROM instructor_mfa WHERE user_id=?', (principal.user_id,)).fetchone()
            has_mfa = bool(row and row['secret'])
        if has_mfa:
            self._reserve_factor(principal.user_id)
        with self.identities._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.identities._assert_actor(db, principal, target=principal.user_id)
            # Re-read inside the commit transaction to close concurrent setup/reset.
            row = db.execute('SELECT secret FROM instructor_mfa WHERE user_id=?', (principal.user_id,)).fetchone()
            if bool(row and row['secret']) != has_mfa:
                raise self._denied()
            if has_mfa:
                self._consume_factor(db, principal.user_id, factor)
            level = 'enroll' if principal.role == 'admin' and not has_mfa else 'full'
            token = self._new_session(db, principal, level)
            self.identities._audit(db, 'web_login' if level == 'full' else 'mfa_setup_login', principal.user_id, actor=principal)
        return token, level

    def mfa_status(self, principal):
        with self.identities._connect(read_only=True) as db:
            row = db.execute('SELECT secret FROM instructor_mfa WHERE user_id=?', (principal.user_id,)).fetchone()
            remaining = db.execute('SELECT COUNT(*) FROM instructor_recovery_codes WHERE user_id=?', (principal.user_id,)).fetchone()[0]
            return {'enabled': bool(row and row['secret']), 'recovery_remaining': remaining}

    def begin_enrollment(self, token):
        with self.identities._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            session = self._session(db, token, allow_enrollment=True)
            principal = session['principal']
            row = db.execute('SELECT * FROM instructor_mfa WHERE user_id=?', (principal.user_id,)).fetchone()
            if row and row['secret']:
                raise ValueError('2단계 인증이 이미 등록되어 있습니다. 분실 시 관리자에게 초기화를 요청하세요.')
            # Reuse an unexpired pending setup so a second tab cannot replace it.
            if row and row['pending'] and row['pending_expires'] > self.clock():
                return self._decrypt(row['pending'])
            seed = base64.b32encode(secrets.token_bytes(20)).decode('ascii')
            encrypted = self._fernet.encrypt(seed.encode()).decode('ascii')
            db.execute('INSERT INTO instructor_mfa(user_id,pending,pending_expires) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET pending=excluded.pending,pending_expires=excluded.pending_expires,last_counter=-1', (principal.user_id, encrypted, self.clock()+600))
            return seed

    def finish_enrollment(self, token, factor):
        context = self.session(token, allow_enrollment=True)
        self._reserve_factor(context['principal'].user_id)
        with self.identities._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            session = self._session(db, token, allow_enrollment=True)
            principal = session['principal']
            existing = db.execute('SELECT secret FROM instructor_mfa WHERE user_id=?', (principal.user_id,)).fetchone()
            if existing and existing['secret']:
                raise self._denied()
            self._consume_factor(db, principal.user_id, factor, pending=True)
            db.execute('UPDATE instructor_mfa SET secret=pending,pending=NULL,pending_expires=NULL WHERE user_id=?', (principal.user_id,))
            codes = ['RC-'+secrets.token_hex(16).upper() for _ in range(8)]
            db.execute('DELETE FROM instructor_recovery_codes WHERE user_id=?', (principal.user_id,))
            db.executemany('INSERT INTO instructor_recovery_codes VALUES(?,?)', [(principal.user_id, _digest(principal.user_id+':'+code)) for code in codes])
            db.execute('UPDATE instructors SET revision=revision+1 WHERE user_id=?', (principal.user_id,))
            db.execute('DELETE FROM instructor_browser_sessions WHERE user_id=?', (principal.user_id,))
            row = db.execute('SELECT * FROM instructors WHERE user_id=?', (principal.user_id,)).fetchone()
            replacement = self._new_session(db, self.identities._principal(db, row), 'full')
            self.identities._audit(db, 'mfa_enrolled', principal.user_id, actor=principal)
            return replacement, codes

    def reauthenticate(self, token, password, factor=''):
        session = self.session(token)
        principal = session['principal']
        verified = self.verify_credentials(principal.username, password)
        if verified.session_binding != principal.session_binding:
            raise self._denied()
        if self.mfa_status(principal)['enabled']:
            self._reserve_factor(principal.user_id)
            with self.identities._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self._session(db, token)
                self._consume_factor(db, principal.user_id, factor)
        return principal

    def revoke(self, username, *, actor=None, reset_mfa=False):
        with self.identities._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.identities._assert_actor(db, actor)
            row = self.identities._user(db, username)
            db.execute('UPDATE instructors SET revision=revision+1 WHERE user_id=?', (row['user_id'],))
            db.execute('DELETE FROM instructor_browser_sessions WHERE user_id=?', (row['user_id'],))
            if reset_mfa:
                db.execute('DELETE FROM instructor_mfa WHERE user_id=?', (row['user_id'],))
                db.execute('DELETE FROM instructor_recovery_codes WHERE user_id=?', (row['user_id'],))
            self.identities._audit(db, 'mfa_reset' if reset_mfa else 'sessions_revoked', row['user_id'], actor=actor)

    def audit(self, limit=100):
        with self.identities._connect(read_only=True) as db:
            return [dict(row) for row in db.execute('SELECT action,user_id,course_key,actor,occurred_at FROM instructor_identity_audit ORDER BY sequence DESC LIMIT ?', (min(100, max(1, limit)),))]
