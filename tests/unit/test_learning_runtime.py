from contextlib import contextmanager
from io import BytesIO
import base64
import os
from pathlib import Path
import stat
import tempfile
import threading

import pytest

from autograde.learning_runtime import rpc, cancel_course, runtime_identity
from autograde.learning_runtime_worker import (DockerEngine, LearningRuntimeWorker, RuntimeFault,
                                              WorkerServer, source_files)
from autograde.platform_service import PlatformAPIError
from autograde.platform_state import PlatformAccessDenied
from test_learning_checkpoints import checkpoints, accept, save, ASSIGNMENT, COURSE, NOW
from test_platform_service_bundle import bundle_platform


CP = "lcp_" + "a" * 32
OWNER = {"course_key": "come2201", "student_identity": "a" * 64, "assignment_id": "basn_lab01"}


def files(content=b"int main(void) { return 0; }", name="main.c"):
    return [{"path": name, "content": base64.b64encode(content).decode()}]


class Engine:
    def __init__(self):
        self.removed, self.calls, self.fail_cleanup = [], [], False

    def ready(self):
        pass

    def reconcile(self):
        pass

    def remove(self, name):
        if self.fail_cleanup:
            raise RuntimeFault(503, "learning_runtime_cleanup_failed", "cleanup failed")
        self.removed.append(name)

    def prepare(self, name, directory):
        pass

    def run(self, name, directory, request):
        self.calls.append((name, directory, request))
        return {"status": "completed", "compile": {"exit_code": 0, "stdout": "", "stderr": ""},
                "run": {"exit_code": 0, "stdout": "hello\n", "stderr": ""}}


@contextmanager
def running_worker(*, clock=None):
    with tempfile.TemporaryDirectory(prefix="ag-lr-", dir="/tmp") as root:
        engine = Engine()
        worker = LearningRuntimeWorker(Path(root) / "worker", engine=engine, **({"clock": clock} if clock else {}))
        socket_path = Path(root) / "worker.sock"
        server = WorkerServer(socket_path, worker)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield worker, engine, socket_path
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()
            socket_path.unlink(missing_ok=True)
            worker.close()


def call(path, operation, **values):
    return rpc(path, {"operation": operation, "owner": OWNER, **values})


def test_private_worker_identity_matches_persisted_instance():
    with running_worker() as (worker, _engine, path):
        assert runtime_identity(path) == worker.instance
        assert runtime_identity(path) == worker.instance
        with pytest.raises(RuntimeFault) as error:
            cancel_course(path, OWNER['course_key'], expected_instance='0' * 24)
        assert error.value.code == 'learning_runtime_cleanup_failed'
        assert OWNER['course_key'] not in worker.deleted_courses
        assert cancel_course(path, OWNER['course_key'], expected_instance=worker.instance)['cleanup_verified']


def test_worker_session_lifecycle_readonly_source_and_idempotent_close():
    with running_worker() as (worker, engine, path):
        created = call(path, "create", checkpoint_id=CP, files=files())["session"]
        sid = created["id"]
        assert call(path, "create", checkpoint_id=CP, files=files())["session"]["id"] == sid
        source = worker.root / sid / "source" / "main.c"
        assert source.read_bytes() == b"int main(void) { return 0; }"
        assert stat.S_IMODE(source.stat().st_mode) == 0o444
        result = call(path, "run", session_id=sid, checkpoint_id=CP, files=files(), language="c", sources=["main.c"], stdin="")
        assert result["run"]["stdout"] == "hello\n" and result["checkpoint_id"] == CP
        assert result["session"]["status"] == "ready"
        closed = call(path, "close", session_id=sid, checkpoint_id=CP)
        assert closed == {"status": "closed", "checkpoint_id": CP, "cleanup_verified": True}
        assert call(path, "close", session_id=sid, checkpoint_id=CP) == closed
        assert not (worker.root / sid).exists()
        assert call(path, "status")["session"] is None


def test_worker_denies_other_owner_and_invalid_sources():
    with running_worker() as (_worker, _engine, path):
        sid = call(path, "create", checkpoint_id=CP, files=files())["session"]["id"]
        with pytest.raises(RuntimeFault) as denied:
            rpc(path, {"operation": "close", "owner": {**OWNER, "student_identity": "b" * 64}, "session_id": sid, "checkpoint_id": CP})
        assert denied.value.status == 404
        for sources in (["/etc/passwd"], ["../main.c"], ["missing.c"], ["main.c", "main.c"], ["-specs=evil.c"]):
            with pytest.raises(RuntimeFault) as invalid:
                call(path, "run", session_id=sid, checkpoint_id=CP, files=files(), language="c", sources=sources, stdin="")
            assert invalid.value.status == 400


def test_python_uses_one_entry_with_owned_snapshot_modules():
    with running_worker() as (worker, engine, path):
        project = files(b"import helper\nprint(helper.answer)\n", "main.py") + files(b"answer=42\n", "helper.py")
        sid = call(path, "create", checkpoint_id=CP, files=project)["session"]["id"]
        result = call(path, "run", session_id=sid, checkpoint_id=CP, files=project,
                      language="python", sources=["main.py"], stdin="hello\n")
        assert result["status"] == "completed"
        assert engine.calls[-1][2] == {"language": "python", "sources": ["main.py"], "stdin": "hello\n"}
        assert (worker.root / sid / "source" / "helper.py").read_text() == "answer=42\n"
        for entries in (["main.py", "helper.py"], ["main.c"], ["../main.py"], ["absent.py"]):
            with pytest.raises(RuntimeFault) as error:
                call(path, "run", session_id=sid, checkpoint_id=CP, files=project, language="python", sources=entries)
            assert error.value.status == 400
        assert len(engine.calls) == 1
        assert call(path, "close", session_id=sid, checkpoint_id=CP)["cleanup_verified"]


def test_course_cancellation_cleans_only_course_and_persists_creation_fence(tmp_path):
    root = tmp_path / "worker"
    engine = Engine()
    worker = LearningRuntimeWorker(root, engine=engine)
    base = dict(operation="create", checkpoint_id=CP, files=files())
    sid = worker.dispatch({**base, "owner": OWNER})["session"]["id"]
    other = worker.dispatch({**base, "owner": {**OWNER, "course_key": "other"}})["session"]["id"]
    assert worker.dispatch({"operation": "cancel_course", "course_key": OWNER["course_key"]}) == {"cleanup_verified": True, "closed_count": 1}
    assert not (root / sid).exists() and (root / other).exists()
    assert sid not in worker.sessions
    assert worker.dispatch({"operation": "cancel_course", "course_key": OWNER["course_key"]})["closed_count"] == 0
    worker.close()
    restarted = LearningRuntimeWorker(root, engine=Engine())
    try:
        with pytest.raises(RuntimeFault) as error:
            restarted.dispatch({**base, "owner": OWNER})
        assert error.value.code == "learning_runtime_expired"
    finally:
        restarted.close()


def test_course_cleanup_failure_keeps_fence_and_can_retry():
    with running_worker() as (worker, engine, path):
        sid = call(path, "create", checkpoint_id=CP, files=files())["session"]["id"]
        worker.sessions[sid]["container"] = "ag-learning-" + worker.instance + "-" + "a" * 16
        engine.fail_cleanup = True
        with pytest.raises(RuntimeFault) as error:
            cancel_course(path, OWNER["course_key"])
        assert error.value.code == "learning_runtime_cleanup_failed"
        assert worker.sessions[sid]["status"] == "cleanup_failed"
        with pytest.raises(RuntimeFault):
            call(path, "create", checkpoint_id=CP, files=files())
        engine.fail_cleanup = False
        assert cancel_course(path, OWNER["course_key"]) == {"cleanup_verified": True, "closed_count": 1}


@pytest.mark.parametrize("operation", ["create", "run"])
def test_course_deletion_fences_requests_that_arrived_before_cancellation(tmp_path, monkeypatch, operation):
    import autograde.learning_runtime_worker as module
    engine = Engine()
    worker = LearningRuntimeWorker(tmp_path / "worker", engine=engine)
    sid = worker.dispatch({"operation": "create", "owner": OWNER, "checkpoint_id": CP, "files": files()})["session"]["id"]
    entered, proceed, errors = threading.Event(), threading.Event(), []
    original = module.source_files
    def delayed_files(value):
        entered.set()
        assert proceed.wait(4)
        return original(value)
    monkeypatch.setattr(module, "source_files", delayed_files)
    def request():
        try:
            worker.dispatch({"operation": operation, "owner": OWNER, "session_id": sid, "checkpoint_id": CP,
                             "files": files(), "language": "c", "sources": ["main.c"]})
        except RuntimeFault as exc:
            errors.append(exc)
    thread = threading.Thread(target=request)
    thread.start()
    try:
        assert entered.wait(3)
        assert worker.dispatch({"operation": "cancel_course", "course_key": OWNER["course_key"]})["cleanup_verified"]
        proceed.set()
        thread.join(5)
        assert not thread.is_alive() and errors[0].code == "learning_runtime_expired"
        assert not engine.calls and not (worker.root / sid).exists()
    finally:
        proceed.set()
        thread.join(5)
        worker.close()


@pytest.mark.parametrize("name", ["../main.c", "/main.c", "a//main.c", "a/./main.c", "a\\main.c", "a\x00.c"])
def test_worker_rejects_traversal_and_noncanonical_file_paths(name):
    with pytest.raises(RuntimeFault):
        source_files(files(name=name))


def test_worker_rejects_file_directory_collision_and_oversized_data():
    with pytest.raises(RuntimeFault):
        source_files(files(name="a") + files(name="a/main.c"))
    with pytest.raises(RuntimeFault):
        source_files(files(name="Main.c") + files(name="main.c"))
    with pytest.raises(RuntimeFault) as large:
        source_files(files(b"x" * (2 * 1024 * 1024 + 1)))
    assert large.value.status == 413


def test_lease_expiry_removes_space_and_restart_reconciles(tmp_path):
    now = [0.0]
    engine = Engine()
    worker = LearningRuntimeWorker(tmp_path / "runtime", engine=engine, clock=lambda: now[0], lease_seconds=10)
    result = worker.dispatch({"operation": "create", "owner": OWNER, "checkpoint_id": CP, "files": files()})
    sid = result["session"]["id"]
    now[0] = 11
    worker.reap()
    assert worker.sessions[sid]["status"] == "expired" and not (worker.root / sid).exists()
    sid2 = worker.dispatch({"operation": "create", "owner": OWNER, "checkpoint_id": CP, "files": files()})["session"]["id"]
    worker.close()
    restarted = LearningRuntimeWorker(tmp_path / "runtime", engine=engine)
    assert restarted.sessions[sid2]["status"] == "expired" and not (restarted.root / sid2).exists()
    restarted.close()


def test_root_exclusive_lock_does_not_reconcile_live_worker(tmp_path):
    worker = LearningRuntimeWorker(tmp_path / "runtime", engine=Engine())
    try:
        with pytest.raises(ValueError, match="already owns"):
            LearningRuntimeWorker(tmp_path / "runtime", engine=Engine())
    finally:
        worker.close()


def test_failed_cleanup_is_not_success_and_can_retry():
    with running_worker() as (worker, engine, path):
        sid = call(path, "create", checkpoint_id=CP, files=files())["session"]["id"]
        worker.sessions[sid]["container"] = "ag-learning-" + worker.instance + "-" + "a" * 16
        engine.fail_cleanup = True
        with pytest.raises(RuntimeFault) as failed:
            call(path, "close", session_id=sid, checkpoint_id=CP)
        assert failed.value.code == "learning_runtime_cleanup_failed"
        assert call(path, "status")["session"]["status"] == "cleanup_failed"
        engine.fail_cleanup = False
        assert call(path, "close", session_id=sid, checkpoint_id=CP)["cleanup_verified"]


def test_socket_permissions_default_and_operator_group():
    with tempfile.TemporaryDirectory(prefix="ag-lr-", dir="/tmp") as root:
        worker = LearningRuntimeWorker(Path(root) / "worker", engine=Engine())
        for name, group, mode in (("private.sock", None, 0o600), ("group.sock", os.getgid(), 0o660)):
            path = Path(root) / name
            server = WorkerServer(path, worker, socket_group=group)
            assert stat.S_IMODE(path.stat().st_mode) == mode
            if group is not None:
                assert path.stat().st_gid == group
            server.server_close()
            path.unlink()
        worker.close()


def test_docker_uses_id_filter_when_verifying_orphan_removal(monkeypatch):
    engine = DockerEngine("sha256:" + "a" * 64, "b" * 24)
    commands = []
    class Reply:
        returncode = 0
        stdout = b""
    monkeypatch.setattr(engine, "command", lambda args, **kw: commands.append(args) or Reply())
    engine.remove("a" * 12)
    assert commands[-1][-1] == "id=" + "a" * 12


def test_disabled_runtime_still_requires_assignment_authorization(checkpoints):
    _state, _store, service, token, _notifications, _payload = checkpoints
    assert service.get_learning_runtime(token, ASSIGNMENT)["enabled"] is False
    with pytest.raises(PlatformAPIError) as wrong:
        service.get_learning_runtime(token, "another-assignment")
    assert wrong.value.status == 403
    with pytest.raises(PlatformAPIError) as disabled:
        service.create_learning_runtime(token, ASSIGNMENT, {"checkpoint_id": CP})
    assert disabled.value.code == "learning_runtime_disabled"


def test_service_uses_only_durable_owned_checkpoints_and_keeps_grades_unchanged(checkpoints):
    state, store, service, token, notifications, payload = checkpoints
    checkpoint = save(service, token, payload)["checkpoint"]["checkpoint_id"]
    before = state.list_bundle_dashboard_rows(course_key=COURSE)
    with running_worker() as (worker, _engine, path):
        service.learning_runtime_socket = str(path)
        created = service.create_learning_runtime(token, ASSIGNMENT, {"checkpoint_id": checkpoint})["session"]
        assert (worker.root / created["id"] / "source" / "main.py").read_text() == "print(42)\n"
        with pytest.raises(PlatformAPIError) as missing:
            service.close_learning_runtime(token, ASSIGNMENT, created["id"], {"checkpoint_id": CP})
        assert missing.value.status == 404
        assert service.close_learning_runtime(token, ASSIGNMENT, created["id"], {"checkpoint_id": checkpoint})["cleanup_verified"]
        assert service.get_learning_checkpoint_bundle(token, ASSIGNMENT, checkpoint).path.exists()
        assert state.list_bundle_dashboard_rows(course_key=COURSE) == before and notifications == []


def test_service_cross_student_and_revocation_fail_closed(checkpoints):
    state, _store, service, token, _notifications, payload = checkpoints
    cp = save(service, token, payload)["checkpoint"]["checkpoint_id"]
    with running_worker() as (_worker, _engine, path):
        service.learning_runtime_socket = str(path)
        sid = service.create_learning_runtime(token, ASSIGNMENT, {"checkpoint_id": cp})["session"]["id"]
        student = state.upsert_local_student(student_key="s002", auth_subject="school:s002", at=NOW)
        state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)
        other = accept(service, "s002")
        assert service.get_learning_runtime(other, ASSIGNMENT)["session"] is None
        with pytest.raises(PlatformAPIError):
            service.create_learning_runtime(other, ASSIGNMENT, {"checkpoint_id": cp})
        service.revoke_current(token)
        with pytest.raises(PlatformAPIError) as revoked:
            service.close_learning_runtime(token, ASSIGNMENT, sid, {"checkpoint_id": cp})
        assert revoked.value.status == 401


def test_student_portal_projection_is_credential_bound(checkpoints):
    state, _store, service, token, _notifications, payload = checkpoints
    cp = save(service, token, payload)["checkpoint"]["checkpoint_id"]
    credential = state.get_student_password_credential(student_key="s001", course_key=COURSE)
    with running_worker() as (_worker, _engine, path):
        service.learning_runtime_socket = str(path)
        service.create_learning_runtime(token, ASSIGNMENT, {"checkpoint_id": cp})
        own = service.learning_runtime_student_overview(credential)
        assert len(own["sessions"]) == 1 and own["sessions"][0]["student_key"] == "s001"
        assert "student_identity" not in own["sessions"][0]
        service.set_student_password(student_key="s001", password="573829")
        with pytest.raises(PlatformAccessDenied):
            service.learning_runtime_student_overview(credential)


def test_missing_worker_returns_readable_unavailable_without_host_fallback(checkpoints):
    _state, _store, service, token, _notifications, payload = checkpoints
    service.learning_runtime_socket = "/no-such-private-runtime/worker.sock"
    assert service.learning_runtime_overview()["status"] == "unavailable"
    with pytest.raises(PlatformAPIError) as missing:
        service.get_learning_runtime(token, ASSIGNMENT)
    assert missing.value.status == 503 and "/no-such" not in str(missing.value)


def test_close_between_create_and_start_cannot_resurrect_execution(tmp_path):
    entered, proceed = threading.Event(), threading.Event()
    class DelayedStart(Engine):
        def __init__(self):
            super().__init__()
            self.prepared, self.started = set(), []
        def prepare(self, name, directory):
            self.prepared.add(name)
        def run(self, name, directory, request):
            entered.set()
            assert proceed.wait(3)
            if name not in self.prepared:
                raise RuntimeFault(503, "learning_runtime_unavailable", "already removed")
            self.started.append(name)
            return super().run(name, directory, request)
        def remove(self, name):
            self.prepared.discard(name)
            super().remove(name)
    engine = DelayedStart()
    worker = LearningRuntimeWorker(tmp_path / "worker", engine=engine)
    sid = worker.dispatch({"operation": "create", "owner": OWNER, "checkpoint_id": CP, "files": files()})["session"]["id"]
    errors = []
    def run():
        try:
            worker.dispatch({"operation": "run", "owner": OWNER, "session_id": sid,
                "checkpoint_id": CP, "files": files(), "language": "c", "sources": ["main.c"], "stdin": ""})
        except RuntimeFault as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(3)
    closed = worker.dispatch({"operation": "close", "owner": OWNER, "session_id": sid, "checkpoint_id": CP})
    assert closed["cleanup_verified"]
    proceed.set()
    thread.join(3)
    assert not thread.is_alive() and errors and not engine.started
    assert worker.sessions[sid]["status"] == "closed" and not (worker.root / sid).exists()
    worker.close()


def test_docker_remove_timeout_is_cleanup_failure(monkeypatch):
    engine = DockerEngine("sha256:" + "a" * 64, "b" * 24)
    def fail(*args, **kwargs):
        raise RuntimeFault(503, "learning_runtime_unavailable", "timeout")
    monkeypatch.setattr(engine, "command", fail)
    with pytest.raises(RuntimeFault) as failed:
        engine.remove("ag-learning-" + "b" * 24 + "-" + "c" * 16)
    assert failed.value.code == "learning_runtime_cleanup_failed"


def test_ambiguous_container_creation_retains_resource_and_blocks_reuse(tmp_path):
    class UncertainCreate(Engine):
        def prepare(self, name, directory):
            raise RuntimeFault(503, "learning_runtime_unavailable", "engine response lost")
    engine = UncertainCreate()
    worker = LearningRuntimeWorker(tmp_path / "worker", engine=engine)
    sid = worker.dispatch({"operation": "create", "owner": OWNER, "checkpoint_id": CP, "files": files()})["session"]["id"]
    with pytest.raises(RuntimeFault) as failed:
        worker.dispatch({"operation": "run", "owner": OWNER, "session_id": sid,
            "checkpoint_id": CP, "files": files(), "language": "c", "sources": ["main.c"]})
    assert failed.value.code == "learning_runtime_cleanup_failed"
    assert worker.sessions[sid]["container"] and worker.sessions[sid]["status"] == "cleanup_failed"
    assert not engine.calls
    worker.dispatch({"operation": "close", "owner": OWNER, "session_id": sid, "checkpoint_id": CP})
    worker.close()


def test_authorization_revoked_during_run_cancels_worker(checkpoints):
    state, store, service, token, _notifications, _payload = checkpoints
    tree = store.root.parent / "runtime-c"
    tree.mkdir()
    (tree / "main.c").write_text("int main(void) { return 0; }\n")
    payload = store.create_from_directory(tree, kind="submission").path.read_bytes()
    cp = save(service, token, payload)["checkpoint"]["checkpoint_id"]
    with running_worker() as (worker, engine, path):
        service.learning_runtime_socket = str(path)
        sid = service.create_learning_runtime(token, ASSIGNMENT, {"checkpoint_id": cp})["session"]["id"]
        entered, cancelled = threading.Event(), threading.Event()
        original_run, original_remove = engine.run, engine.remove
        def delayed_run(*args):
            entered.set()
            assert cancelled.wait(4)
            return original_run(*args)
        def cancel(name):
            original_remove(name)
            cancelled.set()
        engine.run, engine.remove = delayed_run, cancel
        errors = []
        def run():
            try:
                service.run_learning_runtime(token, ASSIGNMENT, sid, {"checkpoint_id": cp, "language": "c", "sources": ["main.c"]})
            except PlatformAPIError as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        assert entered.wait(3)
        service.revoke_current(token)
        thread.join(5)
        assert not thread.is_alive() and errors[0].status in {401, 403}
        assert cancelled.is_set() and worker.sessions[sid]["status"] == "closed"


def test_runtime_config_is_explicit_socket_and_does_not_change_grading(tmp_path):
    from autograde.pilot_config import load_pilot_config, PilotConfigError
    from test_pilot_config import write_config
    config = write_config(tmp_path / "pilot.csv", extra="learning_runtime_socket,/run/autograde/learning.sock\n")
    result = load_pilot_config(config)
    assert result.values["learning_runtime_socket"] == "/run/autograde/learning.sock"
    assert result.values["grading_runtime"] == "pilot-local"
    for path in ("relative.sock", "/tmp/../worker.sock", "/" + "a" * 101):
        write_config(config, extra="learning_runtime_socket," + path + "\n")
        with pytest.raises(PilotConfigError):
            load_pilot_config(config)


def test_create_cancellation_tombstone_prevents_late_allocation(tmp_path):
    worker = LearningRuntimeWorker(tmp_path / "worker", engine=Engine())
    request_id = "lrr_" + "b" * 32
    worker.dispatch({"operation": "cancel_create", "owner": OWNER, "request_id": request_id})
    with pytest.raises(RuntimeFault) as expired:
        worker.dispatch({"operation": "create", "owner": OWNER, "request_id": request_id,
                         "checkpoint_id": CP, "files": files()})
    assert expired.value.status == 409 and worker.sessions == {}
    worker.close()


def test_old_create_cancellation_does_not_close_newer_create(tmp_path):
    worker = LearningRuntimeWorker(tmp_path / "worker", engine=Engine())
    first, second = "lrr_" + "b" * 32, "lrr_" + "c" * 32
    for request_id in (first, second):
        sid = worker.dispatch({"operation": "create", "owner": OWNER, "request_id": request_id,
                               "checkpoint_id": CP, "files": files()})["session"]["id"]
    worker.dispatch({"operation": "cancel_create", "owner": OWNER, "request_id": first})
    assert worker.sessions[sid]["status"] == "ready"
    worker.dispatch({"operation": "cancel_create", "owner": OWNER, "request_id": second})
    assert worker.sessions[sid]["status"] == "closed"
    worker.close()


def test_revocation_during_create_cleans_exact_inflight_space(checkpoints, monkeypatch):
    _state, _store, service, token, _notifications, payload = checkpoints
    cp = save(service, token, payload)["checkpoint"]["checkpoint_id"]
    with running_worker() as (worker, _engine, path):
        service.learning_runtime_socket = str(path)
        entered, proceed = threading.Event(), threading.Event()
        original_stage = worker._stage
        def delayed_stage(*args):
            entered.set()
            assert proceed.wait(4)
            return original_stage(*args)
        monkeypatch.setattr(worker, "_stage", delayed_stage)
        errors = []
        def create():
            try:
                service.create_learning_runtime(token, ASSIGNMENT, {"checkpoint_id": cp})
            except PlatformAPIError as exc:
                errors.append(exc)
        thread = threading.Thread(target=create)
        thread.start()
        assert entered.wait(3)
        service.revoke_current(token)
        proceed.set()
        thread.join(5)
        assert not thread.is_alive() and errors[0].status in {401, 403}
        assert worker.sessions and all(i["status"] == "closed" for i in worker.sessions.values())
        assert not any(p.is_dir() for p in worker.root.iterdir())


def test_prepare_uses_fixed_limits_and_never_starts_student_code(monkeypatch, tmp_path):
    engine = DockerEngine("sha256:" + "a" * 64, "b" * 24)
    commands = []
    class Reply:
        returncode = 0
        stdout = b"a" * 64 + b"\n"
    monkeypatch.setattr(engine, "command", lambda argv, **kw: commands.append(argv) or Reply())
    engine.prepare("ag-learning-" + "b" * 24 + "-" + "c" * 16, tmp_path)
    argv = commands[0]
    assert argv[0] == "create" and "--network=none" in argv and "--read-only" in argv
    assert "--user=10001:10001" in argv and "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges" in argv
    assert "--pids-limit=64" in argv and "--memory=512m" in argv and "--cpus=1" in argv
    assert any("exec,nosuid,nodev" in a for a in argv) and any(a.endswith(",dst=/workspace,readonly") for a in argv)


def test_executor_accepts_bounded_unicode_stdin_without_truncation(monkeypatch):
    import importlib.util
    import json
    import sys
    from types import SimpleNamespace
    executor_path = Path(__file__).parents[2] / "runtime" / "learning-cpp" / "executor.py"
    spec = importlib.util.spec_from_file_location("autograde_test_executor", executor_path)
    executor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(executor)
    stdin = "가" * 21000
    request = json.dumps({"language": "c", "sources": ["main.c"], "stdin": stdin}).encode()
    assert len(request) > 100000
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(request)))
    received = []
    def execute(argv, *, stdin=b"", seconds):
        received.append(stdin)
        return {"exit_code": 0, "stdout": "", "stderr": ""}, None
    monkeypatch.setattr(executor, "execute", execute)
    assert executor.main()["status"] == "completed"
    assert received[-1] == stdin.encode()


def test_python_executor_uses_isolated_interpreter_without_a_compile_phase(monkeypatch):
    import importlib.util
    import json
    import sys
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("autograde_python_executor", Path(__file__).parents[2] / "runtime/learning-cpp/executor.py")
    executor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(executor)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(json.dumps({"language": "python", "sources": ["src/main.py"], "stdin": "42\n"}).encode())))
    calls = []
    def execute(argv, *, stdin=b"", seconds):
        calls.append((argv, stdin, seconds))
        return {"exit_code": 1, "stdout": "", "stderr": "SyntaxError: invalid syntax"}, None
    monkeypatch.setattr(executor, "execute", execute)
    result = executor.main()
    assert result["status"] == "runtime_error"
    assert result["compile"] == {"exit_code": 0, "stdout": "", "stderr": ""}
    assert len(calls) == 1 and calls[0][0][:5] == [sys.executable, "-I", "-B", "-u", "-c"]
    assert calls[0][0][-1] == "/workspace/src/main.py" and calls[0][1:] == (b"42\n", 5)
    assert "runpy.run_path" in calls[0][0][5] and "'/workspace'" in calls[0][0][5]


def test_http_runtime_unicode_envelope_and_query_boundary(checkpoints):
    import json
    from autograde.platform_portal import CourseAPI
    from test_platform_http_bundle import running_server, request
    _state, store, service, token, _notifications, _payload = checkpoints
    tree = store.root.parent / "runtime-http-c"
    tree.mkdir()
    (tree / "main.c").write_text("int main(void) { return 0; }\n")
    payload = store.create_from_directory(tree, kind="submission").path.read_bytes()
    cp = save(service, token, payload)["checkpoint"]["checkpoint_id"]
    with running_worker() as (_worker, engine, path):
        service.learning_runtime_socket = str(path)
        facade = CourseAPI({COURSE: service}, b"s" * 32)
        with running_server(facade) as server:
            root = "/v1/assignments/" + ASSIGNMENT + "/learning-runtime"
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
            status, _, raw = request(server, "POST", root + "/sessions", headers=headers,
                                      body=json.dumps({"checkpoint_id": cp}).encode())
            assert status == 200
            sid = json.loads(raw)["session"]["id"]
            for stdin in ("가" * 21000, "\x01" * 60000):
                wire = json.dumps({"checkpoint_id": cp, "language": "c", "sources": ["main.c"], "stdin": stdin}).encode()
                assert len(wire) > 64 * 1024
                status, _, raw = request(server, "POST", root + "/sessions/" + sid + "/run", headers=headers, body=wire)
                assert status == 200, raw
                assert engine.calls[-1][2]["stdin"] == stdin
            assert request(server, "GET", root + "?other=1", headers=headers)[0] == 400
            assert request(server, "GET", root)[0] == 401
            oversized = json.dumps({"checkpoint_id": cp, "language": "c", "sources": ["main.c"], "stdin": "a" * 70000}).encode()
            assert request(server, "POST", root + "/sessions/" + sid + "/run", headers=headers, body=oversized)[0] == 400
