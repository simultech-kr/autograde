import hashlib
import json

import pytest

from autograde.course_admin import CourseAdminService
from autograde.course_deletion_runtime import configure_course_deletion
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths


@pytest.mark.parametrize('change', ['none', 'worker', 'socket', 'missing', 'unknown'])
def test_pending_cleanup_only_completes_for_the_original_worker(tmp_path, monkeypatch, change):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    courses = CourseAdminService(state)
    old_socket = '/private/tmp/worker.sock'
    context = {'required': True, 'socket_digest': hashlib.sha256(old_socket.encode()).hexdigest(),
               'worker_instance': 'a' * 24 if change != 'unknown' else None}
    with state._write() as db:
        db.execute('INSERT INTO admin_course_deletions VALUES(?,?,?,?,?)',
            ('job1', 'removed', '2026-10-28', json.dumps({'remote_cleanup_required': True,
              'remote_cleanup_context': context}), 'pending'))
    calls = []
    from autograde import learning_runtime
    monkeypatch.setattr(learning_runtime, 'runtime_identity', lambda _: ('b' if change == 'worker' else 'a') * 24)
    monkeypatch.setattr(learning_runtime, 'cancel_course', lambda socket, key, **kw:
        calls.append((socket, key)) or {'cleanup_verified': True})
    class Registry:
        def invalidate(self, key):
            pass
    socket = None if change == 'missing' else '/private/tmp/new.sock' if change == 'socket' else old_socket
    configure_course_deletion(paths, courses, Registry(), socket)
    with state._connection() as db:
        status = db.execute('SELECT cleanup_status FROM admin_course_deletions').fetchone()[0]
    assert status == ('complete' if change == 'none' else 'pending')
    assert bool(calls) == (change == 'none')


def test_file_only_pending_cleanup_does_not_need_a_linux_worker(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    courses = CourseAdminService(state)
    with state._write() as db:
        db.execute('INSERT INTO admin_course_deletions VALUES(?,?,?,?,?)',
            ('job1', 'removed', '2026-10-28', json.dumps({'remote_cleanup_required': False}), 'pending'))
    class Registry:
        def invalidate(self, key):
            assert key == 'removed'
    configure_course_deletion(paths, courses, Registry())
    with state._connection() as db:
        assert db.execute('SELECT cleanup_status FROM admin_course_deletions').fetchone()[0] == 'complete'
