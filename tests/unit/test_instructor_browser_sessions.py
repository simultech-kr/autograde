import base64
import concurrent.futures
import sqlite3

import pytest

from autograde.instructor_identity import InstructorIdentityStore, ScopedCourses
from autograde.instructor_sessions import InstructorBrowserAuth, totp, COOKIE
from autograde.platform_service import PlatformAPIError
from autograde.platform_state import PlatformAccessDenied


PASSWORD = 'synthetic-browser-password-2026'


@pytest.fixture
def browser(tmp_path):
    identities = InstructorIdentityStore(tmp_path / 'instructors.sqlite3', browser_auth=True)
    identities.initialize()
    identities.create('owner', '관리자', 'admin', PASSWORD)
    identities.create('teacher', '교수자', 'instructor', PASSWORD)
    identities.create('reader', '조교', 'assistant', PASSWORD)
    identities.check_ready()
    auth = identities.browser
    auth.now = [1800000000.0]
    auth.clock = lambda: auth.now[0]
    return identities, auth


def enroll(auth, username='owner'):
    token, level = auth.login(username, PASSWORD)
    assert level == 'enroll'
    seed = auth.begin_enrollment(token)
    replacement, codes = auth.finish_enrollment(token, totp(seed, int(auth.clock())//30))
    return replacement, seed, codes


def test_totp_rfc6238_sha1_vector():
    seed = base64.b32encode(b'12345678901234567890').decode()
    assert totp(seed, 59//30) == '287082'
    assert totp(seed, 1111111109//30) == '081804'


def test_admin_password_alone_only_allows_enrollment(browser):
    identities, auth = browser
    token, level = auth.login('owner', PASSWORD)
    assert level == 'enroll'
    assert auth.session(token, allow_enrollment=True)['principal'].role == 'admin'
    with pytest.raises(PlatformAPIError) as error:
        identities.authenticate('InstructorSession '+token)
    assert error.value.code == 'instructor_mfa_enrollment_required'
    basic = 'Basic '+base64.b64encode(('owner:'+PASSWORD).encode()).decode()
    with pytest.raises(PlatformAPIError) as error:
        identities.authenticate(basic)
    assert error.value.status == 401 and 'WWW-Authenticate' not in error.value.headers


def test_enrollment_rotates_session_encrypts_seed_hashes_recovery(browser):
    identities, auth = browser
    old, _ = auth.login('owner', PASSWORD)
    seed = auth.begin_enrollment(old)
    assert auth.begin_enrollment(old) == seed
    token, codes = auth.finish_enrollment(old, totp(seed, int(auth.clock())//30))
    assert auth.session(token)['principal'].role == 'admin'
    with pytest.raises(PlatformAPIError): auth.session(old, allow_enrollment=True)
    text = identities.path.read_bytes()
    assert seed.encode() not in text and token.encode() not in text
    assert not any(code.encode() in text for code in codes)
    assert auth.mfa_status(auth.session(token)['principal']) == {'enabled': True, 'recovery_remaining': 8}
    assert auth.key_path.stat().st_mode & 0o077 == 0
    auth.check_ready()


def test_totp_is_single_use_across_new_store_instances(browser):
    identities, auth = browser
    _, seed, _ = enroll(auth)
    auth.now[0] += 30
    code = totp(seed, int(auth.clock())//30)
    token, _ = auth.login('owner', PASSWORD, code)
    assert auth.session(token)['level'] == 'full'
    other = InstructorIdentityStore(identities.path, browser_auth=True)
    other.check_ready()
    other.browser.clock = auth.clock
    with pytest.raises(PlatformAPIError) as error:
        other.browser.login('owner', PASSWORD, code)
    assert error.value.status == 401


def test_recovery_codes_single_use_even_concurrently(browser):
    _, auth = browser
    _, _, codes = enroll(auth)
    def attempt(_):
        try:
            return auth.login('owner', PASSWORD, codes[0])[1]
        except PlatformAPIError:
            return 'denied'
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['denied', 'full']


def test_mfa_attempt_limit_survives_new_store_and_correct_password(browser):
    identities, auth = browser
    enroll(auth)
    for _ in range(5):
        with pytest.raises(PlatformAPIError) as error: auth.login('owner', PASSWORD, 'invalid')
        assert error.value.status == 401
    other = InstructorIdentityStore(identities.path, browser_auth=True)
    other.check_ready()
    other.browser.clock = auth.clock
    with pytest.raises(PlatformAPIError) as error: other.browser.login('owner', PASSWORD, 'invalid')
    assert error.value.status == 429


def test_logout_expiry_and_course_revocation(browser):
    identities, auth = browser
    identities.set_grant('teacher', 'come2201', allowed=True)
    token, _ = auth.login('teacher', PASSWORD)
    assert identities.authorize_course('InstructorSession '+token, 'come2201').username == 'teacher'
    auth.logout(token)
    with pytest.raises(PlatformAPIError): auth.session(token)
    token, _ = auth.login('teacher', PASSWORD)
    auth.now[0] += 1801
    with pytest.raises(PlatformAPIError): auth.session(token)
    token, _ = auth.login('teacher', PASSWORD)
    identities.set_grant('teacher', 'come2201', allowed=False)
    with pytest.raises(PlatformAPIError): auth.session(token)


def test_absolute_expiry_even_if_regularly_used(browser):
    _, auth = browser
    token, _ = auth.login('teacher', PASSWORD)
    for _ in range(47):
        auth.now[0] += 600
        auth.session(token)
    auth.now[0] += 600
    with pytest.raises(PlatformAPIError): auth.session(token)


def test_password_and_admin_reset_revoke_sessions(browser):
    identities, auth = browser
    a, _ = auth.login('teacher', PASSWORD)
    b, _ = auth.login('teacher', PASSWORD)
    principal = auth.reauthenticate(a, PASSWORD)
    identities.set_password('teacher', PASSWORD+'-new', actor=principal)
    for token in (a,b):
        with pytest.raises(PlatformAPIError): auth.session(token)
    owner, _, _ = enroll(auth)
    actor = auth.session(owner)['principal']
    auth.revoke('owner', actor=actor, reset_mfa=True)
    with pytest.raises(PlatformAPIError): auth.session(owner)
    assert auth.login('owner', PASSWORD)[1] == 'enroll'


def test_missing_key_is_not_recreated(browser):
    identities, auth = browser
    enroll(auth)
    auth.key_path.unlink()
    other = InstructorIdentityStore(identities.path, browser_auth=True)
    with pytest.raises(ValueError): other.check_ready()
    assert not auth.key_path.exists()


def test_changed_key_does_not_silently_decrypt(browser):
    identities, auth = browser
    enroll(auth)
    auth.key_path.write_bytes(b'X'*32)
    other = InstructorIdentityStore(identities.path, browser_auth=True)
    with pytest.raises(PlatformAPIError) as error: other.check_ready()
    assert error.value.code == 'instructor_mfa_unavailable'


def test_read_only_roles_and_local_cli_compatibility(browser):
    identities, auth = browser
    identities.set_grant('reader', 'come2201', allowed=True)
    token, _ = auth.login('reader', PASSWORD)
    principal = auth.session(token)['principal']
    assert principal.read_only and principal.allows('come2201')
    assert next(a for a in identities.list_accounts() if a['username']=='reader')['role'] == 'assistant'
    with pytest.raises(PlatformAccessDenied):
        identities.create('bad', 'Bad', 'admin', PASSWORD, actor=principal)
    with pytest.raises(PlatformAccessDenied):
        identities.set_grant('reader', 'come3105', allowed=True, actor=principal)
    class Courses:
        def get_course(self, key): return {'course_key':key}
    scoped = ScopedCourses(Courses(), principal)
    with pytest.raises(PlatformAccessDenied): scoped.update_course('come2201', name='forged')


def test_actor_audit_does_not_record_secrets(browser):
    identities, auth = browser
    token, seed, codes = enroll(auth)
    owner = auth.session(token)['principal']
    identities.create('newteacher', '새 교수자', 'instructor', PASSWORD, actor=owner)
    events = auth.audit()
    assert events[0]['actor'] == owner.user_id and events[0]['action'] == 'created'
    rendered = repr(events)
    assert token not in rendered and seed not in rendered and PASSWORD not in rendered
    assert not any(code in rendered for code in codes)


@pytest.mark.parametrize('token', ['', None, 'x'*44, 'invalid', ' '])
def test_malformed_or_unknown_tokens_fail_closed(browser, token):
    _, auth = browser
    with pytest.raises(PlatformAPIError) as error: auth.session(token)
    assert error.value.status == 401
