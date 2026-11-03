"""Deployment failure boundaries using disposable sources and owned processes.

No OS packages, services, containers, cloud machines or live data are changed.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts/deploy'
sys.path.insert(0, str(SCRIPTS))
import deploy as deployment
from deployment_config import validate_profile


@pytest.fixture
def short_root():
    # The server intentionally has a short AF_UNIX address limit. macOS's
    # default pytest temp directories can already exceed that limit.
    base = Path(tempfile.gettempdir()).resolve()
    if os.name != 'nt' and Path('/tmp').exists():
        base = Path('/tmp').resolve()
    with tempfile.TemporaryDirectory(prefix='agd-', dir=base) as name:
        yield Path(name).resolve()


@pytest.fixture
def profile(short_root):
    return validate_profile({
        'schema': 'autograde.deployment.v1', 'deployment': 'institution', 'platform': 'macos',
        'installation_id': 'test-campus', 'install_dir': str(short_root / 'installed'),
        'web_url': 'https://classroom.example.edu', 'api_url': 'https://api.classroom.example.edu',
        'runner_image': 'registry.example.edu/grader/fixed@sha256:' + 'a' * 64,
        'tls_certificate': str(short_root / 'certificate.pem'), 'tls_key': str(short_root / 'key.pem'),
        'allowed_cidrs': ['192.168.50.0/24'], 'course_keys': [], 'worker_count': 4,
    })


@pytest.fixture
def source(short_root):
    source = short_root / 'source'
    source.mkdir()
    files = {
        'pyproject.toml': '[project]\nname="synthetic-deployment"\nversion="0.0.0"\n',
        'src/autograde/pilot_portal_cli.py': '# synthetic source\n',
        'web/package-lock.json': '{}\n',
        'web/package.json': '{}\n',
        'scripts/deploy/deploy.py': '# synthetic source\n',
        'scripts/deploy/deployment_config.py': '# synthetic source\n',
        'scripts/deploy/supervisor.py': '# synthetic source\n',
        'scripts/deploy/ubuntu.sh': '#!/bin/bash\n',
        'config/nginx-autograde-portal.conf.example': '# synthetic proxy\n',
        'config/nginx-autograde-next.locations.conf.example': '# synthetic proxy\n',
    }
    for name, text in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(['git', 'init', '--quiet', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    return source


def _mock_install(monkeypatch):
    calls = []
    real_execute = deployment.execute
    monkeypatch.setattr(deployment, 'prerequisites', lambda _: {
        name: '/synthetic/bin/' + name for name in ('node', 'npm', 'git', 'docker', 'nginx')})
    monkeypatch.setattr(deployment, 'render_installation', lambda *args: {'pilot.csv': 'key,value\n'})
    def fake_execute(argv, **kwargs):
        if str(argv[0]) == 'git':
            return real_execute(argv, **kwargs)
        calls.append([str(v) for v in argv])
    monkeypatch.setattr(deployment, 'execute', fake_execute)
    return calls


def test_unmanaged_existing_directory_is_preserved(profile, source, monkeypatch):
    root = Path(profile['install_dir'])
    root.mkdir()
    existing = root / 'student-submission.cpp'
    existing.write_text('student-owned original')
    monkeypatch.setattr(deployment, 'prerequisites', lambda _: pytest.fail('must reject before commands'))
    with pytest.raises(ValueError, match='existing unmanaged'):
        deployment.install(profile, source)
    assert existing.read_text() == 'student-owned original'
    assert sorted(p.name for p in root.iterdir()) == ['student-submission.cpp']


def test_installed_release_is_idempotent_and_profile_changes_are_refused(profile, source, monkeypatch):
    calls = _mock_install(monkeypatch)
    root = Path(profile['install_dir'])
    assert deployment.install(profile, source)['status'] == 'installed'
    assert any('from cryptography.fernet import Fernet; import autograde.instructor_account_web' in c for c in calls)
    private = root / 'data' / 'student-private.txt'
    private.write_text('preserve')
    monkeypatch.setattr(deployment, 'prerequisites', lambda _: pytest.fail('must not reinstall dependencies'))
    assert deployment.install(profile, source)['status'] == 'already-installed'
    changed = {**profile, 'worker_count': 8}
    with pytest.raises(ValueError, match='differs'):
        deployment.install(changed, source)
    assert private.read_text() == 'preserve'
    assert json.loads((root / 'deployment.json').read_text()) == profile


def test_missing_new_instructor_dependency_fails_install_before_web_build(profile, source, monkeypatch):
    calls = _mock_install(monkeypatch)
    default_execute = deployment.execute

    def fail_import(argv, **kwargs):
        if any('cryptography.fernet' in str(value) for value in argv):
            raise RuntimeError('synthetic dependency missing')
        return default_execute(argv, **kwargs)

    monkeypatch.setattr(deployment, 'execute', fail_import)
    with pytest.raises(RuntimeError, match='dependency missing'):
        deployment.install(profile, source)
    root = Path(profile['install_dir'])
    assert json.loads((root / 'installation.json').read_text())['status'] == 'failed'
    assert not any(c[-2:] == ['run', 'build'] for c in calls)
    assert not (root / 'data/instructor-identities.sqlite3').exists()


def test_source_snapshot_excludes_credentials_databases_and_untracked_inputs(source, short_root):
    private_names = ['web/.env.production', 'src/secret.key', 'src/student.sqlite3',
                     'src/student_roster.csv', 'scripts/deploy/private.local']
    for name in private_names:
        (source / name).write_text('do-not-export')
        subprocess.run(['git', '-C', str(source), 'add', '--', name], check=True)
    (source / 'web/untracked-student.txt').write_text('do-not-export')
    (source / 'scripts/deploy/credentials.json').write_text('{"secret":"do-not-export"}')
    target = short_root / 'export'
    manifest = deployment.copy_source(source, target)
    assert not any(name in manifest for name in private_names)
    assert not (target / 'web/untracked-student.txt').exists()
    assert not (target / 'scripts/deploy/credentials.json').exists()
    assert (target / 'scripts/deploy/deploy.py').is_file()
    assert all('do-not-export' not in p.read_text() for p in target.rglob('*') if p.is_file())


def test_export_tampering_and_source_symlinks_are_rejected(source, short_root):
    exported = short_root / 'export'
    deployment.copy_source(source, exported)
    (exported / 'web/package-lock.json').write_text('{"changed":true}')
    with pytest.raises(ValueError, match='checksum'):
        deployment.source_files(exported)
    target = source / 'web/package-lock.json'
    target.unlink()
    target.symlink_to(source / 'web/package.json')
    with pytest.raises(ValueError, match='symbolic'):
        deployment.source_files(source)


def test_source_mutation_during_snapshot_is_detected(source, short_root, monkeypatch):
    copy = deployment.shutil.copyfile
    def changed(src, dst):
        result = copy(src, dst)
        if str(dst).endswith('web/package-lock.json'):
            Path(dst).write_text('{"raced":true}')
        return result
    monkeypatch.setattr(deployment.shutil, 'copyfile', changed)
    with pytest.raises(ValueError, match='checksum'):
        deployment.copy_source(source, short_root / 'export')


def test_cloud_transport_uses_verified_ssh_and_only_release_payload(profile, source, monkeypatch):
    cloud = validate_profile({**profile, 'deployment': 'cloud', 'platform': 'ubuntu',
                              'ssh_target': 'operator@cloud.example.edu'})
    real_run = deployment.subprocess.run
    observed = {}
    monkeypatch.setattr(deployment.shutil, 'which', lambda name: '/synthetic/ssh' if name == 'ssh' else None)
    def remote(argv, **kwargs):
        if str(argv[0]) != '/synthetic/ssh':
            return real_run(argv, **kwargs)
        observed['argv'] = argv
        with tarfile.open(fileobj=kwargs['stdin'], mode='r|') as archive:
            observed['files'] = [member.name for member in archive]
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(deployment.subprocess, 'run', remote)
    result = deployment.cloud_deploy(cloud, source)
    assert 'StrictHostKeyChecking=yes' in observed['argv']
    assert 'BatchMode=yes' in observed['argv']
    assert 'operator@cloud.example.edu' in observed['argv']
    assert 'deployment.json' in observed['files']
    assert 'source/' + deployment.EXPORT_MANIFEST in observed['files']
    assert all(name == 'deployment.json' or name.startswith('source/') for name in observed['files'])
    assert result['started'] is False
    assert len(result['remote_commands']) == 4


@pytest.mark.skipif(os.name == 'nt', reason='POSIX deployment host check')
def test_application_install_refuses_root(profile, monkeypatch):
    monkeypatch.setattr(deployment.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(deployment.os, 'getuid', lambda: 0)
    with pytest.raises(ValueError, match='service user'):
        deployment.check_platform(profile)


@contextmanager
def _http_login():
    from autograde.instructor_account_web import login_page
    body = login_page(SimpleNamespace(secret=b's' * 32, web_url='https://web.example.edu')).body.encode()
    assert body.find(b'name="password"') > 8192  # Covers the former truncation failure.

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = body if self.path == '/instructor/login' else b'{"status":"ready"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_readiness_reads_actual_long_instructor_login_document():
    with _http_login() as url:
        checked = deployment.readiness({'api_url': url, 'web_url': url}, external=True)
    assert checked == {'api': True, 'web': True, 'next': True, 'instructor_login': True}


@pytest.mark.parametrize('name', ['../outside.txt', '/absolute.txt', 'source/../../outside.txt'])
def test_remote_extraction_rejects_archive_path_escape(short_root, name):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        info = tarfile.TarInfo(name)
        info.size = 6
        tar.addfile(info, io.BytesIO(b'unsafe'))
    stage = short_root / 'remote-stage'
    result = subprocess.run([sys.executable, '-c', deployment.REMOTE_EXTRACT, str(stage)],
                            input=archive.getvalue(), capture_output=True)
    assert result.returncode != 0
    assert b'unsafe release archive' in result.stderr
    assert not (short_root / 'outside.txt').exists()


def test_remote_extraction_refuses_symlink_members(short_root):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        info = tarfile.TarInfo('source')
        info.type = tarfile.SYMTYPE
        info.linkname = str(short_root / 'outside')
        tar.addfile(info)
    result = subprocess.run([sys.executable, '-c', deployment.REMOTE_EXTRACT, str(short_root / 'remote-stage')],
                            input=archive.getvalue(), capture_output=True)
    assert result.returncode != 0
    assert not (short_root / 'outside').exists()


@pytest.mark.skipif(os.name == 'nt' or (hasattr(os, 'getuid') and os.getuid() == 0), reason='Runs owned POSIX children as a non-root user')
def test_supervisor_stops_only_its_children_and_removes_socket(short_root):
    from supervisor import control
    root = short_root / 'owned'
    (root / 'run').mkdir(parents=True, mode=0o700)
    (root / 'logs').mkdir(mode=0o700)
    (root / 'installation.json').write_text(json.dumps({'binaries': {'node': sys.executable}}))
    commands = {name: [sys.executable, '-c', 'import time; time.sleep(120)'] for name in ('first', 'second')}
    code = 'import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from supervisor import serve; raise SystemExit(serve(Path(sys.argv[2]),json.loads(sys.argv[3])))'
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
    supervisor = subprocess.Popen([sys.executable, '-c', code, str(SCRIPTS), str(root), json.dumps(commands)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        observed = None
        while time.monotonic() < deadline and supervisor.poll() is None:
            try:
                observed = control(root, 'status', timeout=0.3)
                break
            except OSError:
                time.sleep(0.05)
        assert observed and observed['running'], supervisor.communicate(timeout=2) if supervisor.poll() is not None else ''
        assert set(observed['processes']) == {'first', 'second'}
        assert (root / 'run/control.sock').stat().st_mode & 0o077 == 0
        assert control(root, 'unknown')['error'] == 'unknown_command'
        assert control(root, 'stop')['stopping']
        supervisor.wait(timeout=10)
        assert supervisor.returncode == 0
        assert not (root / 'run/control.sock').exists()
        assert unrelated.poll() is None
        for pid in observed['processes'].values():
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=20)
        supervisor.communicate()
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process group test')
def test_process_guard_cleans_worker_after_main_process_exits(short_root):
    marker = short_root / 'descendant.pid'
    child_script = 'import time; time.sleep(120)'
    main_script = ('import pathlib,subprocess,sys; '
                   'child=subprocess.Popen([sys.executable,"-c",sys.argv[2]]); '
                   'pathlib.Path(sys.argv[1]).write_text(str(child.pid))')
    owned = subprocess.Popen([sys.executable, str(SCRIPTS / 'supervisor.py'), '--guard',
                              sys.executable, '-c', main_script, str(marker), child_script],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        owned.wait(timeout=10)
        assert marker.is_file()
        pid = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            check = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
            if check.returncode or not check.stdout.strip() or check.stdout.strip().startswith('Z'):
                break
            time.sleep(0.05)
        else:
            pytest.fail('owned worker survived the main process and its guard')
    finally:
        if owned.poll() is None:
            import signal
            os.killpg(owned.pid, signal.SIGKILL)
            owned.wait(timeout=5)
