#!/usr/bin/env python3
"""Synthetic HTTP + real Linux execution gate. No production students or grades.

Build runtime/learning-cpp, then pass its exact local sha256 image ID. The worker,
server SQLite, source snapshots and sockets live in a temporary directory. This
gate never substitutes host compilation when Docker is unavailable.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

import check_nextjs_pilot as pilot_fixture
from check_nextjs_pilot import (
    PilotFixture, COURSE, OTHER_COURSE, serving, request, checked, cookie, csrf, basic,
)


def body(response):
    return json.loads(response["body"])


def accept(web, api, number, assignment):
    entry = checked(request(web, f"/courses/{COURSE}"))
    logged = checked(request(web, f"/courses/{COURSE}/login", method="POST", cookie=cookie(entry),
        fields=dict(csrf=csrf(entry), student_key=f"2026{number:04d}", password=f"{number:06d}")))
    claimed = checked(request(web, f"/courses/{COURSE}/claims", method="POST", cookie=cookie(logged),
        fields=dict(csrf=csrf(logged), assignment_id=assignment)))
    code = re.search(rb"<code>(AK1-[A-Z0-9-]+)</code>", claimed["body"]).group(1).decode()
    device = body(checked(request(api, "/v1/device-authorizations", method="POST",
        payload=dict(device_name="Synthetic remote Linux IDE", claim_code=code)), 201))["device_code"]
    checked(request(api, "/v1/assignment-claims/redeem", method="POST",
        payload=dict(device_code=device, claim_code=code)))
    tokens = body(checked(request(api, "/v1/device-authorizations/token", method="POST",
        payload=dict(device_code=device))))
    return "Bearer " + tokens["access_token"]


@contextmanager
def worker(root, image):
    socket = root / "worker.sock"
    log = root / "worker.log"
    with log.open("wb") as output:
        process = subprocess.Popen([sys.executable, "-m", "autograde.learning_runtime_worker",
            "--socket", str(socket), "--root", str(root / "worker"), "--image", image],
            stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 25
            while not socket.exists():
                if process.poll() is not None:
                    raise RuntimeError("Worker failed to start: " + log.read_text()[-3000:])
                if time.monotonic() > deadline:
                    raise RuntimeError("Worker socket startup timed out")
                time.sleep(.1)
            yield socket
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def run_gate(fixture, web, api):
    assignment = fixture.assignment(0)
    route = f"/v1/assignments/{assignment}/learning-runtime"
    auth = accept(web, api, 1, assignment)
    other = accept(web, api, 2, assignment)
    checks = []
    latest = {}
    fixtures_clock = datetime.now(timezone.utc)

    def verify(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def call(path, payload=None, authorization=auth, status=200):
        return body(checked(request(api, path, method="POST" if payload is not None else "GET",
            payload=payload, authorization=authorization), status))

    def save(files, *, authorization=auth):
        nonlocal fixtures_clock
        # Advance only the synthetic server clock; avoids waiting for the real
        # checkpoint throttle and does not alter production policy constants.
        fixtures_clock += timedelta(seconds=12)
        fixture.services[COURSE]._now = lambda: fixtures_clock
        tree = fixture.root / ("source-" + str(time.monotonic_ns()))
        tree.mkdir()
        for name, content in files.items():
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        artifact = fixture.bundles.create_from_directory(tree, kind="submission")
        result = body(checked(request(api, f"/v1/assignments/{assignment}/learning-checkpoints",
            method="POST", authorization=authorization, raw=artifact.path.read_bytes(),
            idempotency="runtime-gate-" + str(time.monotonic_ns()))))
        checkpoint = result["checkpoint"]["checkpoint_id"]
        latest[authorization] = checkpoint
        return checkpoint

    c = '#include <stdio.h>\nint main(void){int n; if(scanf("%d", &n)!=1)return 2; printf("C:%d\\n",n*2);return 0;}\n'
    cpp = '#include <iostream>\nint main(){std::string s;std::cin>>s;std::cout<<"Hello "<<s<<"\\n";}\n'
    cp = save({"main.c": c, "main.cpp": cpp})
    cp_other = save({"main.c": c}, authorization=other)
    current = call(route)
    verify("remote_provider_enabled", current["enabled"] and current["provider"] == "remote_linux")
    session = call(route + "/sessions", {"checkpoint_id": cp})["session"]
    sid = session["id"]
    other_sid = call(route + "/sessions", {"checkpoint_id": cp_other}, other)["session"]["id"]
    verify("students_receive_different_spaces", sid != other_sid)
    endpoint = route + "/sessions/" + sid

    result = call(endpoint + "/run", {"checkpoint_id": cp, "language": "c", "sources": ["main.c"], "stdin": "21\n"})
    verify("c_compile_and_stdin_run", result["status"] == "completed" and result["run"]["stdout"] == "C:42\n")
    result = call(endpoint + "/run", {"checkpoint_id": cp, "language": "cpp", "sources": ["main.cpp"], "stdin": "Autograde\n"})
    verify("cpp_compile_and_stdin_run", result["status"] == "completed" and result["run"]["stdout"] == "Hello Autograde\n")

    foreign = request(api, endpoint + "/run", method="POST", authorization=other,
        payload={"checkpoint_id": cp_other, "language": "c", "sources": ["main.c"], "stdin": "2"})
    verify("cross_student_space_denied", foreign["status"] in (403, 404))
    escaped = request(api, endpoint + "/run", method="POST", authorization=auth,
        payload={"checkpoint_id": cp, "language": "c", "sources": ["../main.c"], "stdin": ""})
    verify("source_path_escape_denied", escaped["status"] in (400, 422))

    errors = save({"main.c": "int main(void) { this is not C; }\n"})
    result = call(endpoint + "/run", {"checkpoint_id": errors, "language": "c", "sources": ["main.c"], "stdin": ""})
    verify("compile_error_is_not_run_success", result["status"] == "compile_error" and bool(result["compile"]["stderr"]) and result["run"] is None)
    nonzero = save({"main.c": "int main(void) { return 7; }\n"})
    result = call(endpoint + "/run", {"checkpoint_id": nonzero, "language": "c", "sources": ["main.c"], "stdin": ""})
    verify("nonzero_exit_visible", result["status"] == "runtime_error" and result["run"]["exit_code"] == 7)
    endless = save({"main.c": "int main(void) { for(;;){} }\n"})
    start = time.monotonic()
    result = call(endpoint + "/run", {"checkpoint_id": endless, "language": "c", "sources": ["main.c"], "stdin": ""})
    verify("infinite_loop_stopped", result["status"] == "timeout" and time.monotonic()-start < 25)
    flood = save({"main.c": '#include <stdio.h>\nint main(void){for(;;)puts("output-bound");}\n'})
    result = call(endpoint + "/run", {"checkpoint_id": flood, "language": "c", "sources": ["main.c"], "stdin": ""})
    verify("output_flood_bounded", result["status"] == "output_limit" and len(result["run"]["stdout"].encode()) <= 65536)
    readonly = save({"main.c": '#include <stdio.h>\nint main(void){FILE*f=fopen("main.c","w");if(f){fclose(f);return 1;}puts("source-read-only");return 0;}\n'})
    result = call(endpoint + "/run", {"checkpoint_id": readonly, "language": "c", "sources": ["main.c"], "stdin": ""})
    verify("source_is_read_only", result["status"] == "completed" and "source-read-only" in result["run"]["stdout"])

    echo = save({"main.c": '#include <stdio.h>\nint main(void){int c;while((c=getchar())!=EOF){if(putchar(c)==EOF)return 2;}return 0;}\n'})
    for check_name, stdin in (("korean_63kib_stdin_roundtrip", "가" * 21000),
                             ("full_64kib_stdin_roundtrip", "a" * 65536),
                             ("escaped_control_stdin_roundtrip", "\x01" * 60000)):
        result = call(endpoint + "/run", {"checkpoint_id": echo, "language": "c", "sources": ["main.c"], "stdin": stdin})
        verify(check_name, result["status"] == "completed" and result["run"]["stdout"] == stdin)
    binary = save({"main.c": '#include <stdio.h>\nint main(void){for(int i=0;i<20000;i++)putchar(255);return 0;}\n'})
    result = call(endpoint + "/run", {"checkpoint_id": binary, "language": "c", "sources": ["main.c"], "stdin": ""})
    verify("non_utf8_output_is_bounded_and_readable", result["status"] == "completed" and result["run"]["stdout"] == "\ufffd" * 20000)
    blocked_network = save({"main.c": '#include <stdio.h>\n#include <unistd.h>\n#include <sys/socket.h>\n#include <arpa/inet.h>\nint main(void){int s=socket(AF_INET,SOCK_STREAM,0);struct sockaddr_in a={0};a.sin_family=AF_INET;a.sin_port=htons(443);inet_pton(AF_INET,"203.0.113.1",&a.sin_addr);int r=connect(s,(struct sockaddr*)&a,sizeof(a));close(s);if(r==0)return 1;puts("network-blocked");return 0;}\n'})
    result = call(endpoint + "/run", {"checkpoint_id": blocked_network, "language": "c", "sources": ["main.c"], "stdin": ""})
    verify("outside_network_connection_blocked", result["status"] == "completed" and "network-blocked" in result["run"]["stdout"])

    verify("python_is_advertised", "python" in current["languages"])
    python = save({"src/main.py": "import helper\nfrom course_math import twice\nprint(helper.greeting, twice(int(input())))\n",
                   "src/helper.py": "greeting = 'Python:'\n", "course_math.py": "def twice(value): return value * 2\n"})
    result = call(endpoint + "/run", {"checkpoint_id": python, "language": "python", "sources": ["src/main.py"], "stdin": "21\n"})
    verify("python_entry_imports_snapshot_modules_and_reads_stdin", result["status"] == "completed" and result["run"]["stdout"] == "Python: 42\n")
    verify("python_has_no_separate_compile_phase", result["compile"] == {"exit_code": 0, "stdout": "", "stderr": ""})
    extra_entry = request(api, endpoint + "/run", method="POST", authorization=auth,
        payload={"checkpoint_id": python, "language": "python", "sources": ["src/main.py", "src/helper.py"], "stdin": ""})
    verify("python_multiple_entry_points_refused", extra_entry["status"] == 400)
    syntax = save({"main.py": "def broken(:\n"})
    result = call(endpoint + "/run", {"checkpoint_id": syntax, "language": "python", "sources": ["main.py"], "stdin": ""})
    verify("python_syntax_diagnostic", result["status"] == "runtime_error" and "SyntaxError" in result["run"]["stderr"])
    exception = save({"main.py": "raise ValueError('check the input')\n"})
    result = call(endpoint + "/run", {"checkpoint_id": exception, "language": "python", "sources": ["main.py"], "stdin": ""})
    verify("python_exception_diagnostic", result["status"] == "runtime_error" and "ValueError: check the input" in result["run"]["stderr"])
    limits = save({"main.py": "while True: pass\n"})
    result = call(endpoint + "/run", {"checkpoint_id": limits, "language": "python", "sources": ["main.py"], "stdin": ""})
    verify("python_timeout_stops_execution", result["status"] == "timeout")
    flood = save({"main.py": "while True: print('python-output-limit')\n"})
    result = call(endpoint + "/run", {"checkpoint_id": flood, "language": "python", "sources": ["main.py"], "stdin": ""})
    verify("python_output_bounded", result["status"] == "output_limit" and len(result["run"]["stdout"].encode()) <= 65536)
    isolated = save({"main.py": """import os, socket, sys
assert sys.flags.isolated == 1 and sys.dont_write_bytecode
assert os.getuid() == 10001
try:
    open('/workspace/main.py', 'w')
    raise AssertionError('source was writable')
except OSError:
    pass
try:
    socket.create_connection(('203.0.113.1', 443), timeout=1)
    raise AssertionError('outside connection succeeded')
except OSError:
    pass
print('isolated-readonly-network-blocked')
""", "sitecustomize.py": "raise RuntimeError('project startup hooks must not run')\n"})
    result = call(endpoint + "/run", {"checkpoint_id": isolated, "language": "python", "sources": ["main.py"], "stdin": ""})
    verify("python_isolated_readonly_network_blocked", result["status"] == "completed" and result["run"]["stdout"] == "isolated-readonly-network-blocked\n")
    unicode_python = save({"main.py": "import sys\nsys.stdout.write(sys.stdin.read())\n"})
    result = call(endpoint + "/run", {"checkpoint_id": unicode_python, "language": "python", "sources": ["main.py"], "stdin": "가" * 21000})
    verify("python_korean_stdin_roundtrip", result["status"] == "completed" and result["run"]["stdout"] == "가" * 21000)
    final_checkpoint = latest[auth]

    before = fixture.state.list_bundle_dashboard_rows(course_key=COURSE)
    verify("practice_does_not_submit_or_grade", all(row.submission_count == 0 for row in before))
    closed = call(endpoint + "/close", {"checkpoint_id": final_checkpoint})
    verify("end_removes_space_after_checkpoint", closed["status"] == "closed" and closed["cleanup_verified"] is True and closed["checkpoint_id"] == final_checkpoint)
    repeated = call(endpoint + "/close", {"checkpoint_id": final_checkpoint})
    verify("close_is_idempotent", repeated["cleanup_verified"] is True)
    downloaded = checked(request(api, f"/v1/assignments/{assignment}/learning-checkpoints/{final_checkpoint}/bundle", authorization=auth))
    verify("server_work_survives_space_deletion", hashlib.sha256(downloaded["body"]).hexdigest() == downloaded["headers"]["X-Autograde-SHA256"])
    closed_other = call(route + "/sessions/" + other_sid + "/close", {"checkpoint_id": cp_other}, other)
    verify("second_space_removed_independently", closed_other["cleanup_verified"] is True)
    denied = request(api, route, authorization=None)
    verify("runtime_requires_authentication", denied["status"] == 401)
    portal = request(web, f"/courses/{COURSE}/instructor/environments", authorization=basic("teacher"))
    verify("instructor_environment_page", portal["status"] == 200 and "실습 환경".encode() in portal["body"])
    wrong_teacher = request(web, f"/courses/{COURSE}/instructor/environments", authorization=basic("other"))
    verify("instructor_course_scope", wrong_teacher["status"] in (403,404))

    # Twenty-five connected learning spaces, with at most four actual Linux
    # executions admitted by the worker. Five callers exercise explicit busy
    # responses and bounded retries without launching 25 compilers at once.
    fixtures_clock += timedelta(seconds=60)
    fixture.services[COURSE]._now = lambda: fixtures_clock
    def prepare_student(number):
        authorization = accept(web, api, number, assignment)
        tree = fixture.root / ("batch-source-" + str(number))
        tree.mkdir()
        (tree / "main.c").write_text('#include <stdio.h>\n#include <unistd.h>\nint main(void){sleep(1);printf("STUDENT:%d\\n",' + str(number) + ');return 0;}\n')
        artifact = fixture.bundles.create_from_directory(tree, kind="submission")
        checkpoint = body(checked(request(api, f"/v1/assignments/{assignment}/learning-checkpoints",
            method="POST", authorization=authorization, raw=artifact.path.read_bytes(),
            idempotency="runtime-batch-" + str(number))))["checkpoint"]["checkpoint_id"]
        session = call(route + "/sessions", {"checkpoint_id": checkpoint}, authorization)["session"]
        return dict(number=number, authorization=authorization, checkpoint=checkpoint, session_id=session["id"])
    batch_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        students = list(pool.map(prepare_student, range(1, 26)))
    verify("25_distinct_student_spaces_prepared", len({s["session_id"] for s in students}) == 25)
    overview = fixture.services[COURSE].learning_runtime_overview()
    verify("25_active_spaces_visible_to_instructor", sum(s["status"] == "ready" for s in overview["sessions"]) == 25)

    def run_student(student):
        endpoint = route + "/sessions/" + student["session_id"]
        payload = {"checkpoint_id": student["checkpoint"], "language": "c", "sources": ["main.c"], "stdin": ""}
        started, retries = time.monotonic(), 0
        while True:
            response = request(api, endpoint + "/run", method="POST", authorization=student["authorization"], payload=payload)
            if response["status"] != 429:
                result = body(checked(response))
                break
            error = body(response)["error"]
            if error["code"] != "learning_runtime_busy" or time.monotonic() - started > 30:
                raise AssertionError("Unexpected or prolonged execution admission rejection")
            retries += 1
            time.sleep(min(.2 * retries, 1))
        if result["status"] != "completed" or result["run"]["stdout"] != "STUDENT:" + str(student["number"]) + "\n":
            raise AssertionError("Student output mixed or execution failed")
        closed = call(endpoint + "/close", {"checkpoint_id": student["checkpoint"]}, student["authorization"])
        if closed["cleanup_verified"] is not True:
            raise AssertionError("Student space cleanup was not verified")
        restored = checked(request(api, f"/v1/assignments/{assignment}/learning-checkpoints/{student['checkpoint']}/bundle",
                                   authorization=student["authorization"]))
        if hashlib.sha256(restored["body"]).hexdigest() != restored["headers"]["X-Autograde-SHA256"]:
            raise AssertionError("Student checkpoint was lost or changed after cleanup")
        return {"retries": retries, "seconds": round(time.monotonic() - started, 3)}

    with ThreadPoolExecutor(max_workers=5) as pool:
        executions = list(pool.map(run_student, students))
    verify("25_students_run_save_restore_close", len(executions) == 25)
    overview = fixture.services[COURSE].learning_runtime_overview()
    verify("25_spaces_cleanup_confirmed", not any(s["status"] not in {"closed", "expired"} for s in overview["sessions"]))
    verify("batch_created_no_official_submissions", all(row.submission_count == 0 for row in fixture.state.list_bundle_dashboard_rows(course_key=COURSE)))
    batch = {"students": 25, "max_parallel_api_runs": 5, "worker_run_limit": 4,
             "busy_retries": sum(e["retries"] for e in executions),
             "elapsed_seconds": round(time.monotonic() - batch_start, 3),
             "max_student_flow_seconds": max(e["seconds"] for e in executions),
             "capacity_certification": False}
    return {"status": "passed", "checks": checks, "check_count": len(checks), "provider": "real-linux-docker",
            "production_data_used": False, "formal_submissions_created": 0, "batch": batch}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Exact local Docker image ID sha256:...")
    parser.add_argument("--report", type=Path, default=Path("build/learning-runtime-report.json"))
    parser.add_argument("--serve", action="store_true", help="Keep synthetic portal/API running for IDE/browser tests")
    parser.add_argument("--api-port", type=int, default=18180)
    parser.add_argument("--web-port", type=int, default=18181)
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
        parser.error("--image must be an immutable sha256 Docker image ID")
    if args.serve:
        pilot_fixture.WEB_ORIGIN = f"http://127.0.0.1:{args.web_port}"
        pilot_fixture.API_ORIGIN = f"http://127.0.0.1:{args.api_port}"
    with tempfile.TemporaryDirectory(prefix="agrt-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        with worker(root, args.image) as socket:
            fixture = PilotFixture(root, learning_runtime_socket=socket, starter_files={
                "main.c": '#include <stdio.h>\nint main(void){int n;if(scanf("%d",&n)!=1)return 2;printf("C:%d\\n",n*2);return 0;}\n',
                "main.cpp": '#include <iostream>\nint main(){std::string name;std::cin>>name;std::cout<<"Hello "<<name<<"\\n";}\n',
                "main.py": "from helper import twice\nprint('Python:', twice(int(input())))\n",
                "helper.py": "def twice(value): return value * 2\n",
                "README.md": "# Remote Linux C/C++/Python test\n\nC: input 21, expected C:42. C++: input Autograde, expected Hello Autograde. Python: input 21, expected Python: 42.\n",
            })
            with serving(fixture.portal, pilot_fixture.WEB_ORIGIN, port=args.web_port if args.serve else 0) as web, \
                 serving(fixture.api, pilot_fixture.API_ORIGIN, port=args.api_port if args.serve else 0) as api:
                if args.serve:
                    # Contains only invented local students/passwords. Do not point
                    # this fixture at an existing data root or public interface.
                    print(json.dumps({"web": f"http://127.0.0.1:{web.server_address[1]}",
                        "api": f"http://127.0.0.1:{api.server_address[1]}", "fixture_root": str(root),
                        "student": "20260001", "password": "000001", "assignment": fixture.assignment(0)}, ensure_ascii=False), flush=True)
                    stop = threading.Event()
                    for number in (signal.SIGINT, signal.SIGTERM):
                        signal.signal(number, lambda *_: stop.set())
                    while not stop.wait(1):
                        pass
                else:
                    report = run_gate(fixture, web, api)
                    report["image"] = args.image
                    args.report.parent.mkdir(parents=True, exist_ok=True)
                    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
                    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
