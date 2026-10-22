#!/usr/bin/env python3
"""Exercise a running prepare_codeoss_e2e.py fixture over real loopback HTTP.

Uses the HTTP-only synthetic student, leaving the GUI student for actual IDE
tests. Passwords and tokens are never printed or persisted here. Claim codes
are never printed; --issue-gui-claim writes one to an owner-only file for IDE input.
This is a server/API test, NOT proof that the IDE UI performed the same steps.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import tarfile
import tempfile
import time
from urllib.parse import urlencode, urlsplit

from autograde.platform_bundle import BundleStore


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Client:
    def __init__(self, origin):
        parsed = urlsplit(origin)
        require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                and parsed.port and not parsed.username and not parsed.password
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
                "E2E only permits explicit loopback HTTP fixture origins")
        self.origin, self.port = origin.rstrip("/"), parsed.port

    def request(self, method, path, *, payload=None, fields=None, raw=None,
                cookie=None, token=None, idempotency=None):
        headers, body = {}, raw
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        if fields is not None:
            headers.update({"Content-Type": "application/x-www-form-urlencoded", "Origin": self.origin})
            body = urlencode(fields).encode()
        if raw is not None:
            headers["Content-Type"] = "application/gzip"
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if idempotency:
            headers["Idempotency-Key"] = idempotency
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def json(self, method, path, *, expected=200, **kwargs):
        status, _, raw = self.request(method, path, **kwargs)
        require(status == expected, f"{method} {path}: expected HTTP {expected}, got {status}")
        return json.loads(raw)


def csrf(raw):
    match = re.search(rb'name="csrf" value="([^"]+)"', raw)
    require(match is not None, "web form did not contain CSRF protection")
    return match[1].decode()


def load_fixture(fixture_path, student_key):
    root = fixture_path.resolve().parent
    fixture = json.loads(fixture_path.read_text())
    require(Path(fixture["fixture_root"]).resolve() == root, "fixture root mismatch")
    require(Path(fixture["credentials_file"]).resolve() == root / "synthetic-credentials.json", "credentials outside fixture")
    credentials = json.loads((root / "synthetic-credentials.json").read_text())
    require(student_key in {"codeoss-http-002", "codeoss-gui-001"}, "only isolated synthetic students are allowed")
    student = next(entry for entry in credentials["students"] if entry["student_key"] == student_key)
    web, api = Client(fixture["web_url"]), Client(fixture["api_url"])
    course, assignment = fixture["course_key"], fixture["assignment_id"]
    require(course == "come2201" and assignment == "basn_codeoss_cpp_e2e", "unexpected synthetic assignment")
    for client in (web, api):
        require(client.json("GET", "/readyz") == {"status": "ready"}, "listener not ready")
    return root, fixture, student, web, api


def issue_claim(web, course, assignment, student):
    status, headers, page = web.request("GET", f"/courses/{course}")
    require(status == 200, "student entry page unavailable")
    cookie = headers["set-cookie"].split(";", 1)[0]
    status, headers, page = web.request("POST", f"/courses/{course}/login", cookie=cookie,
        fields={"csrf": csrf(page), "student_key": student["student_key"], "password": student["password"]})
    require(status == 200 and assignment.encode() in page, "student could not select published fixture")
    cookie = headers["set-cookie"].split(";", 1)[0]
    status, _, page = web.request("POST", f"/courses/{course}/claims", cookie=cookie,
        fields={"csrf": csrf(page), "assignment_id": assignment})
    require(status == 200, "web did not issue assignment claim")
    match = re.search(rb'<div id="claim-code"><code>([^<]+)</code>', page)
    require(match is not None, "claim page did not contain code")
    return match[1].decode()


def issue_gui_claim(fixture_path, assignment_id=None):
    root, fixture, student, web, _api = load_fixture(fixture_path, "codeoss-gui-001")
    assignment_id = assignment_id or fixture["assignment_id"]
    allowed = {fixture["assignment_id"]}
    if fixture.get("observer", {}).get("assignment_id") == "basn_codeoss_observer_e2e":
        allowed.add("basn_codeoss_observer_e2e")
    require(assignment_id in allowed, "only fixture assignments are allowed")
    claim = issue_claim(web, fixture["course_key"], assignment_id, student)
    descriptor, filename = tempfile.mkstemp(prefix="gui-claim-", suffix=".txt", dir=root)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(claim + "\n")
    return {"claim_file": filename, "student_key": student["student_key"],
            "notice": "Read the private file into the IDE; never print or commit the code. It expires."}


def run(fixture_path):
    root, fixture, student, web, api = load_fixture(fixture_path, "codeoss-http-002")
    course, assignment = fixture["course_key"], fixture["assignment_id"]
    claim = issue_claim(web, course, assignment, student)
    device = api.json("POST", "/v1/device-authorizations", expected=201,
        payload={"device_name": "Code-OSS HTTP E2E synthetic client", "client": "vscode-extension",
                 "extension_version": "0.5.7", "claim_code": claim})
    accepted = api.json("POST", "/v1/assignment-claims/redeem",
        payload={"claim_code": claim, "device_code": device["device_code"]})
    require(accepted["assignment_id"] == assignment, "claim accepted a different assignment")
    tokens = api.json("POST", "/v1/device-authorizations/token", payload={"device_code": device["device_code"]})
    token = tokens["access_token"]
    assignments = api.json("GET", "/v1/assignments", token=token)["assignments"]
    require([entry["assignment_id"] for entry in assignments] == [assignment], "accepted-assignment scope mismatch")
    status, headers, starter = api.request("GET", f"/v1/assignments/{assignment}/starter", token=token)
    require(status == 200, "starter download failed")
    require("sha256:" + hashlib.sha256(starter).hexdigest() == headers["x-autograde-sha256"], "starter transport checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(starter), mode="r:gz") as archive:
        files = [entry.name for entry in archive.getmembers() if entry.isfile()]
        require("main.cpp" in files and "README.md" in files, "starter omitted C++ sources or instructions")
        require(not any("grade.py" in name or "expected.txt" in name for name in files), "private grading files leaked")
    store = BundleStore(root / "http-client-bundles")
    compile_error = Path(tempfile.mkdtemp(prefix="compile-error-", dir=root))
    (compile_error / "main.cpp").write_text("int main( { broken syntax\n")
    outcomes = []
    baseline_best = None
    sources = ((Path(fixture["negative_solution"]), 5), (Path(fixture["solution"]), 10),
               (Path(fixture["negative_solution"]), 5), (compile_error, 0))
    for index, (source, score) in enumerate(sources):
        bundle = store.create_from_directory(source, kind="submission")
        idempotency = f"codeoss-e2e-{secrets.token_hex(16)}"
        receipt = api.json("POST", f"/v1/assignments/{assignment}/submissions", expected=202,
            raw=bundle.path.read_bytes(), token=token, idempotency=idempotency)["submission"]
        submission = receipt["submission_id"]
        repeated = api.json("POST", f"/v1/assignments/{assignment}/submissions", expected=202,
            raw=bundle.path.read_bytes(), token=token, idempotency=idempotency)["submission"]
        require(repeated["submission_id"] == submission, "idempotent retry created another submission")
        require(submission not in [entry["submission_id"] for entry in outcomes], "submission receipt reused old ID")
        deadline, grade = time.monotonic() + 60, None
        while time.monotonic() < deadline:
            status, _, body = api.request("GET", f"/v1/submissions/{submission}/result", token=token)
            if status == 200:
                grade = json.loads(body)["result"]
                break
            require(status == 404 and json.loads(body).get("error", {}).get("code") == "result_not_available",
                    "unexpected result polling failure")
            time.sleep(.2)
        require(grade is not None, "grading did not publish within 60 seconds")
        require(grade["submission_id"] == submission, "current submission returned another submission's result")
        require(grade["score"] == score, f"submission {index + 1} score did not match expected {score}")
        require(grade["source_sha256"] == receipt["source_sha256"], "graded source differs from accepted source")
        if index == 0:
            baseline_best = grade["previous_best"]["score"] if grade["previous_best"] else 0
        else:
            require(grade["previous_best"]["score"] == max(baseline_best, *(entry["score"] for entry in outcomes)),
                    "previous best is not independent of current score")
        require(grade["rubric"]["output"]["feedback"], "output feedback absent")
        outcomes.append({"submission_id": submission, "received_at": receipt["received_at"],
            "source_sha256": receipt["source_sha256"], "score": grade["score"],
            "previous_best": grade["previous_best"], "rubric": grade["rubric"]})
    history = api.json("GET", f"/v1/assignments/{assignment}/history", token=token)["submissions"]
    require([entry["submission_id"] for entry in history[:4]] == [entry["submission_id"] for entry in reversed(outcomes)],
            "history does not list latest receipt first")
    summary = {"ok": True, "scope": "real loopback HTTP + SQLite + native C++17 grader; not IDE UI",
        "student_key": student["student_key"], "assignment_id": assignment, "starter_files": files,
        "submissions": outcomes, "checks": ["web password login + CSRF", "claim code issue/redeem",
        "accepted assignment scope", "starter checksum + private-file exclusion", "four real C++ compilations incl. syntax error",
        "idempotent retry deduplication", "current result ID/source binding", "current 5/10/5/0 versus previous best", "newest-first history"]}
    (root / "http-e2e-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path, help="fixture.json created by prepare_codeoss_e2e.py")
    parser.add_argument("--issue-gui-claim", action="store_true", help="issue only a GUI student's claim into a private file")
    parser.add_argument("--assignment", choices=("basn_codeoss_cpp_e2e", "basn_codeoss_observer_e2e"), help="GUI claim assignment")
    arguments = parser.parse_args()
    if arguments.assignment and not arguments.issue_gui_claim:
        parser.error("--assignment requires --issue-gui-claim")
    result = issue_gui_claim(arguments.fixture, arguments.assignment) if arguments.issue_gui_claim else run(arguments.fixture)
    print(json.dumps(result, ensure_ascii=False, indent=2))
