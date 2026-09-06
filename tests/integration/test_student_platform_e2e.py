from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from autograde.gitops import GitCollector
from autograde.platform_grader import PublicGradeResult
from autograde.platform_http import create_server
from autograde.platform_pinner import GitSubmissionPinner
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore
from autograde.platform_worker import SubmissionProcessor
from autograde.workspace import WorkspaceBuilder


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
RUNNER = "ghcr.io/example/runner@sha256:" + "d" * 64


class PassingGrader:
    def grade(self, *, workspace, runner_image, max_score):
        assert (workspace.submission / "answer.py").read_text(encoding="utf-8") == "answer = 42\n"
        assert (workspace.assessment / "grade.py").is_file()
        assert runner_image == RUNNER
        return PublicGradeResult(
            score=10,
            max_score=max_score,
            rubric={
                "solution": {
                    "title": "Solution",
                    "score": 10,
                    "max_score": 10,
                    "feedback": "Passed",
                }
            },
            diagnostics=(),
        )


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "student.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "student@example.test")
    git(work, "config", "user.name", "Student")
    (work / "answer.py").write_text("answer = 42\n", encoding="utf-8")
    git(work, "add", "answer.py")
    git(work, "commit", "-m", "solution")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "origin", "main")
    return remote, git(work, "rev-parse", "HEAD")


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def api(base_url, method, path, *, payload=None, token=None, headers=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = Request(base_url + path, data=body, headers=request_headers, method=method)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None


def form(base_url, path, payload, *, cookie):
    body = urlencode(payload).encode("ascii")
    request = Request(
        base_url + path,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie,
        },
        method="POST",
    )
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
    with response:
        return response.status, response.read().decode("utf-8")


def test_device_login_submit_exact_sha_and_read_result_over_http(tmp_path) -> None:
    remote, commit_sha = repository(tmp_path)
    data_root = tmp_path / "data"
    state = PlatformStateStore(data_root / "state.sqlite3")
    student = state.upsert_student(
        student_key="s001",
        auth_subject="github:101",
        github_user_id=101,
        github_login="student-one",
        at=NOW,
    )
    state.upsert_enrollment(student_id=student.id, course_key="cse101", at=NOW)
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("# hidden instructor code\n", encoding="utf-8")
    builder = WorkspaceBuilder(data_root / "workspaces")
    assessment_digest = builder.digest_instructor_tree(assessment).sha256
    state.register_assignment(
        assignment_id="asn_lab01",
        student_id=student.id,
        course_key="cse101",
        assignment_key="lab01",
        release_id="v1",
        github_repository_id=9001,
        repository_owner="school",
        repository_name="lab01-student-one",
        clone_url=str(remote),
        submission_mode="branch",
        target_ref="main",
        result_policy="immediate",
        assessment_path=str(assessment),
        assessment_digest=assessment_digest,
        runner_image=RUNNER,
        rubric_version="v1",
        max_score=10,
        ready=True,
        at=NOW,
    )
    collector = GitCollector(data_root / "cache", data_root / "snapshots")
    processor = SubmissionProcessor(
        state=state,
        course_key="cse101",
        collector=collector,
        workspace_builder=builder,
        grader=PassingGrader(),
        now=lambda: NOW,
    )
    service = StudentPlatformService(
        state=state,
        server_secret=b"s" * 32,
        course_key="cse101",
        public_base_url="http://127.0.0.1:8000",
        submission_pinner=GitSubmissionPinner(collector),
        notify_submission=processor.process,
        now=lambda: NOW,
    )
    activation = service.issue_student_activation(student_key="s001")
    server = create_server(("127.0.0.1", 0), service)

    with running(server) as base_url:
        status, device = api(
            base_url,
            "POST",
            "/v1/device-authorizations",
            payload={"device_name": "WSL2"},
        )
        assert status == 201
        with urlopen(
            base_url + f"/activate?user_code={device['user_code']}", timeout=10
        ) as page:
            assert page.status == 200
            page_body = page.read().decode("utf-8")
            assert "Autograde 연결" in page_body
            assert "학생 활성화 코드" in page_body
            assert "GitHub로 로그인" not in page_body
            assert activation["activation_code"] not in page_body
            csrf = re.search(r'name="csrf" value="([^"]+)"', page_body).group(1)
            activation_cookie = page.headers["Set-Cookie"].split(";", 1)[0]
        status, approved = form(
            base_url,
            "/activate/approve",
            {
                "user_code": device["user_code"],
                "activation_code": activation["activation_code"],
                "csrf": csrf,
            },
            cookie=activation_cookie,
        )
        assert status == 200
        assert "연결 완료" in approved
        assert activation["activation_code"] not in approved
        status, tokens = api(
            base_url,
            "POST",
            "/v1/device-authorizations/token",
            payload={"device_code": device["device_code"]},
        )
        assert status == 200
        access = tokens["access_token"]

        status, invalid = api(base_url, "GET", "/v1/me", token="a" * 300)
        assert status == 401
        assert invalid["error"]["code"] == "invalid_token"

        status, assignments = api(base_url, "GET", "/v1/assignments", token=access)
        assert status == 200
        assert assignments["assignments"][0]["repository"]["github_repository_id"] == 9001

        status, submitted = api(
            base_url,
            "POST",
            "/v1/submissions",
            token=access,
            headers={"Idempotency-Key": "request-0001"},
            payload={
                "assignment_id": "asn_lab01",
                "github_repository_id": 9001,
                "head_sha": commit_sha,
            },
        )
        assert status == 202
        submission_id = submitted["submission"]["submission_id"]

        status, submission = api(
            base_url, "GET", f"/v1/submissions/{submission_id}", token=access
        )
        assert status == 200
        assert submission["submission"]["state"] == "published"
        status, result = api(
            base_url,
            "GET",
            f"/v1/submissions/{submission_id}/result",
            token=access,
        )
        assert status == 200
        assert result["result"]["head_sha"] == commit_sha
        assert result["result"]["score"] == 10
        assert result["result"]["rubric"]["solution"]["feedback"] == "Passed"
