"""Exercise the checkout launcher with real listeners and owned process groups."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "run_portal.py"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="checkout process groups require POSIX")


WORKER = r'''
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

settings = json.loads(sys.argv[1])
prefix = Path(settings["prefix"])
stopping = False

def stop(signum, frame):
    global stopping
    prefix.with_suffix(".signal").write_text(str(signum))
    stopping = True

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
child = None
if settings.get("descendant"):
    child = subprocess.Popen([sys.executable, __file__, json.dumps({
        "prefix": str(prefix) + "-child", "sleep_only": True,
    })])

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with prefix.with_suffix(".requests").open("a") as log:
            log.write(self.path + "\n")
        redirect = settings.get("redirect") and self.path == "/health"
        self.send_response(302 if redirect else 200)
        if redirect:
            self.send_header("Location", "/healthy")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(settings.get("body", '{"ok":true,"service":"portal"}').encode())

    def log_message(self, *args):
        pass

server = None
if not settings.get("sleep_only"):
    server = http.server.HTTPServer(("127.0.0.1", settings["port"]), Handler)
    server.timeout = 0.03
prefix.with_suffix(".pid.tmp").write_text(json.dumps({
    "pid": os.getpid(), "sid": os.getsid(0), "pgid": os.getpgrp(),
}))
prefix.with_suffix(".pid.tmp").replace(prefix.with_suffix(".pid"))
try:
    while not stopping and not prefix.with_suffix(".exit").exists():
        if server:
            server.handle_request()
        else:
            time.sleep(0.03)
finally:
    if server:
        server.server_close()
    if child is not None and stopping:
        child.wait(timeout=3)
sys.exit(0 if stopping else 7)
'''


SUPERVISOR = r'''
import importlib.util
import json
from pathlib import Path
import sys

source, specification = sys.argv[1:]
spec = importlib.util.spec_from_file_location("checkout_portal_launcher", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
config = json.loads(Path(specification).read_text())
if "build" in config:
    plan = module.Plan(Path("unused.local"), [], sys.executable, config["build"], Path(config["cwd"]))
    sys.exit(module.build_web(plan, 1))
services = [module.Service(
    name=item["name"], argv=item["argv"], cwd=Path(item["cwd"]),
    readiness=tuple((url, expected) for url, expected in item["readiness"]),
) for item in config["services"]]
sys.exit(module.run_services(services, **config["options"]))
'''


def wait_for(predicate, *, timeout=6, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError(f"timed out waiting for {description}")


def is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location("test_checkout_portal_launcher", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture
def processes(tmp_path):
    """Keep cleanup scoped to test-owned processes even when an assertion fails."""
    worker = tmp_path / "fake service.py"
    worker.write_text(WORKER)
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(SUPERVISOR)
    owned = []
    logs = []
    prefixes = []

    class Processes:
        def service(self, name, **settings):
            prefix = tmp_path / name
            prefixes.append(prefix)
            port = unused_port()
            configuration = {"prefix": str(prefix), "port": port, **settings}
            return {
                "name": name,
                "argv": [sys.executable, str(worker), json.dumps(configuration)],
                "cwd": str(tmp_path),
                "readiness": [[f"http://127.0.0.1:{port}/health", {"ok": True, "service": "portal"}]],
            }

        def start(self, services, *, startup_timeout=4, env=None):
            config = tmp_path / f"services-{len(owned)}.json"
            config.write_text(json.dumps({"services": services, "options": {
                "startup_timeout": startup_timeout, "stop_timeout": 1, "poll_interval": 0.025,
            }}))
            log_path = tmp_path / f"supervisor-{len(owned)}.log"
            log = log_path.open("w")
            logs.append(log)
            process = subprocess.Popen(
                [sys.executable, str(supervisor), str(LAUNCHER), str(config)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            owned.append(process)
            self.supervisor_log = log_path
            return process, log_path

        def start_build(self):
            prefix = tmp_path / "npm"
            prefixes.append(prefix)
            npm = tmp_path / "npm"
            npm.write_text(f"#!{sys.executable}\n" +
                           "import runpy, sys\n" +
                           f"sys.argv = [{str(worker)!r}, {json.dumps({'prefix': str(prefix), 'sleep_only': True, 'descendant': True})!r}]\n" +
                           f"runpy.run_path({str(worker)!r}, run_name='__main__')\n")
            npm.chmod(0o755)
            specification = tmp_path / "build.json"
            specification.write_text(json.dumps({"build": str(npm), "cwd": str(tmp_path)}))
            log_path = tmp_path / "build.log"
            log = log_path.open("w")
            logs.append(log)
            process = subprocess.Popen(
                [sys.executable, str(supervisor), str(LAUNCHER), str(specification)],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            )
            owned.append(process)
            return process, log_path

        def standalone(self, service):
            process = subprocess.Popen(service["argv"], cwd=tmp_path, start_new_session=True)
            owned.append(process)
            return process

        def marker(self, name, suffix):
            return (tmp_path / name).with_suffix(suffix)

        def pid(self, name):
            marker = self.marker(name, ".pid")
            wait_for(marker.exists, description=f"{name} pid")
            return json.loads(marker.read_text())["pid"]

        def ready(self, *names):
            for name in names:
                marker = self.marker(name, ".requests")
                wait_for(lambda: marker.exists() and "/health" in marker.read_text(),
                         description=f"{name} health request")
            wait_for(lambda: "READY:" in self.supervisor_log.read_text(),
                     description="supervisor readiness announcement")

    yield Processes()

    for process in reversed(owned):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    for prefix in prefixes:
        # A failing launcher must not leave a fake grandchild behind.
        for name in (prefix, Path(str(prefix) + "-child")):
            marker = name.with_suffix(".pid")
            if marker.exists():
                pid = json.loads(marker.read_text())["pid"]
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    for log in logs:
        log.close()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_stops_owned_services_and_descendants(processes, signum):
    portal = processes.service("portal", descendant=True)
    next_service = processes.service("next")
    supervisor, log = processes.start([portal, next_service])
    processes.ready("portal", "next")
    child_pid = processes.pid("portal-child")
    for name in ("portal", "next"):
        metadata = json.loads(processes.marker(name, ".pid").read_text())
        assert metadata["pid"] == metadata["sid"] == metadata["pgid"]

    supervisor.send_signal(signum)
    assert supervisor.wait(timeout=6) == 128 + signum, log.read_text()

    for name in ("portal", "next", "portal-child"):
        assert processes.marker(name, ".signal").exists(), log.read_text()
    wait_for(lambda: not is_alive(child_pid), description="owned descendant termination")


def test_pre_ready_failure_stops_already_started_service(processes):
    portal = processes.service("portal")
    next_service = processes.service("next")
    processes.marker("next", ".exit").touch()
    supervisor, log = processes.start([portal, next_service])

    assert supervisor.wait(timeout=6) != 0, log.read_text()
    assert processes.marker("portal", ".signal").exists(), log.read_text()
    assert not is_alive(processes.pid("portal"))


def test_next_failure_after_readiness_preserves_backend(processes):
    supervisor, log = processes.start([
        processes.service("portal"), processes.service("next", descendant=True),
    ])
    processes.ready("portal", "next")
    portal_pid = processes.pid("portal")
    next_pid = processes.pid("next")
    descendant_pid = processes.pid("next-child")

    processes.marker("next", ".exit").touch()
    wait_for(lambda: not is_alive(next_pid), description="Next exit")
    wait_for(lambda: not is_alive(descendant_pid), description="failed Next descendant cleanup")

    assert supervisor.poll() is None, log.read_text()
    assert is_alive(portal_pid)
    assert not processes.marker("portal", ".signal").exists()
    supervisor.terminate()
    supervisor.wait(timeout=6)
    message = log.read_text().lower()
    assert "next" in message and any(word in message for word in ("warn", "degrad", "contin", "remain", "계속"))


def test_backend_failure_after_readiness_stops_next(processes):
    supervisor, log = processes.start([processes.service("portal"), processes.service("next")])
    processes.ready("portal", "next")
    next_pid = processes.pid("next")

    processes.marker("portal", ".exit").touch()

    assert supervisor.wait(timeout=6) != 0, log.read_text()
    assert processes.marker("next", ".signal").exists(), log.read_text()
    assert not is_alive(next_pid)


@pytest.mark.parametrize("body", [
    "not JSON",
    '{"ok":false,"service":"portal"}',
    '{"ok":true,"service":"unrelated"}',
    '{"ok":1,"service":"portal"}',
])
def test_unhealthy_payload_times_out_and_cleans_up(processes, body):
    supervisor, log = processes.start(
        [processes.service("portal", body=body), processes.service("next")], startup_timeout=0.7,
    )

    assert supervisor.wait(timeout=5) != 0, log.read_text()
    assert processes.marker("portal", ".requests").exists()
    assert processes.marker("portal", ".signal").exists(), log.read_text()
    assert not is_alive(processes.pid("portal"))


def test_readiness_does_not_follow_redirects(processes):
    supervisor, log = processes.start(
        [processes.service("portal", redirect=True), processes.service("next")], startup_timeout=0.7,
    )

    assert supervisor.wait(timeout=5) != 0, log.read_text()
    requests = processes.marker("portal", ".requests").read_text().splitlines()
    assert requests and set(requests) == {"/health"}
    assert processes.marker("portal", ".signal").exists()


def test_readiness_ignores_http_proxy_environment(processes):
    proxy = processes.service("proxy", body='{"ok":false}')
    proxy_process = processes.standalone(proxy)
    processes.pid("proxy")
    proxy_url = proxy["readiness"][0][0].removesuffix("/health")
    env = {**os.environ, "HTTP_PROXY": proxy_url, "http_proxy": proxy_url,
           "ALL_PROXY": proxy_url, "all_proxy": proxy_url, "NO_PROXY": "", "no_proxy": ""}
    supervisor, log = processes.start([processes.service("portal"), processes.service("next")], env=env)

    processes.ready("portal")
    assert supervisor.poll() is None, log.read_text()
    assert not processes.marker("proxy", ".requests").exists()
    supervisor.terminate()
    supervisor.wait(timeout=6)
    assert proxy_process.poll() is None


def test_occupied_port_does_not_terminate_unrelated_process(launcher, processes):
    service = processes.service("unrelated")
    process = processes.standalone(service)
    processes.pid("unrelated")
    port = int(service["readiness"][0][0].split(":")[2].split("/")[0])

    with pytest.raises((OSError, ValueError, RuntimeError)):
        launcher.assert_ports_free([("127.0.0.1", port)])

    assert process.poll() is None
    assert not processes.marker("unrelated", ".signal").exists()
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        pass


def test_free_ports_preflight_releases_its_probe_socket(launcher):
    port = unused_port()
    launcher.assert_ports_free([("127.0.0.1", port)])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))


@pytest.fixture
def checkout(tmp_path, monkeypatch, launcher):
    """A configured checkout with harmless real Node/npm stand-ins."""
    root = tmp_path / "checkout with spaces"
    pilot = root / "pilot"
    web = root / "web"
    tools = root / "tools"
    for folder in (pilot, web, tools):
        folder.mkdir(parents=True)
    ports = set()
    while len(ports) < 3:
        ports.add(unused_port())
    api_port, web_port, next_port = sorted(ports)
    config = pilot / "portal.https.local"
    config.write_text(
        "key,value\n"
        "course_key,launcher-test\n"
        "data_root,data with spaces\n"
        "public_base_url,https://api.example.test\n"
        "web_public_base_url,https://web.example.test\n"
        "listen,127.0.0.1\n"
        f"port,{api_port}\nweb_port,{web_port}\nnext_web_port,{next_port}\n"
    )
    engines = json.loads((ROOT / "web/package.json").read_text())["engines"]
    (web / "package.json").write_text(json.dumps({"engines": engines}))
    next_entry = web / "node_modules/next/dist/bin/next"
    next_entry.parent.mkdir(parents=True)
    next_entry.write_text("// fake Next entry for read-only preflight\n")
    (web / ".next").mkdir()
    (web / ".next/BUILD_ID").write_text("test-build\n")
    node = tools / "node"
    node.write_text(f"#!{sys.executable}\nprint('v26.13.0')\n")
    node.chmod(0o755)
    npm = tools / "npm"
    npm.write_text(f"#!{sys.executable}\n" + '''
import json
import os
from pathlib import Path
import sys

with Path("npm-invocations.log").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
with Path("npm-environment.log").open("a") as log:
    log.write(json.dumps({"NODE_ENV": os.environ.get("NODE_ENV")}) + "\\n")
if sys.argv[1:] == ["ci", "--include=dev"]:
    entry = Path("node_modules/next/dist/bin/next")
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// fake Next entry\\n")
elif sys.argv[1:] == ["run", "build"]:
    Path(".next").mkdir(exist_ok=True)
    Path(".next/BUILD_ID").write_text("new-test-build\\n")
else:
    sys.exit(17)
''')
    npm.chmod(0o755)
    monkeypatch.setattr(launcher.shutil, "which", lambda command: str(tools / command)
                        if command in {"node", "npm"} else None)
    return root


def snapshot_tree(root):
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


@pytest.mark.parametrize(("version", "supported"), [
    ("v24.14.9", False), ("v24.15.0", True), ("v24.19.0", True),
    ("v25.8.1", False), ("v26.0.0", True), ("v26.13.1", True),
    ("v27.0.0", False), ("v26.14.0-rc.1", False), ("v026.13.1", False), ("not-a-version", False),
])
def test_preflight_supports_declared_stable_node_lines(launcher, checkout, version, supported):
    (checkout / "tools/node").write_text(f"#!{sys.executable}\nprint({version!r})\n")
    assert launcher.main(["--check"], root=checkout) == (0 if supported else 1)
    assert not (checkout / "pilot/data with spaces").exists()


def test_web_engine_and_lockfile_use_the_same_range():
    package = json.loads((ROOT / "web/package.json").read_text())
    lock = json.loads((ROOT / "web/package-lock.json").read_text())
    assert package["engines"] == lock["packages"][""]["engines"]


def test_check_is_read_only_and_default_config_is_relative_to_checkout(
    launcher, checkout, tmp_path, monkeypatch,
):
    elsewhere = tmp_path / "unrelated working directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    before = snapshot_tree(checkout)
    monkeypatch.setattr(launcher, "run_services", lambda *args, **kwargs: pytest.fail("--check launched servers"))
    monkeypatch.setattr(launcher, "build_web", lambda *args, **kwargs: pytest.fail("--check installed dependencies"))

    assert launcher.main(["--check"], root=checkout) == 0

    assert snapshot_tree(checkout) == before
    assert list(elsewhere.iterdir()) == []
    assert not (checkout / "pilot/data with spaces").exists()


def test_default_config_does_not_guess_a_csv_suffix(launcher, checkout):
    original = checkout / "pilot/portal.https.local"
    original.rename(original.with_name(original.name + ".csv"))

    assert launcher.main(["--check"], root=checkout) == 1

    assert not original.exists()
    assert not (checkout / "pilot/data with spaces").exists()
    assert launcher.main(["--check", "--config", "pilot/portal.https.local.csv"], root=checkout) == 0


def test_config_symlink_does_not_bypass_existing_loader_checks(launcher, checkout):
    original = checkout / "pilot/portal.https.local"
    actual = original.with_name("actual.local")
    original.rename(actual)
    original.symlink_to(actual)

    assert launcher.main(["--check"], root=checkout) == 1
    assert original.is_symlink()
    assert not (checkout / "pilot/data with spaces").exists()


def test_config_spaces_and_shell_metacharacters_remain_one_argument(launcher, checkout):
    original = checkout / "pilot/portal.https.local"
    config = original.with_name("portal $(touch UNEXPECTED); with spaces.local")
    original.rename(config)

    plan = launcher.prepare(launcher.parse_args(["--config", str(config.relative_to(checkout))]), root=checkout)

    portal = next(service for service in plan.services if service.name == "portal")
    assert plan.config == config
    assert portal.argv[portal.argv.index("--config") + 1] == str(config)
    assert portal.argv.count(str(config)) == 1
    assert portal.cwd == checkout
    assert not (checkout / "UNEXPECTED").exists()


def test_isolated_mode_is_validated_and_forwarded_to_python_only(launcher, checkout):
    config = checkout / "pilot/portal.https.local"
    with config.open("a") as target:
        target.write("grading_runtime,docker\n")

    with pytest.raises(ValueError, match="isolated"):
        launcher.prepare(launcher.parse_args([]), root=checkout)
    plan = launcher.prepare(launcher.parse_args(["--isolated"]), root=checkout)

    by_name = {service.name: service for service in plan.services}
    assert "--isolated" in by_name["portal"].argv
    assert "--isolated" not in by_name["next"].argv


def test_missing_build_requires_explicit_build_and_runs_npm_before_services(
    launcher, checkout, monkeypatch,
):
    web = checkout / "web"
    (web / ".next/BUILD_ID").unlink()
    (web / "node_modules/next/dist/bin/next").unlink()
    launches = []
    monkeypatch.setenv("NODE_ENV", "development")

    def run_services(services, **options):
        assert (web / ".next/BUILD_ID").read_text() == "new-test-build\n"
        commands = [json.loads(line) for line in (web / "npm-invocations.log").read_text().splitlines()]
        assert commands == [["ci", "--include=dev"], ["run", "build"]]
        environments = [json.loads(line) for line in (web / "npm-environment.log").read_text().splitlines()]
        assert environments == [{"NODE_ENV": "production"}, {"NODE_ENV": "production"}]
        launches.append(services)
        return 0

    monkeypatch.setattr(launcher, "run_services", run_services)
    assert launcher.main([], root=checkout) == 1
    assert not (web / "npm-invocations.log").exists()
    assert launches == []

    assert launcher.main(["--build"], root=checkout) == 0
    assert len(launches) == 1
    assert not (checkout / "pilot/data with spaces").exists()


def test_interrupted_build_cleans_up_npm_process_group(processes):
    supervisor, log = processes.start_build()
    npm_pid = processes.pid("npm")
    descendant_pid = processes.pid("npm-child")

    supervisor.terminate()

    assert supervisor.wait(timeout=6) == 128 + signal.SIGTERM, log.read_text()
    assert processes.marker("npm", ".signal").exists()
    assert processes.marker("npm-child", ".signal").exists()
    assert not is_alive(npm_pid)
    wait_for(lambda: not is_alive(descendant_pid), description="npm descendant termination")
