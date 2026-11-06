from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
import json
import hashlib
import os
import re
import threading

import pytest

from autograde.learning_checkpoints import LearningCheckpointStore
from autograde.platform_bundle import BundleStorageError
from autograde.platform_service import PlatformAPIError, StudentPlatformService
from autograde.platform_portal import CourseAPI
from autograde.platform_http import PlatformRequestHandler
from autograde.privacy_file_guard import privacy_file_guard
from autograde.course_file_guard import course_file_guard, mark_course_files_deleted

from test_platform_service_bundle import bundle_platform, COURSE, NOW
from test_platform_http_bundle import running_server, request


ASSIGNMENT = "basn_lab01"


def accept(service, student_key="s001"):
    service.set_student_password(student_key=student_key, password="482731")
    page = service.assignment_claim_page({"assignment_id": ASSIGNMENT})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.body).group(1)
    cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    issued = service.issue_assignment_claim({"student_key": student_key, "password": "482731",
        "assignment_id": ASSIGNMENT, "csrf": csrf}, {"autograde_assignment_claim": cookie})
    code = re.search(r"<code>(AK1-[^<]+)</code>", issued.body).group(1)
    device = service.create_device_authorization({"device_name": "Checkpoint test"})
    service.redeem_assignment_claim({"claim_code": code, "device_code": device["device_code"]})
    return service.exchange_device_authorization({"device_code": device["device_code"]})["access_token"]


@pytest.fixture()
def checkpoints(bundle_platform):
    state, store, service, _token, notifications, _starter, payload = bundle_platform
    token = accept(service)
    return state, store, service, token, notifications, payload


def save(service, token, payload, key="checkpoint-request-001"):
    return service.save_learning_checkpoint(token, key, ASSIGNMENT, BytesIO(payload), len(payload))


def changed_payload(store, number):
    tree = store.root.parent / ("changed-" + str(number))
    tree.mkdir(exist_ok=True)
    (tree / "main.cpp").write_text(f"int main() {{ return {number}; }}\n")
    (tree / "한글 파일.txt").write_text("자료\n")
    return store.create_from_directory(tree, kind="submission").path.read_bytes()


def test_checkpoint_durable_restore_git_files_and_no_grade_effect(checkpoints):
    state, store, service, token, notifications, payload = checkpoints
    before = state.list_bundle_dashboard_rows(course_key=COURSE)
    assert service.list_learning_checkpoints(token, ASSIGNMENT)["latest"] is None
    reply = save(service, token, payload)
    checkpoint = reply["checkpoint"]
    assert reply["replayed"] is False
    assert re.fullmatch("[a-f0-9]{64}", checkpoint["archive_sha256"])
    assert checkpoint["version"] == 1 and checkpoint["file_count"] == 1
    downloaded = service.get_learning_checkpoint_bundle(token, ASSIGNMENT, checkpoint["checkpoint_id"])
    assert downloaded.path.read_bytes() == payload
    assert downloaded.headers["X-Autograde-SHA256"] == checkpoint["archive_sha256"]
    snapshot_store = service._checkpoint_store()
    owner = next(path for path in snapshot_store.root.iterdir() if path.is_dir())
    code = snapshot_store._git(owner / "source.git", "show", checkpoint["git_commit"] + ":main.py")
    assert code == b"print(42)\n"
    assert state.list_bundle_dashboard_rows(course_key=COURSE) == before
    assert notifications == []
    restarted = StudentPlatformService(state=state, server_secret=b"s" * 32, course_key=COURSE,
        public_base_url="http://127.0.0.1:8000", bundle_store=store, now=lambda: NOW)
    assert restarted.list_learning_checkpoints(token, ASSIGNMENT)["latest"] == checkpoint
    assert restarted.get_learning_checkpoint_bundle(token, ASSIGNMENT, checkpoint["checkpoint_id"]).path.read_bytes() == payload


def test_idempotency_dedup_changed_versions_and_rate_limit(checkpoints):
    _state, store, service, token, _notifications, payload = checkpoints
    first = save(service, token, payload)
    assert save(service, token, payload)["replayed"]
    assert save(service, token, payload, "new-request-same-content")["checkpoint"] == first["checkpoint"]
    changed = changed_payload(store, 2)
    with pytest.raises(PlatformAPIError) as conflict:
        save(service, token, changed)
    assert conflict.value.status == 409
    with pytest.raises(PlatformAPIError) as limited:
        save(service, token, changed, "changed-request")
    assert limited.value.status == 429 and limited.value.headers["Retry-After"] == "10"
    service._now = lambda: NOW + timedelta(seconds=30)
    second = save(service, token, changed, "changed-request")
    assert second["checkpoint"]["version"] == 2
    assert service.list_learning_checkpoints(token, ASSIGNMENT)["checkpoints"] == [second["checkpoint"], first["checkpoint"]]
    assert save(service, token, payload)["checkpoint"] == first["checkpoint"]
    assert service.list_learning_checkpoints(token, ASSIGNMENT)["latest"] == second["checkpoint"]


def test_dedup_uses_payload_content_not_gzip_header(checkpoints):
    _state, _store, service, token, _notifications, payload = checkpoints
    first = save(service, token, payload)
    variant = bytearray(payload)
    variant[4:8] = b"\x01\x00\x00\x00"  # Different gzip mtime, identical validated files.
    same = save(service, token, bytes(variant), "different-envelope")
    assert same["replayed"] and same["checkpoint"] == first["checkpoint"]
    assert same["received_archive_sha256"] == hashlib.sha256(variant).hexdigest()
    assert same["received_archive_sha256"] != same["checkpoint"]["archive_sha256"]


def test_corrupt_backup_is_not_acknowledged_as_saved_or_downloaded(checkpoints):
    _state, _store, service, token, _notifications, payload = checkpoints
    checkpoint = save(service, token, payload)["checkpoint"]
    path = service.get_learning_checkpoint_bundle(token, ASSIGNMENT, checkpoint["checkpoint_id"]).path
    path.chmod(0o600)
    corrupted = bytearray(path.read_bytes())
    corrupted[-1] ^= 1
    path.write_bytes(corrupted)
    with pytest.raises(PlatformAPIError) as corrupted_download:
        service.get_learning_checkpoint_bundle(token, ASSIGNMENT, checkpoint["checkpoint_id"])
    assert corrupted_download.value.status == 503
    with pytest.raises(PlatformAPIError) as corrupted_replay:
        save(service, token, payload)
    assert corrupted_replay.value.status == 503


def test_acceptance_is_required(bundle_platform):
    _state, _store, service, token, _notifications, _starter, payload = bundle_platform
    with pytest.raises(PlatformAPIError) as missing_acceptance:
        save(service, token, payload)
    assert missing_acceptance.value.code == "assignment_acceptance_required"


def test_auth_scope_student_isolation_and_revocation(checkpoints):
    state, _store, service, token, _notifications, payload = checkpoints
    with pytest.raises(PlatformAPIError) as wrong_assignment:
        service.list_learning_checkpoints(token, "another-assignment")
    assert wrong_assignment.value.status == 403
    first = save(service, token, payload)["checkpoint"]
    student = state.upsert_local_student(student_key="s002", auth_subject="school:s002", at=NOW)
    state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)
    other_token = accept(service, "s002")
    assert service.list_learning_checkpoints(other_token, ASSIGNMENT)["latest"] is None
    with pytest.raises(PlatformAPIError) as forbidden:
        service.get_learning_checkpoint_bundle(other_token, ASSIGNMENT, first["checkpoint_id"])
    assert forbidden.value.status == 404
    service.revoke_current(token)
    with pytest.raises(PlatformAPIError) as revoked:
        service.get_learning_checkpoint_bundle(token, ASSIGNMENT, first["checkpoint_id"])
    assert revoked.value.status == 401


def test_invalid_bundle_size_and_symlink_are_not_published(checkpoints):
    _state, _store, service, token, _notifications, payload = checkpoints
    for data in (b"invalid gzip", b""):
        with pytest.raises(PlatformAPIError) as invalid:
            save(service, token, data)
        assert invalid.value.status in {400, 422}
    with pytest.raises(PlatformAPIError) as size:
        service.save_learning_checkpoint(token, "large", ASSIGNMENT, BytesIO(payload), 100 * 1024 * 1024)
    assert size.value.status == 413
    with pytest.raises(PlatformAPIError) as mismatch:
        service.save_learning_checkpoint(token, "wrong-length", ASSIGNMENT, BytesIO(payload), len(payload) + 1)
    assert mismatch.value.status == 400
    assert service.list_learning_checkpoints(token, ASSIGNMENT)["latest"] is None
    checkpoint_store = service._checkpoint_store()
    owner = next(checkpoint_store.root.iterdir())
    (owner / "index.json").symlink_to(checkpoint_store.root.parent / "nonexistent")
    with pytest.raises(PlatformAPIError) as unsafe:
        save(service, token, payload)
    assert unsafe.value.status == 503


def test_retention_removes_git_refs_archives_and_expires_retries(checkpoints, monkeypatch):
    _state, store, service, token, _notifications, payload = checkpoints
    monkeypatch.setattr(LearningCheckpointStore, "MAX_VERSIONS", 2)
    old = save(service, token, payload)["checkpoint"]
    for number in (2, 3):
        service._now = lambda number=number: NOW + timedelta(seconds=30 * number)
        save(service, token, changed_payload(store, number), "request-" + str(number))
    history = service.list_learning_checkpoints(token, ASSIGNMENT)
    assert [item["version"] for item in history["checkpoints"]] == [3, 2]
    with pytest.raises(PlatformAPIError) as expired:
        save(service, token, payload)
    assert expired.value.status == 410
    with pytest.raises(PlatformAPIError) as missing:
        service.get_learning_checkpoint_bundle(token, ASSIGNMENT, old["checkpoint_id"])
    assert missing.value.status == 404
    checkpoint_store = service._checkpoint_store()
    owner = next(checkpoint_store.root.iterdir())
    refs = checkpoint_store._git(owner / "source.git", "for-each-ref", "--format=%(refname)").decode()
    assert old["checkpoint_id"] not in refs
    assert len(list((owner / "archives").iterdir())) == 2
    with pytest.raises(BundleStorageError):
        checkpoint_store._git(owner / "source.git", "cat-file", "-e", old["git_commit"])


def test_aggregate_quota_reclaims_history_before_version_count(checkpoints, monkeypatch):
    _state, store, service, token, _notifications, payload = checkpoints
    monkeypatch.setattr(LearningCheckpointStore, "MAX_SOURCE_BYTES", 60)
    save(service, token, payload)
    for number in (2, 3):
        service._now = lambda number=number: NOW + timedelta(seconds=30 * number)
        save(service, token, changed_payload(store, number), "quota-" + str(number))
    rows = service.list_learning_checkpoints(token, ASSIGNMENT)["checkpoints"]
    assert len(rows) < 3 and rows[0]["version"] == 3
    assert sum(row["source_bytes"] for row in rows) <= 60


def test_directory_fanout_limit_rejects_before_git_materialization(checkpoints, monkeypatch):
    _state, store, service, token, _notifications, _payload = checkpoints
    tree = store.root.parent / "directory-limit"
    for name in ("a", "b"):
        (tree / name).mkdir(parents=True)
        (tree / name / "main.c").write_text("int main() {}")
    payload = store.create_from_directory(tree, kind="submission").path.read_bytes()
    monkeypatch.setattr(LearningCheckpointStore, "MAX_DIRECTORIES", 1)
    with pytest.raises(PlatformAPIError) as limited:
        save(service, token, payload)
    assert limited.value.status == 413
    assert service.list_learning_checkpoints(token, ASSIGNMENT)["latest"] is None


def test_inactive_enrollment_cannot_restore_private_code(checkpoints):
    state, _store, service, token, _notifications, payload = checkpoints
    checkpoint = save(service, token, payload)["checkpoint"]
    _session, student = service._authorize(token)
    state.upsert_enrollment(student_id=student.id, course_key=COURSE, active=False, at=NOW)
    with pytest.raises(PlatformAPIError) as denied:
        service.get_learning_checkpoint_bundle(token, ASSIGNMENT, checkpoint["checkpoint_id"])
    assert denied.value.status == 401


def test_failure_does_not_acknowledge_and_retry_recovers_orphans(checkpoints, monkeypatch):
    _state, _store, service, token, _notifications, payload = checkpoints
    store = service._checkpoint_store()
    original = store._publish
    def fail(*_args):
        raise OSError("disk full")
    monkeypatch.setattr(store, "_publish", fail)
    with pytest.raises(PlatformAPIError) as failed:
        save(service, token, payload)
    assert failed.value.status == 503
    assert service.list_learning_checkpoints(token, ASSIGNMENT)["latest"] is None
    monkeypatch.setattr(store, "_publish", original)
    saved = save(service, token, payload)["checkpoint"]
    assert saved["version"] == 1
    owner = next(store.root.iterdir())
    refs = store._git(owner / "source.git", "for-each-ref", "--format=%(refname)").decode().splitlines()
    assert refs == ["refs/checkpoints/" + saved["checkpoint_id"]]


def test_concurrent_instances_never_publish_duplicate_version(checkpoints):
    state, store, service, token, _notifications, payload = checkpoints
    other = StudentPlatformService(state=state, server_secret=b"s" * 32, course_key=COURSE,
        public_base_url="http://127.0.0.1:8000", bundle_store=store, now=lambda: NOW)
    barrier = threading.Barrier(2)
    def upload(instance):
        barrier.wait()
        try:
            return save(instance, token, payload)
        except PlatformAPIError as exc:
            assert exc.code == "checkpoint_busy"
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upload, (service, other)))
    assert any(result for result in results)
    assert len(service.list_learning_checkpoints(token, ASSIGNMENT)["checkpoints"]) == 1
    assert save(other, token, payload)["replayed"]


def test_revocation_during_upload_does_not_publish(checkpoints, monkeypatch):
    _state, _store, service, token, _notifications, payload = checkpoints
    store = service._checkpoint_store()
    original = store._git_snapshot
    def revoke(*args, **kwargs):
        commit = original(*args, **kwargs)
        service.revoke_current(token)
        return commit
    monkeypatch.setattr(store, "_git_snapshot", revoke)
    with pytest.raises(PlatformAPIError) as denied:
        save(service, token, payload)
    assert denied.value.status == 401
    fresh = accept(service)
    assert service.list_learning_checkpoints(fresh, ASSIGNMENT)["latest"] is None


def test_http_portal_checkpoint_round_trip_and_transport_limits(checkpoints):
    _state, _store, service, token, notifications, payload = checkpoints
    facade = CourseAPI({COURSE: service}, b"s" * 32)
    route = "/v1/assignments/" + ASSIGNMENT + "/learning-checkpoints"
    auth = {"Authorization": "Bearer " + token}
    upload_headers = {**auth, "Content-Type": "application/gzip", "Idempotency-Key": "checkpoint-http-test"}
    with running_server(facade, max_bundle_request_bytes=len(payload)) as server:
        status, _, body = request(server, "POST", route, body=payload, headers=upload_headers)
        assert status == 200
        checkpoint = json.loads(body)["checkpoint"]
        status, _, body = request(server, "GET", route, headers=auth)
        assert status == 200 and json.loads(body)["latest"] == checkpoint
        download = route + "/" + checkpoint["checkpoint_id"] + "/bundle"
        status, headers, body = request(server, "GET", download, headers=auth)
        assert status == 200 and body == payload
        assert headers["x-autograde-sha256"] == checkpoint["archive_sha256"]
        assert headers["cache-control"] == "no-store"
        status, _, _ = request(server, "POST", route, body=payload + b"x", headers=upload_headers)
        assert status == 413
        status, _, _ = request(server, "POST", route, body=b"{}", headers={**upload_headers, "Content-Type": "application/json"})
        assert status == 415
        status, _, _ = request(server, "GET", download)
        assert status == 401
    assert notifications == []


def test_http_retention_before_open_returns_expired_not_internal_error(checkpoints, monkeypatch):
    _state, store, service, token, _notifications, payload = checkpoints
    monkeypatch.setattr(LearningCheckpointStore, "MAX_VERSIONS", 1)
    old = save(service, token, payload)["checkpoint"]
    next_payload = changed_payload(store, 2)
    original = service.get_learning_checkpoint_bundle
    def prune_before_response(*args):
        response = original(*args)
        service._now = lambda: NOW + timedelta(seconds=30)
        save(service, token, next_payload, "concurrent-retention")
        assert not response.path.exists()
        return response
    monkeypatch.setattr(service, "get_learning_checkpoint_bundle", prune_before_response)
    facade = CourseAPI({COURSE: service}, b"s" * 32)
    route = f"/v1/assignments/{ASSIGNMENT}/learning-checkpoints/{old['checkpoint_id']}/bundle"
    with running_server(facade) as server:
        status, _, body = request(server, "GET", route, headers={"Authorization": "Bearer " + token})
    assert status == 410
    assert json.loads(body)["error"]["code"] == "checkpoint_expired"


def test_http_retention_after_open_keeps_pinned_descriptor_and_closes_it(checkpoints, monkeypatch):
    _state, _store, service, token, _notifications, payload = checkpoints
    checkpoint = save(service, token, payload)["checkpoint"]
    response = service.get_learning_checkpoint_bundle(token, ASSIGNMENT, checkpoint["checkpoint_id"])
    original_open = os.open
    original_send = PlatformRequestHandler._send_file_result
    sending = threading.local()
    opened = []
    def send(handler, *args, **kwargs):
        sending.active = True
        try:
            return original_send(handler, *args, **kwargs)
        finally:
            sending.active = False
    def open_and_unlink(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if getattr(sending, "active", False) and path == response.path:
            response.path.unlink()
            opened.append(descriptor)
        return descriptor
    monkeypatch.setattr(PlatformRequestHandler, "_send_file_result", send)
    monkeypatch.setattr(os, "open", open_and_unlink)
    facade = CourseAPI({COURSE: service}, b"s" * 32)
    route = f"/v1/assignments/{ASSIGNMENT}/learning-checkpoints/{checkpoint['checkpoint_id']}/bundle"
    with running_server(facade) as server:
        status, _, body = request(server, "GET", route, headers={"Authorization": "Bearer " + token})
    assert status == 200 and body == payload
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_http_privacy_cleanup_contention_is_retryable_and_recovers(checkpoints):
    state, _store, service, token, _notifications, payload = checkpoints
    checkpoint = save(service, token, payload)["checkpoint"]
    facade = CourseAPI({COURSE: service}, b"s" * 32)
    route = f"/v1/assignments/{ASSIGNMENT}/learning-checkpoints"
    download = route + f"/{checkpoint['checkpoint_id']}/bundle"
    auth = {"Authorization": "Bearer " + token}
    upload = {**auth, "Content-Type": "application/gzip", "Idempotency-Key": "privacy-busy-save"}
    with running_server(facade) as server:
        # Request handlers use another thread and must contend for this lease.
        with privacy_file_guard(state.database.parent, exclusive=True):
            for method, target, data, headers in [
                ("GET", route, None, auth), ("GET", download, None, auth),
                ("POST", route, payload, upload),
            ]:
                status, response_headers, body = request(server, method, target, body=data, headers=headers)
                assert status == 429 and response_headers["retry-after"] == "2"
                assert json.loads(body)["error"]["code"] == "checkpoint_busy"
        assert request(server, "GET", route, headers=auth)[0] == 200
        assert request(server, "GET", download, headers=auth)[2] == payload
        status, _, body = request(server, "POST", route, body=payload, headers=upload)
        assert status == 200 and json.loads(body)["checkpoint"] == checkpoint


def test_guard_corruption_is_storage_failure_not_contention(checkpoints):
    state, _store, service, token, _notifications, payload = checkpoints
    save(service, token, payload)
    lock = state.database.parent / ".privacy-files.lock"
    alias = state.database.parent / "unsafe-privacy-lock-alias"
    os.link(lock, alias)
    with pytest.raises(PlatformAPIError) as error:
        service.list_learning_checkpoints(token, ASSIGNMENT)
    assert error.value.status == 503
    assert error.value.code == "checkpoint_storage_unavailable"
    alias.unlink()
    with course_file_guard(state.database.parent, COURSE, exclusive=True):
        mark_course_files_deleted(state.database.parent, COURSE)
    with pytest.raises(PlatformAPIError) as deleted:
        service.list_learning_checkpoints(token, ASSIGNMENT)
    assert deleted.value.status == 410 and deleted.value.code == "course_deleted"


@pytest.mark.parametrize("mutation", [
    lambda ledger: ledger.update(version=True),
    lambda ledger: ledger.update(checkpoints=[{}]),
    lambda ledger: ledger.update(checkpoints=[]),
    lambda ledger: ledger["checkpoints"][0].update(assignment_id="another-assignment"),
    lambda ledger: ledger["checkpoints"][0].update(archive_sha256="../outside"),
    lambda ledger: ledger["checkpoints"][0].update(content_sha256=None),
    lambda ledger: ledger["checkpoints"][0].update(created_at="not-a-date"),
    lambda ledger: ledger["checkpoints"][0].update(created_at="2026-11-06T10:00:00"),
    lambda ledger: ledger["checkpoints"][0].update(compressed_bytes=True),
    lambda ledger: ledger["checkpoints"][0].update(source_bytes=-1),
    lambda ledger: ledger["checkpoints"].append(dict(ledger["checkpoints"][0])),
    lambda ledger: ledger.update(keys=[{}]),
    lambda ledger: ledger["keys"][0].update(digest="unvalidated"),
    lambda ledger: ledger["keys"].append(dict(ledger["keys"][0])),
])
def test_corrupt_ledger_is_rejected_before_reclamation_without_changing_files(checkpoints, monkeypatch, mutation):
    _state, _store, service, token, _notifications, payload = checkpoints
    checkpoint = save(service, token, payload)["checkpoint"]
    store = service._checkpoint_store()
    owner = next(store.root.iterdir())
    index = owner / "index.json"
    ledger = json.loads(index.read_text())
    mutation(ledger)
    index.write_text(json.dumps(ledger))
    def snapshot():
        return {str(file.relative_to(owner)): file.read_bytes() for file in owner.rglob("*") if file.is_file()}
    before = snapshot()
    def no_reclaim(*_args):
        pytest.fail("Corrupt metadata reached destructive reclamation")
    monkeypatch.setattr(store, "_reclaim", no_reclaim)
    for operation in (lambda: service.list_learning_checkpoints(token, ASSIGNMENT),
                      lambda: service.get_learning_checkpoint_bundle(token, ASSIGNMENT, checkpoint["checkpoint_id"]),
                      lambda: save(service, token, payload)):
        with pytest.raises(PlatformAPIError) as error:
            operation()
        assert error.value.status == 503 and error.value.code == "checkpoint_storage_unavailable"
    assert snapshot() == before


def test_http_corrupt_ledger_is_safe_storage_error_and_repair_restores_access(checkpoints):
    _state, _store, service, token, _notifications, payload = checkpoints
    checkpoint = save(service, token, payload)["checkpoint"]
    owner = next(service._checkpoint_store().root.iterdir())
    index = owner / "index.json"
    original = index.read_bytes()
    value = json.loads(original)
    value["checkpoints"][0]["assignment_id"] = "private-corruption-detail"
    index.write_text(json.dumps(value))
    facade = CourseAPI({COURSE: service}, b"s" * 32)
    route = f"/v1/assignments/{ASSIGNMENT}/learning-checkpoints"
    auth = {"Authorization": "Bearer " + token}
    with running_server(facade) as server:
        for method, target, data, headers in [
            ("GET", route, None, auth),
            ("GET", route + f"/{checkpoint['checkpoint_id']}/bundle", None, auth),
            ("POST", route, payload, {**auth, "Content-Type": "application/gzip", "Idempotency-Key": "repair-test"}),
        ]:
            status, response_headers, body = request(server, method, target, body=data, headers=headers)
            assert status == 503 and response_headers["retry-after"] == "10"
            assert json.loads(body)["error"]["code"] == "checkpoint_storage_unavailable"
            assert b"private-corruption-detail" not in body and token.encode() not in body
        # Valid pre-change ledgers use this same unversioned shape.
        index.write_bytes(original)
        status, _, body = request(server, "GET", route, headers=auth)
        assert status == 200 and json.loads(body)["latest"] == checkpoint
    assert save(service, token, payload)["checkpoint"] == checkpoint


def test_duplicate_ledger_json_keys_are_not_silently_accepted(checkpoints):
    _state, _store, service, token, _notifications, payload = checkpoints
    save(service, token, payload)
    index = next(service._checkpoint_store().root.iterdir()) / "index.json"
    content = index.read_text()
    index.write_text(content.replace('"version":1', '"version":0,"version":1', 1))
    with pytest.raises(PlatformAPIError) as error:
        service.list_learning_checkpoints(token, ASSIGNMENT)
    assert error.value.status == 503


@pytest.mark.parametrize("interrupt_repack", [False, True])
def test_retention_removes_packed_expired_code_and_retries_interrupted_reclamation(checkpoints, monkeypatch, interrupt_repack):
    _state, bundles, service, token, _notifications, payload = checkpoints
    monkeypatch.setattr(LearningCheckpointStore, "MAX_VERSIONS", 1)
    first = save(service, token, payload)["checkpoint"]
    store = service._checkpoint_store()
    owner = next(store.root.iterdir())
    repo = owner / "source.git"
    store._git(repo, "repack", "-ad")
    second_payload = changed_payload(bundles, 2)
    service._now = lambda: NOW + timedelta(seconds=30)
    original_git = store._git
    repack_calls = []
    def git(repository, *args, **kwargs):
        if args[0] == "repack":
            repack_calls.append(args)
            if interrupt_repack and len(repack_calls) == 1:
                raise BundleStorageError("synthetic interrupted packed reclamation")
        return original_git(repository, *args, **kwargs)
    monkeypatch.setattr(store, "_git", git)
    if interrupt_repack:
        with pytest.raises(PlatformAPIError) as error:
            save(service, token, second_payload, "packed-second")
        assert error.value.status == 503
        assert (owner / ".git-reclaim-pending").exists()
        assert first["checkpoint_id"] not in original_git(repo, "for-each-ref", "--format=%(refname)").decode()
    second = save(service, token, second_payload, "packed-second")["checkpoint"]
    assert second["version"] == 2
    with pytest.raises(BundleStorageError):
        original_git(repo, "cat-file", "-e", first["git_commit"])
    assert not (owner / "archives" / (first["archive_sha256"] + ".tar.gz")).exists()
    assert not (owner / ".git-reclaim-pending").exists()
    assert service.get_learning_checkpoint_bundle(token, ASSIGNMENT, second["checkpoint_id"]).path.read_bytes() == second_payload
    assert original_git(repo, "show", second["git_commit"] + ":main.cpp") == b"int main() { return 2; }\n"
    assert service.list_learning_checkpoints(token, ASSIGNMENT)["checkpoints"] == [second]
    count = len(repack_calls)
    assert count == (2 if interrupt_repack else 1)
    assert save(service, token, second_payload, "packed-second")["checkpoint"] == second
    assert save(service, token, second_payload, "packed-same-content")["checkpoint"] == second
    assert len(repack_calls) == count  # Ordinary periodic/replayed saves never repack.


def test_unpacked_retention_does_not_create_packs_or_run_expensive_gc(checkpoints, monkeypatch):
    _state, bundles, service, token, _notifications, payload = checkpoints
    monkeypatch.setattr(LearningCheckpointStore, "MAX_VERSIONS", 1)
    first = save(service, token, payload)["checkpoint"]
    store = service._checkpoint_store()
    owner = next(store.root.iterdir())
    original_git = store._git
    def git(repository, *args, **kwargs):
        assert args[0] not in {"repack", "gc"}, "Loose-object retention must not create packs"
        return original_git(repository, *args, **kwargs)
    monkeypatch.setattr(store, "_git", git)
    for version in (2, 3):
        service._now = lambda version=version: NOW + timedelta(seconds=30 * version)
        current = save(service, token, changed_payload(bundles, version), f"unpacked-{version}")["checkpoint"]
        assert current["version"] == version
    assert not list((owner / "source.git" / "objects" / "pack").glob("*.pack"))
    assert not (owner / ".git-reclaim-pending").exists()
    with pytest.raises(BundleStorageError):
        original_git(owner / "source.git", "cat-file", "-e", first["git_commit"])
