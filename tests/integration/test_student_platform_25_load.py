from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import http.client
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Iterator

import pytest

from autograde.gitops import GitCollector
from autograde.platform_http import PlatformHTTPServer, create_server
from autograde.platform_pinner import GitSubmissionPinner
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore


COURSE = "cse101-load"
NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
RUNNER = "ghcr.io/example/runner@sha256:" + "d" * 64
STUDENT_COUNT = 25


@dataclass(frozen=True)
class StudentCase:
    index: int
    student_id: int
    student_key: str
    access_token: str
    assignment_id: str
    repository_id: int
    commit_sha: str

    @property
    def idempotency_key(self) -> str:
        return f"load-admission-{self.index:03d}"

    @property
    def submission_payload(self) -> dict[str, object]:
        return {
            "assignment_id": self.assignment_id,
            "github_repository_id": self.repository_id,
            "head_sha": self.commit_sha,
        }


def git(*arguments: str, cwd: Path | None = None) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_DATE": "2026-08-30T12:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-08-30T12:00:00+00:00",
        }
    )
    result = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def create_student_remote(root: Path, index: int) -> tuple[Path, str]:
    remote = root / "remotes" / f"student-{index:03d}.git"
    working = root / "working" / f"student-{index:03d}"
    remote.parent.mkdir(parents=True, exist_ok=True)
    working.parent.mkdir(parents=True, exist_ok=True)
    git("init", "--bare", str(remote))
    git("init", "--initial-branch=main", str(working))
    git("config", "user.name", f"Load Student {index:03d}", cwd=working)
    git(
        "config",
        "user.email",
        f"load-student-{index:03d}@example.invalid",
        cwd=working,
    )
    git("config", "commit.gpgSign", "false", cwd=working)
    (working / "answer.py").write_text(
        f"student = {index}\nanswer = 42\n",
        encoding="utf-8",
    )
    git("add", "answer.py", cwd=working)
    git("commit", "-m", "submit exact solution", cwd=working)
    commit_sha = git("rev-parse", "HEAD", cwd=working)
    git("remote", "add", "origin", str(remote), cwd=working)
    git("push", "origin", "main", cwd=working)
    return remote, commit_sha


@contextmanager
def running_server(service: StudentPlatformService) -> Iterator[PlatformHTTPServer]:
    server = create_server(
        ("127.0.0.1", 0),
        service,
        max_concurrent_requests=STUDENT_COUNT,
        request_timeout_seconds=120,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def post_submission(
    server: PlatformHTTPServer,
    case: StudentCase,
) -> dict[str, object]:
    body = json.dumps(case.submission_payload).encode("utf-8")
    connection = http.client.HTTPConnection(
        server.server_address[0],
        server.server_address[1],
        timeout=120,
    )
    try:
        connection.request(
            "POST",
            "/v1/submissions",
            body=body,
            headers={
                "Authorization": f"Bearer {case.access_token}",
                "Content-Type": "application/json",
                "Idempotency-Key": case.idempotency_key,
            },
        )
        response = connection.getresponse()
        response_body = response.read()
    finally:
        connection.close()
    decoded = json.loads(response_body)
    assert response.status == 202, decoded
    assert isinstance(decoded, dict)
    return decoded


def concurrent_submit(
    server: PlatformHTTPServer,
    cases: list[StudentCase],
) -> dict[int, dict[str, object]]:
    barrier = threading.Barrier(len(cases) + 1)

    def submit(case: StudentCase) -> tuple[int, dict[str, object]]:
        barrier.wait()
        return case.index, post_submission(server, case)

    with ThreadPoolExecutor(max_workers=len(cases)) as executor:
        futures = [executor.submit(submit, case) for case in cases]
        barrier.wait()
        return dict(future.result() for future in futures)


@pytest.mark.integration
def test_http_admits_twenty_five_exact_sha_submissions_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    state = PlatformStateStore(data_root / "state.sqlite3")
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    assessment_source = b"# non-confidential instructor assessment for the load gate\n"
    (assessment / "grade.py").write_bytes(assessment_source)

    collector = GitCollector(data_root / "cache", data_root / "snapshots")
    service = StudentPlatformService(
        state=state,
        server_secret=b"s" * 32,
        course_key=COURSE,
        public_base_url="http://127.0.0.1:8000",
        submission_pinner=GitSubmissionPinner(collector),
        now=lambda: NOW,
    )

    cases: list[StudentCase] = []
    for index in range(1, STUDENT_COUNT + 1):
        remote, commit_sha = create_student_remote(tmp_path, index)
        student_key = f"load-s{index:03d}"
        github_user_id = 10_000 + index
        student = state.upsert_student(
            student_key=student_key,
            auth_subject=f"github:{github_user_id}",
            github_user_id=github_user_id,
            github_login=f"load-student-{index:03d}",
            at=NOW,
        )
        state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)

        assignment_id = f"asn-load-{index:03d}"
        repository_id = 910_000 + index
        state.register_assignment(
            assignment_id=assignment_id,
            student_id=student.id,
            course_key=COURSE,
            assignment_key="lab01",
            release_id="v1",
            github_repository_id=repository_id,
            repository_owner="load-classroom",
            repository_name=f"lab01-load-student-{index:03d}",
            clone_url=str(remote),
            submission_mode="branch",
            target_ref="main",
            result_policy="immediate",
            assessment_path=str(assessment),
            assessment_digest=hashlib.sha256(assessment_source).hexdigest(),
            runner_image=RUNNER,
            rubric_version="v1",
            max_score=10,
            opens_at=NOW - timedelta(days=1),
            due_at=NOW + timedelta(days=1),
            ready=True,
            at=NOW,
        )

        authorization = service.create_device_authorization(
            {"device_name": f"WSL2 load student {index:03d}"}
        )
        service.approve_user_code(
            user_code=str(authorization["user_code"]),
            github_user_id=github_user_id,
        )
        tokens = service.exchange_device_authorization(
            {"device_code": authorization["device_code"]}
        )
        access_token = str(tokens["access_token"])
        assert service.get_me(access_token)["student_key"] == student_key
        cases.append(
            StudentCase(
                index=index,
                student_id=student.id,
                student_key=student_key,
                access_token=access_token,
                assignment_id=assignment_id,
                repository_id=repository_id,
                commit_sha=commit_sha,
            )
        )

    with running_server(service) as server:
        admitted = concurrent_submit(server, cases)

        assert len(admitted) == STUDENT_COUNT
        submission_ids: set[str] = set()
        receipt_ids: dict[int, str] = {}
        source_digests: set[str] = set()
        source_paths: set[Path] = set()
        for case in cases:
            response = admitted[case.index]
            assert response["replayed"] is False
            submission = response["submission"]
            assert isinstance(submission, dict)
            assert submission["state"] == "accepted"
            assert submission["head_sha"] == case.commit_sha
            submission_id = str(submission["submission_id"])
            submission_ids.add(submission_id)

            receipt = state.get_receipt(submission_id)
            source_path = Path(receipt.source_path)
            assert receipt.student_id == case.student_id
            assert receipt.assignment_id == case.assignment_id
            assert receipt.github_repository_id == case.repository_id
            assert receipt.commit_sha == case.commit_sha
            assert receipt.snapshot_key is not None
            assert source_path.is_file()
            archive_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
            assert receipt.source_digest == f"sha256:{archive_digest}"
            receipt_ids[case.index] = receipt.receipt_id
            source_digests.add(receipt.source_digest)
            source_paths.add(source_path)

        assert len(submission_ids) == STUDENT_COUNT
        assert len(set(receipt_ids.values())) == STUDENT_COUNT
        assert len(source_digests) == STUDENT_COUNT
        assert len(source_paths) == STUDENT_COUNT
        assert len(list((data_root / "snapshots").rglob("*.tar.gz"))) == STUDENT_COUNT

        replayed = concurrent_submit(server, cases)

        for case in cases:
            response = replayed[case.index]
            assert response["replayed"] is True
            assert response["submission"]["submission_id"] == admitted[case.index][
                "submission"
            ]["submission_id"]
            receipt = state.get_receipt(str(response["submission"]["submission_id"]))
            assert receipt.receipt_id == receipt_ids[case.index]
            assert receipt.source_digest in source_digests
            assert Path(receipt.source_path) in source_paths

        assert len(list((data_root / "snapshots").rglob("*.tar.gz"))) == STUDENT_COUNT
    operator_view = state.list_operator_submissions(
        course_key=COURSE,
        state="accepted",
        limit=100,
    )
    assert len(operator_view) == STUDENT_COUNT
    assert {item.submission_id for item in operator_view} == submission_ids
    assert all(item.receipt_id is not None for item in operator_view)
    assert all(item.source_digest in source_digests for item in operator_view)
