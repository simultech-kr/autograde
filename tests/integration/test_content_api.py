"""Synthetic students, real local HTTP, real Git-backed published content."""
from datetime import timedelta
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from autograde.content_store import ContentService
from autograde.platform_auth import secret_digest
from autograde.platform_bundle import BundleStore
from test_course_portal import connect
from test_student_material_api import (
    COURSE, NOW, SECRET, PDF, ZIP, material_api,
    publish as publish_personal_material, call as personal_material_request,
)


@pytest.fixture
def content_api(material_api, tmp_path):
    env = material_api
    env.content = ContentService(env.state, tmp_path / "content", clock=lambda: env.now[0])
    env.api.content_service = env.content
    return env


def request(env, path="/v1/content/collections", *, student="001", method="GET", payload=None, pin=None):
    headers = {}
    if student:
        headers["Authorization"] = "Bearer " + env.tokens[student]
    if pin is not None:
        headers["X-Autograde-Manifest-SHA256"] = pin
    data = None if payload is None else json.dumps(payload).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(*env.server.server_address[:2], timeout=10)
    try:
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def publish(env, *, students=None):
    row = env.content.create(COURSE, title="실습 참고자료", instructions="답안과 별도로 저장하세요.",
                             opens_at=NOW, closes_at=NOW + timedelta(hours=1),
                             student_ids=students, actor="synthetic instructor")
    for path, data in (("README.md", "# 강의자료\n".encode()), ("examples/main.cpp", b"int main() { return 0; }\n")):
        row = env.content.upload(COURSE, row["collection_id"], revision=row["revision"],
                                 path=path, data=data, actor="synthetic instructor")
    release = env.content.publish(COURSE, row["collection_id"], revision=row["revision"],
                                  idempotency_key="synthetic-publish-001", actor="synthetic instructor")
    return row, release


def manifest(env, release, student="001"):
    status, headers, body = request(env, f'/v1/content/releases/{release["release_id"]}/manifest', student=student)
    assert status == 200, body
    assert headers["Cache-Control"] == "no-store"
    return json.loads(body)


def pins(manifest):
    return {"expected_manifest_id": manifest["manifest_id"], "manifest_sha256": manifest["manifest_sha256"],
            "generation": manifest["generation"], "policy_revision": manifest["policy_revision"]}


def test_context_catalog_and_complete_download_verify(content_api):
    env = content_api
    _, release = publish(env)
    status, _, body = request(env, "/v1/content/context")
    assert status == 200
    context = json.loads(body)
    assert context["schema"] == "autograde.content.context.v1"
    assert len(context["subject_scope"]) == 64
    assert context["course_key"] == COURSE
    assert context["limits"]["max_files"] == 1000
    status, _, body = request(env)
    assert status == 200
    catalog = json.loads(body)
    assert len(catalog["collections"]) == 1
    assert catalog["subject_scope"] == context["subject_scope"]
    content = manifest(env, release)
    assert content["subject_scope"] == context["subject_scope"]
    assert len(content["files"]) == 2
    assert "git_commit" not in content and "student_id" not in content
    for file in content["files"]:
        status, headers, data = request(env,
            f'/v1/content/releases/{release["release_id"]}/objects/{file["sha256"]}', pin=content["manifest_sha256"])
        assert status == 200, data
        assert headers["Content-Type"] == "application/octet-stream"
        assert headers["X-Autograde-SHA256"] == file["sha256"]
        assert hashlib.sha256(data).hexdigest() == file["sha256"]
        assert len(data) == file["size_bytes"]
    status, _, body = request(env, f'/v1/content/releases/{release["release_id"]}/verify',
                              method="POST", payload=pins(content))
    assert status == 200, body
    result = json.loads(body)
    assert result["verified"] is True and result["manifest_id"] == content["manifest_id"]
    assert result["offline_export_allowed"] is False


def test_one_claim_session_downloads_accepted_assignment_personal_and_course_materials(content_api, tmp_path):
    """The IDE download hub reuses one claim login without widening its scope."""
    env = content_api
    service = env.services[COURSE]
    service.bundle_store = BundleStore(tmp_path / "hub-bundles")
    source = tmp_path / "hub-starter"
    source.mkdir()
    (source / "README.md").write_text("# Synthetic download lab\n")
    (source / "main.cpp").write_text("int main() { return 0; }\n")
    starter = service.bundle_store.create_from_directory(source, kind="starter")
    accepted_id, unaccepted_id = "basn_hub_accepted", "basn_hub_unaccepted"
    for assignment_id in (accepted_id, unaccepted_id):
        env.state.register_bundle_assignment_release(
            assignment_id=assignment_id, course_key=COURSE, assignment_key=assignment_id,
            release_id="v1", title="Synthetic hub assignment", starter_path=str(starter.path),
            starter_digest=starter.digest, starter_size_bytes=starter.compressed_bytes,
            assessment_path=str(tmp_path / "private-assessment"), assessment_digest="a" * 64,
            runner_image="registry.school/autograde@sha256:" + "e" * 64,
            rubric_version="v1", max_score=10, result_policy="immediate",
            opens_at=NOW - timedelta(minutes=1), due_at=NOW + timedelta(days=1), ready=True, at=NOW,
        )
    personal = publish_personal_material(env, files=[("001.pdf", PDF)])
    other_personal = publish_personal_material(env, files=[("002.zip", ZIP)])
    _, course_release = publish(env)
    _, other_release = publish(env, students=[env.students["002"].id])

    service.set_student_password(student_key="001", password="123456")
    credential = service.authenticate_student_password("001", "123456")
    _, claim = service.create_authenticated_assignment_claim(credential, accepted_id)
    _, tokens = connect(env.server, claim)
    env.tokens["001"] = tokens["access_token"]
    assert service.require_assignment_acceptance(env.tokens["001"]) == ("bundle", accepted_id)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(request, env, "/v1/assignments"),
            pool.submit(personal_material_request, env, token=env.tokens["001"]),
            pool.submit(request, env, "/v1/content/collections"),
        ]
        replies = [future.result() for future in futures]
    assert all(status == 200 and headers["Cache-Control"] == "no-store" for status, headers, _ in replies)
    assignments, materials, collections = [json.loads(body) for _, _, body in replies]
    assert [item["assignment_id"] for item in assignments["assignments"]] == [accepted_id]
    assert [item["material_id"] for item in materials["materials"]] == [personal["material_id"]]
    assert [item["release_id"] for item in collections["collections"]] == [course_release["release_id"]]

    status, headers, body = request(env, f"/v1/assignments/{accepted_id}/starter")
    assert status == 200 and body == starter.path.read_bytes()
    assert headers["X-Autograde-SHA256"] == starter.digest
    material = materials["materials"][0]
    status, _, body = personal_material_request(env, material["download_url"],
        token=env.tokens["001"], pin=material["sha256"])
    assert status == 200 and body == PDF
    content = manifest(env, course_release)
    for file in content["files"]:
        status, _, body = request(env,
            f'/v1/content/releases/{course_release["release_id"]}/objects/{file["sha256"]}',
            pin=content["manifest_sha256"])
        assert status == 200 and hashlib.sha256(body).hexdigest() == file["sha256"]

    assert request(env, f"/v1/assignments/{unaccepted_id}/starter")[0] in (403, 404)
    assert personal_material_request(env, f'/v1/materials/{other_personal["material_id"]}/download',
        token=env.tokens["001"], pin=hashlib.sha256(ZIP).hexdigest())[0] == 404
    assert request(env, f'/v1/content/releases/{other_release["release_id"]}/manifest')[0] == 404


def test_authentication_course_and_selected_audience(content_api):
    env = content_api
    _, release = publish(env, students=[env.students["001"].id])
    first = manifest(env, release)
    for student in ("002", "004"):
        assert json.loads(request(env, student=student)[2])["collections"] == []
        assert request(env, f'/v1/content/releases/{release["release_id"]}/manifest', student=student)[0] == 404
        assert request(env, f'/v1/content/releases/{release["release_id"]}/objects/{first["files"][0]["sha256"]}',
                       student=student, pin=first["manifest_sha256"])[0] == 404
    assert request(env, student=None)[0] == 401
    assert request(env, "/v1/content/context", student=None)[0] == 401
    env.services[COURSE].revoke_current(env.tokens["001"])
    assert request(env)[0] == 401


def test_subject_manifest_cannot_be_reused_across_students(content_api):
    env = content_api
    _, release = publish(env)
    first = manifest(env, release)
    second = manifest(env, release, "002")
    assert first["subject_scope"] != second["subject_scope"]
    assert first["manifest_sha256"] != second["manifest_sha256"]
    assert request(env, f'/v1/content/releases/{release["release_id"]}/verify', student="002",
                   method="POST", payload=pins(first))[0] == 409


@pytest.mark.parametrize("bad_pin", [None, "", "invalid", "0" * 64])
def test_object_digest_pin_is_required(content_api, bad_pin):
    env = content_api
    _, release = publish(env)
    content = manifest(env, release)
    status, _, _ = request(env, f'/v1/content/releases/{release["release_id"]}/objects/{content["files"][0]["sha256"]}', pin=bad_pin)
    assert status == (409 if bad_pin == "0" * 64 else 400)


@pytest.mark.parametrize("change", ["missing", "bool", "extra", "generation", "policy", "digest", "identity"])
def test_final_verify_pins_every_field(content_api, change):
    env = content_api
    _, release = publish(env)
    content = manifest(env, release)
    payload = pins(content)
    if change == "missing":
        payload.pop("policy_revision")
    elif change == "bool":
        payload["generation"] = True
    elif change == "extra":
        payload["student_id"] = env.students["002"].id
    elif change == "generation":
        payload["generation"] += 1
    elif change == "policy":
        payload["policy_revision"] += 1
    elif change == "digest":
        payload["manifest_sha256"] = "0" * 64
    else:
        payload["expected_manifest_id"] = "cm_" + "0" * 64
    assert request(env, f'/v1/content/releases/{release["release_id"]}/verify', method="POST", payload=payload)[0] == (
        400 if change in {"missing", "bool", "extra"} else 409)


def test_current_only_republish_and_withdraw(content_api):
    env = content_api
    row, release = publish(env)
    original = manifest(env, release)
    row = env.content.get(COURSE, row["collection_id"])
    row = env.content.upload(COURSE, row["collection_id"], revision=row["revision"], path="README.md",
                             data=b"changed\n", actor="synthetic instructor")
    # Draft changes must not change the already published delivery manifest.
    assert manifest(env, release) == original
    latest = env.content.publish(COURSE, row["collection_id"], revision=row["revision"],
                                 idempotency_key="synthetic-publish-002", actor="synthetic instructor")
    assert latest["generation"] == 2
    assert request(env, f'/v1/content/releases/{release["release_id"]}/verify', method="POST", payload=pins(original))[0] == 409
    updated = manifest(env, latest)
    unchanged = {f["sha256"] for f in original["files"]} & {f["sha256"] for f in updated["files"]}
    assert len(unchanged) == 1
    row = env.content.get(COURSE, row["collection_id"])
    env.content.withdraw(COURSE, row["collection_id"], revision=row["revision"], actor="synthetic instructor")
    assert json.loads(request(env)[2])["collections"] == []
    assert request(env, f'/v1/content/releases/{latest["release_id"]}/verify', method="POST", payload=pins(updated))[0] in {403, 404}


@pytest.mark.parametrize("suffix", ["context", "collections"])
def test_no_identity_or_scope_query_override(content_api, suffix):
    assert request(content_api, "/v1/content/" + suffix + "?student_id=2")[0] == 400
    assert request(content_api, "/v1/content/" + suffix, method="POST", payload={})[0] == 405


def test_disabled_content_is_not_silently_empty(content_api):
    content_api.api.content_service = None
    assert request(content_api, "/v1/content/context")[0] == 404


def test_revoke_during_manifest_generation_suppresses_response(content_api, monkeypatch):
    env = content_api
    _, release = publish(env)
    original = env.content.get_student_manifest
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        env.services[COURSE].revoke_current(env.tokens["001"])
        return result
    monkeypatch.setattr(env.content, "get_student_manifest", delayed)
    status, _, body = request(env, f'/v1/content/releases/{release["release_id"]}/manifest')
    assert status == 401 and b"files" not in body


def test_real_ide_sync_changed_files_only_and_answer_preservation(content_api, tmp_path):
    """Uses compiled extension transport, parser, cache and export, not a mock client."""
    env = content_api
    repo = Path(__file__).resolve().parents[2]
    bundled_node = repo / ".cache/node-v24.18.0-darwin-arm64/bin/node"
    node = str(bundled_node) if bundled_node.is_file() else shutil.which("node")
    if not node or not (repo / "extensions/vscode/out/contentSync.js").is_file():
        pytest.skip("Build extensions/vscode first and provide Node.js for cross-language HTTP test")
    row, _ = publish(env)
    workspace, exported = tmp_path / "answers", tmp_path / "exports"
    workspace.mkdir()
    exported.mkdir()
    answer = workspace / "main.cpp"
    answer.write_text("student answer -- must be preserved\n")
    settings = {"origin": "http://127.0.0.1:" + str(env.server.server_address[1]),
                "token": env.tokens["001"], "root": str(tmp_path / "materials"),
                "workspace": str(workspace), "exports": str(exported)}
    def run():
        result = subprocess.run([node, str(repo / "tests/content_sync_http.cjs")], input=json.dumps(settings),
                                text=True, capture_output=True, timeout=45, cwd=repo)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    first = run()
    assert first["downloaded"] == 2 and first["objectRequests"] == 2 and first["repeated"] == 0
    old_export = Path(first["exported"]) / "README.md"
    old_export.write_text("student notes in own exported copy\n")
    row = env.content.get(COURSE, row["collection_id"])
    row = env.content.upload(COURSE, row["collection_id"], revision=row["revision"], path="README.md",
                             data="# 새 자료\n".encode(), actor="synthetic instructor")
    env.content.publish(COURSE, row["collection_id"], revision=row["revision"],
                         idempotency_key="synthetic-e2e-update", actor="synthetic instructor")
    second = run()
    assert second["generation"] == 2 and second["downloaded"] == 1 and second["reused"] == 1
    assert second["objectRequests"] == 1 and second["repeated"] == 0
    assert (Path(second["exported"]) / "README.md").read_text() == "# 새 자료\n"
    assert old_export.read_text() == "student notes in own exported copy\n"
    assert answer.read_text() == "student answer -- must be preserved\n"


def test_content_transfer_limit_does_not_block_metadata(content_api):
    env = content_api
    _, release = publish(env)
    content = manifest(env, release)
    for _ in range(8):
        assert env.server.content_download_slots.acquire(blocking=False)
    try:
        status, headers, _ = request(env, f'/v1/content/releases/{release["release_id"]}/objects/{content["files"][0]["sha256"]}',
                                    pin=content["manifest_sha256"])
        assert status == 429 and headers["Retry-After"] == "1"
        assert request(env)[0] == 200
    finally:
        for _ in range(8):
            env.server.content_download_slots.release()
    assert request(env, f'/v1/content/releases/{release["release_id"]}/objects/{content["files"][0]["sha256"]}',
                   pin=content["manifest_sha256"])[0] == 200


def test_twenty_students_can_read_scoped_manifests_and_files(content_api):
    env = content_api
    _, release = publish(env)
    service = env.services[COURSE]
    keys = [f"load-{index:03}" for index in range(20)]
    for key in keys:
        student = env.state.upsert_local_student(student_key=key, auth_subject="local:" + key, at=NOW)
        env.state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)
        auth = service.create_device_authorization({"device_name": "synthetic content load " + key})
        env.state.approve_device_authorization(user_code_hmac=secret_digest(SECRET, "user-code", auth["user_code"]),
            auth_subject=student.auth_subject, course_key=COURSE, at=NOW)
        env.tokens[key] = service.exchange_device_authorization({"device_code": auth["device_code"]})["access_token"]
    def download(key):
        content = manifest(env, release, key)
        file = content["files"][0]
        for attempt in range(10):
            status, _, body = request(env, f'/v1/content/releases/{release["release_id"]}/objects/{file["sha256"]}',
                                      student=key, pin=content["manifest_sha256"])
            if status != 429:
                break
            time.sleep(0.05 * (attempt + 1))
        assert status == 200 and hashlib.sha256(body).hexdigest() == file["sha256"]
        return content["subject_scope"]
    with ThreadPoolExecutor(max_workers=20) as pool:
        scopes = list(pool.map(download, keys))
    assert len(set(scopes)) == 20
