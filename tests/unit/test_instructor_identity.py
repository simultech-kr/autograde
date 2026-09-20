import base64
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from autograde.instructor_identity import InstructorIdentityStore, hash_password, verify_password
from autograde.instructor_identity_cli import main
from autograde.platform_service import PlatformAPIError
from autograde.pilot_config import load_pilot_config, PilotConfigError, apply_pilot_config
from types import SimpleNamespace


PASSWORD = 'only-for-synthetic-tests-2026'


def basic(username, password=PASSWORD):
    return 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()


@pytest.fixture
def identities(tmp_path):
    store = InstructorIdentityStore(tmp_path / 'instructors.sqlite3')
    store.initialize()
    store.create('owner', '운영 관리자', 'admin', PASSWORD)
    store.create('teacher', '담당 교수자', 'instructor', PASSWORD)
    return store


def test_password_hashes_salted_bounded_and_separate_from_student_policy():
    a, b = hash_password(PASSWORD), hash_password(PASSWORD)
    assert a != b and PASSWORD not in a
    assert verify_password(PASSWORD, a)
    assert not verify_password('wrong-password', a)
    assert not verify_password(PASSWORD, 'broken')
    for invalid in ['123456', 'x'*129, 'x'*13+'\n']:
        with pytest.raises(ValueError): hash_password(invalid)


def test_initialize_readiness_permissions_and_no_overwrite(tmp_path):
    store = InstructorIdentityStore(tmp_path / 'new.sqlite3')
    with pytest.raises(sqlite3.Error): store.check_ready()
    assert not store.path.exists()
    store.initialize()
    assert store.path.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError): store.check_ready()
    before = store.path.read_bytes()
    with pytest.raises(ValueError): store.initialize()
    assert store.path.read_bytes() == before


def test_identity_grants_revision_and_admin_scope(identities):
    identities.check_ready()
    teacher = identities.authenticate(basic('teacher'))
    assert not teacher.allows('come2201')
    identities.set_grant('teacher', 'come2201', allowed=True)
    assigned = identities.authorize_course(basic('teacher'), 'come2201')
    assert assigned.revision > teacher.revision and assigned.user_id == teacher.user_id
    assert assigned.courses == frozenset({'come2201'})
    with pytest.raises(PlatformAPIError) as error: identities.authorize_course(basic('teacher'), 'come3105')
    assert error.value.status == 403
    assert identities.authorize_course(basic('owner'), 'come3105').role == 'admin'
    identities.set_grant('teacher', 'come2201', allowed=False)
    assert not identities.authenticate(basic('teacher')).allows('come2201')
    public = json.dumps(identities.list_accounts())
    assert 'password' not in public and 'scrypt' not in public and PASSWORD not in public


def test_password_reset_and_disable_revoke_old_authority(identities):
    before = identities.authenticate(basic('teacher'))
    identities.set_password('teacher', PASSWORD + '-new')
    with pytest.raises(PlatformAPIError) as error: identities.authenticate(basic('teacher'))
    assert error.value.status == 401
    after = identities.authenticate(basic('teacher', PASSWORD + '-new'))
    assert after.session_binding != before.session_binding
    identities.set_active('teacher', False)
    with pytest.raises(PlatformAPIError): identities.authenticate(basic('teacher', PASSWORD + '-new'))
    identities.set_active('teacher', True)
    assert identities.authenticate(basic('teacher', PASSWORD + '-new')).revision > after.revision
    with pytest.raises(ValueError): identities.set_active('owner', False)


@pytest.mark.parametrize('auth', [None, '', 'Bearer student', 'Basic !!!', basic('unknown'), basic('teacher', 'wrong-password')])
def test_generic_auth_failure(identities, auth):
    with pytest.raises(PlatformAPIError) as error: identities.authenticate(auth)
    assert error.value.status == 401 and error.value.code == 'instructor_auth_required'


def test_failure_limits_and_concurrency_capacity(identities):
    for _ in range(5):
        with pytest.raises(PlatformAPIError) as error: identities.authenticate(basic('unknown'))
        assert error.value.status == 401
    with pytest.raises(PlatformAPIError) as error: identities.authenticate(basic('another'))
    assert error.value.status == 429
    assert identities.authenticate(basic('owner')).role == 'admin'
    assert identities._slots.acquire(False) and identities._slots.acquire(False)
    try:
        assert identities.authenticate(basic('owner')).role == 'admin'
        with pytest.raises(PlatformAPIError) as error: identities.authenticate(basic('teacher'))
        assert error.value.status == 429
    finally:
        identities._slots.release(); identities._slots.release()


def test_three_valid_cold_authentications_wait_instead_of_rejecting(identities):
    identities.create('third', 'Third', 'instructor', PASSWORD)
    barrier = threading.Barrier(3)
    def authenticate(username):
        barrier.wait(timeout=5)
        return identities.authenticate(basic(username)).username
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert set(pool.map(authenticate, ['owner', 'teacher', 'third'])) == {'owner', 'teacher', 'third'}


def test_hash_reuse_has_absolute_expiry_and_does_not_store_credentials(identities, monkeypatch):
    from autograde import instructor_identity
    clock = [100.0]
    identities._clock = lambda: clock[0]
    original = instructor_identity.verify_password
    calls = []
    def verify(password, encoded):
        calls.append(True)
        return original(password, encoded)
    monkeypatch.setattr(instructor_identity, 'verify_password', verify)
    identities.authenticate(basic('teacher'))
    for second in (110, 130, 159):
        clock[0] = second
        identities.authenticate(basic('teacher'))
    assert len(calls) == 1
    assert PASSWORD not in repr(identities._verified)
    assert basic('teacher') not in repr(identities._verified)
    clock[0] = 160
    identities.authenticate(basic('teacher'))
    assert len(calls) == 2


def test_warm_auth_survives_wrong_password_attempts_but_not_store_failure(identities):
    identities.authenticate(basic('teacher'))
    for _ in range(5):
        with pytest.raises(PlatformAPIError) as error:
            identities.authenticate(basic('teacher', 'wrong-password'))
        assert error.value.status == 401
    assert identities.authenticate(basic('teacher')).username == 'teacher'
    with pytest.raises(PlatformAPIError) as error:
        identities.authenticate(basic('teacher', 'wrong-password'))
    assert error.value.status == 429
    identities.path = identities.path.parent / 'missing.sqlite3'
    with pytest.raises(PlatformAPIError) as error:
        identities.authenticate(basic('teacher'))
    assert error.value.status == 503
    assert not identities.path.exists()


def test_warm_auth_observes_revocation_from_separate_store(identities):
    identities.set_grant('teacher', 'come2201', allowed=True)
    identities.authorize_course(basic('teacher'), 'come2201')
    operator = InstructorIdentityStore(identities.path)
    operator.set_grant('teacher', 'come2201', allowed=False)
    with pytest.raises(PlatformAPIError) as error:
        identities.authorize_course(basic('teacher'), 'come2201')
    assert error.value.status == 403
    operator.set_active('teacher', False)
    with pytest.raises(PlatformAPIError) as error:
        identities.authenticate(basic('teacher'))
    assert error.value.status == 401


def test_revocation_during_password_verification_is_rejected(identities, monkeypatch):
    from autograde import instructor_identity
    original = instructor_identity.verify_password
    def revoke(password, encoded):
        identities.set_active('teacher', False)
        return original(password, encoded)
    monkeypatch.setattr(instructor_identity, 'verify_password', revoke)
    with pytest.raises(PlatformAPIError) as error: identities.authenticate(basic('teacher'))
    assert error.value.status == 401


def test_last_admin_fence_is_transactional(identities):
    identities.create('second', 'Second admin', 'admin', PASSWORD)
    def disable(username):
        try:
            identities.set_active(username, False)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(disable, ['owner', 'second']))
    assert sorted(results) == [False, True]
    identities.check_ready()


def config(tmp_path, mode='personal', extra=''):
    source = tmp_path / 'pilot.csv'
    source.write_text('key,value\ncourse_key,come2201\ndata_root,state\n'
        'public_base_url,http://127.0.0.1:18080\nlisten,127.0.0.1\nport,18080\n'
        'web_public_base_url,http://127.0.0.1:18081\nweb_port,18081\n'
        f'instructor_auth_mode,{mode}\ninstructor_assignment_web_enabled,true\n' + extra)
    return source


def test_cli_account_setup_no_secrets_and_mode_validation(tmp_path, monkeypatch, capsys):
    source = config(tmp_path)
    prefix = ['--config', str(source)]
    assert main(prefix + ['init']) == 0
    monkeypatch.setattr('autograde.instructor_identity_cli.getpass.getpass', lambda _: PASSWORD)
    assert main(prefix + ['create', '--username', 'owner', '--name', 'Owner', '--role', 'admin']) == 0
    assert main(prefix + ['list']) == 0
    output = capsys.readouterr().out
    assert PASSWORD not in output and 'scrypt$' not in output
    assert 'owner' in output
    assert main(prefix + ['disable', '--username', 'owner']) == 2
    assert load_pilot_config(source).values['instructor_auth_mode'] == 'personal'
    with pytest.raises(PilotConfigError): apply_pilot_config(SimpleNamespace(command='serve'), load_pilot_config(source), argv=[])
    with pytest.raises(PilotConfigError): load_pilot_config(config(tmp_path, 'invalid'))


def test_store_failure_is_unavailable_not_shared_fallback(identities, monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('private filesystem details')
    monkeypatch.setattr(identities, '_connect', unavailable)
    with pytest.raises(PlatformAPIError) as error: identities.authenticate(basic('owner'))
    assert error.value.status == 503 and 'private filesystem' not in str(error.value)


def test_personal_mode_missing_database_refuses_startup(tmp_path):
    from autograde.pilot_portal_cli import run
    source = config(tmp_path)
    with pytest.raises(ValueError): run(load_pilot_config(source))
    assert not (tmp_path / 'state' / 'instructor-identities.sqlite3').exists()


@pytest.mark.parametrize('replacement', [
    ('instructor_assignment_web_enabled,true', 'instructor_assignment_web_enabled,false'),
    ('web_port,18081\n', ''),
])
def test_personal_mode_requires_portal(tmp_path, replacement):
    source = config(tmp_path)
    source.write_text(source.read_text().replace(*replacement))
    with pytest.raises(PilotConfigError): load_pilot_config(source)
