"""Cross-process fence for privacy cleanup versus complete file publication.

The shared lease must cover both filesystem creation and its database reference.
Locks never wait while SQLite is locked. Nested leases on the same thread reuse
the descriptor; an attempted shared-to-exclusive upgrade is rejected.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import threading

from .platform_state import PlatformConflict

_local = threading.local()


@contextmanager
def privacy_file_guard(root, *, exclusive=False):
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise PlatformConflict('개인정보 정리 저장 경로를 확인할 수 없습니다.')
    key = str(root)
    leases = getattr(_local, 'leases', None)
    if leases is None:
        leases = _local.leases = {}
    if key in leases:
        if exclusive and not leases[key]:
            raise PlatformConflict('파일 저장 중에는 개인정보 정리를 시작할 수 없습니다.')
        yield
        return
    descriptor = os.open(root / '.privacy-files.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise PlatformConflict('개인정보 정리 잠금을 확인할 수 없습니다.')
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PlatformConflict('파일 저장·다운로드 또는 수업 개인정보 정리가 진행 중입니다. 잠시 후 다시 시도하세요.') from exc
        leases[key] = exclusive
        try:
            yield
        finally:
            del leases[key]
    finally:
        os.close(descriptor)
