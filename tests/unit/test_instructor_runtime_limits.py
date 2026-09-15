"""Runtime failure recovery and explicit feature configuration."""
import threading

import pytest

from autograde import pilot_portal_cli
from autograde.pilot_config import load_pilot_config, PilotConfigError
from autograde.platform_bundle import BundleStore
from autograde.platform_cli import _exclusive_course_service_lock
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths
from autograde.platform_qr import course_login_qr_svg


def test_failed_course_service_creation_releases_lock_and_grader(tmp_path, monkeypatch):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    store = BundleStore(paths.bundles)
    original = pilot_portal_cli.StudentPlatformService
    closed = []

    class Grader:
        def close(self):
            closed.append(self)

    monkeypatch.setattr(pilot_portal_cli, '_course_grader', lambda *args, **kwargs: Grader())
    attempts = []

    def create_service(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise ValueError('synthetic one-time startup failure')
        return original(**kwargs)

    monkeypatch.setattr(pilot_portal_cli, 'StudentPlatformService', create_service)
    arguments = dict(isolated=False, secret=b'f' * 32, instructor='synthetic-token-for-test-only-1234',
                     bundles=store, notify=lambda _: True, password_slots=threading.BoundedSemaphore(4))
    values = {'public_base_url': 'https://example.edu:20000'}
    with pytest.raises(ValueError, match='synthetic'):
        pilot_portal_cli._create_course_runtime(values, paths, state, 'come2201', **arguments)
    assert len(closed) == 1
    with _exclusive_course_service_lock(paths, 'come2201'):
        pass
    service, processor, grader, resources = pilot_portal_cli._create_course_runtime(
        values, paths, state, 'come2201', **arguments)
    try:
        assert service.course_key == processor.course_key == 'come2201'
        with pytest.raises(Exception, match='active'):
            with _exclusive_course_service_lock(paths, 'come2201'):
                pass
    finally:
        resources.close()
    assert len(closed) == 2
    with _exclusive_course_service_lock(paths, 'come2201'):
        pass


@pytest.mark.parametrize('extra,expected', [
    ('', (False, 'csv')),
    ('instructor_assignment_web_enabled,true\n', (True, 'csv')),
    ('instructor_assignment_web_enabled,true\nroster_bootstrap_mode,web\n', (True, 'web')),
    ('instructor_assignment_web_enabled,yes\n', None),
    ('roster_bootstrap_mode,web\n', None),
    ('roster_bootstrap_mode,ignore-errors\n', None),
])
def test_explicit_configuration_modes(tmp_path, extra, expected):
    path = tmp_path / 'config.csv'
    path.write_text('key,value\ncourse_key,come2201\ndata_root,data\npublic_base_url,http://127.0.0.1:18080\n'
                    'listen,127.0.0.1\nport,18080\n' + extra)
    if expected is None:
        with pytest.raises(PilotConfigError):
            load_pilot_config(path)
    else:
        values = load_pilot_config(path).values
        assert (values['instructor_assignment_web_enabled'], values['roster_bootstrap_mode']) == expected


def test_course_qr_is_local_svg_and_refuses_credentials():
    assert '<svg' in course_login_qr_svg('https://example.edu:20010/courses/come2201/login')
    for address in ('https://user:password@example.edu/courses/come2201/login',
                    'https://example.edu/courses/come2201/login?password=123456',
                    'https://example.edu/arbitrary/path', 'javascript:alert(1)'):
        with pytest.raises(ValueError):
            course_login_qr_svg(address)
