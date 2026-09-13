from __future__ import annotations

import base64
import http.client
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode

from autograde.platform_bundle import BundleStore


def _clean_environment(fake_bin: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AUTOGRADE_")
    }
    environment["PATH"] = os.pathsep.join(
        (str(fake_bin), environment.get("PATH", ""))
    )
    return environment


def _cli(
    repository: Path,
    config: Path,
    environment: dict[str, str],
    *arguments: str,
) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "autograde.platform_cli",
            "--pilot-config",
            str(config),
            *arguments,
        ],
        cwd=repository,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)["result"]


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


def _json_request(
    port: int,
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    status, _, raw = _request(
        port,
        method,
        path,
        body=body,
        headers=request_headers,
    )
    return status, json.loads(raw) if raw else {}


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_zero_env_csv_pilot_downloads_submits_and_grades_without_docker(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    unused_tcp_port = _unused_loopback_port()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    runtime_marker = tmp_path / "container-runtime-was-invoked"
    for runtime in ("docker", "podman"):
        executable = fake_bin / runtime
        executable.write_text(
            f"#!/bin/sh\nprintf invoked > {str(runtime_marker)!r}\nexit 99\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
    environment = _clean_environment(fake_bin)

    config = tmp_path / "course.csv"
    config.write_text(
        "key,value\n"
        "course_key,cse101-pilot-e2e\n"
        "data_root,state\n"
        f"public_base_url,http://127.0.0.1:{unused_tcp_port}\n"
        "listen,127.0.0.1\n"
        f"port,{unused_tcp_port}\n"
        "grading_runtime,pilot-local\n"
        "bundle_worker_count,4\n",
        encoding="utf-8",
    )
    roster = tmp_path / "roster.csv"
    roster.write_text("student_key,active\ns001,true\n", encoding="utf-8")

    initialized = _cli(repository, config, environment, "init")
    assert initialized["course_key"] == "cse101-pilot-e2e"
    imported = _cli(
        repository,
        config,
        environment,
        "student",
        "import",
        str(roster),
    )
    assert imported["count"] == 1

    example = repository / "examples" / "direct-bundle"
    assignment = _cli(
        repository,
        config,
        environment,
        "assignment",
        "bundle-add",
        "lab01",
        "--assignment-id",
        "basn_pilot_lab01",
        "--release-id",
        "lab01-v1",
        "--title",
        "Lab 01",
        "--starter",
        str(example / "starter"),
        "--assessment",
        str(example / "assessment"),
        "--data",
        str(example / "data"),
        "--max-score",
        "10",
    )
    assert assignment["grading_runtime"] == "pilot-local"
    assert assignment["ready"] is False
    solution = tmp_path / "solution"
    solution.mkdir()
    (solution / "main.py").write_text("print(int(input()) * 2)\n", encoding="utf-8")
    checked = _cli(repository, config, environment, "assignment", "bundle-check", "basn_pilot_lab01",
        "--solution", str(solution), "--negative-solution", str(example / "starter"), "--negative-score", "2")
    assert checked["status"] == "passed"
    published = _cli(repository, config, environment, "assignment", "bundle-ready", "basn_pilot_lab01")
    assert published["ready"] is True

    code_directory = tmp_path / "activation-codes"
    code_directory.mkdir(mode=0o700)
    code_file = code_directory / "s001.txt"
    _cli(
        repository,
        config,
        environment,
        "auth",
        "issue",
        "s001",
        "--output",
        str(code_file),
    )
    activation_code = code_file.read_text(encoding="utf-8").strip()

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "autograde.platform_cli",
            "--pilot-config",
            str(config),
            "serve",
            "--recovery-interval",
            "0.05",
        ],
        cwd=repository,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    returncode: int | None = None
    try:
        readable, _, _ = select.select((process.stdout,), (), (), 10)
        if not readable:
            error_ready, _, _ = select.select((process.stderr,), (), (), 0)
            detail = process.stderr.readline().strip() if error_ready else ""
            raise AssertionError("pilot server did not start: " + detail)
        startup = json.loads(process.stdout.readline())["result"]
        assert startup["grading_runtime"] == "pilot-local"
        assert startup["security"]["sandboxed"] is False
        assert startup["bundle_assignments"]["worker_count"] == 4

        status, device = _json_request(
            unused_tcp_port,
            "POST",
            "/v1/device-authorizations",
            payload={"device_name": "pilot-test"},
        )
        assert status == 201
        status, page_headers, page = _request(
            unused_tcp_port,
            "GET",
            f"/activate?user_code={device['user_code']}",
        )
        assert status == 200
        csrf_match = re.search(rb'name="csrf" value="([^"]+)"', page)
        assert csrf_match is not None
        cookie = page_headers["Set-Cookie"].split(";", 1)[0]
        form = urlencode(
            {
                "user_code": device["user_code"],
                "activation_code": activation_code,
                "csrf": csrf_match.group(1).decode("ascii"),
            }
        ).encode("ascii")
        status, _, _ = _request(
            unused_tcp_port,
            "POST",
            "/activate/approve",
            body=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie,
            },
        )
        assert status == 200
        status, tokens = _json_request(
            unused_tcp_port,
            "POST",
            "/v1/device-authorizations/token",
            payload={"device_code": device["device_code"]},
        )
        assert status == 200
        access_token = tokens["access_token"]

        status, assignments = _json_request(
            unused_tcp_port,
            "GET",
            "/v1/assignments",
            token=access_token,
        )
        assert status == 200
        assert assignments["assignments"][0]["assignment_id"] == "basn_pilot_lab01"
        status, _, starter = _request(
            unused_tcp_port,
            "GET",
            "/v1/assignments/basn_pilot_lab01/starter",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert status == 200
        assert starter.startswith(b"\x1f\x8b")

        answer = tmp_path / "answer"
        answer.mkdir()
        (answer / "main.py").write_text(
            "value = int(input())\nprint(value * 2)\n",
            encoding="utf-8",
        )
        submission = BundleStore(tmp_path / "client-bundles").create_from_directory(
            answer,
            kind="submission",
        )
        status, _, submitted_raw = _request(
            unused_tcp_port,
            "POST",
            "/v1/assignments/basn_pilot_lab01/submissions",
            body=submission.path.read_bytes(),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/gzip",
                "Idempotency-Key": "pilot-e2e-submission-0001",
            },
        )
        assert status == 202
        submission_id = json.loads(submitted_raw)["submission"]["submission_id"]

        deadline = time.monotonic() + 10
        result: dict | None = None
        while time.monotonic() < deadline:
            status, response = _json_request(
                unused_tcp_port,
                "GET",
                f"/v1/submissions/{submission_id}/result",
                token=access_token,
            )
            if status == 200:
                result = response["result"]
                break
            time.sleep(0.05)
        assert result is not None
        assert result["score"] == 10
        assert result["rubric"]["output"]["score"] == 8

        instructor_token = (tmp_path / "state" / "platform-instructor-token").read_text(
            encoding="utf-8"
        ).strip()
        basic = base64.b64encode(
            f"instructor:{instructor_token}".encode("utf-8")
        ).decode("ascii")
        status, dashboard = _json_request(
            unused_tcp_port,
            "GET",
            "/v1/instructor/dashboard",
            headers={"Authorization": f"Basic {basic}"},
        )
        assert status == 200
        assert dashboard["rows"][0]["student_key"] == "s001"
        assert dashboard["rows"][0]["score"] == 10
        assert not runtime_marker.exists()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            _, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate(timeout=5)
        returncode = process.returncode
    assert returncode == 0, stderr
