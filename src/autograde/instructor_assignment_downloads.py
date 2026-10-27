"""Course-scoped instructor exports of registered assignment material.

Controllers authenticate teachers before calling this service. Nothing here
publishes material to students or changes an assignment. Upload BLOBs are kept
byte-for-byte; CLI files must match their course-bound registered tree digest.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

from .assignment_admin import (
    DOCUMENT_FIELDS, MAX_DRAFT_BYTES, MAX_ZIP_BYTES, _archive_files, _document,
    _grading_bundle, _zip_files,
)
from .platform_bundle import BundleError, BundleFile, BundleStore
from .platform_state import PlatformNotFound


MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_TREE_MEMBERS = 5000
_LABELS = {
    'starter': '학생용 파일 원본', 'solution': '정답 코드 원본',
    'negative': '오답 코드 원본', 'grading': '채점 자료', 'all': '등록 자료 전체',
}
_MISSING = '등록된 원본 파일이 없습니다.'
_DAMAGED = '등록 파일이 없거나 손상되어 다운로드할 수 없습니다.'
_TOO_LARGE = '다운로드 가능한 파일 용량을 초과했습니다.'


class AssignmentDownloadUnavailable(RuntimeError):
    """A private export cache could not be safely prepared; retry may help."""


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _zip(files):
    if len(files) > 10000 or sum(map(len, files.values())) > MAX_DOWNLOAD_BYTES:
        raise ValueError(_TOO_LARGE)
    content = _archive_files(files)
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError(_TOO_LARGE)
    return content


class InstructorAssignmentDownloads:
    """Read-only downloads; caller must already authorize the course's teacher."""

    def __init__(self, admin, bundle_store=None):
        self.admin = admin
        self.bundle_store = bundle_store

    def catalog(self, course_key, *, assignment_id=None, draft_id=None):
        snapshot = self._snapshot(course_key, assignment_id=assignment_id, draft_id=draft_id)
        _, reasons = self._contents(snapshot)
        return {
            'title': snapshot['document']['title'],
            'assignment_id': snapshot['assignment_id'], 'draft_id': snapshot['draft_id'],
            'origin': snapshot['origin'],
            'items': [dict(kind=kind, label=label, available=available, reason=reason)
                      for kind, label in _LABELS.items()
                      for available, reason in [reasons[kind]]],
        }

    def download(self, course_key, kind, *, assignment_id=None, draft_id=None):
        if kind not in _LABELS:
            raise ValueError('다운로드할 자료 종류를 확인하세요.')
        snapshot = self._snapshot(course_key, assignment_id=assignment_id, draft_id=draft_id)
        contents, reasons = self._contents(snapshot)
        if kind not in contents:
            raise PlatformNotFound(reasons[kind][1])
        try:
            result = self.admin._cached_archive(contents[kind], 'assignment-downloads')
        except OSError:
            raise AssignmentDownloadUnavailable('다운로드 파일을 준비할 수 없습니다. 관리자에게 문의하세요.') from None
        # Stored titles/identifiers never become paths or HTTP header syntax.
        identity = snapshot['assignment_id'] or snapshot['draft_id']
        safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', identity)[:100] or 'assignment'
        return {**result, 'filename': f'{safe_id}-{kind}.zip'}

    def _snapshot(self, course, *, assignment_id, draft_id):
        if bool(assignment_id) == bool(draft_id):
            raise ValueError('과제 또는 초안 하나를 선택하세요.')
        with self.admin.state._connection() as connection:
            connection.execute('BEGIN')
            release = None
            if assignment_id:
                release = dict(self.admin._release(connection, course, assignment_id, allow_deleted=True))
                row = connection.execute(
                    'SELECT * FROM instructor_assignment_drafts WHERE course_key=? AND published_assignment_id=?',
                    (course, assignment_id),
                ).fetchone()
            else:
                row = self.admin._row(connection, course, draft_id)
                assignment_id = row['published_assignment_id']
                if assignment_id:
                    release = dict(self.admin._release(connection, course, assignment_id, allow_deleted=True))
            uploads = {}
            if row is not None:
                draft_id = row['draft_id']
                try:
                    if len(row['document_json']) > 2 * 1024 * 1024:
                        raise ValueError()
                    document = _document(json.loads(row['document_json']))
                except (ValueError, TypeError, KeyError, OverflowError):
                    raise PlatformNotFound('과제 설정을 확인할 수 없습니다. 관리자에게 문의하세요.') from None
                # Do not fetch an unbounded corrupt BLOB before checking length.
                for upload in connection.execute(
                    "SELECT role,metadata_json,length(archive) AS bytes FROM instructor_assignment_uploads "
                    "WHERE draft_id=? AND role IN ('starter','solution','negative')", (draft_id,),
                ):
                    data = None
                    if 0 < upload['bytes'] <= MAX_ZIP_BYTES:
                        data = connection.execute(
                            'SELECT archive FROM instructor_assignment_uploads WHERE draft_id=? AND role=?',
                            (draft_id, upload['role']),
                        ).fetchone()[0]
                    uploads[upload['role']] = (data, upload['metadata_json'])
                origin = 'web'
            else:
                draft_id, origin = None, 'cli'
                # Deliberately omit filesystem paths, runner settings and any
                # unrelated account/session information from the export.
                document = {key: release[key] for key in (
                    'title', 'assignment_key', 'release_id', 'rubric_version',
                    'max_score', 'result_policy', 'opens_at', 'due_at',
                )}
            if release:
                from .assignment_documents import current
                instructions = current(connection, course, assignment_id, with_history=False)
                if instructions is not None:
                    document['description'] = instructions['content']
                document['opens_at'], document['due_at'] = release['opens_at'], release['due_at']
            return dict(document=document, uploads=uploads, release=release,
                        assignment_id=assignment_id, draft_id=draft_id, origin=origin)

    def _contents(self, snapshot):
        contents, source_files = {}, {}
        reasons = {kind: (False, _MISSING) for kind in _LABELS}
        document, release = snapshot['document'], snapshot['release']
        total = 0
        for role, (content, raw_metadata) in snapshot['uploads'].items():
            try:
                if content is None or len(raw_metadata) > 256 * 1024:
                    raise ValueError()
                metadata = json.loads(raw_metadata)
                if metadata['role'] != role or hashlib.sha256(content).hexdigest() != metadata['sha256']:
                    raise ValueError()
                files = _zip_files(content, document['language'])
                main = 'main.c' if document['language'] == 'c' else 'main.cpp'
                size = sum(map(len, files.values()))
                if (sorted(files) != metadata['files'] or size != metadata['size_bytes']
                        or hashlib.sha256(files[main]).hexdigest() != metadata['source_sha256']
                        or total + size > MAX_DRAFT_BYTES):
                    raise ValueError()
                total += size
                contents[role], source_files[role] = content, files
                reasons[role] = (True, '')
            except (ValueError, TypeError, KeyError, OSError):
                reasons[role] = (False, _DAMAGED)

        # Template-mode releases contain real generated delivery artifacts,
        # but a template preview is never a substitute for an uploaded original.
        if release and (snapshot['origin'] == 'cli' or document.get('mode') == 'template'):
            try:
                _, files, _ = self._bundle(release['starter_digest'], 'starter')
                if not files:
                    raise ValueError()
                contents['starter'] = _zip(files)
                reasons['starter'] = (True, '공개본에 등록된 학생용 파일입니다.')
            except (ValueError, OSError, BundleError, tarfile.TarError):
                reasons['starter'] = (False, _DAMAGED)

        if snapshot['origin'] == 'web':
            if 'solution' in source_files and 'negative' in source_files and document.get('tests'):
                try:
                    main = 'main.c' if document['language'] == 'c' else 'main.cpp'
                    files = {f'{role}/{main}': source_files[role][main] for role in ('solution', 'negative')}
                    files['tests.json'] = _json_bytes({key: document[key] for key in ('tests', 'negative_score')})
                    grading = _zip(files)
                    _, configuration = _grading_bundle(grading, document['language'])
                    _document({key: document[key] for key in DOCUMENT_FIELDS} | configuration)
                    contents['grading'] = grading
                    reasons['grading'] = (True, '등록된 정답·오답의 main 파일과 테스트 설정입니다. 채점 ZIP으로 다시 등록할 수 있습니다.')
                except (ValueError, KeyError, TypeError):
                    reasons['grading'] = (False, _DAMAGED)
            else:
                reasons['grading'] = (False, '등록된 정답·오답 원본과 테스트 설정이 모두 필요합니다.')
        else:
            reasons['solution'] = reasons['negative'] = (False, 'CLI 등록본에는 별도 정답·오답 업로드 원본이 보존되지 않습니다.')
            try:
                grading = self._instructor_files(release, 'assessment')
                if release['data_path'] is not None:
                    grading.update(self._instructor_files(release, 'data'))
                contents['grading'] = _zip(grading)
                reasons['grading'] = (True, '등록된 assessment·data 파일입니다. 웹 채점 ZIP 가져오기 형식과 다릅니다.')
            except (ValueError, OSError, BundleError, tarfile.TarError):
                reasons['grading'] = (False, '등록된 채점 번들을 안전하게 확인할 수 없거나 원본이 없습니다.')

        omitted = {kind: reason for kind, (available, reason) in reasons.items() if kind != 'all' and not available}
        try:
            contents['all'] = _zip({f'{kind}.zip': data for kind, data in contents.items()} | {
                'document.json': _json_bytes(document),
                'download-info.json': _json_bytes(dict(
                    assignment_id=snapshot['assignment_id'], draft_id=snapshot['draft_id'],
                    origin=snapshot['origin'], unavailable=omitted,
                )),
            })
            reasons['all'] = (True, '받을 수 있는 등록 파일과 과제 설정을 포함합니다. 미등록·누락 항목은 download-info.json에서 확인하세요.' if omitted else '')
        except ValueError:
            reasons['all'] = (False, _TOO_LARGE)
        return contents, reasons

    def _bundle(self, digest, kind):
        if self.bundle_store is None:
            raise ValueError()
        # Bound disk reads before BundleStore hashes/validates its archive and
        # sidecar. Addresses are derived solely from a strict full digest.
        digest_hex = digest.removeprefix('sha256:')
        if not re.fullmatch('[0-9a-f]{64}', digest_hex):
            raise ValueError()
        directory = self.bundle_store.root / 'sha256' / digest_hex[:2] / digest_hex
        for parent in (self.bundle_store.root, directory.parent.parent, directory.parent, directory):
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise ValueError()
        for name, limit in (('bundle.tar.gz', 25 * 1024 * 1024), ('metadata.json', 16 * 1024 * 1024)):
            info = (directory / name).lstat()
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise ValueError()
        artifact = self.bundle_store.get(digest)
        if (artifact.metadata.kind != kind or artifact.compressed_bytes > 25 * 1024 * 1024
                or artifact.metadata.expanded_bytes > MAX_DOWNLOAD_BYTES
                or artifact.metadata.member_count > 5000):
            raise ValueError()
        # Recheck the exact bytes read after BundleStore's integrity validation.
        with artifact.path.open('rb') as stream:
            content = stream.read(25 * 1024 * 1024 + 1)
        if (len(content) != artifact.compressed_bytes
                or 'sha256:' + hashlib.sha256(content).hexdigest() != artifact.archive_sha256):
            raise ValueError()
        files = {}
        with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as archive:
            for item in artifact.metadata.files:
                entry = archive.getmember(item.path)
                if not entry.isreg() or entry.size != item.size:
                    raise ValueError()
                source = archive.extractfile(entry)
                if source is None:
                    raise ValueError()
                with source:
                    data = source.read(item.size + 1)
                if len(data) != item.size or 'sha256:' + hashlib.sha256(data).hexdigest() != item.sha256:
                    raise ValueError()
                files[item.path] = data
        return content, files, artifact.metadata

    def _instructor_files(self, release, kind):
        # This path comes only from the course-bound release snapshot, never
        # from an HTTP parameter. Legacy files still need the registered hash.
        path = Path(release[f'{kind}_path'] or '')
        root = self.admin.paths.instructor_inputs / kind
        expected = release['assessment_digest' if kind == 'assessment' else 'dataset_digest']
        if path.parent == root and re.fullmatch('[0-9a-f]{64}', path.name):
            content, files, metadata = self._bundle(path.name, kind)
            if self._tree_digest(files, metadata) != expected:
                raise ValueError()
            return {kind + '.tar.gz': content}
        files = self._registered_tree(path, expected)
        return {kind + '/' + name: content for name, content in files.items()}

    @staticmethod
    def _open_directory(parent_fd, parts):
        """Open every component without traversing a symlink; return a new fd."""
        descriptor = os.dup(parent_fd)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        try:
            for part in parts:
                opened = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = opened
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _file_identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    @classmethod
    def _scan_tree(cls, root_fd):
        """Collect bounded metadata before reading any file contents."""
        entries, portable_names, pending, total = {}, set(), [''], 0
        while pending:
            relative = pending.pop()
            directory_fd = cls._open_directory(root_fd, relative.split('/') if relative else [])
            try:
                with os.scandir(directory_fd) as iterator:
                    for item in iterator:
                        if len(entries) >= MAX_TREE_MEMBERS:
                            raise ValueError(_TOO_LARGE)
                        name = relative + '/' + item.name if relative else item.name
                        BundleStore._member_path(name)
                        portable = BundleStore._path_key(name)
                        if portable in portable_names:
                            raise ValueError()
                        portable_names.add(portable)
                        info = item.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(name)
                        elif stat.S_ISREG(info.st_mode):
                            total += info.st_size
                            if total > MAX_DOWNLOAD_BYTES:
                                raise ValueError(_TOO_LARGE)
                        else:
                            raise ValueError()
                        entries[name] = cls._file_identity(info)
            finally:
                os.close(directory_fd)
        return entries

    @classmethod
    def _registered_tree(cls, path, expected):
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError()
        # Pin the root by opening its full ancestor chain without symlinks.
        anchor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            root_fd = cls._open_directory(anchor, path.parts[1:])
        finally:
            os.close(anchor)
        try:
            initial = cls._scan_tree(root_fd)
            files, descriptions, directories = {}, [], []
            for name, identity in initial.items():
                if stat.S_ISDIR(identity[2]):
                    directories.append(name)
                    continue
                components = name.split('/')
                parent_fd = cls._open_directory(root_fd, components[:-1])
                try:
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0)
                    descriptor = os.open(components[-1], flags, dir_fd=parent_fd)
                finally:
                    os.close(parent_fd)
                with os.fdopen(descriptor, 'rb') as stream:
                    if cls._file_identity(os.fstat(stream.fileno())) != identity:
                        raise ValueError()
                    data = stream.read(identity[3] + 1)
                    if (len(data) != identity[3]
                            or cls._file_identity(os.fstat(stream.fileno())) != identity):
                        raise ValueError()
                files[name] = data
                descriptions.append(BundleFile(name, len(data), '', bool(identity[2] & 0o111)))
            if cls._scan_tree(root_fd) != initial:
                raise ValueError()
            # Reuse the same path/type/executable/content hash contract as
            # WorkspaceBuilder, applied to the bytes that will be exported.
            from types import SimpleNamespace
            metadata = SimpleNamespace(directories=directories, files=descriptions)
            if cls._tree_digest(files, metadata) != expected:
                raise ValueError()
            return files
        finally:
            os.close(root_fd)

    @staticmethod
    def _tree_digest(files, metadata):
        """Match WorkspaceBuilder's tree format without reading stored paths."""
        directories = set(metadata.directories)
        for name in [*directories, *files]:
            directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != '.')
        modes = {item.path: item.executable for item in metadata.files}
        digest = hashlib.sha256(b'autograde-instructor-tree-v1\0')
        children_by_parent, files_by_parent = {}, {}
        for name in directories:
            children_by_parent.setdefault(str(PurePosixPath(name).parent), []).append(name)
        for name in files:
            files_by_parent.setdefault(str(PurePosixPath(name).parent), []).append(name)

        pending = ['.']
        while pending:
            parent = pending.pop()
            children = sorted(children_by_parent.get(parent, ()), key=lambda name: name.encode())
            for name in children:
                digest.update(b'D\0' + name.encode() + b'\0')
            for name in sorted(files_by_parent.get(parent, ()), key=lambda name: name.encode()):
                digest.update(b'F\0' + name.encode() + (b'\0X\0' if modes[name] else b'\0-\0'))
                digest.update(str(len(files[name])).encode() + b'\0' + files[name] + b'\0')
            pending.extend(reversed(children))
        return 'sha256:' + digest.hexdigest()
