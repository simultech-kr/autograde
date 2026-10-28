"""Operator entry point for backups, support diagnostics and lab provisioning."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
import csv
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import tarfile

from .deployment_backup import create_snapshot, verify_snapshot, restore_snapshot, prune_snapshots
from .operations import OperationsService, SeatRegistry
from .pilot_config import load_pilot_config
from .settings import AppPaths


@contextmanager
def credential_output(path):
    """Reserve a new file and remove only our own inode after a failed issue."""
    stream = open(path, 'x', opener=lambda name, flags: os.open(name, flags, 0o600))
    owned = os.fstat(stream.fileno())
    try:
        with stream:
            yield stream
    except BaseException:
        try:
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                os.unlink(path)
        except FileNotFoundError:
            pass
        raise


def preflight(config):
    values = config.values
    paths = AppPaths.from_value(values['data_root'])
    checks = {'https': all(values.get(k, '').startswith('https://') for k in ('public_base_url', 'web_public_base_url')),
              'personal_instructors': values.get('instructor_auth_mode') == 'personal',
              'isolated_grading': values['grading_runtime'] in {'docker', 'podman'},
              'runtime_executable': shutil.which(values['grading_runtime']) is not None,
              'private_storage': paths.root.is_dir() and not paths.root.is_symlink() and not (paths.root.stat().st_mode & 0o077)}
    checks['backup_verified'] = False
    if paths.database.is_file():
        report = OperationsService(paths).snapshot(force=True)
        age = report['backup']['age_seconds']
        checks['backup_verified'] = report['backup']['status'] == 'verified' and age is not None and age < 36 * 3600
        checks['database_readable'] = report['database'] == 'readable'
        checks['storage_reserve'] = bool(report['storage'] and report['storage']['free_bytes'] >= 1024 ** 3)
    else:
        checks.update(database_readable=False, storage_reserve=False)
    return {'checks': checks, 'blocking_checks': [key for key, passed in checks.items() if not passed],
            'production_ready': False, 'required_external_evidence': ['signed_os_packages', 'restore_drill', 'supported_seat_load', 'institution_acceptance']}


def check_running(config, report):
    """Query only the configured loopback listeners, with no credentials."""
    import ipaddress
    host = config.values['listen']
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError('health monitoring requires loopback listeners')
    listeners = {}
    for name, key in [('api','port'),('web','web_port')]:
        try:
            # Direct HTTP client never follows a redirect outside loopback.
            with closing(http.client.HTTPConnection(host, config.values[key], timeout=3)) as connection:
                connection.request('GET', '/readyz')
                response = connection.getresponse()
                raw = response.read(4097)
                payload = json.loads(raw) if len(raw) <= 4096 and response.status == 200 else None
                listeners[name] = isinstance(payload, dict) and payload.get('status') == 'ready'
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            listeners[name] = False
    report['listeners'] = listeners
    report['workers'] = {'status': 'observed_by_readiness' if all(listeners.values()) else 'unavailable'}
    report['alerts'] = [a for a in report['alerts'] if a != 'workers_unavailable']
    if not all(listeners.values()):
        report['alerts'].append('listeners_unavailable')
    report['status'] = 'attention' if report['alerts'] else 'ok'
    return report


def configure(directory, *, api_url, web_url, runner_image, runtime='docker'):
    """Create a new institution CSV only; no secrets or live services are changed."""
    directory = Path(directory).absolute()
    if directory.exists() or directory.is_symlink():
        raise ValueError('installation directory must be new')
    if not api_url.startswith('https://') or not web_url.startswith('https://'):
        raise ValueError('institution origins require HTTPS')
    if runtime not in {'docker', 'podman'}:
        raise ValueError('institution requires an isolated runtime')
    if '@sha256:' not in runner_image:
        raise ValueError('a pinned instructor runner image is required')
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.configure-', dir=directory.parent) as temporary:
        stage = Path(temporary)
        config = stage / 'pilot.csv'
        with config.open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerows([('key','value'), ('course_key','institution'), ('data_root', 'data'),
                ('public_base_url',api_url), ('web_public_base_url',web_url), ('listen','127.0.0.1'),
                ('port','18080'), ('web_port','18081'), ('external_access_mode','disabled'),
                ('grading_runtime',runtime), ('instructor_runner_image',runner_image), ('bundle_worker_count','4'),
                ('instructor_auth_mode','personal'), ('instructor_assignment_web_enabled','true'), ('roster_bootstrap_mode','web')])
        config.chmod(0o600)
        load_pilot_config(config)
        directory.mkdir(mode=0o700)
        os.rename(config, directory / 'pilot.csv')
    return {'config': str(directory / 'pilot.csv'), 'next_steps': ['initialize instructor administrator', 'load pinned runtime image', 'configure HTTPS proxy', 'start with --isolated', 'backup and preflight'], 'started': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Autograde 기관 운영·백업·복원')
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('--config', required=True)
    backup.add_argument('--destination', required=True)
    backup.add_argument('--release-id', required=True)
    verify = commands.add_parser('verify-backup')
    verify.add_argument('archive')
    restore = commands.add_parser('restore')
    restore.add_argument('archive')
    restore.add_argument('--target', required=True)
    retain = commands.add_parser('prune-backups')
    retain.add_argument('--directory', required=True)
    retain.add_argument('--keep', type=int, default=7)
    retain.add_argument('--apply', action='store_true')
    for name in ('diagnostics', 'preflight', 'check'):
        sub = commands.add_parser(name)
        sub.add_argument('--config', required=True)
    register = commands.add_parser('register-seat')
    register.add_argument('--config', required=True)
    register.add_argument('--seat-id', required=True)
    register.add_argument('--label', required=True)
    register.add_argument('--output', required=True, help='new private device JSON; token is never printed')
    rotate = commands.add_parser('rotate-seat')
    rotate.add_argument('--config', required=True)
    rotate.add_argument('--seat-id', required=True)
    rotate.add_argument('--output', required=True)
    revoke = commands.add_parser('revoke-seat')
    revoke.add_argument('--config', required=True)
    revoke.add_argument('--seat-id', required=True)
    setup = commands.add_parser('configure')
    setup.add_argument('--directory', required=True)
    setup.add_argument('--api-url', required=True)
    setup.add_argument('--web-url', required=True)
    setup.add_argument('--runner-image', required=True)
    setup.add_argument('--runtime', choices=('docker','podman'), default='docker')
    args = parser.parse_args(argv)
    try:
        config = load_pilot_config(args.config) if hasattr(args, 'config') else None
        paths = AppPaths.from_value(config.values['data_root']) if config else None
        if args.command == 'backup':
            result = create_snapshot(paths, args.destination, config=config.source, release_id=args.release_id)
        elif args.command == 'verify-backup':
            result = verify_snapshot(args.archive)
        elif args.command == 'restore':
            result = restore_snapshot(args.archive, args.target)
        elif args.command == 'prune-backups':
            result = prune_snapshots(args.directory, keep=args.keep, apply=args.apply)
        elif args.command == 'preflight':
            result = preflight(config)
        elif args.command in {'diagnostics', 'check'}:
            result = OperationsService(paths).diagnostics()
            if args.command == 'check':
                result = check_running(config, result)
        elif args.command in {'register-seat','rotate-seat'}:
            # Reserve the output before issuing a credential to avoid losing it.
            with credential_output(args.output) as stream:
                registry = SeatRegistry(paths.root)
                def deliver(issued):
                    json.dump({'apiUrl': config.values['public_base_url'], 'seatId': issued['seat_id'],
                               'reportToken': issued['report_token']}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                if args.command == 'register-seat':
                    registry.register(args.seat_id, args.label, deliver=deliver)
                else:
                    registry.rotate(args.seat_id, deliver=deliver)
            result = {'seat_id': args.seat_id, 'credential_written': True}
        elif args.command == 'revoke-seat':
            result = SeatRegistry(paths.root).revoke(args.seat_id)
        else:
            result = configure(args.directory, api_url=args.api_url, web_url=args.web_url, runner_image=args.runner_image, runtime=args.runtime)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        if args.command == 'preflight' and result['blocking_checks']:
            return 2
        if args.command == 'check' and result['status'] != 'ok':
            return 2
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error, tarfile.TarError, KeyError, TypeError) as exc:
        # Exceptions may embed private data or SQL. Use stable support codes.
        print(json.dumps({'ok': False, 'error': type(exc).__name__, 'message': '운영 명령 실패. 서버 중지·경로·백업 파일·설정 조건을 확인하세요.'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
