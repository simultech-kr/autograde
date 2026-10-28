"""Recoverable, course-scoped file removal coordinated with a SQLite deletion.

The database owns deletion intent. Files move into a private same-filesystem
staging area before commit; startup restores uncommitted moves or finishes a
committed deletion. Global CAS caches survive online deletion: another course
can be between writing immutable bytes and committing its database reference.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
from contextlib import contextmanager

from .platform_state import PlatformConflict

HEX = re.compile(r"[0-9a-f]{64}")
ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _regular_tree(path):
    """Reject links/devices and capture the identity used at staging time."""
    info = path.lstat()
    if stat.S_ISREG(info.st_mode):
        return (info.st_dev, info.st_ino), 1, info.st_size
    if not stat.S_ISDIR(info.st_mode):
        raise PlatformConflict('교과목 파일에 링크 또는 특수 파일이 있어 삭제를 중단했습니다.')
    count, size = 0, 0
    for child in path.iterdir():
        _, n, b = _regular_tree(child)
        count += n
        size += b
    return (info.st_dev, info.st_ino), count, size


def _allowed(relative, database_name='state.sqlite3'):
    p = Path(relative)
    if p.is_absolute() or any(x in {'', '.', '..'} for x in p.parts):
        return False
    parts = p.parts
    if len(parts) == 4 and parts[:2] == ('bundles', 'sha256'):
        return bool(HEX.fullmatch(parts[3]) and parts[2] == parts[3][:2])
    if len(parts) == 3 and parts[0] == 'instructor-inputs':
        return parts[1] in {'assessment', 'data'} and bool(HEX.fullmatch(parts[2]))
    if len(parts) == 2 and parts[0] in {'workspaces', 'snapshots', 'reports'}:
        return bool(re.fullmatch(r'[A-Za-z0-9_-]+(?:\.json)?', parts[1]))
    if len(parts) == 2 and parts[0] == 'student-materials':
        return bool(re.fullmatch(r'mat_[0-9a-f]+-[0-9]+-[0-9a-f]{64}\.(pdf|zip)', parts[1]))
    if len(parts) == 2 and parts[0] == 'exam-papers':
        return bool(re.fullmatch(r'ep_[0-9a-f]+-[0-9a-f]{64}\.pdf', parts[1]))
    if len(parts) == 2 and parts[0] == 'ide-extensions':
        return bool(re.fullmatch(r'[0-9a-f]{64}\.vsix', parts[1]))
    if len(parts) == 3 and parts[:2] == ('content', 'objects'):
        return bool(HEX.fullmatch(parts[2]))
    if len(parts) == 3 and parts[:2] == ('content', 'git'):
        return bool(re.fullmatch(r'[0-9a-f]{64}\.git', parts[2]))
    return len(parts) == 2 and parts[0] == database_name + '.learning-checkpoints' and bool(HEX.fullmatch(parts[1]))


def _validate_path(root, relative, database_name):
    if not _allowed(relative, database_name):
        raise PlatformConflict('교과목 삭제 경로를 확인할 수 없습니다.')
    current = root
    for part in Path(relative).parts:
        if current.is_symlink():
            raise PlatformConflict('교과목 삭제 경로에 링크가 있습니다.')
        current = current / part
    if current.is_symlink():
        raise PlatformConflict('교과목 삭제 대상에 링크가 있습니다.')
    return current


def _sync(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _writable_parent(folder):
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PlatformConflict('삭제 대상의 상위 폴더 소유권을 확인할 수 없습니다.')
    original = stat.S_IMODE(info.st_mode)
    try:
        folder.chmod(original | stat.S_IWUSR | stat.S_IXUSR)
        yield
    finally:
        current = folder.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise PlatformConflict('삭제 중 상위 폴더가 변경되었습니다.')
        folder.chmod(original)


def _move(source, destination):
    # macOS requires write permission on a moved directory itself when its
    # parent changes. Preserve immutable bundle modes on the new inode path.
    before = source.lstat()
    directory = stat.S_ISDIR(before.st_mode)
    original = stat.S_IMODE(before.st_mode)
    if directory:
        source.chmod(original | stat.S_IWUSR | stat.S_IXUSR)
    try:
        os.replace(source, destination)
    finally:
        current = destination if destination.exists() else source
        info = current.lstat()
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise PlatformConflict('이동 중 삭제 대상이 변경되었습니다.')
        if directory:
            current.chmod(original)


def _write_manifest(folder, manifest):
    temporary = folder / 'manifest.tmp'
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, folder / 'manifest.json')
    _sync(folder)


class CourseFilePlan:
    def __init__(self, paths, course_key, candidates, *, shared=0, external=0):
        self.root = paths.root
        self.database_name = paths.database.name
        self.course_key = course_key
        self.entries = []
        self.folder = None
        self._locks = []
        self._course_guard = None
        self._refresh = None
        count, size = 0, 0
        for relative in sorted(candidates):
            target = _validate_path(self.root, relative, self.database_name)
            if not target.exists():
                continue
            identity, n, b = _regular_tree(target)
            parent = target.parent.lstat()
            self.entries.append({'relative': relative, 'identity': list(identity),
                                 'mode': stat.S_IMODE(target.lstat().st_mode),
                                 'parent_identity': [parent.st_dev, parent.st_ino],
                                 'parent_mode': stat.S_IMODE(parent.st_mode)})
            count += n
            size += b
        self.summary = {'file_roots': len(self.entries), 'files': count, 'bytes': size,
                        'shared_objects_preserved': shared, 'external_paths_preserved': external}

    def stage(self, deletion_id):
        if not ID.fullmatch(deletion_id):
            raise ValueError('invalid deletion identifier')
        from .course_file_guard import course_file_guard
        guard = course_file_guard(self.root, self.course_key, exclusive=True)
        guard.__enter__()
        self._course_guard = guard
        if self._refresh is not None:
            refreshed = self._refresh()
            if refreshed.entries != self.entries or refreshed.summary != self.summary:
                raise PlatformConflict('삭제 확인 중 저장된 파일이 변경되었습니다. 삭제 범위를 다시 확인하세요.')
        try:
            for entry in self.entries:
                if Path(entry['relative']).parts[0] != self.database_name + '.learning-checkpoints':
                    continue
                owner = _validate_path(self.root, entry['relative'], self.database_name)
                if not (owner / 'lock').exists() and not (owner / 'lock').is_symlink():
                    continue  # Course guard also covers an unfinished first owner creation.
                descriptor = os.open(owner / 'lock', os.O_RDWR | os.O_NOFOLLOW)
                self._locks.append(descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._unlock()
            raise PlatformConflict('작업 저장이 진행 중입니다. 잠시 후 삭제를 다시 시도하세요.') from exc
        parent = self.root / '.course-deletions'
        if self.root.is_symlink() or parent.is_symlink():
            raise PlatformConflict('삭제 대기 폴더가 링크입니다.')
        parent.mkdir(mode=0o700, exist_ok=True)
        folder = parent / deletion_id
        folder.mkdir(mode=0o700)
        self.folder = folder
        (self.folder / 'files').mkdir(mode=0o700)
        manifest = {'schema': 1, 'job_id': deletion_id, 'course_key': self.course_key,
                    'database_name': self.database_name, 'entries': self.entries}
        # Persist the full intent before the first move. Recovery checks which
        # source/destination exists, so a crash between any two moves is safe.
        _write_manifest(self.folder, manifest)
        _sync(parent)
        for index, entry in enumerate(self.entries):
            source = _validate_path(self.root, entry['relative'], self.database_name)
            info = source.lstat()
            if [info.st_dev, info.st_ino] != entry['identity']:
                raise PlatformConflict('삭제 중 파일이 변경되었습니다. 새로고침 후 다시 확인하세요.')
            _regular_tree(source)
            with _writable_parent(source.parent):
                _move(source, self.folder / 'files' / str(index))
                _sync(source.parent)
            _sync(self.folder / 'files')

    def rollback(self):
        try:
            if self.folder is not None and self.folder.exists():
                _finish(self.root, self.folder, committed=False)
        finally:
            self._unlock()

    def purge(self):
        try:
            if self.folder is not None and self.folder.exists():
                _finish(self.root, self.folder, committed=True)
        finally:
            self._unlock()

    def _unlock(self):
        for descriptor in self._locks:
            os.close(descriptor)
        self._locks.clear()
        if self._course_guard is not None:
            self._course_guard.__exit__(None, None, None)
            self._course_guard = None


def plan_course_files(paths, db, course_key, rows):
    """Call under the same BEGIN IMMEDIATE that captures deletion rows."""
    target_text = []
    for entries in rows.values():
        target_text.extend(value for row in entries for value in row.values() if isinstance(value, str))
    surviving = []
    for record in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        table = record[0]
        if table == 'admin_course_deletions':
            continue
        excluded = {row['__rowid__'] for row in rows.get(table, ())}
        cursor = db.execute(f'SELECT rowid AS __rowid__, * FROM {_quote(table)}')
        for row in cursor:
            if row[0] not in excluded:
                surviving.extend(value for value in row[1:] if isinstance(value, str))
    surviving_text = '\n'.join(surviving)
    candidates, shared, external = set(), set(), set()
    digests = set(HEX.findall('\n'.join(target_text)))
    for digest in digests:
        bundle = f'bundles/sha256/{digest[:2]}/{digest}'
        vsix = f'ide-extensions/{digest}.vsix'
        for relative in (bundle, vsix):
            if (paths.root / relative).exists():
                shared.add(relative)
    # Explicit persisted paths also cover the instructor materialization cache.
    for entries in rows.values():
        for row in entries:
            for key, value in row.items():
                if not key.endswith('_path') or not isinstance(value, str) or not Path(value).is_absolute():
                    continue
                try:
                    parts = Path(value).relative_to(paths.root).parts
                except ValueError:
                    external.add(value)
                    continue
                if parts[:2] == ('bundles', 'sha256') and len(parts) >= 4:
                    relative = str(Path(*parts[:4]))
                elif parts and parts[0] == 'instructor-inputs' and len(parts) >= 3:
                    relative = str(Path(*parts[:3]))
                elif parts and parts[0] in {'workspaces', 'snapshots', 'reports'} and len(parts) >= 2:
                    relative = str(Path(*parts[:2]))
                else:
                    continue
                if (parts[0] in {'bundles', 'instructor-inputs', 'snapshots'}
                        or str(paths.root / relative) in surviving_text):
                    shared.add(relative)
                else:
                    candidates.add(relative)
    for table, folder, column, pattern in (
        ('student_materials', 'student-materials', 'material_id', r'mat_[0-9a-f]+'),
        ('exam_papers', 'exam-papers', 'paper_id', r'ep_[0-9a-f]+'),
    ):
        parent = paths.root / folder
        if parent.is_symlink():
            raise PlatformConflict('자료 폴더가 링크입니다.')
        for row in rows.get(table, ()):
            identifier = row.get(column, '')
            if re.fullmatch(pattern, identifier) and parent.is_dir():
                candidates.update(str(p.relative_to(paths.root)) for p in parent.glob(identifier + '-*'))
    namespace = hashlib.sha256(course_key.encode()).hexdigest()
    candidates.update((f'content/objects/{namespace}', f'content/git/{namespace}.git'))
    assignments = {row['assignment_id'] for row in rows.get('bundle_assignment_releases', ())}
    checkpoint_root = paths.database.parent / (paths.database.name + '.learning-checkpoints')
    if checkpoint_root.is_symlink():
        raise PlatformConflict('작업 저장 폴더가 링크입니다.')
    if checkpoint_root.is_dir():
        for folder in checkpoint_root.iterdir():
            if not HEX.fullmatch(folder.name) or not folder.is_dir() or folder.is_symlink():
                continue
            owner = folder / 'owner.json'
            if owner.is_symlink():
                raise PlatformConflict('작업 저장 소유권 기록이 링크입니다.')
            if owner.is_file() and owner.stat().st_size <= 4096:
                metadata = json.loads(owner.read_text())
                if metadata.get('course_key') == course_key:
                    candidates.add(str(folder.relative_to(paths.root)))
                    continue
            index = folder / 'index.json'
            if index.is_symlink():
                raise PlatformConflict('작업 저장 인덱스가 링크입니다.')
            if not index.is_file() or index.stat().st_size > 1024 * 1024:
                continue
            ledger = json.loads(index.read_text())
            ids = {item.get('assignment_id') for item in ledger.get('checkpoints', ()) if isinstance(item, dict)}
            if ids and ids <= assignments:
                candidates.add(str(folder.relative_to(paths.root)))
    for table in ('bundle_submission_requests', 'submission_requests'):
        for row in rows.get(table, ()):
            identifier = row.get('submission_id', '')
            if ID.fullmatch(identifier):
                candidates.update((f'workspaces/{identifier}', f'reports/{identifier}.json'))
    candidates -= shared
    plan = CourseFilePlan(paths, course_key, candidates, shared=len(shared), external=len(external))
    plan._refresh = lambda: plan_course_files(paths, db, course_key, rows)
    return plan


def _finish(root, folder, *, committed):
    if root.is_symlink() or folder.parent != root / '.course-deletions' or folder.is_symlink():
        raise PlatformConflict('삭제 복구 폴더가 올바르지 않습니다.')
    manifest_path = folder / 'manifest.json'
    if not committed and not manifest_path.exists() and not manifest_path.is_symlink():
        # No move can precede the durable manifest. An interrupted mkdir/write
        # may leave only an empty files directory and a temporary JSON file.
        children = list(folder.iterdir())
        if (all(p.name in {'files', 'manifest.tmp'} and not p.is_symlink() for p in children)
                and (not (folder / 'files').exists() or not list((folder / 'files').iterdir()))):
            shutil.rmtree(folder)
            _sync(folder.parent)
            return
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise PlatformConflict('삭제 복구 기록을 확인할 수 없습니다.')
    manifest = json.loads(manifest_path.read_text())
    if manifest['schema'] != 1 or manifest['job_id'] != folder.name or not ID.fullmatch(folder.name):
        raise PlatformConflict('삭제 복구 기록이 올바르지 않습니다.')
    if committed:
        from .course_file_guard import mark_course_files_deleted
        mark_course_files_deleted(root, manifest['course_key'])
    if (folder / 'files').is_symlink():
        raise PlatformConflict('삭제 복구 파일 폴더가 링크입니다.')
    for index, entry in reversed(list(enumerate(manifest['entries']))):
        source = _validate_path(root, entry['relative'], manifest['database_name'])
        staged = folder / 'files' / str(index)
        if not staged.exists() and not staged.is_symlink():
            continue
        identity, _, _ = _regular_tree(staged)
        if list(identity) != entry['identity']:
            raise PlatformConflict('삭제 대기 파일이 변경되어 복구를 중단했습니다.')
        if committed:
            if staged.is_dir():
                # Instructor snapshots are read-only. Change only owned staged
                # directories; never follow a symlink or alter external files.
                for base, dirs, _files in os.walk(staged, followlinks=False):
                    Path(base).chmod(0o700)
                shutil.rmtree(staged)
            else:
                staged.unlink()
        else:
            if source.exists() or source.is_symlink():
                raise PlatformConflict('원래 위치가 변경되어 삭제 복구를 중단했습니다.')
            source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with _writable_parent(source.parent):
                _move(staged, source)
                _sync(source.parent)
        _sync(folder / 'files')
    # Also restore CAS shard permissions after a crash between chmod/rename.
    for entry in manifest['entries']:
        source = _validate_path(root, entry['relative'], manifest['database_name'])
        if not committed and source.exists():
            current = source.lstat()
            if [current.st_dev, current.st_ino] == entry['identity']:
                source.chmod(entry['mode'])
        info = source.parent.lstat()
        if [info.st_dev, info.st_ino] != entry['parent_identity'] or info.st_uid != os.getuid():
            raise PlatformConflict('삭제 복구 중 상위 폴더 소유권이 변경되었습니다.')
        source.parent.chmod(entry['parent_mode'])
    shutil.rmtree(folder)
    _sync(folder.parent)


def recover_course_files(paths, state):
    """Must run before starting request handlers or grading workers."""
    parent = paths.root / '.course-deletions'
    if parent.is_symlink():
        raise PlatformConflict('삭제 복구 폴더가 링크입니다.')
    if not parent.exists():
        return 0
    recovered = 0
    for folder in parent.iterdir():
        if folder.is_symlink() or not folder.is_dir() or not ID.fullmatch(folder.name):
            raise PlatformConflict('삭제 복구 항목을 확인할 수 없습니다.')
        with state._connection() as db:
            committed = db.execute('SELECT 1 FROM admin_course_deletions WHERE job_id=?', (folder.name,)).fetchone() is not None
        if (folder / 'manifest.json').is_symlink():
            raise PlatformConflict('삭제 복구 기록이 링크입니다.')
        manifest = json.loads((folder / 'manifest.json').read_text()) if (folder / 'manifest.json').is_file() else None
        if manifest:
            from .course_file_guard import course_file_guard
            with course_file_guard(paths.root, manifest['course_key'], exclusive=True):
                _finish(paths.root, folder, committed=committed)
        else:
            _finish(paths.root, folder, committed=committed)
        recovered += 1
    return recovered
