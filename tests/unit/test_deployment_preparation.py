"""Deployment guards tested without installing or invoking a container daemon."""
import json
import threading
from types import SimpleNamespace

import pytest

from autograde import pilot_portal_cli as portal
from autograde.platform_bundle_worker import BundleSubmissionWorker
from autograde.platform_grader import PILOT_LOCAL_RUNNER
from autograde.platform_readiness import PortalReadiness
from autograde.platform_runner_image import RunnerImageAvailabilityError
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths
from test_bundle_platform_state import BundleFixture, COURSE, RUNNER_IMAGE
from test_platform_http import running_server, request


def profile(**overrides):
    return {"external_access_mode": "disabled", "grading_runtime": "docker",
            "public_base_url": "https://grade.example.edu:20000",
            "web_public_base_url": "https://grade.example.edu:20010", **overrides}


@pytest.mark.parametrize("overrides", [
    {"grading_runtime": "pilot-local"},
    {"public_base_url": "http://127.0.0.1:20000"},
    {"web_public_base_url": "http://127.0.0.1:20010"},
    {"external_access_mode": "insecure-http"},
    {"web_public_base_url": ""},
])
def test_isolated_rejects_unsafe_profile_before_initialization(overrides):
    with pytest.raises(ValueError):
        portal._validate_mode(profile(**overrides), isolated=True)


def test_isolated_requires_explicit_opt_in():
    with pytest.raises(ValueError):
        portal._validate_mode(profile(), isolated=False)
    portal._validate_mode(profile(), isolated=True)
    portal._validate_mode(profile(grading_runtime="podman"), isolated=True)
    portal._validate_mode(profile(grading_runtime="pilot-local"), isolated=False)


def test_bad_mode_does_not_create_state(tmp_path):
    paths = tmp_path / "must-not-exist"
    config = SimpleNamespace(values={**profile(grading_runtime="pilot-local"),
                                    "course_key": "come3105", "data_root": str(paths)})
    with pytest.raises(ValueError):
        portal.run(config, isolated=True)
    assert not paths.exists()


def test_failed_runtime_reconciliation_closes_grader_before_binding(tmp_path, monkeypatch):
    events = []
    class FailedGrader:
        def reconcile_orphans(self):
            events.append("reconcile")
            raise RuntimeError("runtime failed")
        def close(self):
            events.append("close")
    config = SimpleNamespace(source=tmp_path / "config.csv", values={**profile(),
        "course_key": "come3105", "data_root": str(tmp_path / "data"),
        "web_port": 18081, "port": 18080, "listen": "127.0.0.1", "bundle_worker_count": 4})
    monkeypatch.setattr(portal, "_course_grader", lambda *args, **kwargs: FailedGrader())
    monkeypatch.setattr(portal, "create_server", lambda *args, **kwargs: pytest.fail("bound before runtime check"))
    with pytest.raises(RuntimeError, match="runtime failed"):
        portal.run(config, isolated=True)
    assert events == ["reconcile", "close"]


def test_runner_query_includes_hidden_pending_receipts(tmp_path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    fixture = BundleFixture(state)
    fixture.assignment()
    assert state.bundle_runner_images_in_use(course_key=COURSE) == (RUNNER_IMAGE,)
    fixture.submit()
    state.set_bundle_assignment_availability("bundle_asn_01", course_key=COURSE, ready=False)
    assert state.bundle_runner_images_in_use(course_key=COURSE) == (RUNNER_IMAGE,)
    assert state.bundle_runner_images_in_use(course_key="another-course") == ()


def test_isolated_never_falls_back_to_local(tmp_path, monkeypatch):
    state = SimpleNamespace(bundle_runner_images_in_use=lambda **_: (PILOT_LOCAL_RUNNER,))
    monkeypatch.setattr(portal, "PilotLocalGrader", lambda: pytest.fail("unsafe fallback"))
    with pytest.raises(RunnerImageAvailabilityError):
        portal._course_grader(profile(), AppPaths(tmp_path), state, COURSE, isolated=True)


def test_pilot_rejects_container_contract(tmp_path):
    state = SimpleNamespace(bundle_runner_images_in_use=lambda **_: (RUNNER_IMAGE,))
    with pytest.raises(ValueError):
        portal._course_grader(profile(), AppPaths(tmp_path), state, COURSE, isolated=False)


def test_isolated_checks_images_and_uses_stable_course_label(tmp_path, monkeypatch):
    state = SimpleNamespace(bundle_runner_images_in_use=lambda **_: (RUNNER_IMAGE,))
    observations = []
    class Checker:
        def __init__(self, **options):
            observations.append(options)
        def check_many(self, references):
            observations.append(references)
    monkeypatch.setattr(portal, "RunnerImageAvailabilityChecker", Checker)
    monkeypatch.setattr(portal, "ContainerGrader", lambda **options: options)
    options = portal._course_grader(profile(), AppPaths(tmp_path), state, COURSE, isolated=True)
    assert options["runtime"] == "docker"
    assert options["instance_label"] == portal._grader_instance_label(AppPaths(tmp_path), COURSE)
    assert observations == [{"runtime": "docker"}, (RUNNER_IMAGE,)]


@pytest.fixture
def readiness(tmp_path):
    paths = AppPaths(tmp_path / "data").ensure()
    PlatformStateStore(paths.database)
    return PortalReadiness(paths, [SimpleNamespace(healthy=True), SimpleNamespace(healthy=True)],
                           threading.Event(), min_free_bytes=0)


def test_readiness_detects_missing_db_without_recreating_it(readiness):
    assert readiness()
    readiness.paths.database.unlink()
    assert not readiness()
    assert not readiness.paths.database.exists()


def test_readiness_detects_corrupt_db_without_leaking_details(readiness):
    readiness.paths.database.write_bytes(b"not a sqlite database")
    assert not readiness()


def test_readiness_detects_storage_and_pool_failures(readiness, monkeypatch):
    readiness.workers[1].healthy = False
    assert not readiness()
    readiness.workers[1].healthy = True
    readiness.stopping.set()
    assert not readiness()
    readiness.stopping.clear()
    readiness.min_free_bytes = 1
    monkeypatch.setattr("autograde.platform_readiness.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    assert not readiness()


def test_readiness_rejects_missing_storage(readiness):
    readiness.paths.workspaces.rmdir()
    assert not readiness()
    assert not readiness.paths.workspaces.exists()


def test_readiness_requires_real_schema(tmp_path):
    paths = AppPaths(tmp_path / "data").ensure()
    paths.database.touch()
    check = PortalReadiness(paths, [SimpleNamespace(healthy=True)], threading.Event(), min_free_bytes=0)
    assert not check()


def test_enabled_web_stores_fail_readiness_without_stopping_core(readiness, tmp_path):
    from autograde.instructor_identity import InstructorIdentityStore
    from autograde.rubric_store import RubricStore
    identities = InstructorIdentityStore(tmp_path / 'identity.sqlite3')
    identities.initialize()
    identities.create('owner', 'Owner', 'admin', 'synthetic-readiness-password')
    rubrics = RubricStore(tmp_path / 'rubric.sqlite3')
    rubrics.initialize()
    web = PortalReadiness(readiness.paths, readiness.workers, readiness.stopping, min_free_bytes=0,
                          module_checks={'instructor_identity': identities.check_ready,
                                         'rubric_catalog': rubrics.check_available})
    assert web.status() == {'core': True, 'instructor_identity': True, 'rubric_catalog': True}
    assert web() and readiness()
    identity_path, rubric_path = identities.path, rubrics.path
    identities.path = tmp_path / 'missing-identity.sqlite3'
    assert not web() and readiness()
    assert web.status()['instructor_identity'] is False
    assert not identities.path.exists()
    identities.path = identity_path
    rubrics.path = tmp_path / 'missing-rubric.sqlite3'
    assert not web() and readiness()
    assert web.status()['rubric_catalog'] is False
    assert not rubrics.path.exists()
    rubrics.path = rubric_path
    assert web()
    with running_server(object(), readiness_check=web) as server:
        assert request(server, 'GET', '/readyz')[0] == 200
        rubrics.path = tmp_path / 'missing-rubric.sqlite3'
        assert request(server, 'GET', '/readyz')[0] == 503
        assert request(server, 'GET', '/healthz')[0] == 200


@pytest.mark.parametrize('outcome,expected', [(None, True), (True, True), (False, False), (1, False), ('yes', False)])
def test_module_probe_contract_is_explicit(readiness, outcome, expected):
    readiness.module_checks = {'synthetic': lambda: outcome}
    assert readiness.status()['synthetic'] is expected
    assert readiness() is expected


@pytest.mark.parametrize("web", [False, True])
def test_http_separates_liveness_and_readiness(readiness, web):
    facade = SimpleNamespace(portal_request=lambda *args: {"page": True}) if web else object()
    with running_server(facade, readiness_check=readiness) as server:
        assert request(server, "GET", "/readyz")[0] == 200
        readiness.workers[0].healthy = False
        status, headers, body = request(server, "GET", "/readyz")
        assert status == 503
        assert json.loads(body) == {"status": "not_ready"}
        assert "no-store" in headers["cache-control"]
        assert request(server, "GET", "/healthz")[0] == 200


@pytest.mark.parametrize("check", [None, lambda: "yes", lambda: 1])
def test_readiness_fails_closed_when_not_configured_or_invalid(check):
    with running_server(object(), readiness_check=check) as server:
        assert request(server, "GET", "/readyz")[0] == 503


def test_readiness_exception_is_redacted():
    def broken():
        raise RuntimeError("secret password /private/database")
    with running_server(object(), readiness_check=broken) as server:
        status, _, body = request(server, "GET", "/readyz")
        assert status == 503
        assert b"secret" not in body and b"private" not in body


def test_pool_health_covers_start_and_stop():
    processor = SimpleNamespace(course_key=COURSE, state=SimpleNamespace(
        list_bundle_submissions_for_processing=lambda **_: [],
        list_publishable_bundle_submissions=lambda **_: []), _aware_now=lambda: None)
    worker = BundleSubmissionWorker(processor, course_key=COURSE, worker_count=2,
                                   recovery_interval_seconds=0.05)
    assert not worker.healthy
    try:
        worker.start()
        assert worker.healthy
    finally:
        worker.stop(timeout=2)
    assert not worker.healthy
