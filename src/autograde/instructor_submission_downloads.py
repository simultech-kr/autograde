"""Bounded instructor exports of immutable received source, never grading inputs.

Controllers authenticate the instructor before calling this service. Every lookup
is course-fenced before the bundle store or export cache is accessed. Archive
members are streamed without extraction, with BundleStore validation plus hashes.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
import time
import unicodedata
import zipfile

try:
    import fcntl
except ImportError:  # Windows uses the same one-byte cooperative cache lock.
    fcntl = None

from .platform_bundle import BundleError, BundleStorageError
from .platform_state import PlatformNotFound


class InstructorDownloadUnavailable(RuntimeError):
    """Safe public error for an unavailable original or export cache."""


class InstructorDownloadLimitError(ValueError):
    """A whole export exceeds a documented bound; no partial ZIP is returned."""


_UNAVAILABLE = '제출 원본을 내려받을 수 없습니다. 보관 상태를 확인하고 다시 시도하세요.'
_LIMIT = '다운로드 한도를 초과했습니다. 학생별 제출을 개별적으로 내려받으세요.'
_CHUNK = 1024 * 1024


def _component(value):
    """Portable, bounded display label, made unique even for colliding keys."""
    value = str(value)
    label = re.sub(r'[^A-Za-z0-9_-]+', '-', value).strip('-')[:64] or 'submission'
    return label + '-' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]


def _filename(value):
    return ''.join('_' if unicodedata.category(c) in {'Cc', 'Cf'} else c
                   for c in PurePosixPath(value).name)


def _hash_stream(stream):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return 'sha256:' + digest.hexdigest(), size


def _open_regular(path):
    if Path(path).is_symlink():
        raise BundleStorageError('unsafe file link')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BundleStorageError('not a regular file')
        return os.fdopen(descriptor, 'rb')
    except Exception:
        os.close(descriptor)
        raise


class InstructorSubmissionDownloads:
    MAX_BATCH_SUBMISSIONS = 500
    MAX_FILES = 10_000  # Includes explicit directories and batch grouping entries.
    MAX_EXPANDED_BYTES = 128 * 1024 * 1024
    MAX_COMPRESSED_BYTES = 128 * 1024 * 1024
    MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # Must fit the HTTP file-response limit.
    MAX_CACHE_BYTES = 512 * 1024 * 1024
    MAX_CACHE_FILES = 128
    CACHE_GRACE_SECONDS = 600  # Do not evict a response another request may open.

    def __init__(self, state, store, cache_root):
        self.state, self.store = state, store
        # Cache creation is deferred until after the course-scoped DB lookup.
        self.cache_root = Path(cache_root).expanduser().absolute()

    def download(self, course_key, submission_id, file_index=None):
        row, _ = self.state.get_instructor_submission_review(
            course_key=course_key, submission_id=submission_id)
        if file_index is not None and (type(file_index) is not int or file_index < 0):
            raise PlatformNotFound('file unavailable')
        try:
            receipt = self.state.get_bundle_receipt(submission_id)
        except PlatformNotFound:
            raise InstructorDownloadUnavailable(_UNAVAILABLE) from None
        if (receipt.course_key, receipt.assignment_id, receipt.student_id) != (
                course_key, row['assignment_id'], row['student_id']):
            raise InstructorDownloadUnavailable(_UNAVAILABLE)
        self._check_limits(0, 0, receipt.source_size_bytes)
        try:
            artifact = self._artifact(receipt.source_digest)
            self._bounds([artifact])
            if file_index is not None and file_index >= len(artifact.metadata.files):
                raise PlatformNotFound('file unavailable')
            filename = (_component(row['student_key']) + '-' + _component(submission_id) + '.zip'
                        if file_index is None else _filename(artifact.metadata.files[file_index].path))
            with self._cache_build('.zip' if file_index is None else '.bin') as temporary:
                if file_index is None:
                    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as target:
                        self._write_bundle(target, artifact)
                else:
                    with temporary.open('wb') as target, self._archive(artifact) as source:
                        self._copy_member(source, artifact.metadata.files[file_index], target)
                return self._publish(temporary, filename,
                    'application/zip' if file_index is None else 'application/octet-stream')
        except (BundleError, OSError, tarfile.TarError, KeyError, EOFError, zipfile.BadZipFile):
            raise InstructorDownloadUnavailable(_UNAVAILABLE) from None

    def batch(self, course_key, assignment_id, mode='latest'):
        if mode not in {'latest', 'all'}:
            raise ValueError('다운로드 범위가 올바르지 않습니다.')
        rows = self._batch_rows(course_key, assignment_id, mode)
        if len(rows) > self.MAX_BATCH_SUBMISSIONS:
            raise InstructorDownloadLimitError(_LIMIT)
        if not rows:
            raise PlatformNotFound('내려받을 제출이 없습니다.')
        self._check_limits(0, 0, sum(row['source_size_bytes'] or 0 for row in rows))
        try:
            artifacts, expanded, members, compressed = [], 0, 1, 0
            for row in rows:
                if not row['source_digest']:
                    raise BundleStorageError('receipt unavailable')
                artifact = self._artifact(row['source_digest'])
                artifacts.append(artifact)
                expanded += artifact.metadata.expanded_bytes
                members += artifact.metadata.member_count + 1
                compressed += artifact.compressed_bytes
                self._check_limits(expanded, members, compressed)
            manifest = {'schema': 'autograde.instructor-submissions.v1',
                        'course_key': course_key, 'assignment_id': assignment_id,
                        'mode': mode, 'submission_count': len(rows), 'submissions': []}
            for row in rows:
                prefix = _component(row['student_key']) + '/' + _component(row['submission_id'])
                manifest['submissions'].append({
                    key: row[key] for key in ('student_key', 'submission_id', 'receipt_id',
                    'received_at', 'accepted_at', 'version', 'source_digest', 'state', 'score', 'max_score')})
                manifest['submissions'][-1]['directory'] = prefix
            content = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
            self._check_limits(expanded + len(content), members, compressed)
            with self._cache_build('.zip') as temporary:
                with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as target:
                    target.writestr(self._zip_info('manifest.json'), content)
                    for item, artifact in zip(manifest['submissions'], artifacts):
                        prefix = item['directory'] + '/'
                        target.writestr(self._zip_info(prefix, directory=True), b'')
                        self._write_bundle(target, artifact, prefix)
                return self._publish(temporary,
                    _component(assignment_id) + '-submissions-' + mode + '.zip', 'application/zip')
        except (BundleError, OSError, tarfile.TarError, KeyError, EOFError, zipfile.BadZipFile):
            raise InstructorDownloadUnavailable(_UNAVAILABLE) from None

    def _batch_rows(self, course_key, assignment_id, mode):
        # Include failed/latest requests and LEFT JOIN their receipt: corruption
        # must fail the entire export rather than select an older good attempt.
        with self.state._connection() as connection:
            if connection.execute('SELECT 1 FROM bundle_assignment_releases WHERE course_key=? AND assignment_id=?',
                                  (course_key, assignment_id)).fetchone() is None:
                raise PlatformNotFound('assignment not found in this course')
            rows = connection.execute('''WITH attempts AS (
                SELECT r.submission_id,r.student_id,r.assignment_id,r.received_at,r.state,
                    p.student_key,c.receipt_id,c.accepted_at,c.source_digest,c.source_size_bytes,
                    g.score,COALESCE(g.max_score,c.max_score) AS max_score,
                    ROW_NUMBER() OVER (PARTITION BY r.student_id ORDER BY r.received_at,r.rowid) AS version,
                    ROW_NUMBER() OVER (PARTITION BY r.student_id ORDER BY r.received_at DESC,r.rowid DESC) AS newest
                FROM bundle_submission_requests r
                JOIN bundle_assignment_releases a ON a.assignment_id=r.assignment_id
                JOIN platform_students p ON p.id=r.student_id
                LEFT JOIN bundle_submission_receipts c ON c.submission_id=r.submission_id
                    AND c.course_key=a.course_key AND c.assignment_id=r.assignment_id AND c.student_id=r.student_id
                LEFT JOIN bundle_submission_results g ON g.submission_id=r.submission_id
                WHERE a.course_key=? AND a.assignment_id=?
            ) SELECT * FROM attempts WHERE (?='all' OR newest=1)
              ORDER BY student_key,version LIMIT ?''',
                (course_key, assignment_id, mode, self.MAX_BATCH_SUBMISSIONS + 1)).fetchall()
        return [dict(row) for row in rows]

    def _artifact(self, digest):
        if self.store is None:
            raise BundleStorageError('source storage unavailable')
        try:
            artifact = self.store.get(digest)
        except (ValueError, TypeError):
            raise BundleStorageError('invalid source digest') from None
        if artifact.metadata.kind != 'submission':
            raise BundleStorageError('not a student submission')
        # BundleStore rejects unsafe members and leaf symlinks. Also reject a
        # replaced intermediate store directory before reopening its archive.
        try:
            artifact.path.relative_to(self.store.root)
        except ValueError:
            raise BundleStorageError('source outside managed storage') from None
        for directory in (artifact.path.parent, *artifact.path.parents):
            if directory.is_symlink():
                raise BundleStorageError('unsafe source directory')
            if directory == self.store.root:
                break
        return artifact

    def _check_limits(self, expanded, members, compressed):
        if (expanded > self.MAX_EXPANDED_BYTES or members > self.MAX_FILES
                or compressed > self.MAX_COMPRESSED_BYTES):
            raise InstructorDownloadLimitError(_LIMIT)

    def _bounds(self, artifacts):
        self._check_limits(sum(a.metadata.expanded_bytes for a in artifacts),
                           sum(a.metadata.member_count for a in artifacts),
                           sum(a.compressed_bytes for a in artifacts))

    @contextmanager
    def _archive(self, artifact):
        # Verify the descriptor we actually consume; never extract archive paths.
        with _open_regular(artifact.path) as source:
            digest, size = _hash_stream(source)
            if digest != artifact.digest or size != artifact.compressed_bytes:
                raise BundleStorageError('source changed')
            source.seek(0)
            with tarfile.open(fileobj=source, mode='r:gz') as archive:
                yield archive

    @staticmethod
    def _copy_member(source, entry, target):
        member = source.getmember(entry.path)
        if not member.isfile() or member.size != entry.size:
            raise BundleStorageError('source member changed')
        digest, size = hashlib.sha256(), 0
        with source.extractfile(member) as stream:
            while chunk := stream.read(min(_CHUNK, entry.size - size + 1)):
                size += len(chunk)
                if size > entry.size:
                    raise BundleStorageError('source grew')
                digest.update(chunk)
                target.write(chunk)
        if size != entry.size or 'sha256:' + digest.hexdigest() != entry.sha256:
            raise BundleStorageError('source member changed')

    @staticmethod
    def _zip_info(path, *, directory=False, executable=False):
        info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = ((stat.S_IFDIR | 0o755) if directory else
                              (stat.S_IFREG | (0o755 if executable else 0o644))) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        return info

    def _write_bundle(self, target, artifact, prefix=''):
        with self._archive(artifact) as source:
            for directory in artifact.metadata.directories:
                target.writestr(self._zip_info(prefix + directory + '/', directory=True), b'')
            for entry in artifact.metadata.files:
                with target.open(self._zip_info(prefix + entry.path, executable=entry.executable), 'w') as output:
                    self._copy_member(source, entry, output)

    @contextmanager
    def _cache_build(self, suffix):
        root = self.cache_root
        if any(directory.is_symlink() for directory in (root, *root.parents)):
            raise BundleStorageError('unsafe export cache')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir():
            raise BundleStorageError('unsafe export cache')
        root.chmod(0o700)
        descriptor = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        temporary = None
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise BundleStorageError('unsafe export lock')
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b'0')
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            locked = True
            # A single cache lock also makes crash leftovers safe to reclaim.
            for leftover in root.glob('.building-*'):
                if leftover.is_symlink() or leftover.is_file():
                    leftover.unlink()
            handle, name = tempfile.mkstemp(prefix='.building-', suffix=suffix, dir=root)
            os.close(handle)
            temporary = Path(name)
            yield temporary
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if locked and fcntl is None:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            os.close(descriptor)

    def _publish(self, temporary, filename, content_type):
        with _open_regular(temporary) as stream:
            digest, size = _hash_stream(stream)
        if size > self.MAX_DOWNLOAD_BYTES:
            raise InstructorDownloadLimitError(_LIMIT)
        destination = self.cache_root / (digest.removeprefix('sha256:') + temporary.suffix)
        if destination.is_symlink():
            raise BundleStorageError('unsafe cached export')
        if destination.exists():
            with _open_regular(destination) as stream:
                if _hash_stream(stream) != (digest, size):
                    raise BundleStorageError('corrupt cached export')
            os.utime(destination, None, follow_symlinks=False)
        else:
            entries = []
            for candidate in self.cache_root.iterdir():
                if re.fullmatch(r'[0-9a-f]{64}\.(zip|bin)', candidate.name):
                    info = candidate.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        raise BundleStorageError('unsafe cached export')
                    entries.append((info.st_mtime, info.st_size, candidate))
            total = sum(item[1] for item in entries)
            count = len(entries)
            for modified, cached_size, candidate in sorted(entries):
                if total + size <= self.MAX_CACHE_BYTES and count < self.MAX_CACHE_FILES:
                    break
                if modified > time.time() - self.CACHE_GRACE_SECONDS:
                    continue
                candidate.unlink()
                total -= cached_size
                count -= 1
            if total + size > self.MAX_CACHE_BYTES or count >= self.MAX_CACHE_FILES:
                raise InstructorDownloadLimitError('다운로드 보관 한도에 도달했습니다. 잠시 후 다시 시도하세요.')
            temporary.chmod(0o400)
            os.replace(temporary, destination)
        return {'path': destination, 'sha256': digest, 'size_bytes': size,
                'filename': filename, 'content_type': content_type}
