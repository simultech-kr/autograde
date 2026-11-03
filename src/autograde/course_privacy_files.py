"""Managed student file erasure under the global privacy publication fence."""
import hashlib
import json
from pathlib import Path
import re

from .course_deletion_files import CourseFilePlan, HEX, _quote, _regular_tree, plan_course_files
from .platform_state import PlatformConflict


def _surviving_text(db, rows):
    pieces = []
    for record in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        table = record[0]
        if table in {'admin_course_deletions', 'admin_course_privacy_archives'}:
            continue
        excluded = {row['__rowid__'] for row in rows.get(table, ())}
        for row in db.execute(f'SELECT rowid AS __rowid__,* FROM {_quote(table)}'):
            if row[0] not in excluded:
                pieces.extend(value for value in row[1:] if isinstance(value, str))
    return '\n'.join(pieces)


def plan_privacy_files(paths, db, course_key, rows):
    baseline = plan_course_files(paths, db, course_key, rows)
    namespace = hashlib.sha256(course_key.encode()).hexdigest()
    # Keep public teaching content. Private objects and Git history are handled
    # separately; the general course-deletion plan would remove the whole tree.
    excluded = {f'content/objects/{namespace}', f'content/git/{namespace}.git'}
    candidates = {entry['relative'] for entry in baseline.entries} - excluded
    # Older checkpoint indexes predate owner.json. Assignment releases remain
    # public, so they aren't part of the personal row-deletion closure.
    assignments = {row[0] for row in db.execute('SELECT assignment_id FROM bundle_assignment_releases WHERE course_key=?', (course_key,))}
    checkpoint_root = paths.database.parent / (paths.database.name + '.learning-checkpoints')
    if checkpoint_root.is_dir():
        for folder in checkpoint_root.iterdir():
            index = folder / 'index.json'
            if not HEX.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                raise PlatformConflict('학생 작업 저장 경로를 확인하세요.')
            if index.is_symlink():
                raise PlatformConflict('학생 작업 저장 인덱스가 링크입니다.')
            if index.is_file():
                if index.stat().st_size > 1024 * 1024:
                    raise PlatformConflict('학생 작업 저장 인덱스를 확인하세요.')
                ledger = json.loads(index.read_text())
                ids = {item.get('assignment_id') for item in ledger.get('checkpoints', ()) if isinstance(item, dict)}
                if ids & assignments:
                    if not ids <= assignments:
                        raise PlatformConflict('여러 수업의 작업이 하나의 저장 공간에 있습니다. 운영자가 먼저 분리해야 합니다.')
                    candidates.add(str(folder.relative_to(paths.root)))
            elif not (folder / 'owner.json').is_file() and any(p.name != 'lock' for p in folder.iterdir()):
                raise PlatformConflict('소유 수업을 확인할 수 없는 작업 저장 공간이 있습니다. 운영자가 먼저 확인해야 합니다.')
    surviving = _surviving_text(db, rows)
    bundle_root = paths.bundles / 'sha256'
    if bundle_root.is_symlink():
        raise PlatformConflict('제출 원본 저장소가 링크입니다.')
    shared = 0
    if paths.bundles.is_dir():
        for temporary in paths.bundles.iterdir():
            if re.fullmatch(r'\.(incoming|create)-[A-Za-z0-9_-]{1,100}', temporary.name):
                candidates.add(str(temporary.relative_to(paths.root)))
    if bundle_root.is_dir():
        for shard in bundle_root.iterdir():
            if shard.is_symlink() or not shard.is_dir() or not re.fullmatch(r'[0-9a-f]{2}', shard.name):
                raise PlatformConflict('제출 원본 저장소 구조를 확인하세요.')
            for folder in shard.iterdir():
                if not HEX.fullmatch(folder.name) or folder.name[:2] != shard.name or folder.is_symlink():
                    raise PlatformConflict('제출 원본 경로를 확인하세요.')
                metadata = folder / 'metadata.json'
                if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > 4 * 1024 * 1024:
                    raise PlatformConflict('제출 원본 메타데이터를 확인하세요.')
                value = json.loads(metadata.read_text())
                if value.get('manifest', {}).get('kind') != 'submission':
                    continue
                # Course-specific references are about to be erased. An object
                # still referenced by another course remains that course's data.
                if folder.name in surviving:
                    shared += 1
                else:
                    candidates.add(str(folder.relative_to(paths.root)))
    content_root = paths.root / 'content' / 'objects' / namespace
    if content_root.is_symlink():
        raise PlatformConflict('강의자료 원본 경로가 링크입니다.')
    if content_root.is_dir():
        for item in content_root.iterdir():
            if re.fullmatch(r'\.pending-[0-9a-f]{32}', item.name):
                candidates.add(str(item.relative_to(paths.root)))
                continue
            if not HEX.fullmatch(item.name) or item.is_symlink() or not item.is_file():
                raise PlatformConflict('강의자료 원본 경로를 확인하세요.')
            if item.name not in surviving:
                candidates.add(str(item.relative_to(paths.root)))
    # This is a regenerable cross-course export cache, never an original. All
    # export builders hold the same global shared fence through publication.
    cache = paths.cache / 'instructor-submission-downloads'
    if cache.is_symlink():
        raise PlatformConflict('제출 내보내기 임시 폴더가 링크입니다.')
    if cache.is_dir():
        for item in cache.iterdir():
            if re.fullmatch(r'([0-9a-f]{64}|\.building-[A-Za-z0-9_-]{1,100})\.(zip|bin)', item.name):
                candidates.add(str(item.relative_to(paths.root)))
            elif item.name.startswith('.building-'):
                raise PlatformConflict('중단된 제출 내보내기 경로를 확인할 수 없습니다. 운영자에게 확인을 요청하세요.')
    material_root = paths.root / 'student-materials'
    if material_root.is_dir():
        for item in material_root.iterdir():
            if re.fullmatch(r'\.upload-[0-9a-f]{32}', item.name):
                candidates.add(str(item.relative_to(paths.root)))
    if baseline.summary['external_paths_preserved']:
        raise PlatformConflict('관리 저장소 밖에 학생 제출 원본이 있습니다. 원본을 관리 저장소로 이전한 뒤 보관하세요.')
    # Snapshot paths can be shared by course references; delete a source only
    # after its last reference disappears, never based solely on a filename.
    for records in rows.values():
        for row in records:
            for field, value in row.items():
                if not field.endswith('_path') or not isinstance(value, str):
                    continue
                try:
                    parts = Path(value).relative_to(paths.root).parts
                except ValueError:
                    continue
                if len(parts) >= 2 and parts[0] == 'snapshots':
                    relative = str(Path(*parts[:2]))
                    if str(paths.root / relative) not in surviving:
                        candidates.add(relative)
    plan = CourseFilePlan(paths, course_key, candidates, shared=shared, purpose='privacy_archive')
    plan._refresh = lambda: plan_privacy_files(paths, db, course_key, rows)
    return plan


def cleanup_extra(paths, state, course_key):
    """Idempotent post-commit cleanup; failures remain visible in the journal."""
    _cleanup_content_git(paths, state, course_key)
    _cleanup_rubric(paths, course_key)


def _cleanup_content_git(paths, state, course_key):
    from .content_store import ContentService
    content = ContentService(state, paths.root / 'content')
    repository = paths.root / 'content' / 'git' / (hashlib.sha256(course_key.encode()).hexdigest() + '.git')
    if not repository.exists() and not repository.is_symlink():
        return
    _regular_tree(repository)
    with state._connection() as db:
        allowed = {'refs/releases/' + row[0] for row in db.execute(
            'SELECT r.release_id FROM content_releases r JOIN content_collections c USING(collection_id) WHERE c.course_key=?', (course_key,))}
    actual = content._git(repository, 'for-each-ref', '--format=%(refname)').decode('ascii').splitlines()
    for ref in actual:
        if not re.fullmatch(r'refs/releases/[A-Za-z0-9_-]{1,200}', ref):
            raise PlatformConflict('강의자료 Git 참조를 확인할 수 없습니다.')
        if ref not in allowed:
            content._git(repository, 'update-ref', '-d', ref)
    content._git(repository, 'reflog', 'expire', '--expire=now', '--all')
    content._git(repository, 'gc', '--prune=now')
    if content._git(repository, 'fsck', '--no-reflogs', '--unreachable').strip():
        raise PlatformConflict('개별 강의자료 Git 객체 정리를 확인하지 못했습니다.')


def _cleanup_rubric(paths, course_key):
    from .rubric_store import RubricStore
    path = paths.root / 'rubric-catalog.sqlite3'
    if not path.exists() and not path.is_symlink():
        return
    store = RubricStore(path)
    with store._connect() as db:
        db.execute('PRAGMA secure_delete=ON')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS rubric_course_privacy_archives(course_key TEXT PRIMARY KEY, archived_at TEXT NOT NULL)')
        from .platform_state import utc_iso
        db.execute('INSERT OR IGNORE INTO rubric_course_privacy_archives VALUES(?,?)', (course_key, utc_iso()))
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('rubric_web_assessments', 'rubric_assessments', 'test_evidence_runs', 'source_receipts', 'rubric_audit'):
            if table in tables:
                for action in ('INSERT', 'UPDATE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS privacy_closed_{action.lower()}_{table} BEFORE {action} ON {table} '
                               'WHEN EXISTS(SELECT 1 FROM rubric_course_privacy_archives WHERE course_key=NEW.course_key) '
                               "BEGIN SELECT RAISE(ABORT,'course_privacy_closed'); END")
                db.execute(f'DELETE FROM {table} WHERE course_key=?', (course_key,))
    with store._connect() as db:
        row = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if row and row[0]:
            raise PlatformConflict('루브릭 데이터베이스의 이전 기록 정리가 대기 중입니다.')
