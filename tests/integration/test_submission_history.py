"""Authenticated immutable submission history on the pilot's real HTTP listeners."""
import json
import re

from test_course_portal import portal, claim, connect, request, login, cookie, csrf, WEB


def accepted(portal, tmp_path):
    web, api, services, _, store = portal
    _, session = connect(api, claim(web, "come3105"))
    token = session["access_token"]
    source = tmp_path / "source"
    source.mkdir()
    (source / "answer.txt").write_text("my solution")
    bundle = store.create_from_directory(source, kind="submission")
    status, _, raw = request(api, "/v1/assignments/asn_come3105/submissions", method="POST",
                             token=token, raw=bundle.path.read_bytes())
    assert status == 202, raw
    return token, json.loads(raw)["submission"]["submission_id"], bundle


def test_history_source_roundtrip_and_relogin(portal, tmp_path):
    token, submission_id, bundle = accepted(portal, tmp_path)
    web, api, _, _, _ = portal
    endpoint = "/v1/assignments/asn_come3105/history"
    status, headers, raw = request(api, endpoint, token=token)
    assert status == 200, raw
    history = json.loads(raw)
    assert history["has_more"] is False and history["limit"] == 100
    assert [r["submission_id"] for r in history["submissions"]] == [submission_id]
    assert history["submissions"][0]["source_sha256"] == bundle.digest
    assert not {"score", "rubric", "diagnostics", "assessment_path", "student_id"} & history["submissions"][0].keys()
    assert "no-store" in headers["Cache-Control"]
    status, headers, raw = request(api, f"/v1/submissions/{submission_id}/source", token=token)
    assert status == 200 and raw == bundle.path.read_bytes()
    assert headers["Content-Type"] == "application/gzip"
    assert "no-store" in headers["Cache-Control"]
    # A retry cannot create a phantom version.
    assert request(api, "/v1/assignments/asn_come3105/submissions", method="POST", token=token, raw=raw)[0] == 202
    assert len(json.loads(request(api, endpoint, token=token)[2])["submissions"]) == 1
    assert request(api, "/v1/sessions/current", method="DELETE", token=token)[0] == 204
    assert request(api, endpoint, token=token)[0] == 401
    _, new_session = connect(api, claim(web, "come3105"))
    assert json.loads(request(api, endpoint, token=new_session["access_token"])[2]) == history


def test_history_and_source_deny_other_student_course_and_inactive(portal, tmp_path):
    token, submission_id, _ = accepted(portal, tmp_path)
    web, api, services, _, _ = portal
    history = "/v1/assignments/asn_come3105/history"
    source = f"/v1/submissions/{submission_id}/source"
    for endpoint in (history, source):
        assert request(api, endpoint)[0] == 401
    _, other_course = connect(api, claim(web, "come2201", "000002"))
    for endpoint in (history, source):
        assert request(api, endpoint, token=other_course["access_token"])[0] in (403, 404)
    state = services["come3105"].state
    other = state.upsert_local_student(student_key="20260002", auth_subject="local:20260002")
    state.upsert_enrollment(student_id=other.id, course_key="come3105")
    services["come3105"].set_student_password(student_key="20260002", password="123456")
    _, other_session = connect(api, claim(web, "come3105", "123456", "20260002"))
    assert json.loads(request(api, history, token=other_session["access_token"])[2])["submissions"] == []
    assert request(api, source, token=other_session["access_token"])[0] == 404
    credential = state.get_student_password_credential(course_key="come3105", student_key="20260001")
    state.upsert_enrollment(student_id=credential.student_id, course_key="come3105", active=False)
    for endpoint in (history, source):
        assert request(api, endpoint, token=token)[0] in (401, 403)


def test_source_storage_failure_is_explicit_and_unknown_is_404(portal, tmp_path, monkeypatch):
    token, submission_id, _ = accepted(portal, tmp_path)
    _, api, _, _, store = portal
    assert request(api, "/v1/submissions/bsub_missing/source", token=token)[0] == 404
    def unavailable(_digest):
        raise OSError("private storage path must not leak")
    monkeypatch.setattr(store, "get", unavailable)
    status, _, raw = request(api, f"/v1/submissions/{submission_id}/source", token=token)
    assert status == 503
    assert b"private storage" not in raw


def test_same_student_other_assignment_claim_cannot_read_history_or_source(portal, tmp_path):
    _, submission_id, _ = accepted(portal, tmp_path)
    web, api, services, _, _ = portal
    state = services["come3105"].state
    previous = state.get_bundle_assignment("asn_come3105")
    fields = ("course_key", "starter_path", "starter_digest", "starter_size_bytes", "assessment_path",
              "assessment_digest", "runner_image", "rubric_version", "max_score", "result_policy", "opens_at", "due_at")
    state.register_bundle_assignment_release(**{key: getattr(previous, key) for key in fields},
        assignment_id="asn_other", assignment_key="lab02", release_id="lab02-v1", title="Other lab", ready=True)
    status, headers, body = login(web, "come3105")
    assert status == 200
    status, _, body = request(web, "/courses/come3105/claims", method="POST", cookie=cookie(headers), origin=WEB,
        data={"csrf": csrf(body), "assignment_id": "asn_other"})
    assert status == 200, body
    code = re.search(rb"<code>(AK1-[A-Z0-9-]+)</code>", body)[1].decode()
    _, session = connect(api, code)
    for endpoint in ("/v1/assignments/asn_come3105/history", f"/v1/submissions/{submission_id}/source"):
        assert request(api, endpoint, token=session["access_token"])[0] == 403
