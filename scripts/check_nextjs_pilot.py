#!/usr/bin/env python3
"""Local, synthetic HTTP admission/load gate for the Next.js read-only pilot.

After the editable project install, run
``.venv/bin/python scripts/check_nextjs_pilot.py`` without extra environment variables.
Only loopback listeners and temporary files are used. No student code is executed.
The 5,000 historical submissions are explicitly seeded fixture records, not 5,000
HTTP admissions; the separate concurrency gate exercises 25 actual admissions.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import http.client
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode

from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_web import InstructorWeb
from autograde.platform_bundle import BundleStore
from autograde.platform_http import create_server
from autograde.platform_portal import CourseAPI, CoursePortal
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore

COURSE = "come2201"
OTHER_COURSE = "come3105"
SECRET = b"synthetic-next-pilot-secret-value"
PASSWORD = "synthetic-next-pilot-password"
WEB_ORIGIN = "http://127.0.0.1:18081"
API_ORIGIN = "http://127.0.0.1:18080"
API_PREFIX = "/instructor/api/v1"
STUDENTS = 25


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def basic(username):
    return "Basic " + base64.b64encode(f"{username}:{PASSWORD}".encode()).decode()


@contextmanager
def serving(facade, origin, port=0):
    server = create_server(("127.0.0.1", port), facade,
                           public_base_url=origin, max_concurrent_requests=32,
                           request_timeout_seconds=60)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def request(server, path, *, method="GET", fields=None, payload=None,
            cookie=None, authorization=None, raw=None, idempotency=None):
    headers = {}
    body = raw
    if fields is not None:
        body = urlencode(fields).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = WEB_ORIGIN
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if raw is not None:
        headers["Content-Type"] = "application/gzip"
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    if cookie:
        headers["Cookie"] = cookie
    if authorization:
        headers["Authorization"] = authorization
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=60)
    start = time.perf_counter()
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        data = response.read()
        return dict(status=response.status, headers=dict(response.getheaders()),
                    body=data, ms=(time.perf_counter() - start) * 1000,
                    bytes=len(data))
    finally:
        connection.close()


def checked(result, status=200):
    require(result["status"] == status,
            f"HTTP {result['status']}, expected {status}: {result['body'][:500]!r}")
    return result


def cookie(result):
    return result["headers"]["Set-Cookie"].split(";", 1)[0]


def csrf(result):
    found = re.search(rb'name="csrf" value="([^"]+)"', result["body"])
    require(found is not None, "missing student form CSRF")
    return found[1].decode()


def stats(samples):
    values = sorted(item["ms"] for item in samples)
    require(bool(values), "missing measurement samples")
    return dict(requests=len(values), median_ms=round(statistics.median(values), 2),
                p95_ms=round(values[math.ceil(len(values) * .95) - 1], 2),
                max_ms=round(values[-1], 2),
                median_bytes=int(statistics.median(item["bytes"] for item in samples)))


class PilotFixture:
    def __init__(self, root, *, learning_runtime_socket=None, starter_files=None):
        self.root = root
        self.state = PlatformStateStore(root / "synthetic.sqlite3")
        self.bundles = BundleStore(root / "bundles")
        self.identities = InstructorIdentityStore(root / "identities.sqlite3")
        self.identities.initialize()
        for username, course in (("teacher", COURSE), ("other", OTHER_COURSE)):
            self.identities.create(username, f"Synthetic {username}", "instructor", PASSWORD)
            self.identities.set_grant(username, course, allowed=True)
        self.identities.create("reviewer", "Synthetic browser reviewer", "instructor", PASSWORD)
        for course in (COURSE, OTHER_COURSE):
            self.identities.set_grant("reviewer", course, allowed=True)
        starter = root / "starter"
        starter.mkdir()
        for name, content in (starter_files or {"answer.txt": "Synthetic admission fixture only.\n"}).items():
            (starter / name).write_text(content)
        self.starter = self.bundles.create_from_directory(starter, kind="starter")
        self.source = self.bundles.create_from_directory(starter, kind="submission")
        self.now = datetime.now(timezone.utc)
        self.services = {course: StudentPlatformService(
            state=self.state, server_secret=SECRET, course_key=course,
            public_base_url=API_ORIGIN, bundle_store=self.bundles,
            learning_runtime_socket=learning_runtime_socket,
            instructor_authorizer=self.identities.authorize_course)
            for course in (COURSE, OTHER_COURSE)}
        self.courses = CourseAdminService(self.state)
        self.policy = frozenset((COURSE, OTHER_COURSE))
        self.instructor = InstructorWeb(
            self.courses, EnrollmentAdminService(self.state, SECRET), None,
            authorize=self.services[COURSE]._authorize_instructor, secret=SECRET,
            web_url=WEB_ORIGIN, identities=self.identities,
            next_ui_policy=lambda: self.policy,
            learning_runtime_overview=lambda key: self.services[key].learning_runtime_overview(),
            submissions=lambda key, auth: self.services[key].instructor_dashboard_page(auth, portal=True),
            submission_updates=lambda key, auth: self.services[key].instructor_dashboard_updates(auth, portal=True))
        self.portal = CoursePortal(self.services, SECRET, WEB_ORIGIN, API_ORIGIN,
                                   courses=self.courses, instructor=self.instructor)
        self.api = CourseAPI(self.services, SECRET, courses=self.courses)
        self.students = []
        for number in range(1, STUDENTS + 1):
            key = f"2026{number:04d}"
            student = self.state.upsert_local_student(student_key=key, auth_subject="local:" + key)
            self.state.upsert_enrollment(student_id=student.id, course_key=COURSE)
            self.services[COURSE].set_student_password(student_key=key, password=f"{number:06d}")
            self.students.append((number, student))
        self.release(0)
        self.release(0, course=OTHER_COURSE)

    @staticmethod
    def assignment(index, course=COURSE):
        return f"pilot_{course}_{index:02d}"

    def release(self, index, course=COURSE):
        self.state.register_bundle_assignment_release(
            assignment_id=self.assignment(index, course), course_key=course,
            assignment_key=f"lab-{index:02d}", release_id="v1", title=f"Synthetic exercise {index:02d}",
            starter_path=str(self.starter.path), starter_digest=self.starter.digest,
            starter_size_bytes=self.starter.compressed_bytes, assessment_path=str(self.root / "unused-assessment"),
            assessment_digest="a" * 64, runner_image="registry.example.invalid/synthetic@sha256:" + "e" * 64,
            rubric_version="v1", max_score=100, result_policy="immediate",
            opens_at=self.now - timedelta(days=2), due_at=self.now + timedelta(days=2), ready=True)

    def student_flow(self, web, api, number):
        samples = []

        def visit(server, path, status=200, *, operation, **options):
            result = checked(request(server, path, **options), status)
            result["operation"] = operation
            samples.append(result)
            return result

        entry = visit(web, f"/courses/{COURSE}", operation="login_page")
        logged = visit(web, f"/courses/{COURSE}/login", operation="password_login", method="POST", cookie=cookie(entry),
                       fields=dict(csrf=csrf(entry), student_key=f"2026{number:04d}", password=f"{number:06d}"))
        claimed = visit(web, f"/courses/{COURSE}/claims", operation="claim", method="POST", cookie=cookie(logged),
                        fields=dict(csrf=csrf(logged), assignment_id=self.assignment(0)))
        match = re.search(rb"<code>(AK1-[A-Z0-9-]+)</code>", claimed["body"])
        require(match is not None, "student claim code missing")
        code = match[1].decode()
        device = json.loads(visit(api, "/v1/device-authorizations", operation="device_start", status=201, method="POST",
                                 payload=dict(device_name="Synthetic Next pilot", claim_code=code))["body"])["device_code"]
        visit(api, "/v1/assignment-claims/redeem", operation="redeem", method="POST", payload=dict(device_code=device, claim_code=code))
        token = json.loads(visit(api, "/v1/device-authorizations/token", operation="token", method="POST",
                                payload=dict(device_code=device))["body"])["access_token"]
        auth = "Bearer " + token
        visit(api, f"/v1/assignments/{self.assignment(0)}/starter", operation="download", authorization=auth)
        received = json.loads(visit(api, f"/v1/assignments/{self.assignment(0)}/submissions",
                                   operation="submit", status=202, method="POST", authorization=auth,
                                   raw=self.source.path.read_bytes(), idempotency=f"next-pilot-{number:03d}")["body"])
        sid = received["submission"]["submission_id"]
        current = json.loads(visit(api, f"/v1/submissions/{sid}", operation="status", authorization=auth)["body"])
        require(current["submission"]["submission_id"] == sid, "student status returned another submission")
        return dict(number=number, submission_id=sid, samples=samples)

    def concurrent_admissions(self, web, api):
        barrier = threading.Barrier(STUDENTS + 2)
        completed = threading.Event()

        def student(number):
            barrier.wait(timeout=30)
            return self.student_flow(web, api, number)

        def instructor(username, course):
            barrier.wait(timeout=30)
            authorization = basic(username)
            entry = checked(request(web, API_PREFIX + "/entry?screen=results&course_key=" + course,
                                    authorization=authorization), 303)
            session = cookie(entry)
            samples = [entry]
            while not completed.is_set() or len(samples) < 3:
                result = checked(request(web, f"{API_PREFIX}/courses/{course}/results?page_size=20",
                                         authorization=authorization, cookie=session))
                body = json.loads(result["body"])
                require(body["course_key"] == course, "instructor response crossed courses")
                require(result["headers"]["Cache-Control"] == "no-store", "sensitive response is cacheable")
                samples.append(result)
                completed.wait(.05)
            return dict(username=username, course=course, samples=samples)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=STUDENTS + 2) as pool:
            students = [pool.submit(student, number) for number, _ in self.students]
            instructors = [pool.submit(instructor, "teacher", COURSE), pool.submit(instructor, "other", OTHER_COURSE)]
            try:
                results = [future.result(timeout=120) for future in students]
            finally:
                completed.set()
            reads = [future.result(timeout=60) for future in instructors]
        require(len({result["submission_id"] for result in results}) == STUDENTS, "duplicate student receipts")
        with self.state._connection() as db:
            count = db.execute("SELECT COUNT(*) FROM bundle_submission_requests").fetchone()[0]
        require(count == STUDENTS, "admission count differs from distinct students")
        student_samples = [sample for result in results for sample in result["samples"]]
        operations = {name: stats([sample for sample in student_samples if sample["operation"] == name])
                      for name in sorted({sample["operation"] for sample in student_samples})}
        return dict(students=STUDENTS, instructors=2, elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                    student_requests=stats(student_samples), student_operations=operations,
                    instructor_requests=stats([sample for result in reads for sample in result["samples"]]),
                    admitted_submissions=count, student_workflow="login, claim, redeem, token, download, submit, status")

    def seed_history(self, assignment_count=20, attempts=10):
        """Bulk historical fixture: clone real admission shapes inside one transaction.

        This bypasses admission quotas intentionally for reproducible read testing.
        Every record still has an immutable receipt and foreign-key-valid session.
        No grading code runs; the final attempt scores 0, the previous one 100.
        """
        for index in range(1, assignment_count):
            self.release(index)
        now = self.now + timedelta(minutes=1)
        with self.state._write() as db:
            seeds = [dict(row) for row in db.execute("SELECT * FROM bundle_submission_requests ORDER BY student_id")]
            require(len(seeds) == STUDENTS, "history must follow exactly 25 real admissions")

            def insert(table, values):
                names = list(values)
                db.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                           [values[name] for name in names])

            for seed in seeds:
                receipt = dict(db.execute("SELECT * FROM bundle_submission_receipts WHERE submission_id=?", (seed["submission_id"],)).fetchone())
                for index in range(assignment_count):
                    for attempt in range(attempts):
                        sid = f"bsub_pilot_{seed['student_id']:04d}_{index:02d}_{attempt:02d}"
                        stamp = (now + timedelta(seconds=index * attempts + attempt)).isoformat()
                        if index == 0 and attempt == 0:
                            sid = seed["submission_id"]
                            # The receipt remains byte-for-byte immutable.
                            db.execute("UPDATE bundle_submission_requests SET state='published' WHERE submission_id=?", (sid,))
                            stamp = seed["received_at"]
                        else:
                            item = dict(seed, submission_id=sid, assignment_id=self.assignment(index),
                                        idempotency_key=sid, state="published", received_at=stamp, updated_at=stamp)
                            insert("bundle_submission_requests", item)
                            insert("bundle_submission_receipts", dict(receipt, receipt_id="receipt-" + sid,
                                   submission_id=sid, assignment_id=self.assignment(index), assignment_key=f"lab-{index:02d}",
                                   received_at=stamp, accepted_at=stamp))
                        insert("bundle_submission_results", dict(result_id="result-" + sid, submission_id=sid,
                               student_id=seed["student_id"], source_digest=seed["source_digest"],
                               score=0 if attempt == attempts - 1 else 100, max_score=100,
                               rubric_json="{}", diagnostics_json="[]", created_at=stamp, published_at=stamp))
            require(not list(db.execute("PRAGMA foreign_key_check")), "invalid synthetic foreign keys")
            count = db.execute("SELECT COUNT(*) FROM bundle_submission_requests").fetchone()[0]
        require(count == STUDENTS * assignment_count * attempts, "incorrect history fixture size")
        return dict(students=STUDENTS, assignments=assignment_count,
                    result_rows=STUDENTS * assignment_count, submissions=count)

    def compare_reads(self, web, samples=10):
        auth = basic("teacher")
        entry = checked(request(web, API_PREFIX + "/entry?screen=results", authorization=auth), 303)
        session = cookie(entry)
        paths = dict(legacy_html=f"/courses/{COURSE}/instructor/submissions",
                     legacy_live=f"/courses/{COURSE}/instructor/submissions/live",
                     next_json=f"{API_PREFIX}/courses/{COURSE}/results?page=1&page_size=20")
        measurements = {name: [] for name in paths}
        # Warm up each path once; alternate order to reduce first-path/cache bias.
        for path in paths.values():
            checked(request(web, path, authorization=auth, cookie=session))
        for iteration in range(samples):
            for name in (list(paths) if iteration % 2 == 0 else list(reversed(paths))):
                measured = checked(request(web, paths[name], authorization=auth, cookie=session))
                require(measured["headers"]["Cache-Control"] == "no-store", "cacheable result response")
                measurements[name].append(measured)
        payload = json.loads(measurements["next_json"][-1]["body"])
        require(len(payload["rows"]) == 20, "new API page is not bounded at 20 rows")
        require(all(row["score"] == 0 and row["previous_best"]["score"] == 100 for row in payload["rows"]),
                "latest zero and previous best 100 must remain separate")
        with self.state._connection() as db:
            rows = db.execute("SELECT COUNT(DISTINCT student_id || ':' || assignment_id) FROM bundle_submission_requests").fetchone()[0]
        require(payload["total"] == rows, "paginated total does not match synthetic ledger")
        return {name: stats(values) for name, values in measurements.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--output", type=Path, help="save the sanitized measurement report as JSON")
    parser.add_argument("--serve", action="store_true", help="keep synthetic web fixture on --port for browser QA after checks")
    parser.add_argument("--port", type=int, default=18081)
    options = parser.parse_args()
    require(options.samples >= 1, "samples must be positive")
    with tempfile.TemporaryDirectory(prefix="autograde-next-pilot-") as directory, ExitStack() as stack:
        fixture = PilotFixture(Path(directory))
        web = stack.enter_context(serving(fixture.portal, WEB_ORIGIN, options.port if options.serve else 0))
        api = stack.enter_context(serving(fixture.api, API_ORIGIN))
        report = dict(concurrent=fixture.concurrent_admissions(web, api))
        report["fixture"] = fixture.seed_history()
        report["comparison"] = fixture.compare_reads(web, samples=options.samples)
        processor = platform.processor()
        if platform.system() == "Darwin":
            processor = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        report["environment"] = dict(os=platform.system(), os_version=platform.mac_ver()[0] or platform.release(),
                                     architecture=platform.machine(), processor=processor,
                                     logical_cpus=os.cpu_count(), python=platform.python_version())
        report["status"] = "passed"
        report["http_failures"] = 0
        report["measured_at"] = datetime.now(timezone.utc).isoformat()
        report["method"] = "One warmup per read path; alternating path order; p95 is nearest-rank; latencies include HTTP and auth."
        report["limitations"] = ["Local synthetic loopback measurement; not production capacity or WAN latency.",
                                  "Historical records are seeded; only 25 admissions use real HTTP.",
                                  "No grader runs; queue wait and grading runtime are not measured.",
                                  "Legacy reads return all 500 rows; JSON read returns one 20-row page."]
        print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
        if options.output:
            options.output.parent.mkdir(parents=True, exist_ok=True)
            options.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if options.serve:
            print(f"Synthetic browser fixture listening on http://127.0.0.1:{web.server_address[1]}; credentials reviewer/{PASSWORD}", flush=True)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
