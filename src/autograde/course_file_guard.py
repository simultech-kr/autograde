"""Cross-process course file fencing shared by writers and permanent deletion."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

from .platform_state import PlatformConflict


class CourseFileBusy(PlatformConflict):
    pass


class CourseFilesDeleted(PlatformConflict):
    pass


def _directory(root, course_key):
    root = Path(root).absolute()
    if not isinstance(course_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', course_key):
        raise PlatformConflict('교과목 파일 식별자가 올바르지 않습니다.')
    if root.is_symlink() or not root.is_dir():
        raise PlatformConflict('교과목 파일 저장 경로를 확인할 수 없습니다.')
    folder = root / '.course-file-locks'
    try:
        folder.mkdir(mode=0o700)
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    except FileExistsError:
        pass
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PlatformConflict('교과목 파일 잠금 경로를 확인할 수 없습니다.')
    folder.chmod(0o700)
    return folder, hashlib.sha256(course_key.encode()).hexdigest()


@contextmanager
def course_file_guard(root, course_key, *, exclusive=False):
    """Shared for writers/readers, exclusive for deletion; never waits on DB.

    Exclusive callers may revisit a deleted course for cleanup recovery. The
    deletion marker must only be written after the database commit, while this
    exclusive lock remains held. Lock files are never removed or replaced.
    """
    folder, key = _directory(root, course_key)
    descriptor = os.open(folder / (key + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise PlatformConflict('교과목 파일 잠금을 확인할 수 없습니다.')
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CourseFileBusy('교과목 파일을 저장하거나 정리 중입니다. 잠시 후 다시 시도하세요.') from exc
        if not exclusive and os.path.lexists(folder / (key + '.deleted')):
            raise CourseFilesDeleted('영구 삭제된 교과목입니다. 파일을 저장하거나 받을 수 없습니다.')
        yield
    finally:
        os.close(descriptor)


def mark_course_files_deleted(root, course_key):
    """Durably fence future file requests; caller holds the exclusive guard."""
    folder, key = _directory(root, course_key)
    marker = folder / (key + '.deleted')
    if os.path.lexists(marker):
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise PlatformConflict('교과목 삭제 표시를 확인할 수 없습니다.')
        return
    temporary = folder / ('.marker-' + secrets.token_hex(16))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(b'1\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, marker)
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
