from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import List, Sequence

import pytest

from autograde.platform_grader import (
    AssessmentGradingError,
    ContainerGrader,
    ContainerLimits,
    InfrastructureGradingError,
    ProcessResult,
    PublicGradeResult,
    SubprocessExecutor,
    sanitize_grade_result,
)
from autograde.workspace import PreparedWorkspace


RUNNER_IMAGE = "ghcr.io/example/course-grader@sha256:" + "ab" * 32


class FakeExecutor:
    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls: List[tuple[tuple[str, ...], float, int, int]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        self.calls.append(
            (
                tuple(argv),
                timeout_seconds,
                max_stdout_bytes,
                max_stderr_bytes,
            )
        )
        if not self.responses:
            raise AssertionError("unexpected container-runtime call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, ProcessResult)
        return response


def _workspace(tmp_path: Path, *, with_data: bool = True) -> PreparedWorkspace:
    root = tmp_path / "published-workspace"
    submission = root / "submission"
    assessment = root / "assessment"
    submission.mkdir(parents=True)
    assessment.mkdir()
    (submission / "student.py").write_text("raise SystemExit('host must not run me')\n")
    (assessment / "hidden.py").write_text("# instructor owned\n")
    data = None
    if with_data:
        data = root / "data"
        data.mkdir()
        (data / "cases.json").write_text("{}\n")
    manifest = root / "manifest.json"
    manifest.write_text("{}\n")
    return PreparedWorkspace(
        workspace_key="submission-1",
        path=root,
        submission_path=submission,
        manifest_path=manifest,
        source_sha256="1" * 64,
        source_file_count=1,
        source_member_count=1,
        source_unpacked_bytes=42,
        assessment_path=assessment,
        data_path=data,
        assessment_sha256="2" * 64,
        data_sha256="3" * 64 if data is not None else None,
    )


def _valid_output(**updates: object) -> bytes:
    payload = {
        "score": 8.5,
        "max_score": 10,
        "rubric": {
            "correctness": {
                "score": 8.5,
                "max_score": 10,
                "title": "Correctness",
                "feedback": "Good work",
            }
        },
        "diagnostics": [
            {
                "path": "src/main.py",
                "line": 4,
                "message": "Check one edge case",
                "severity": "warning",
            }
        ],
    }
    payload.update(updates)
    return json.dumps(payload).encode("utf-8")


def _successful_executor(output: bytes | None = None) -> FakeExecutor:
    return FakeExecutor(
        [
            ProcessResult(0, stdout=b"container-id\n"),
            ProcessResult(0, stdout=output or _valid_output()),
            ProcessResult(0),
        ]
    )


def test_container_grader_uses_only_hardened_runtime_argument_vectors(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    executor = _successful_executor()
    limits = ContainerLimits(
        timeout_seconds=7,
        runtime_operation_timeout_seconds=3,
        memory_bytes=256 * 1024 * 1024,
        cpu_count=0.5,
        pids_limit=32,
        tmpfs_bytes=8 * 1024 * 1024,
        max_stdout_bytes=16 * 1024,
        max_stderr_bytes=4 * 1024,
    )
    grader = ContainerGrader(
        runtime="docker",
        limits=limits,
        container_command=("/opt/autograde/run", "--format=json"),
        executor=executor,
        container_user="10001:10001",
        name_factory=lambda: "autograde-fixed",
        instance_label="service-course-a",
    )

    result = grader.grade(
        workspace=workspace,
        runner_image=RUNNER_IMAGE,
        max_score=10,
    )

    assert isinstance(result, PublicGradeResult)
    assert result.score == 8.5
    assert result.max_score == 10
    assert result.rubric["correctness"]["score"] == 8.5
    assert result.diagnostics[0]["path"] == "src/main.py"

    create, start, remove = [call[0] for call in executor.calls]
    assert create[:4] == ("docker", "create", "--name", "autograde-fixed")
    labels = [
        create[index + 1]
        for index, argument in enumerate(create)
        if argument == "--label"
    ]
    assert labels == [
        "io.autograde.managed=true",
        "io.autograde.instance=service-course-a",
    ]
    assert ("--network", "none") == create[
        create.index("--network") : create.index("--network") + 2
    ]
    assert "--read-only" in create
    assert create[create.index("--cap-drop") + 1] == "ALL"
    assert create[create.index("--security-opt") + 1] == "no-new-privileges:true"
    assert create[create.index("--pids-limit") + 1] == "32"
    assert create[create.index("--memory") + 1] == str(256 * 1024 * 1024)
    assert create[create.index("--memory-swap") + 1] == str(256 * 1024 * 1024)
    assert create[create.index("--cpus") + 1] == "0.5"
    assert create[create.index("--user") + 1] == "10001:10001"
    assert create[create.index("--tmpfs") + 1].startswith("/tmp:rw,nosuid,nodev,")
    assert "--pull=never" in create
    assert create[-3:] == (RUNNER_IMAGE, "/opt/autograde/run", "--format=json")

    mount_values = [
        create[index + 1]
        for index, argument in enumerate(create)
        if argument == "--mount"
    ]
    assert mount_values == [
        f"type=bind,src={workspace.submission_path.resolve()},dst=/workspace/submission,readonly",
        f"type=bind,src={workspace.assessment_path.resolve()},dst=/workspace/assessment,readonly",
        f"type=bind,src={workspace.data_path.resolve()},dst=/workspace/data,readonly",
    ]
    assert start == ("docker", "start", "--attach", "autograde-fixed")
    assert remove == ("docker", "rm", "--force", "autograde-fixed")
    assert all(
        call[1:] == (3, 16 * 1024, 4 * 1024)
        for call in (executor.calls[0], executor.calls[2])
    )
    assert executor.calls[1][1:] == (7, 16 * 1024, 4 * 1024)
    assert str(workspace.submission_path / "student.py") not in create


@pytest.mark.parametrize(
    "image",
    [
        "python:3.12",
        "python@sha256:" + "a" * 64,
        "ghcr.io/course/grader@sha256:short",
        "--privileged@sha256:" + "a" * 64,
        "ghcr.io/course/grader@sha256:" + "z" * 64,
    ],
)
def test_mutable_or_malformed_runner_image_is_rejected_before_runtime(
    tmp_path: Path, image: str
) -> None:
    executor = FakeExecutor([])
    grader = ContainerGrader(executor=executor)

    with pytest.raises(InfrastructureGradingError) as caught:
        grader.grade(workspace=_workspace(tmp_path), runner_image=image, max_score=10)

    assert caught.value.code == "invalid_runner_image"
    assert executor.calls == []


def test_host_root_nonexistent_symlink_and_overlapping_mounts_are_rejected(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    executor = FakeExecutor([])
    grader = ContainerGrader(executor=executor)

    with pytest.raises(InfrastructureGradingError, match="dedicated directory"):
        grader.grade(
            workspace=replace(
                workspace,
                path=Path("/"),
                submission_path=Path("/"),
                assessment_path=None,
                data_path=None,
            ),
            runner_image=RUNNER_IMAGE,
            max_score=10,
        )

    missing = tmp_path / "missing"
    with pytest.raises(InfrastructureGradingError, match="unavailable"):
        grader.grade(
            workspace=replace(workspace, submission_path=missing),
            runner_image=RUNNER_IMAGE,
            max_score=10,
        )

    link = tmp_path / "submission-link"
    link.symlink_to(workspace.submission_path, target_is_directory=True)
    with pytest.raises(InfrastructureGradingError, match="symlink"):
        grader.grade(
            workspace=replace(workspace, submission_path=link),
            runner_image=RUNNER_IMAGE,
            max_score=10,
        )

    with pytest.raises(InfrastructureGradingError, match="prepared workspace"):
        grader.grade(
            workspace=replace(workspace, submission_path=workspace.path),
            runner_image=RUNNER_IMAGE,
            max_score=10,
        )
    assert executor.calls == []


def test_assessment_timeout_is_classified_and_container_is_removed(tmp_path: Path) -> None:
    executor = FakeExecutor(
        [
            ProcessResult(0, stdout=b"container-id\n"),
            ProcessResult(-9, timed_out=True),
            ProcessResult(0),
        ]
    )
    grader = ContainerGrader(
        executor=executor,
        name_factory=lambda: "autograde-timeout",
    )

    with pytest.raises(AssessmentGradingError) as caught:
        grader.grade(workspace=_workspace(tmp_path), runner_image=RUNNER_IMAGE, max_score=10)

    assert caught.value.code == "assessment_timeout"
    assert executor.calls[-1][0] == (
        "docker",
        "rm",
        "--force",
        "autograde-timeout",
    )


def test_create_and_cleanup_failures_are_infrastructure_errors(tmp_path: Path) -> None:
    create_failure = FakeExecutor([ProcessResult(1, stderr=b"daemon details")])
    with pytest.raises(InfrastructureGradingError) as caught:
        ContainerGrader(executor=create_failure).grade(
            workspace=_workspace(tmp_path / "create"),
            runner_image=RUNNER_IMAGE,
            max_score=10,
        )
    assert caught.value.code == "sandbox_create_failed"
    assert "daemon details" not in str(caught.value)
    assert create_failure.calls[-1][0][1:3] == ("rm", "--force")

    cleanup_failure = FakeExecutor(
        [
            ProcessResult(0),
            ProcessResult(0, stdout=_valid_output()),
            ProcessResult(1, stderr=b"sensitive daemon response"),
        ]
    )
    with pytest.raises(InfrastructureGradingError) as caught:
        ContainerGrader(executor=cleanup_failure).grade(
            workspace=_workspace(tmp_path / "cleanup"),
            runner_image=RUNNER_IMAGE,
            max_score=10,
        )
    assert caught.value.code == "sandbox_cleanup_failed"
    assert "sensitive daemon response" not in str(caught.value)


def test_keyboard_interrupt_force_removes_registered_container(tmp_path: Path) -> None:
    executor = FakeExecutor(
        [
            ProcessResult(0, stdout=b"container-id\n"),
            KeyboardInterrupt(),
            ProcessResult(0),
        ]
    )
    grader = ContainerGrader(
        executor=executor,
        name_factory=lambda: "autograde-interrupted",
    )

    with pytest.raises(KeyboardInterrupt):
        grader.grade(
            workspace=_workspace(tmp_path),
            runner_image=RUNNER_IMAGE,
            max_score=10,
        )

    assert executor.calls[-1][0] == (
        "docker",
        "rm",
        "--force",
        "autograde-interrupted",
    )
    assert grader.active_container_names == ()


@pytest.mark.parametrize(
    ("process", "error_type", "code"),
    [
        (
            ProcessResult(0, stdout=_valid_output(), stdout_truncated=True),
            AssessmentGradingError,
            "assessment_output_limit",
        ),
        (
            ProcessResult(1, stderr=b"hidden assessment traceback"),
            AssessmentGradingError,
            "assessment_process_failed",
        ),
        (
            ProcessResult(125, stderr=b"runtime failure"),
            InfrastructureGradingError,
            "sandbox_start_failed",
        ),
        (
            ProcessResult(0, stdout=b'{"score": 1} {"score": 2}'),
            AssessmentGradingError,
            "invalid_grade_result",
        ),
        (
            ProcessResult(0, stdout=b'{"score":NaN,"max_score":10}'),
            AssessmentGradingError,
            "invalid_grade_result",
        ),
        (
            ProcessResult(0, stdout=b'{"score":1,"score":2,"max_score":10}'),
            AssessmentGradingError,
            "invalid_grade_result",
        ),
    ],
)
def test_runtime_and_assessment_failures_have_stable_classification(
    tmp_path: Path,
    process: ProcessResult,
    error_type: type[Exception],
    code: str,
) -> None:
    executor = FakeExecutor([ProcessResult(0), process, ProcessResult(0)])
    grader = ContainerGrader(executor=executor)

    with pytest.raises(error_type) as caught:
        grader.grade(
            workspace=_workspace(tmp_path), runner_image=RUNNER_IMAGE, max_score=10
        )

    assert getattr(caught.value, "code") == code
    assert len(executor.calls) == 3


def test_sanitizer_strips_unknown_fields_secrets_and_server_paths() -> None:
    result = sanitize_grade_result(
        {
            "score": 8.5,
            "max_score": 10,
            "raw_output": "must not escape",
            "access_token": "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "rubric": {
                "correctness": {
                    "score": 8.5,
                    "max_score": 10,
                    "feedback": "token=super-secret otherwise good",
                    "expected_answer": "hidden",
                },
                "private_key": {"feedback": "must disappear"},
            },
            "diagnostics": [
                {
                    "path": "/srv/assessment/hidden.py",
                    "line": 3,
                    "message": "Bearer abcdefghijklmnop",
                    "traceback": "hidden details",
                },
                {
                    "path": "../another-student/main.py",
                    "message": "Use the public API",
                },
            ],
        },
        assignment_max_score=10,
    )

    public = result.as_dict()
    assert set(public) == {"score", "max_score", "rubric", "diagnostics"}
    assert set(public["rubric"]) == {"correctness"}
    assert set(public["rubric"]["correctness"]) == {
        "score",
        "max_score",
        "feedback",
    }
    serialized = json.dumps(public)
    assert "super-secret" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "hidden" not in serialized
    assert "/srv/" not in serialized
    assert "../another-student" not in serialized
    assert public["diagnostics"][0]["message"] == "[REDACTED]"


@pytest.mark.parametrize(
    "payload",
    [
        {"score": True, "max_score": 10},
        {"score": float("nan"), "max_score": 10},
        {"score": float("inf"), "max_score": 10},
        {"score": 10**10_000, "max_score": 10},
        {"score": -1, "max_score": 10},
        {"score": 11, "max_score": 10},
        {"score": 5, "max_score": 9},
        {"score": 5, "max_score": 10, "rubric": []},
        {"score": 5, "max_score": 10, "diagnostics": {}},
        {
            "score": 5,
            "max_score": 10,
            "rubric": {"quality": {"score": 6}},
        },
    ],
)
def test_sanitizer_rejects_non_finite_or_inconsistent_scores(payload: object) -> None:
    with pytest.raises(AssessmentGradingError):
        sanitize_grade_result(payload, assignment_max_score=10)  # type: ignore[arg-type]


def test_sanitizer_bounds_counts_strings_and_total_json_size() -> None:
    diagnostics = [
        {
            "path": f"src/file-{index}.py",
            "line": index + 1,
            "message": "x" * 10_000,
        }
        for index in range(250)
    ]
    rubric = {
        f"criterion-{index}": {
            "score": 0,
            "max_score": 1,
            "feedback": "y" * 10_000,
        }
        for index in range(250)
    }

    result = sanitize_grade_result(
        {
            "score": 0,
            "max_score": 100,
            "rubric": rubric,
            "diagnostics": diagnostics,
        },
        assignment_max_score=100,
    )
    public = result.as_dict()
    encoded = json.dumps(
        public, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")

    assert len(encoded) <= 64 * 1024
    assert len(public["rubric"]) <= 100
    assert len(public["diagnostics"]) <= 100
    assert all(
        len(item.get("feedback", "")) <= 2_048
        for item in public["rubric"].values()
    )
    assert all(len(item["message"]) <= 2_048 for item in public["diagnostics"])


def test_configuration_requires_nonroot_user_docker_or_podman_and_positive_limits() -> None:
    with pytest.raises(ValueError, match="non-root"):
        ContainerGrader(container_user="0:0")
    with pytest.raises(ValueError, match="docker or podman"):
        ContainerGrader(runtime="sh")
    with pytest.raises(ValueError, match="absolute"):
        ContainerGrader(runtime="student/docker")
    with pytest.raises(TypeError, match="sequence"):
        ContainerGrader(container_command="student.sh")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="instance_label"):
        ContainerGrader(instance_label="Course A / secret")
    with pytest.raises(ValueError, match="positive"):
        ContainerLimits(timeout_seconds=0)


def test_subprocess_executor_enforces_timeout_and_memory_bounded_pipes() -> None:
    trusted_test_program = (
        "import sys,time;"
        "sys.stdout.buffer.write(b'x'*10000);sys.stdout.flush();"
        "sys.stderr.buffer.write(b'y'*10000);sys.stderr.flush();"
        "time.sleep(10)"
    )

    result = SubprocessExecutor().run(
        (sys.executable, "-c", trusted_test_program),
        timeout_seconds=0.1,
        max_stdout_bytes=127,
        max_stderr_bytes=83,
    )

    assert result.timed_out is True
    assert len(result.stdout) == 127
    assert len(result.stderr) == 83
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_reconcile_orphans_uses_both_labels_and_only_returned_container_ids() -> None:
    first = "a" * 64
    second = "b" * 64
    executor = FakeExecutor(
        [
            ProcessResult(0, stdout=f"{first}\n{second}\n{first}\n".encode("ascii")),
            ProcessResult(0),
            ProcessResult(0),
        ]
    )
    grader = ContainerGrader(
        runtime="podman",
        executor=executor,
        instance_label="service-stable-123",
    )

    assert grader.reconcile_orphans() == 2

    listed, removed_first, removed_second = [call[0] for call in executor.calls]
    assert listed == (
        "podman",
        "ps",
        "--all",
        "--quiet",
        "--no-trunc",
        "--filter",
        "label=io.autograde.managed=true",
        "--filter",
        "label=io.autograde.instance=service-stable-123",
    )
    assert removed_first == ("podman", "rm", "--force", first)
    assert removed_second == ("podman", "rm", "--force", second)


def test_reconcile_rejects_untrusted_runtime_output_without_removal() -> None:
    executor = FakeExecutor([ProcessResult(0, stdout=b"not-a-container-id\n")])
    grader = ContainerGrader(executor=executor, instance_label="service-safe")

    with pytest.raises(InfrastructureGradingError) as caught:
        grader.reconcile_orphans()

    assert caught.value.code == "orphan_reconciliation_protocol_error"
    assert len(executor.calls) == 1


class BlockingLifecycleExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.released = threading.Event()
        self.calls: List[tuple[str, ...]] = []
        self._lock = threading.Lock()

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        command = tuple(argv)
        with self._lock:
            self.calls.append(command)
        if command[1] == "create":
            return ProcessResult(0)
        if command[1] == "start":
            self.started.set()
            assert self.released.wait(timeout=2)
            return ProcessResult(-9, timed_out=True)
        if command[1] == "rm":
            self.released.set()
            return ProcessResult(0)
        raise AssertionError(f"unexpected command: {command}")


def test_active_tracking_blocks_reconciliation_and_close_force_cleans(
    tmp_path: Path,
) -> None:
    executor = BlockingLifecycleExecutor()
    grader = ContainerGrader(
        executor=executor,
        instance_label="service-thread-safe",
        name_factory=lambda: "autograde-active",
    )
    failures: list[BaseException] = []

    def run_grade() -> None:
        try:
            grader.grade(
                workspace=_workspace(tmp_path),
                runner_image=RUNNER_IMAGE,
                max_score=10,
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run_grade)
    thread.start()
    assert executor.started.wait(timeout=2)
    assert grader.active_container_names == ("autograde-active",)

    with pytest.raises(InfrastructureGradingError) as caught:
        grader.grade(
            workspace=_workspace(tmp_path / "collision"),
            runner_image=RUNNER_IMAGE,
            max_score=10,
        )
    assert caught.value.code == "container_name_collision"
    assert grader.active_container_names == ("autograde-active",)

    with pytest.raises(InfrastructureGradingError) as caught:
        grader.reconcile_orphans()
    assert caught.value.code == "reconciliation_while_active"

    grader.close()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert grader.closed is True
    assert grader.active_container_names == ()
    assert any(
        command == ("docker", "rm", "--force", "autograde-active")
        for command in executor.calls
    )
    assert failures and isinstance(failures[0], AssessmentGradingError)

    call_count = len(executor.calls)
    with pytest.raises(InfrastructureGradingError) as caught:
        grader.grade(
            workspace=_workspace(tmp_path / "closed"),
            runner_image=RUNNER_IMAGE,
            max_score=10,
        )
    assert caught.value.code == "grader_closed"
    assert len(executor.calls) == call_count

    grader.close()
    assert len(executor.calls) == call_count
