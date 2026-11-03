"""Own only this installation's children; never terminate a PID read from a file."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time


def control(root: Path, command: str, timeout: float = 3) -> dict:
    address = root / 'run' / 'control.sock'
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(address))
        client.sendall((command + '\n').encode('ascii'))
        reply = client.recv(8192)
    return json.loads(reply)


def terminate(children: list[subprocess.Popen], grace: float = 15) -> None:
    # Popen retains ownership of unreaped child PIDs. No unrelated PID file is used.
    for child in children:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + grace
    for child in children:
        try:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)


def guard(argv: list[str]) -> None:
    """Keep the session leader alive until its whole process group is stopped.

    A web server may exit while one of its workers survives. The guard remains
    the live owner of the group, so cleanup never depends on a recycled PID.
    """
    if not argv or os.getsid(0) != os.getpid():
        raise ValueError('process guard requires its own new session')
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    process = None
    try:
        process = subprocess.Popen(argv)
        while process.poll() is None and not stopping:
            time.sleep(0.1)
        # This signal also reaches the guard, whose handler remains installed.
        os.killpg(os.getpgrp(), signal.SIGTERM)
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        # Allow ordinary workers to flush; then kill this still-owned group,
        # including the guard itself and any surviving descendant.
        time.sleep(0.2)
    finally:
        os.killpg(os.getpgrp(), signal.SIGKILL)


def serve(root: Path, commands: dict[str, list[str]] | None = None) -> int:
    root = root.resolve(strict=True)
    if os.getuid() == 0:
        raise ValueError('Run application processes as the installation owner, not root.')
    run = root / 'run'
    if run.is_symlink() or run.stat().st_uid != os.getuid() or run.stat().st_mode & 0o077:
        raise ValueError('run directory must belong to this user with mode 0700')
    address = run / 'control.sock'
    if len(os.fsencode(address)) > 100:
        raise ValueError('installation path is too long for a private control socket')
    binaries = json.loads((root / 'installation.json').read_text())['binaries']
    if commands is None:
        commands = {
            'portal': [str(root / '.venv/bin/python'), '-m', 'autograde.pilot_portal_cli',
                       '--config', str(root / 'pilot.csv'), '--isolated'],
            'web': [binaries['node'], str(root / 'source/web/node_modules/next/dist/bin/next'),
                    'start', '--hostname', '127.0.0.1', '--port', '18082'],
        }
    with open(run / 'supervisor.lock', 'a', opener=lambda p, f: os.open(p, f, 0o600)) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if address.exists():
            if not stat.S_ISSOCK(address.lstat().st_mode):
                raise ValueError('unexpected control socket path')
            address.unlink()
        children, streams = {}, []
        stopping = False

        def shutdown(*_):
            nonlocal stopping
            stopping = True

        previous = {sig: signal.signal(sig, shutdown) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(address))
                address.chmod(0o600)
                listener.listen(4)
                listener.settimeout(0.5)
                environment = dict(os.environ, NODE_ENV='production', PYTHONUNBUFFERED='1')
                environment.pop('PYTHONPATH', None)
                environment.pop('NODE_OPTIONS', None)
                directories = list(dict.fromkeys(str(Path(path).parent) for path in binaries.values()))
                environment['PATH'] = os.pathsep.join(directories) + os.pathsep + environment.get('PATH', '')
                for name, argv in commands.items():
                    stream = open(root / 'logs' / (name + '.log'), 'ab',
                                  opener=lambda p, f: os.open(p, f, 0o600))
                    streams.append(stream)
                    children[name] = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--guard', *argv],
                        cwd=root / 'source/web' if name == 'web' else root,
                        env=environment, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                        start_new_session=True)
                while not stopping:
                    if any(child.poll() is not None for child in children.values()):
                        return 1
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(2)
                        try:
                            command = connection.recv(64).decode('ascii').strip()
                            result = {'running': True, 'processes': {name: p.pid for name, p in children.items()}}
                            if command == 'stop':
                                result['stopping'] = True
                                stopping = True
                            elif command != 'status':
                                result = {'error': 'unknown_command'}
                            connection.sendall(json.dumps(result).encode())
                        except (OSError, UnicodeError):
                            continue
                return 0
        finally:
            terminate(list(children.values()))
            if address.exists() and stat.S_ISSOCK(address.lstat().st_mode):
                address.unlink()
            for stream in streams:
                stream.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--guard':
        guard(sys.argv[2:])
        raise SystemExit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(serve(args.root))
