#!/usr/bin/env python3
"""Institution installation and SSH deployment, without a preinstalled package.

Only explicit subcommands mutate state. DNS, certificates, OS packages, existing
installations and cloud infrastructure are never implicitly replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
import uuid
import plistlib

from deployment_config import load_profile, render_installation, RUNTIME_DIRECTORIES

SOURCE_ROOT = Path(__file__).resolve().parents[2]
EXPORT_MANIFEST = '.autograde-source-manifest.json'
BOOTSTRAP_FILES = frozenset({'deploy.py', 'deployment_config.py', 'supervisor.py',
                            'ubuntu.sh', 'macos.sh', 'windows.ps1', 'README.md', 'check_nginx_config.py'})


def execute(argv, *, cwd=None, capture=False, timeout=900, env=None):
    result = subprocess.run([str(v) for v in argv], cwd=cwd, env=env,
                            capture_output=capture, text=capture, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{Path(str(argv[0])).name} failed (exit {result.returncode}); check its output.')
    return result.stdout.strip() if capture else None


def write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('refusing a symbolic link output')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(temporary, 'x', encoding='utf-8', opener=lambda p, f: os.open(p, f, 0o600)) as out:
            out.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, data):
    write_private(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def _source_name(name):
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or '..' in parts or '\\' in name:
        return False
    if any(p in {'node_modules', '.next', '__pycache__', '.git', '.venv'} for p in parts):
        return False
    if any(p.startswith('.env') or p.endswith(('.local', '.pem', '.key', '.sqlite3', '.db')) for p in parts):
        return False
    if any(p.lower() in {'roster.csv', 'student_roster.csv', 'credentials.json', '.npmrc', '.pypirc'} for p in parts):
        return False
    if name.startswith('scripts/deploy/') and (len(parts) != 3 or parts[-1] not in BOOTSTRAP_FILES):
        return False
    return (name in {'pyproject.toml', 'README.md', 'uv.lock', 'LICENSE', 'NOTICE'}
            or name.startswith(('src/', 'web/', 'scripts/deploy/'))
            or name in {'config/nginx-autograde-portal.conf.example', 'config/nginx-autograde-next.locations.conf.example'})


def source_files(source):
    source = Path(source).resolve(strict=True)
    manifest = source / EXPORT_MANIFEST
    if manifest.is_file() and not manifest.is_symlink():
        expected = json.loads(manifest.read_text())
        names = list(expected)
    else:
        expected = None
        names = execute(['git', '-C', source, 'ls-files', '--cached', '-z'], capture=True).split('\0')
        # New deployment code can be tested before its first commit; no other
        # untracked source/config/data is swept into a release.
        names += ['scripts/deploy/' + name for name in BOOTSTRAP_FILES
                  if (source / 'scripts/deploy' / name).is_file()]
    selected = {}
    for name in sorted(set(names)):
        if not _source_name(name):
            if expected is not None:
                raise ValueError('invalid source export manifest')
            continue
        path = source / name
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != source.parent):
            raise ValueError('source releases must not contain symbolic links')
        if not path.is_file():
            raise ValueError(f'missing tracked source: {name}')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected is not None and expected[name] != digest:
            raise ValueError('source export checksum mismatch')
        selected[name] = digest
    for required in ('pyproject.toml', 'src/autograde/pilot_portal_cli.py', 'web/package-lock.json',
                     'scripts/deploy/deploy.py', 'scripts/deploy/deployment_config.py', 'scripts/deploy/supervisor.py'):
        if required not in selected:
            raise ValueError('release source is incomplete: ' + required)
    return selected


def copy_source(source, target):
    manifest = source_files(source)
    target.mkdir(mode=0o700)
    for name in manifest:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source / name, destination)
        destination.chmod(0o700 if name.endswith('.sh') else 0o600)
    write_json(target / EXPORT_MANIFEST, manifest)
    source_files(target)  # Detect source changes during copying, before executing it.
    return manifest


def check_platform(profile):
    actual = platform.system()
    if actual == 'Windows':
        raise ValueError('Use windows.ps1: the server runs inside WSL2 Ubuntu.')
    if profile['platform'] == 'macos':
        if actual != 'Darwin':
            raise ValueError('macos profile requires macOS')
    else:
        release = Path('/etc/os-release').read_text().splitlines() if actual == 'Linux' else []
        if actual != 'Linux' or not any(line in {'ID=ubuntu', 'ID="ubuntu"'} for line in release):
            raise ValueError('This profile requires Ubuntu (native, VM or WSL2).')
        if profile['platform'] == 'windows-wsl2' and 'microsoft' not in platform.release().lower():
            raise ValueError('windows-wsl2 profile requires an Ubuntu WSL2 environment')
    if os.getuid() == 0:
        raise ValueError('Use the service user for install/start; sudo is only needed for proxy-start on port 443.')


def prerequisites(profile):
    check_platform(profile)
    if sys.version_info < (3, 10):
        raise ValueError('Python 3.10 or newer is required')
    binaries = {name: shutil.which(name) for name in ('node', 'npm', 'git', 'docker', 'nginx')}
    missing = [name for name, path in binaries.items() if path is None]
    if missing:
        raise ValueError('Install prerequisites first: ' + ', '.join(missing))
    if not execute([binaries['node'], '--version'], capture=True).startswith('v24.'):
        raise ValueError('Node.js 24 is required by the locked web application')
    if execute([binaries['docker'], 'info', '--format', '{{.OSType}}'], capture=True, timeout=20) != 'linux':
        raise ValueError('Docker must be running Linux containers')
    inspected = json.loads(execute([binaries['docker'], 'image', 'inspect', '--format',
        '{"digests":{{json .RepoDigests}},"os":{{json .Os}}}', profile['runner_image']], capture=True, timeout=20))
    digest = profile['runner_image'].split('@', 1)[1]
    if inspected.get('os') != 'linux' or not any(v.endswith('@' + digest) for v in inspected.get('digests') or []):
        raise ValueError('Preload the approved Linux runner image using its exact repository digest')
    return binaries


def service_files(root, profile):
    supervisor = root / 'source/scripts/deploy/supervisor.py'
    py = root / '.venv/bin/python'
    # Escaping rules differ from shell quoting. Avoid systemd specifier/env expansion.
    def unit_quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
    command = ' '.join(unit_quote(v) for v in (py, supervisor, '--root', root))
    unit = ('[Unit]\nDescription=Autograde portal and Next.js\nAfter=network-online.target\n'
            '[Service]\nType=simple\nUMask=0077\nKillMode=control-group\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=45\n'
            'ExecStart=' + command + '\n[Install]\nWantedBy=default.target\n')
    write_private(root / 'services/autograde.service', unit)
    launch = {'Label': 'info.autograde.' + profile['installation_id'],
              'ProgramArguments': [str(py), str(supervisor), '--root', str(root)],
              'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False},
              'WorkingDirectory': str(root),
              'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
              'StandardOutPath': str(root / 'logs/supervisor.log'),
              'StandardErrorPath': str(root / 'logs/supervisor.log')}
    write_private(root / 'services/autograde.plist', plistlib.dumps(launch).decode())


def install(profile, source):
    root = Path(profile['install_dir'])
    source = Path(source).resolve(strict=True)
    if root == source or source in root.parents or root in source.parents:
        raise ValueError('installation must be outside the source checkout')
    if len(os.fsencode(root / 'run/control.sock')) > 100:
        raise ValueError('use a shorter installation directory (control socket limit: 100 bytes)')
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise ValueError('installation path cannot contain symbolic links')
    record = root / 'installation.json'
    if root.exists():
        if not record.is_file() or record.is_symlink():
            raise ValueError('refusing to reuse an existing unmanaged installation directory')
        if json.loads((root / 'deployment.json').read_text()) != profile:
            raise ValueError('existing deployment differs; do not overwrite an operating installation')
        state = json.loads(record.read_text())
        if state.get('status') == 'installed':
            return {'installation': str(root), 'status': 'already-installed', 'started': False}
        if state.get('status') not in {'preparing', 'failed'}:
            raise ValueError('unrecognized installation state')
        if (root / 'data/instructor-identities.sqlite3').exists():
            raise ValueError('existing account data requires an explicit upgrade plan, not a fresh install')
        source_files(root / 'source')  # Resumption uses exactly the recorded release.
    binaries = prerequisites(profile)
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
        write_json(root / 'deployment.json', profile)
        state = {'status': 'preparing', 'binaries': binaries}
        write_json(record, state)
        try:
            state['source_manifest'] = copy_source(source, root / 'source')
            state['release_id'] = profile['installation_id'] + '-' + hashlib.sha256(
                json.dumps(state['source_manifest'], sort_keys=True).encode()).hexdigest()[:16]
        except Exception:
            state['status'] = 'failed'
            write_json(record, state)
            raise
    state.update(status='preparing', binaries=binaries)
    write_json(record, state)
    try:
        for directory in (*RUNTIME_DIRECTORIES, 'data'):
            (root / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, content in render_installation(profile, root / 'source', root).items():
            write_private(root / name, content)
        venv = root / '.venv'
        execute([sys.executable, '-m', 'venv', venv])
        py = venv / 'bin/python'
        execute([py, '-m', 'pip', 'install', str(root / 'source')])
        execute([py, '-m', 'pip', 'check'])
        execute([py, '-c', 'from cryptography.fernet import Fernet; import autograde.instructor_account_web'])
        execute([py, '-c', 'from autograde.pilot_config import load_pilot_config; import sys; load_pilot_config(sys.argv[1])', root / 'pilot.csv'])
        env = dict(os.environ)
        env['PATH'] = str(Path(binaries['node']).parent) + os.pathsep + env.get('PATH', '')
        execute([binaries['npm'], 'ci'], cwd=root / 'source/web', env=env)
        execute([binaries['npm'], 'run', 'build'], cwd=root / 'source/web', env=env)
        service_files(root, profile)
        state['status'] = 'installed'
        write_json(record, state)
    except Exception:
        state['status'] = 'failed'
        write_json(record, state)
        raise
    return {'installation': str(root), 'status': 'installed', 'started': False,
            'next_steps': ['init-admin', 'start', 'proxy-start', 'check', 'MFA enrollment', 'backup and restore drill']}


def installed(profile):
    root = Path(profile['install_dir'])
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise ValueError('unsafe installation path')
    state = json.loads((root / 'installation.json').read_text())
    if state.get('status') != 'installed' or json.loads((root / 'deployment.json').read_text()) != profile:
        raise ValueError('use the installed deployment.json and complete installation first')
    return root, state


def status(root):
    from supervisor import control
    try:
        return control(root, 'status')
    except (OSError, ValueError):
        return {'running': False}


def probe(url):
    parsed = urlsplit(url)
    connection = (http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection)(
        parsed.hostname, parsed.port, timeout=5)
    try:
        connection.request('GET', parsed.path)
        response = connection.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            raise ValueError('health response exceeds the size limit')
        return response.status, body
    finally:
        connection.close()


def readiness(profile, *, external=False):
    base = {'api': 'http://127.0.0.1:18080', 'web': 'http://127.0.0.1:18081'}
    if external:
        base = {'api': profile['api_url'], 'web': profile['web_url']}
    checks = {}
    for name, origin in base.items():
        try:
            code, body = probe(origin + '/readyz')
            checks[name] = code == 200 and json.loads(body).get('status') == 'ready'
        except (OSError, ValueError, http.client.HTTPException):
            checks[name] = False
    for name, url in [('next', (profile['web_url'] if external else 'http://127.0.0.1:18082') + '/app/healthz'),
                      ('instructor_login', base['web'] + '/instructor/login')]:
        try:
            code, body = probe(url)
            checks[name] = code == 200 and (b'name="password"' in body if name == 'instructor_login' else True)
        except (OSError, ValueError, http.client.HTTPException):
            checks[name] = False
    return checks


def start(profile, root, state):
    check_platform(profile)
    from supervisor import control
    if status(root)['running']:
        return {'status': 'already-running', 'checks': readiness(profile)}
    # Refuse all port collisions before creating any process.
    reservations = []
    try:
        for port in (18080, 18081, 18082):
            sock = socket.socket()
            reservations.append(sock)
            sock.bind(('127.0.0.1', port))
    finally:
        for sock in reservations:
            sock.close()
    execute([root / '.venv/bin/python', '-c',
             'from cryptography.fernet import Fernet; import autograde.instructor_account_web'])
    with open(root / 'logs/supervisor.log', 'ab', opener=lambda p, f: os.open(p, f, 0o600)) as log:
        child = subprocess.Popen([str(root / '.venv/bin/python'), str(root / 'source/scripts/deploy/supervisor.py'),
            '--root', str(root)], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 60
    while child.poll() is None and time.monotonic() < deadline:
        if status(root)['running']:
            checks = readiness(profile)
            if all(checks.values()):
                return {'running': True, 'checks': checks, 'https_proxy': 'run proxy-start separately'}
        time.sleep(0.5)
    # Stop only the supervisor child created here, never a stale PID file.
    if child.poll() is None:
        child.terminate()
        child.wait(timeout=30)
    raise RuntimeError('startup failed; inspect logs/portal.log, web.log and supervisor.log')


def stop(root):
    from supervisor import control
    if not status(root)['running']:
        return {'running': False}
    control(root, 'stop')
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if not (root / 'run/control.sock').exists():
            return {'running': False, 'proxy': 'unchanged'}
        time.sleep(0.25)
    raise RuntimeError('shutdown still in progress; no unrelated processes were terminated')


def proxy_start(profile, root, state):
    for key in ('tls_certificate', 'tls_key'):
        if not Path(profile[key]).is_file():
            raise ValueError('Provision the configured HTTPS certificate and key before proxy-start')
    if Path(profile['tls_key']).stat().st_mode & 0o077:
        raise ValueError('TLS private key must not be readable by other users (use mode 0600).')
    nginx = state['binaries']['nginx']
    prefix = ['-p', str(root) + '/', '-c', str(root / 'config/nginx.conf')]
    execute([nginx, '-t', *prefix], timeout=15)
    execute([nginx, *prefix], timeout=15)
    return {'proxy': 'started', 'web_url': profile['web_url'], 'api_url': profile['api_url']}


REMOTE_EXTRACT = r'''
import json,os,pathlib,subprocess,sys,tarfile
root=pathlib.Path(sys.argv[1]);root.mkdir(mode=0o700)
total=0;seen=set()
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:
 for member in archive:
  name=pathlib.PurePosixPath(member.name)
  if name.is_absolute() or '..' in name.parts or '\\' in member.name or member.name in seen or not member.isfile(): raise ValueError('unsafe release archive')
  if member.size<0 or member.size>64*1024*1024: raise ValueError('release member too large')
  total+=member.size
  if total>512*1024*1024: raise ValueError('release too large')
  seen.add(member.name);out=root.joinpath(*name.parts);out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
  with open(out,'xb',opener=lambda p,f:os.open(p,f,0o600)) as stream:
   payload=archive.extractfile(member)
   while True:
    block=payload.read(1024*1024)
    if not block:break
    stream.write(block)
os.chmod(root/'source/scripts/deploy/ubuntu.sh',0o700)
raise SystemExit(subprocess.call(['python3',str(root/'source/scripts/deploy/deploy.py'),'install','--profile',str(root/'deployment.json'),'--source-root',str(root/'source')]))
'''


def cloud_deploy(profile, source):
    if profile['deployment'] != 'cloud':
        raise ValueError('cloud-deploy requires a cloud profile with an SSH target')
    ssh = shutil.which('ssh')
    if not ssh:
        raise ValueError('OpenSSH client is required')
    # Strict host verification: the operator enrolls a verified host key first.
    remote_stage = '/tmp/autograde-deploy-' + uuid.uuid4().hex
    remote_command = shlex.join(['python3', '-c', REMOTE_EXTRACT, remote_stage])
    with tempfile.TemporaryDirectory(prefix='autograde-deploy-') as temporary:
        stage = Path(temporary)
        copy_source(Path(source), stage / 'source')
        write_json(stage / 'deployment.json', profile)
        archive = stage / 'release.tar'
        with tarfile.open(archive, 'w') as tar:
            for path in sorted(stage.rglob('*')):
                if path.is_file() and path != archive:
                    tar.add(path, arcname=path.relative_to(stage).as_posix(), recursive=False)
        with archive.open('rb') as stream:
            result = subprocess.run([ssh, '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes',
                                     '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                                     '-p', str(profile['ssh_port']),
                                     profile['ssh_target'], remote_command], stdin=stream)
        if result.returncode:
            raise RuntimeError('SSH deployment failed; the existing installation was not replaced. Check remote prerequisite output.')
    remote_entry = str(Path(profile['install_dir']) / 'source/scripts/deploy/deploy.py')
    remote_profile = str(Path(profile['install_dir']) / 'deployment.json')
    remote_commands = [shlex.join(['python3', remote_entry, 'init-admin', '--profile', remote_profile,
                                   '--username', 'owner', '--name', '기관 관리자'])]
    for command in ('start', 'proxy-start', 'check'):
        remote_commands.append(shlex.join((['sudo'] if command == 'proxy-start' else []) +
                                         ['python3', remote_entry, command, '--profile', remote_profile]))
    return {'installed_on': profile['ssh_target'], 'started': False,
            'remote_profile': str(Path(profile['install_dir']) / 'deployment.json'),
            'next_steps': ['SSH interactively as the service user', 'init-admin', 'start', 'proxy-start', 'check', 'MFA enrollment'],
            'remote_commands': remote_commands,
            'staging_directory': remote_stage}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Autograde institution / cloud deployment')
    parser.add_argument('command', choices=['plan', 'install', 'init-admin', 'start', 'stop', 'status', 'check', 'backup', 'cloud-deploy', 'proxy-start'])
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=SOURCE_ROOT)
    parser.add_argument('--username')
    parser.add_argument('--name')
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        if args.command == 'plan':
            result = {'deployment': profile['deployment'], 'target_platform': profile['platform'],
                      'installation': profile['install_dir'], 'web_url': profile['web_url'], 'api_url': profile['api_url'],
                      'runtime': 'isolated Docker / SQLite / personal instructor MFA', 'will_change': False,
                      'prerequisites': ['Python >=3.10 with venv', 'Node.js 24 and npm', 'Git', 'Linux Docker daemon with pinned runner image',
                                        'Nginx', 'DNS and HTTPS certificate', 'non-root installation owner'],
                      'network_changes': 'none; DNS records and proxy configuration are generated only'}
        elif args.command == 'install':
            result = install(profile, args.source_root)
        elif args.command == 'cloud-deploy':
            result = cloud_deploy(profile, args.source_root)
        else:
            root, state = installed(profile)
            if args.command == 'init-admin':
                check_platform(profile)
                if not args.username or not args.name:
                    raise ValueError('init-admin requires --username and --name; password is prompted privately')
                prefix = [root / '.venv/bin/python', '-m', 'autograde.instructor_identity_cli', '--config', root / 'pilot.csv']
                if not (root / 'data/instructor-identities.sqlite3').exists():
                    execute([*prefix, 'init'])
                execute([*prefix, 'create', '--username', args.username, '--name', args.name, '--role', 'admin'])
                result = {'account_created': True, 'next_step': 'start, then complete MFA on /instructor/login'}
            elif args.command == 'start':
                result = start(profile, root, state)
            elif args.command == 'stop':
                result = stop(root)
            elif args.command == 'status':
                result = status(root)
            elif args.command == 'proxy-start':
                result = proxy_start(profile, root, state)
            elif args.command == 'backup':
                if args.destination is None:
                    raise ValueError('backup requires --destination outside the data directory')
                if status(root)['running']:
                    raise ValueError('stop the application and all data writers before backup')
                execute([root / '.venv/bin/python', '-m', 'autograde.operations_cli', 'backup', '--config', root / 'pilot.csv',
                         '--destination', args.destination, '--release-id', state['release_id']])
                result = {'backup': 'completed'}
            else:
                result = {'internal': readiness(profile), 'https': readiness(profile, external=True), 'production_ready': False}
                print(json.dumps({'ok': all(result['internal'].values()) and all(result['https'].values()), 'result': result}, ensure_ascii=False))
                return 0 if all(result['internal'].values()) and all(result['https'].values()) else 2
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
