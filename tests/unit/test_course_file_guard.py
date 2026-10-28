from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import stat
import threading

import pytest

from autograde.course_file_guard import (course_file_guard, mark_course_files_deleted,
                                         CourseFileBusy, CourseFilesDeleted)
from autograde.platform_service import PlatformAPIError
from test_learning_checkpoints import checkpoints, bundle_platform, save, ASSIGNMENT
from test_content_store import setup, create, upload, publish, COURSE


def test_shared_writers_exclude_deletion_and_marker_is_durable(tmp_path):
    with course_file_guard(tmp_path, 'one'):
        with course_file_guard(tmp_path, 'one'):
            with pytest.raises(CourseFileBusy):
                with course_file_guard(tmp_path, 'one', exclusive=True):
                    pass
    with course_file_guard(tmp_path, 'one', exclusive=True):
        with pytest.raises(CourseFileBusy):
            with course_file_guard(tmp_path, 'one'):
                pass
        mark_course_files_deleted(tmp_path, 'one')
    with pytest.raises(CourseFilesDeleted):
        with course_file_guard(tmp_path, 'one'):
            pass
    with course_file_guard(tmp_path, 'one', exclusive=True):
        mark_course_files_deleted(tmp_path, 'one')
    with course_file_guard(tmp_path, 'two'):
        pass
    marker = tmp_path / '.course-file-locks' / (hashlib.sha256(b'one').hexdigest() + '.deleted')
    assert marker.read_bytes() == b'1\n' and stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_guard_never_follows_replaced_lock(tmp_path):
    with course_file_guard(tmp_path, 'one'):
        pass
    lock = tmp_path / '.course-file-locks' / (hashlib.sha256(b'one').hexdigest() + '.lock')
    lock.unlink()
    outside = tmp_path / 'outside'
    outside.write_text('keep')
    lock.symlink_to(outside)
    with pytest.raises(OSError):
        with course_file_guard(tmp_path, 'one'):
            pass
    assert outside.read_text() == 'keep'


def test_first_checkpoint_save_is_guarded_before_owner_directory_exists(checkpoints, monkeypatch):
    _state, _bundle, service, token, _notifications, payload = checkpoints
    store = service._checkpoint_store()
    entered, proceed = threading.Event(), threading.Event()
    original = store._owner
    def delayed_owner(*args):
        entered.set()
        assert proceed.wait(5)
        return original(*args)
    monkeypatch.setattr(store, '_owner', delayed_owner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(save, service, token, payload)
        try:
            assert entered.wait(5)
            assert not store.root.exists()
            with pytest.raises(CourseFileBusy):
                with course_file_guard(store.root.parent, service.course_key, exclusive=True):
                    pass
        finally:
            proceed.set()
        assert future.result(timeout=5)['checkpoint']['checkpoint_id']


def test_deleted_course_rejects_checkpoint_save_history_download_before_creating_paths(checkpoints):
    _state, _bundle, service, token, _notifications, payload = checkpoints
    store = service._checkpoint_store()
    with course_file_guard(store.root.parent, service.course_key, exclusive=True):
        with pytest.raises(PlatformAPIError) as busy:
            save(service, token, payload)
        assert busy.value.status == 429
        mark_course_files_deleted(store.root.parent, service.course_key)
    for operation in (lambda: save(service, token, payload),
                      lambda: service.list_learning_checkpoints(token, ASSIGNMENT),
                      lambda: service.get_learning_checkpoint_bundle(token, ASSIGNMENT, 'lcp_' + 'a' * 32)):
        with pytest.raises(PlatformAPIError) as gone:
            operation()
        assert gone.value.status == 410
    assert not store.root.exists()


def test_failed_first_checkpoint_keeps_course_metadata_before_index_publish(checkpoints, monkeypatch):
    _state, _bundle, service, token, _notifications, payload = checkpoints
    store = service._checkpoint_store()
    def fail(*_args):
        raise OSError('synthetic publication failure')
    monkeypatch.setattr(store, '_publish', fail)
    with pytest.raises(PlatformAPIError):
        save(service, token, payload)
    owner = next(store.root.iterdir())
    assert json.loads((owner / 'owner.json').read_text()) == {'course_key': service.course_key, 'assignment_id': ASSIGNMENT}
    assert not (owner / 'index.json').exists()
    assert (owner / 'source.git').is_dir()
    (owner / 'owner.json').write_text(json.dumps({'course_key': 'wrong', 'assignment_id': ASSIGNMENT}))
    with pytest.raises(PlatformAPIError) as unsafe:
        service.list_learning_checkpoints(token, ASSIGNMENT)
    assert unsafe.value.status == 503


def test_content_upload_and_publish_share_deletion_fence(setup, monkeypatch):
    _state, service, _times, _students = setup
    collection = create(service)
    entered, proceed = threading.Event(), threading.Event()
    original = service._store_object
    def delayed_store(*args):
        entered.set()
        assert proceed.wait(5)
        return original(*args)
    monkeypatch.setattr(service, '_store_object', delayed_store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(upload, service, collection)
        try:
            assert entered.wait(5)
            with pytest.raises(CourseFileBusy):
                with course_file_guard(service.root.parent, COURSE, exclusive=True):
                    pass
        finally:
            proceed.set()
        collection = future.result(timeout=5)
    with course_file_guard(service.root.parent, COURSE, exclusive=True):
        with pytest.raises(CourseFileBusy):
            publish(service, collection)
        mark_course_files_deleted(service.root.parent, COURSE)
    for operation in (lambda: upload(service, collection, 'new.md', b'new'), lambda: publish(service, collection)):
        with pytest.raises(CourseFilesDeleted):
            operation()
    assert not (service.root / 'git').exists()
