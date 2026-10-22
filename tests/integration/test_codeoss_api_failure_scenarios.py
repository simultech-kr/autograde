"""Student exception/recovery flows over real loopback web/API listeners.

Only the existing synthetic portal fixture and temporary SQLite are used. The
service clock is advanced for expiry tests; there is no sleep, remote access,
real roster, credential logging or execution of student programs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import io
import json
import re
import tarfile

import pytest

from test_course_portal import WEB, cookie, csrf, login, portal, request
from test_accepted_assignment_catalog import clone_release


COURSE = "come3105"
FIRST = "asn_come3105"


def api(server, path, *, method="GET", payload=None, raw=None, token=None,
        key="exception-scenario-001", content_type="application/gzip"):
    headers = {}
    if payload is not None:
        raw = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        headers.update({"Content-Type": content_type, "Idempotency-Key": key})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=10)
    try:
        connection.request(method, path, raw, headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, {name.lower(): value for name, value in response.getheaders()}, body
    finally:
        connection.close()


def decoded(response, expected=200):
    # Never put response bodies, claims or tokens in assertion messages.
    assert response[0] == expected, f"expected HTTP {expected}, got {response[0]}"
    return json.loads(response[2])


def error(response, status, code):
    assert decoded(response, status)["error"]["code"] == code


def claim(portal, assignment=FIRST, *, student="20260001", password="000001"):
    status, headers, body = login(portal[0], COURSE, password, student)
    assert status == 200
    status, _, body = request(portal[0], f"/courses/{COURSE}/claims", method="POST",
        cookie=cookie(headers), origin=WEB, data={"csrf": csrf(body), "assignment_id": assignment})
    assert status == 200
    found = re.search(rb'<div id="claim-code"><code>([^<]+)</code>', body)
    assert found is not None
    return found[1].decode()


def device(portal, code):
    return decoded(api(portal[1], "/v1/device-authorizations", method="POST",
        payload={"device_name": "synthetic exception test", "claim_code": code}), 201)["device_code"]


def redeem(portal, code, device_code):
    return api(portal[1], "/v1/assignment-claims/redeem", method="POST",
        payload={"claim_code": code, "device_code": device_code})


def connect(portal, assignment=FIRST, **credentials):
    code = claim(portal, assignment, **credentials)
    device_code = device(portal, code)
    assert redeem(portal, code, device_code)[0] == 200
    return decoded(api(portal[1], "/v1/device-authorizations/token", method="POST",
        payload={"device_code": device_code}))


def bundle(portal, tmp_path, name="answer", text="my solution"):
    source = tmp_path / name
    source.mkdir()
    (source / "answer.txt").write_text(text)
    return portal[4].create_from_directory(source, kind="submission").path.read_bytes()


def submit(portal, token, body, **options):
    assignment = options.pop("assignment", FIRST)
    return api(portal[1], f"/v1/assignments/{assignment}/submissions", method="POST", token=token, raw=body, **options)


@pytest.mark.parametrize("bad_code,status,code", [
    (None, 403, "assignment_claim_denied"),
    ("123456", 403, "assignment_claim_denied"),
    ("AK1-AAAA-BBBB-CCCC", 401, "invalid_grant"),
])
def test_bad_claim_is_denied_without_500_then_fresh_claim_works(portal, bad_code, status, code):
    error(api(portal[1], "/v1/device-authorizations", method="POST",
        payload={"device_name": "test", "claim_code": bad_code}), status, code)
    assert api(portal[1], "/v1/me", token=connect(portal)["access_token"])[0] == 200


def test_expired_claim_never_approves_device_and_new_claim_recovers(portal):
    service = portal[2][COURSE]
    now = datetime.now(timezone.utc)
    service._now = lambda: now
    code = claim(portal)
    now += timedelta(seconds=601)
    device_code = device(portal, code)
    error(redeem(portal, code, device_code), 403, "assignment_claim_denied")
    error(api(portal[1], "/v1/device-authorizations/token", method="POST",
        payload={"device_code": device_code}), 400, "authorization_pending")
    assert api(portal[1], "/v1/me", token=connect(portal)["access_token"])[0] == 200


def test_expired_device_can_be_replaced_without_consuming_still_valid_claim(portal):
    service = portal[2][COURSE]
    now = datetime.now(timezone.utc)
    service._now = lambda: now
    code = claim(portal)
    old = device(portal, code)
    now += timedelta(seconds=301)
    error(redeem(portal, code, old), 403, "assignment_claim_denied")
    error(api(portal[1], "/v1/device-authorizations/token", method="POST",
        payload={"device_code": old}), 400, "expired_token")
    new = device(portal, code)
    assert redeem(portal, code, new)[0] == 200
    decoded(api(portal[1], "/v1/device-authorizations/token", method="POST", payload={"device_code": new}))


def test_consumed_claim_and_device_cannot_issue_a_second_session(portal):
    code = claim(portal)
    first = device(portal, code)
    assert redeem(portal, code, first)[0] == 200
    tokens = decoded(api(portal[1], "/v1/device-authorizations/token", method="POST", payload={"device_code": first}))
    error(redeem(portal, code, device(portal, code)), 403, "assignment_claim_denied")
    error(api(portal[1], "/v1/device-authorizations/token", method="POST", payload={"device_code": first}), 400, "invalid_grant")
    assert api(portal[1], "/v1/me", token=tokens["access_token"])[0] == 200


def test_access_expiry_refresh_and_absolute_session_expiry(portal):
    service = portal[2][COURSE]
    now = datetime.now(timezone.utc)
    service._now = lambda: now
    tokens = connect(portal)
    now += timedelta(seconds=901)
    error(api(portal[1], "/v1/me", token=tokens["access_token"]), 401, "invalid_token")
    refreshed = decoded(api(portal[1], "/v1/tokens/refresh", method="POST", payload={"refresh_token": tokens["refresh_token"]}))
    assert api(portal[1], "/v1/me", token=refreshed["access_token"])[0] == 200
    now += timedelta(hours=4)
    error(api(portal[1], "/v1/tokens/refresh", method="POST", payload={"refresh_token": refreshed["refresh_token"]}), 401, "invalid_grant")
    assert api(portal[1], "/v1/me", token=connect(portal)["access_token"])[0] == 200


def test_new_login_revokes_old_shared_seat_tokens(portal):
    old, new = connect(portal), connect(portal)
    error(api(portal[1], "/v1/me", token=old["access_token"]), 401, "invalid_token")
    error(api(portal[1], "/v1/tokens/refresh", method="POST", payload={"refresh_token": old["refresh_token"]}), 401, "invalid_grant")
    assert api(portal[1], "/v1/me", token=new["access_token"])[0] == 200


def test_unauthenticated_other_course_and_other_student_cannot_download_or_read_submission(portal, tmp_path):
    path = f"/v1/assignments/{FIRST}/starter"
    assert api(portal[1], path)[0] == 401
    assert api(portal[1], path, token="synthetic-invalid-token")[0] == 401
    token = connect(portal)["access_token"]
    assert api(portal[1], "/v1/assignments/asn_come2201/starter", token=token)[0] in {403, 404}
    receipt = decoded(submit(portal, token, bundle(portal, tmp_path)), 202)["submission"]
    service = portal[2][COURSE]
    other = service.state.upsert_local_student(student_key="synthetic-other", auth_subject="local:synthetic-other")
    service.state.upsert_enrollment(student_id=other.id, course_key=COURSE)
    service.set_student_password(student_key="synthetic-other", password="900001")
    other_token = connect(portal, student="synthetic-other", password="900001")["access_token"]
    for suffix in ("", "/source", "/result"):
        assert api(portal[1], f"/v1/submissions/{receipt['submission_id']}{suffix}", token=other_token)[0] == 404
    assert decoded(api(portal[1], f"/v1/assignments/{FIRST}/history", token=other_token))["submissions"] == []


@pytest.mark.parametrize("bad", ["not-gzip", "starter-kind", "parent-traversal"])
def test_invalid_bundle_does_not_create_receipt_and_valid_retry_recovers(portal, tmp_path, bad):
    token = connect(portal)["access_token"]
    if bad == "not-gzip":
        body = b"not a gzip archive"
    elif bad == "starter-kind":
        body = portal[2][COURSE].get_bundle_starter(token, FIRST).path.read_bytes()
    else:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            info = tarfile.TarInfo("../outside.txt")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"data"))
        body = output.getvalue()
    error(submit(portal, token, body), 422, "invalid_bundle")
    assert decoded(api(portal[1], f"/v1/assignments/{FIRST}/history", token=token))["submissions"] == []
    decoded(submit(portal, token, bundle(portal, tmp_path)), 202)
    assert not (tmp_path.parent / "outside.txt").exists()


def test_upload_type_and_size_rejections_do_not_break_next_valid_request(portal, tmp_path, monkeypatch):
    token = connect(portal)["access_token"]
    body = bundle(portal, tmp_path)
    error(submit(portal, token, body, content_type="application/zip"), 415, "unsupported_media_type")
    with monkeypatch.context() as context:
        context.setattr(portal[1], "max_bundle_request_bytes", 64)
        error(submit(portal, token, body), 413, "payload_too_large")
    decoded(submit(portal, token, body), 202)


def test_duplicate_retry_is_same_receipt_but_changed_payload_same_key_conflicts(portal, tmp_path):
    token = connect(portal)["access_token"]
    body = bundle(portal, tmp_path)
    first = decoded(submit(portal, token, body), 202)
    again = decoded(submit(portal, token, body), 202)
    assert again["replayed"] is True
    assert again["submission"]["submission_id"] == first["submission"]["submission_id"]
    changed = bundle(portal, tmp_path, "changed", "different solution")
    error(submit(portal, token, changed), 409, "idempotency_conflict")
    assert len(decoded(api(portal[1], f"/v1/assignments/{FIRST}/history", token=token))["submissions"]) == 1
    second = decoded(submit(portal, token, changed, key="intentional-second-version"), 202)
    assert second["submission"]["submission_id"] != first["submission"]["submission_id"]
    error(api(portal[1], f"/v1/submissions/{second['submission']['submission_id']}/result", token=token), 404, "result_not_available")


def test_deadline_rejects_new_work_but_allows_exact_acknowledgement_retry(portal, tmp_path):
    service = portal[2][COURSE]
    now = datetime.now(timezone.utc)
    service._now = lambda: now
    assignment = "asn_exception_deadline"
    clone_release(portal, assignment, due_at=now + timedelta(seconds=30))
    token = connect(portal, assignment)["access_token"]
    body = bundle(portal, tmp_path)
    first = decoded(submit(portal, token, body, assignment=assignment), 202)
    now += timedelta(seconds=30)
    replay = decoded(submit(portal, token, body, assignment=assignment), 202)
    assert replay["submission"]["submission_id"] == first["submission"]["submission_id"]
    error(submit(portal, token, body, assignment=assignment, key="after-deadline-new"), 403, "assignment_closed")


def test_queue_limit_returns_retry_hint_instead_of_accepting_lost_work(portal, tmp_path):
    token = connect(portal)["access_token"]
    for number in range(3):
        body = bundle(portal, tmp_path, f"answer-{number}", f"solution {number}")
        decoded(submit(portal, token, body, key=f"outstanding-{number}"), 202)
    response = submit(portal, token, bundle(portal, tmp_path, "fourth"), key="outstanding-fourth")
    error(response, 429, "submission_limit_exceeded")
    assert response[1]["retry-after"] == "60"
    assert len(decoded(api(portal[1], f"/v1/assignments/{FIRST}/history", token=token))["submissions"]) == 3


def test_storage_failure_is_retryable_and_does_not_admit_partial_submission(portal, tmp_path, monkeypatch):
    token = connect(portal)["access_token"]
    body = bundle(portal, tmp_path)
    def fail(*_args, **_kwargs):
        raise OSError("synthetic-private-detail-must-not-be-exposed")
    with monkeypatch.context() as context:
        context.setattr(portal[4], "ingest", fail)
        response = submit(portal, token, body)
        error(response, 503, "bundle_submission_unavailable")
        assert b"synthetic-private-detail" not in response[2]
    assert decoded(api(portal[1], f"/v1/assignments/{FIRST}/history", token=token))["submissions"] == []
    decoded(submit(portal, token, body), 202)


def test_queue_notification_failure_preserves_durable_receipt_for_recovery(portal, tmp_path, capsys):
    token = connect(portal)["access_token"]
    def fail(_submission):
        raise RuntimeError("synthetic-private-detail-must-not-be-exposed")
    portal[2][COURSE].notify_bundle_submission = fail
    first = decoded(submit(portal, token, bundle(portal, tmp_path)), 202)
    receipt = first["submission"]["submission_id"]
    assert decoded(api(portal[1], f"/v1/submissions/{receipt}", token=token))["submission"]["state"] == "accepted"
    assert [row["submission_id"] for row in decoded(api(portal[1], f"/v1/assignments/{FIRST}/history", token=token))["submissions"]] == [receipt]
    captured = capsys.readouterr()
    assert "bundle_submission_notify_failed" in captured.err
    assert "synthetic-private-detail" not in captured.err + captured.out
    assert token not in captured.err + captured.out
    events = [json.loads(line) for line in captured.err.splitlines() if line]
    failures = [event for event in events if event["event"] == "bundle_submission_notify_failed"]
    assert len(failures) == 1
    assert failures[0]["submission_id"] == receipt
