"""Course-password proof for a short portal session, without creating a member.

Never copy an imported password into student_accounts: roster replacement must
continue to revoke access. Only courses whose current password was proved are
available, and every request rechecks that proof and the course revision.
"""
import hmac

from .platform_auth import verify_student_password
from .platform_state import PlatformAccessDenied, PlatformNotFound, utc_iso
from .student_registration import RegistrationError, _SLOTS

MAX_LOGIN_COURSES = 20


def _can_use_course_password(db, key):
    row = db.execute('SELECT active,student_id,verified FROM student_accounts WHERE student_key=?', (key,)).fetchone()
    # A pending, unbound signup cannot reserve another person's course identity.
    # A linked, verified, or disabled member can never be bypassed this way.
    return row is None or (row['active'] and row['student_id'] is None and not row['verified'])


def account(credentials, revisions):
    first = credentials[0]
    return {'student_key': first.student_key, 'student_id': first.student_id,
            'password_hash': first.password_hash, 'active': 1, 'verified': True,
            '_course_credentials': tuple(credentials), '_course_revisions': tuple(revisions)}


def validate_in_connection(db, identity):
    key = identity['student_key']
    if not _can_use_course_password(db, key):
        raise PlatformAccessDenied('학생 인증 상태가 변경되었습니다.')
    revisions = dict(identity['_course_revisions'])
    for credential in identity['_course_credentials']:
        row = db.execute('''SELECT pw.*,s.id AS student_id,s.student_key,e.course_key,c.auth_revision
            FROM platform_student_passwords pw
            JOIN platform_enrollments e ON e.id=pw.enrollment_id
            JOIN platform_students s ON s.id=e.student_id
            JOIN admin_courses c ON c.course_key=e.course_key
            WHERE pw.enrollment_id=? AND s.active=1 AND e.active=1 AND c.status='active' ''',
            (credential.enrollment_id,)).fetchone()
        if (not row or row['student_id'] != identity['student_id'] or row['student_key'] != key
                or row['course_key'] != credential.course_key
                or row['auth_revision'] != revisions.get(credential.course_key)
                or not hmac.compare_digest(row['password_hash'], credential.password_hash)
                or row['locked_until'] and row['locked_until'] > utc_iso()):
            raise PlatformAccessDenied('수업 인증 상태가 변경되었습니다. 다시 로그인하세요.')


def validate(store, identity, allowed_courses):
    if not identity['_course_credentials'] or any(c.course_key not in allowed_courses for c in identity['_course_credentials']):
        raise PlatformAccessDenied('수업 인증 상태가 변경되었습니다.')
    with store.state._connection() as db:
        validate_in_connection(db, identity)
    return identity


def authenticate(store, key, password, allowed_courses):
    # Use the existing global/per-student limits for both kinds of login. Never
    # try a course password after a rate limit or a valid member login.
    try:
        return store.authenticate(key, password)
    except RegistrationError as exc:
        if exc.status != 403:
            raise
    key = key.strip()  # authenticate() already validated the identifier.
    denied = RegistrationError('학번과 비밀번호를 확인하세요.', 403)
    with store.state._connection() as db:
        if not _can_use_course_password(db, key):
            raise denied
        rows = db.execute('''SELECT e.course_key,c.auth_revision FROM platform_enrollments e
            JOIN platform_students s ON s.id=e.student_id
            JOIN platform_student_passwords pw ON pw.enrollment_id=e.id
            JOIN admin_courses c ON c.course_key=e.course_key
            WHERE s.student_key=? AND s.active=1 AND e.active=1 AND c.status='active'
            ORDER BY e.course_key''', (key,)).fetchall()
    rows = [row for row in rows if row['course_key'] in allowed_courses]
    if not rows or len(rows) > MAX_LOGIN_COURSES:
        raise denied
    credentials = []
    if not _SLOTS.acquire(timeout=2):
        raise RegistrationError('서버가 잠시 바쁩니다. 다시 시도하세요.', 503)
    try:
        for row in rows:
            try:
                credential = store.state.get_student_password_credential(student_key=key, course_key=row['course_key'])
            except PlatformNotFound:
                continue
            if credential.locked_until and credential.locked_until > utc_iso():
                continue
            if verify_student_password(password, credential.password_hash):
                credentials.append(credential)
    finally:
        _SLOTS.release()
    if not credentials:
        raise denied
    proven = {c.course_key for c in credentials}
    identity = account(credentials, [(row['course_key'], row['auth_revision']) for row in rows if row['course_key'] in proven])
    # Recheck after hashing, inside the transaction that clears failed attempts.
    with store.state._write() as db:
        validate_in_connection(db, identity)
        db.execute("DELETE FROM student_account_attempts WHERE scope='login' AND identity=?", (key,))
    return identity


def summary(store, identity):
    with store.state._connection() as db:
        validate_in_connection(db, identity)
        return [dict(db.execute('SELECT * FROM admin_courses WHERE course_key=?', (c.course_key,)).fetchone())
                | {'enrolled': True, 'eligible': True, 'request_status': 'matched'}
                for c in identity['_course_credentials']]
