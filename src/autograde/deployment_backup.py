"""Offline, verified institution snapshots. Never overwrite a running installation.

The lifecycle locks exclude HTTP/workers and IMMEDIATE transactions fence CLI
writes to every SQLite database. Restore preserves the original absolute root:
immutable submission receipts contain absolute artifact paths.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import uuid

from .platform_cli import _exclusive_course_service_lock
from .settings import AppPaths

SCHEMA = "autograde-backup-v1"
MAX_FILES = 500_000
MAX_BYTES = 128 * 1024 ** 3


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("snapshot requires private regular files, without links")
    return info


def _directory(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("snapshot paths must not contain symbolic links")
    if not path.is_dir():
        raise ValueError("existing snapshot directory required")
    return path


def _inventory(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("snapshot refuses symbolic links")
        for name in sorted(files):
            path = Path(directory) / name
            info = _regular(path)
            rel = path.relative_to(root).as_posix()
            # Locks are process state, SQLite sidecars are incorporated by backup().
            if name.startswith('.autograde-platform-') and name.endswith('.lock'):
                continue
            if rel == '.operations/backup-status.json':
                continue
            result[rel] = (info.st_size, info.st_mtime_ns, info.st_ino)
    return result


def _is_database(path):
    with path.open('rb') as source:
        return source.read(16) == b'SQLite format 3\x00'


def _database_check(connection):
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('snapshot database integrity check failed')
    if connection.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('snapshot database foreign key check failed')


def _check_artifact_roots(connection, root):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('bundle_assignment_releases', 'bundle_submission_receipts', 'platform_assignments', 'submission_receipts'):
        if table not in tables:
            continue
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
                   if row[1].endswith('_path')]
        for column in columns:
            if column == 'assignment_path':
                continue  # Repository-relative student source selection, not a host artifact.
            for (value,) in connection.execute(f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                path = Path(value)
                if not value or not path.is_absolute() or not path.is_relative_to(root) or not path.exists():
                    raise ValueError('artifact outside data_root or missing; consolidate assets before backup')


def record_backup_status(root, status, *, snapshot_id=None, error_code=None):
    directory = Path(root) / '.operations'
    if directory.is_symlink():
        raise ValueError('unsafe operations directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    previous = {}
    destination = directory / 'backup-status.json'
    if destination.is_file() and not destination.is_symlink():
        try:
            previous = json.loads(destination.read_text())
        except (ValueError, OSError):
            pass
    payload = {'status': status, 'attempted_at': _now(), 'error_code': error_code,
               'snapshot_id': snapshot_id, 'last_success_at': previous.get('last_success_at')}
    if status == 'verified':
        payload['last_success_at'] = payload['attempted_at']
    fd, name = tempfile.mkstemp(prefix='.backup-status-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def create_snapshot(paths, destination, *, config=None, release_id='unrecorded'):
    root = _directory(paths.root)
    destination = _directory(destination)
    if destination.is_relative_to(root):
        raise ValueError('backups must be outside data_root')
    if not isinstance(release_id, str) or not 1 <= len(release_id) <= 128 or any(ord(c) < 32 for c in release_id):
        raise ValueError('invalid release identifier')
    snapshot_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
    final = destination / f'autograde-backup-{snapshot_id}.tar.gz'
    try:
        with ExitStack() as stack:
            stack.enter_context(_exclusive_course_service_lock(paths, 'portal-runtime'))
            with closing(sqlite3.connect(paths.database.as_uri() + '?mode=ro', uri=True)) as probe:
                tables = {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                courses = set()
                for table in ('admin_courses', 'platform_enrollments', 'platform_assignments', 'bundle_assignment_releases'):
                    if table in tables:
                        courses.update(row[0] for row in probe.execute(f'SELECT DISTINCT course_key FROM {table}'))
            for course in sorted(courses):
                stack.enter_context(_exclusive_course_service_lock(paths, course))
            before = _inventory(root)
            databases = {rel for rel in before if _is_database(root / rel)}
            if 'state.sqlite3' not in databases:
                raise ValueError('platform database missing')
            for rel in sorted(databases):
                conn = stack.enter_context(closing(sqlite3.connect((root / rel).as_uri() + '?mode=rw', uri=True, timeout=2, isolation_level=None)))
                conn.execute('BEGIN IMMEDIATE')
                _database_check(conn)
                _check_artifact_roots(conn, root)
            # SQLite may create sidecars when the write fence is acquired.
            before = _inventory(root)
            sidecars = {rel + suffix for rel in databases for suffix in ('-wal', '-shm', '-journal')}
            with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=destination) as temporary:
                stage = Path(temporary)
                data = stage / 'data'
                data.mkdir(mode=0o700)
                for directory, dirs, _ in os.walk(root):
                    for name in dirs:
                        (data / (Path(directory) / name).relative_to(root)).mkdir(parents=True, exist_ok=True, mode=0o700)
                for rel in sorted(before):
                    if rel in sidecars:
                        continue
                    target = data / rel
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if rel in databases:
                        with closing(sqlite3.connect((root / rel).as_uri() + '?mode=ro', uri=True)) as reader:
                            with closing(sqlite3.connect(target)) as saved:
                                reader.backup(saved)
                                _database_check(saved)
                    else:
                        shutil.copyfile(root / rel, target)
                    target.chmod(0o700 if (root / rel).stat().st_mode & 0o111 else 0o600)
                after = _inventory(root)
                comparable = lambda items: {k: v for k, v in items.items() if k not in sidecars}
                if comparable(before) != comparable(after):
                    raise ValueError('data changed during snapshot; stop all writers and retry')
                if config:
                    config = Path(config).absolute()
                    _regular(config)
                    (stage / 'config').mkdir(mode=0o700)
                    for source, name in ((config, 'pilot.csv'), (config.parent / 'student_roster.csv', 'student_roster.csv')):
                        if source.exists():
                            _regular(source)
                            shutil.copyfile(source, stage / 'config' / name)
                            (stage / 'config' / name).chmod(0o600)
                files = {}
                for rel in _inventory(stage):
                    path = stage / rel
                    files[rel] = {'sha256': _hash(path), 'size': path.stat().st_size,
                                  'executable': bool(path.stat().st_mode & 0o111)}
                if len(files) > MAX_FILES or sum(item['size'] for item in files.values()) > MAX_BYTES:
                    raise ValueError('snapshot exceeds supported size')
                manifest = {'schema': SCHEMA, 'snapshot_id': snapshot_id, 'created_at': _now(),
                    'source_root': str(root), 'release_id': release_id, 'files': files,
                    'databases': sorted('data/' + rel for rel in databases),
                    'directories': sorted(p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_dir())}
                (stage / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True))
                packed = stage / 'snapshot.tar.gz'
                with tarfile.open(packed, 'w:gz', compresslevel=6) as archive:
                    for rel in ['manifest.json', *manifest['directories'], *sorted(files)]:
                        archive.add(stage / rel, arcname=rel, recursive=False)
                packed.chmod(0o600)
                verify_snapshot(packed)
                with packed.open('rb') as stream:
                    os.fsync(stream.fileno())
                os.rename(packed, final)
        record_backup_status(root, 'verified', snapshot_id=snapshot_id)
        return {'snapshot_id': snapshot_id, 'archive': str(final), 'sha256': _hash(final),
                'files': len(files), 'databases': len(databases), 'size_bytes': final.stat().st_size}
    except Exception:
        record_backup_status(root, 'failed', error_code='backup_failed')
        raise


def _unpack(archive, destination):
    _directory(archive.parent)
    _regular(archive)
    with tarfile.open(archive, 'r:gz') as source:
        seen = set()
        total = 0
        for item in source:
            name = PurePosixPath(item.name)
            if (not (item.isfile() or item.isdir()) or name.is_absolute() or '..' in name.parts or '\\' in item.name
                    or str(name) != item.name or not name.parts or item.name in seen or item.size < 0):
                raise ValueError('unsafe or duplicate snapshot entry')
            seen.add(item.name)
            total += item.size
            if len(seen) > MAX_FILES + 1 or total > MAX_BYTES or (item.name == 'manifest.json' and item.size > 128 * 1024 ** 2):
                raise ValueError('snapshot exceeds supported size')
            target = destination / item.name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with source.extractfile(item) as incoming, target.open('xb') as output:
                shutil.copyfileobj(incoming, output)
            target.chmod(0o600)
    manifest = json.loads((destination / 'manifest.json').read_text())
    if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA or not isinstance(manifest.get('files'), dict):
        raise ValueError('unsupported snapshot manifest')
    if not isinstance(manifest.get('snapshot_id'), str) or not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{12}', manifest['snapshot_id']):
        raise ValueError('invalid snapshot identifier')
    directories = manifest.get('directories', [])
    if not isinstance(directories, list) or any(not isinstance(d, str) or not (d in {'data','config'} or d.startswith(('data/','config/'))) for d in directories):
        raise ValueError('invalid directory inventory')
    if seen != {'manifest.json', *manifest['files'], *directories}:
        raise ValueError('snapshot inventory mismatch')
    for rel, expected in manifest['files'].items():
        if not rel.startswith(('data/', 'config/')):
            raise ValueError('unsupported snapshot path')
        path = destination / rel
        if (not isinstance(expected, dict) or set(expected) != {'sha256','size','executable'}
                or not isinstance(expected['executable'], bool) or not isinstance(expected['size'], int)
                or not isinstance(expected['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', expected['sha256'])):
            raise ValueError('invalid file manifest')
        if path.stat().st_size != expected['size'] or _hash(path) != expected['sha256']:
            raise ValueError('snapshot content checksum mismatch')
        if expected.get('executable') is True:
            path.chmod(0o700)
    databases = manifest.get('databases')
    if not isinstance(databases, list) or 'data/state.sqlite3' not in databases:
        raise ValueError('snapshot platform database missing')
    for rel in databases:
        if rel not in manifest['files'] or not rel.startswith('data/'):
            raise ValueError('snapshot database inventory mismatch')
        with closing(sqlite3.connect((destination / rel).as_uri() + '?mode=ro', uri=True)) as db:
            _database_check(db)
    return manifest


def verify_snapshot(archive):
    archive = Path(archive).absolute()
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=archive.parent) as temporary:
        manifest = _unpack(archive, Path(temporary))
    return {key: manifest[key] for key in ('schema', 'snapshot_id', 'created_at', 'source_root', 'release_id')}


def restore_snapshot(archive, target):
    """Restore on a replacement host at the original path; never edit receipts."""
    archive = Path(archive).absolute()
    target = Path(target).absolute()
    _directory(target.parent)
    if os.path.lexists(target):
        raise ValueError('restore target must not exist; preserve the previous installation first')
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=target.parent) as temporary:
        stage = Path(temporary)
        manifest = _unpack(archive, stage)
        if str(target) != manifest.get('source_root'):
            raise ValueError('immutable receipts require restoring the original absolute data_root')
        config_target = target.parent / (target.name + '.restored-config-' + manifest['snapshot_id'])
        if (stage / 'config').exists() and os.path.lexists(config_target):
            raise ValueError('restored configuration destination already exists')
        # Directory rename is on the same filesystem and leaves no half-restored root.
        os.rename(stage / 'data', target)
        if (stage / 'config').exists():
            os.rename(stage / 'config', config_target)
    record_backup_status(target, 'verified', snapshot_id=manifest['snapshot_id'])
    return {'restored': True, 'snapshot_id': manifest['snapshot_id'], 'data_root': str(target),
            'release_id': manifest['release_id'], 'configuration': str(config_target) if config_target.exists() else None,
            'activation': 'stopped; verify release, runtime image digests and representative grading before serving'}


def prune_snapshots(directory, *, keep=7, apply=False):
    """Only verified snapshots created by this tool are eligible for retention."""
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 2:
        raise ValueError('retain at least two verified snapshots')
    directory = _directory(directory)
    candidates = []
    roots = set()
    for path in sorted(directory.glob('autograde-backup-*.tar.gz')):
        result = verify_snapshot(path)
        if path.name != 'autograde-backup-' + result['snapshot_id'] + '.tar.gz':
            raise ValueError('snapshot filename mismatch')
        roots.add(result['source_root'])
        candidates.append(path)
    if len(roots) > 1:
        raise ValueError('retention requires a dedicated backup directory per installation')
    removed = candidates[:-keep]
    if apply:
        for path in removed:
            path.unlink()
    return {'apply': apply, 'retained': len(candidates) - len(removed), 'eligible': [p.name for p in removed]}
