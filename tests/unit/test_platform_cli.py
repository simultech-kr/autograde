from __future__ import annotations

import json
import io
import os
import re
import signal
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import autograde.platform_cli as platform_cli
from autograde.platform_cli import main
from autograde.platform_grader import InfrastructureGradingError
from autograde.platform_service import PlatformAPIError, StudentPlatformService
from autograde.platform_state import (
    OperatorSubmissionView,
    PlatformAccessDenied,
    PlatformNotFound,
    PlatformStateStore,
    StudentActivationState,
    SubmissionState,
)
from autograde.settings import AppPaths


RUNNER = "ghcr.io/example/autograde@sha256:" + "a" * 64
COURSE = "cse101-2026f"


def invoke(data_root, *arguments):
    return main(
        [
            "--data-root",
            str(data_root),
            "--course-key",
            "cse101-2026f",
            *arguments,
        ]
    )


def output(capsys):
    return json.loads(capsys.readouterr().out)


def test_json_output_is_flushed_for_supervisor_readiness() -> None:
    class RecordingStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flush_count = 0

        def flush(self) -> None:
            self.flush_count += 1
            super().flush()

    stream = RecordingStream()

    platform_cli._print_json({"ok": True}, stream=stream)

    assert json.loads(stream.getvalue()) == {"ok": True}
    assert stream.flush_count == 1


@pytest.fixture(autouse=True)
def locally_available_runner_image(monkeypatch):
    """Keep existing CLI tests independent of a host Docker/Podman daemon."""

    monkeypatch.setattr(
        platform_cli.RunnerImageAvailabilityChecker,
        "check",
        lambda self, runner_image: platform_cli.RunnerImageAvailability(
            runtime=self.runtime,
            runner_image=runner_image,
        ),
    )


def bootstrap_ready_local_assignment(
    data_root,
    assessment,
    repository,
    capsys,
    monkeypatch,
) -> None:
    assert invoke(data_root, "init") == 0
    output(capsys)
    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
        )
        == 0
    )
    output(capsys)
    monkeypatch.setattr(
        platform_cli.GitCollector,
        "preflight",
        lambda *_args, **_kwargs: SimpleNamespace(commit_sha="a" * 40),
    )
    assert (
        invoke(
            data_root,
            "assignment",
            "add",
            "lab01",
            "20260001",
            "--assignment-id",
            "asn_lab01",
            "--release-id",
            "lab01-v1",
            "--github-repository-id",
            "9001",
            "--repository-owner",
            "school",
            "--repository-name",
            "lab01-student-one",
            "--clone-url",
            str(repository),
            "--assessment",
            str(assessment),
            "--runner-image",
            RUNNER,
            "--grading-runtime",
            "docker",
            "--max-score",
            "10",
        )
        == 0
    )
    assert output(capsys)["result"]["ready"] is True


def test_github_app_collection_configuration_is_all_or_none(
    tmp_path,
) -> None:
    private_key = tmp_path / "app.pem"
    private_key.write_text("test-key-material", encoding="utf-8")
    private_key.chmod(0o600)
    askpass = tmp_path / "askpass"
    askpass.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    askpass.chmod(0o700)
    parser = platform_cli.build_parser()

    partial = parser.parse_args(
        ["--course-key", COURSE, "--github-app-id", "123", "init"]
    )
    with pytest.raises(ValueError, match="configured together"):
        platform_cli._github_app_provider(partial)

    complete = parser.parse_args(
        [
            "--course-key",
            COURSE,
            "--github-app-id",
            "123",
            "--github-app-installation-id",
            "456",
            "--github-app-private-key",
            str(private_key),
            "--git-askpass-path",
            str(askpass),
            "init",
        ]
    )
    provider = platform_cli._github_app_provider(complete)
    assert provider is not None
    assert provider.app_id == 123
    assert provider.installation_id == 456
    assert provider.askpass_path == askpass


def test_platform_cli_bootstraps_student_and_assignment(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    data_root = tmp_path / "private"
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("print('runner owned')\n", encoding="utf-8")

    assert invoke(data_root, "init") == 0
    initialized = output(capsys)["result"]
    assert initialized["platform_schema_version"] >= 1
    assert os.stat(data_root / "platform-auth-secret").st_mode & 0o777 == 0o600

    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
        )
        == 0
    )
    assert output(capsys)["result"]["active"] is True

    monkeypatch.setattr(
        platform_cli.GitCollector,
        "preflight",
        lambda *_args, **_kwargs: SimpleNamespace(commit_sha="a" * 40),
    )

    assert (
        invoke(
            data_root,
            "assignment",
            "add",
            "lab01",
            "20260001",
            "--assignment-id",
            "asn_lab01",
            "--release-id",
            "lab01-v1",
            "--github-repository-id",
            "9001",
            "--repository-owner",
            "school",
            "--repository-name",
            "lab01-student-one",
            "--clone-url",
            str(tmp_path / "local-student.git"),
            "--assessment",
            str(assessment),
            "--runner-image",
            RUNNER,
            "--grading-runtime",
            "docker",
            "--max-score",
            "10",
        )
        == 0
    )
    registered = output(capsys)["result"]
    assert registered["assignment_id"] == "asn_lab01"
    assert registered["assessment_digest"].startswith("sha256:")
    assert registered["ready"] is True

    state = PlatformStateStore(data_root / "state.sqlite3")
    assignment = state.get_assignment("asn_lab01")
    assert assignment.github_repository_id == 9001
    assert assignment.runner_image == RUNNER

    assert invoke(data_root, "assignment", "hide", "asn_lab01") == 0
    assert output(capsys)["result"]["ready"] is False
    assert (
        invoke(
            data_root,
            "assignment",
            "ready",
            "asn_lab01",
            "--grading-runtime",
            "docker",
        )
        == 0
    )
    assert output(capsys)["result"]["ready"] is True


def test_assignment_preflight_is_a_nonzero_readiness_gate_with_stable_details(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    data_root = tmp_path / "private"
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("print('ok')\n", encoding="utf-8")
    assert invoke(
        data_root,
        "student",
        "add",
        "20260001",
        "--github-user-id",
        "101",
        "--github-login",
        "student-one",
    ) == 0
    output(capsys)

    def add_assignment(clone_url: str) -> int:
        return invoke(
            data_root,
            "assignment",
            "add",
            "lab01",
            "20260001",
            "--assignment-id",
            "asn_lab01",
            "--release-id",
            "lab01-v1",
            "--github-repository-id",
            "9001",
            "--repository-owner",
            "school",
            "--repository-name",
            "lab01-student-one",
            "--clone-url",
            clone_url,
            "--target-ref",
            "submission/lab01",
            "--assessment",
            str(assessment),
            "--runner-image",
            RUNNER,
            "--grading-runtime",
            "docker",
            "--max-score",
            "10",
        )

    assert add_assignment("https://github.com/school/lab01-student-one.git") == 1
    add_failure = json.loads(capsys.readouterr().err)
    assert add_failure["result"]["items"][0]["code"] == "github_app_not_configured"
    state = PlatformStateStore(data_root / "state.sqlite3")
    assert state.get_assignment("asn_lab01").ready is False

    assert invoke(data_root, "assignment", "preflight", "--jobs", "4") == 1
    failed = json.loads(capsys.readouterr().err)
    assert failed["error"]["code"] == "repository_preflight_failed"
    assert failed["result"]["go"] is False
    assert failed["result"]["items"][0]["code"] == "github_app_not_configured"

    assert invoke(data_root, "assignment", "ready", "asn_lab01") == 1
    ready_failure = json.loads(capsys.readouterr().err)
    assert ready_failure["result"]["items"][0]["code"] == "github_app_not_configured"
    assert state.get_assignment("asn_lab01").ready is False

    with state._write() as connection:
        connection.execute(
            "UPDATE platform_assignments SET clone_url = ? WHERE assignment_id = ?",
            (str(tmp_path / "local.git"), "asn_lab01"),
        )
    calls = []

    def fake_preflight(
        _self,
        repository_id,
        remote_url,
        *,
        assignment_subpath,
        target_ref,
    ):
        calls.append((repository_id, remote_url, assignment_subpath, target_ref))
        return SimpleNamespace(new_sha="a" * 40)

    monkeypatch.setattr(platform_cli.GitCollector, "preflight", fake_preflight, raising=False)
    assert invoke(data_root, "assignment", "preflight", "--jobs", "4") == 0
    passed = output(capsys)["result"]
    assert passed["go"] is True
    assert passed["passed"] == 1
    assert calls == [
        ("9001", str(tmp_path / "local.git"), ".", "submission/lab01")
    ]
    assert (
        invoke(
            data_root,
            "assignment",
            "ready",
            "asn_lab01",
            "--grading-runtime",
            "docker",
        )
        == 0
    )
    assert output(capsys)["result"]["ready"] is True


def test_add_and_ready_keep_assignment_hidden_when_runner_image_is_missing(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    data_root = tmp_path / "private"
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("print('ok')\n", encoding="utf-8")
    assert invoke(
        data_root,
        "student",
        "add",
        "20260001",
        "--github-user-id",
        "101",
        "--github-login",
        "student-one",
    ) == 0
    output(capsys)
    monkeypatch.setattr(
        platform_cli.GitCollector,
        "preflight",
        lambda *_args, **_kwargs: SimpleNamespace(commit_sha="a" * 40),
    )
    inspections = []

    def missing(self, runner_image):
        inspections.append((self.runtime, self.timeout_seconds, runner_image))
        raise platform_cli.RunnerImageAvailabilityError(
            "runner_image_missing",
            "runner image is not available in the configured container runtime",
        )

    monkeypatch.setattr(
        platform_cli.RunnerImageAvailabilityChecker,
        "check",
        missing,
    )
    add_arguments = (
        "assignment",
        "add",
        "lab01",
        "20260001",
        "--assignment-id",
        "asn_lab01",
        "--release-id",
        "lab01-v1",
        "--github-repository-id",
        "9001",
        "--repository-owner",
        "school",
        "--repository-name",
        "lab01-student-one",
        "--clone-url",
        str(tmp_path / "student.git"),
        "--assessment",
        str(assessment),
        "--runner-image",
        RUNNER,
        "--max-score",
        "10",
        "--container-runtime",
        "podman",
        "--runner-image-inspect-timeout",
        "4",
    )

    assert invoke(data_root, *add_arguments) == 1
    failed_add = json.loads(capsys.readouterr().err)
    assert failed_add["error"]["code"] == "runner_image_missing"
    state = PlatformStateStore(data_root / "state.sqlite3")
    assert state.get_assignment("asn_lab01").ready is False

    assert invoke(
        data_root,
        "assignment",
        "ready",
        "asn_lab01",
        "--container-runtime",
        "podman",
        "--runner-image-inspect-timeout",
        "5",
    ) == 1
    failed_ready = json.loads(capsys.readouterr().err)
    assert failed_ready["error"]["code"] == "runner_image_missing"
    assert state.get_assignment("asn_lab01").ready is False
    assert inspections == [("podman", 4.0, RUNNER), ("podman", 5.0, RUNNER)]


def test_runner_runtime_defaults_are_shared_and_inspect_timeout_is_bounded(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOGRADE_CONTAINER_RUNTIME", "podman")
    monkeypatch.setenv("AUTOGRADE_RUNNER_IMAGE_INSPECT_TIMEOUT", "6")
    parser = platform_cli.build_parser()

    for arguments in (
        ["--course-key", COURSE, "serve"],
        ["--course-key", COURSE, "assignment", "ready", "asn_01"],
        ["--course-key", COURSE, "assignment", "bundle-ready", "basn_01"],
        ["--course-key", COURSE, "submission", "process", "sub_01"],
    ):
        parsed = parser.parse_args(arguments)
        assert parsed.grading_runtime == "podman"
        assert parsed.runner_image_inspect_timeout == 6.0

    serve = parser.parse_args(["--course-key", COURSE, "serve"])
    assert serve.grading_runtime == "podman"
    assert serve.bundle_worker_count == 4
    assert serve.max_bundle_compressed_bytes == 25 * 1024 * 1024
    assert serve.max_bundle_expanded_bytes == 100 * 1024 * 1024
    assert serve.max_bundle_files == 5_000

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--course-key",
                COURSE,
                "serve",
                "--runner-image-inspect-timeout",
                "61",
            ]
        )


def test_grading_runtime_defaults_to_pilot_and_container_alias_is_compatible(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOGRADE_GRADING_RUNTIME", raising=False)
    monkeypatch.delenv("AUTOGRADE_CONTAINER_RUNTIME", raising=False)
    parser = platform_cli.build_parser()

    assert parser.parse_args(["--course-key", COURSE, "serve"]).grading_runtime == (
        "pilot-local"
    )
    assert parser.parse_args(
        ["--course-key", COURSE, "serve", "--grading-runtime", "docker"]
    ).grading_runtime == "docker"
    assert parser.parse_args(
        ["--course-key", COURSE, "serve", "--container-runtime", "podman"]
    ).grading_runtime == "podman"


@pytest.mark.parametrize("invalid", ("0", "-1", "nan", "inf", "-inf"))
@pytest.mark.parametrize("assignment_command", ("add", "bundle-add"))
def test_assignment_max_score_must_be_finite_and_positive_before_dispatch(
    tmp_path,
    invalid,
    assignment_command,
) -> None:
    parser = platform_cli.build_parser()
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    starter = tmp_path / "starter"
    starter.mkdir()
    common = [
        "--course-key",
        COURSE,
        "assignment",
        assignment_command,
        "lab01",
    ]
    if assignment_command == "add":
        arguments = [
            *common,
            "s001",
            "--release-id",
            "v1",
            "--github-repository-id",
            "1",
            "--repository-owner",
            "school",
            "--repository-name",
            "lab01-s001",
            "--clone-url",
            "https://example.test/lab01-s001.git",
            "--assessment",
            str(assessment),
            "--max-score",
            invalid,
        ]
    else:
        arguments = [
            *common,
            "--release-id",
            "v1",
            "--title",
            "Lab 01",
            "--starter",
            str(starter),
            "--assessment",
            str(assessment),
            "--max-score",
            invalid,
        ]

    with pytest.raises(SystemExit):
        parser.parse_args(arguments)


@pytest.mark.parametrize(
    "runtime_arguments",
    (
        ("--runner-image", RUNNER),
        ("--grading-runtime", "docker"),
    ),
)
def test_invalid_runner_selection_fails_before_bundle_artifact_write(
    tmp_path,
    capsys,
    monkeypatch,
    runtime_arguments,
) -> None:
    monkeypatch.delenv("AUTOGRADE_GRADING_RUNTIME", raising=False)
    monkeypatch.delenv("AUTOGRADE_CONTAINER_RUNTIME", raising=False)
    data_root = tmp_path / "private"
    starter = tmp_path / "starter"
    assessment = tmp_path / "assessment"
    starter.mkdir()
    assessment.mkdir()
    (starter / "main.py").write_text("pass\n", encoding="utf-8")
    (assessment / "grade.py").write_text("print('{}')\n", encoding="utf-8")

    assert invoke(
        data_root,
        "assignment",
        "bundle-add",
        "lab01",
        "--release-id",
        "v1",
        "--title",
        "Lab 01",
        "--starter",
        str(starter),
        "--assessment",
        str(assessment),
        "--max-score",
        "10",
        *runtime_arguments,
    ) == 1

    failure = json.loads(capsys.readouterr().err)
    assert failure["error"]["code"] == "value_error"
    assert list((data_root / "bundles").iterdir()) == []
    assert list((data_root / "instructor-inputs").iterdir()) == []


def test_platform_cli_issues_activation_once_to_stdout_or_private_file_and_revokes(
    tmp_path,
    capsys,
) -> None:
    data_root = tmp_path / "private"
    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
        )
        == 0
    )
    output(capsys)

    assert invoke(data_root, "auth", "issue", "20260001", "--show-code") == 0
    revealed = output(capsys)["result"]
    first_code = revealed["activation_code"]
    assert re.fullmatch(
        r"AG1-(?:[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}-){6}"
        r"[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{2}",
        first_code,
    )
    state = PlatformStateStore(data_root / "state.sqlite3")
    assert state.get_student_activation(
        revealed["activation_id"], course_key=COURSE
    ).state == StudentActivationState.ISSUED
    persisted = b"".join(
        path.read_bytes() for path in data_root.glob("state.sqlite3*")
    )
    assert first_code.encode("ascii") not in persisted

    code_file = tmp_path / "student-20260001.activation"
    assert (
        invoke(
            data_root,
            "auth",
            "issue",
            "20260001",
            "--output",
            str(code_file),
        )
        == 0
    )
    written = output(capsys)["result"]
    assert "activation_code" not in written
    assert written["activation_code_file"] == str(code_file)
    assert os.stat(code_file).st_mode & 0o777 == 0o600
    second_code = code_file.read_text(encoding="ascii").strip()
    assert second_code.startswith("AG1-")
    assert first_code != second_code
    assert state.get_student_activation(
        revealed["activation_id"], course_key=COURSE
    ).state == StudentActivationState.REVOKED
    assert state.get_student_activation(
        written["activation_id"], course_key=COURSE
    ).state == StudentActivationState.ISSUED

    assert invoke(data_root, "auth", "revoke", "20260001") == 0
    revoked = output(capsys)["result"]
    assert revoked["revoked"] == 1
    assert state.get_student_activation(
        written["activation_id"], course_key=COURSE
    ).state == StudentActivationState.REVOKED


def test_platform_cli_refuses_activation_file_overwrite_before_reissuing(
    tmp_path,
    capsys,
) -> None:
    data_root = tmp_path / "private"
    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
        )
        == 0
    )
    output(capsys)
    assert invoke(data_root, "auth", "issue", "20260001", "--show-code") == 0
    issued = output(capsys)["result"]

    existing = tmp_path / "existing.activation"
    existing.write_text("do-not-replace\n", encoding="ascii")
    assert (
        invoke(
            data_root,
            "auth",
            "issue",
            "20260001",
            "--output",
            str(existing),
        )
        == 1
    )
    failure = json.loads(capsys.readouterr().err)
    assert failure["ok"] is False
    assert existing.read_text(encoding="ascii") == "do-not-replace\n"
    state = PlatformStateStore(data_root / "state.sqlite3")
    assert state.get_student_activation(
        issued["activation_id"], course_key=COURSE
    ).state == StudentActivationState.ISSUED


def test_platform_cli_lists_revokes_and_resets_lost_student_sessions(
    tmp_path,
    capsys,
) -> None:
    data_root = tmp_path / "private"
    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
        )
        == 0
    )
    output(capsys)
    state = PlatformStateStore(data_root / "state.sqlite3")
    service = StudentPlatformService(
        state=state,
        server_secret=(data_root / "platform-auth-secret").read_bytes(),
        course_key=COURSE,
        public_base_url="http://127.0.0.1:8000",
    )

    def sign_in(device_name: str):
        authorization = service.create_device_authorization(
            {"device_name": device_name}
        )
        service.approve_user_code(
            user_code=authorization["user_code"], github_user_id=101
        )
        return service.exchange_device_authorization(
            {"device_code": authorization["device_code"]}
        )

    first = sign_in("first lost WSL")
    first_id = service.get_me(first["access_token"])["session"]["id"]
    second = sign_in("second lost WSL")

    assert (
        invoke(
            data_root,
            "auth",
            "sessions",
            "list",
            "--student-key",
            "20260001",
        )
        == 0
    )
    listed = output(capsys)["result"]
    assert listed["count"] == 2
    assert sum(item["active"] for item in listed["sessions"]) == 1
    encoded = json.dumps(listed)
    assert "access_token" not in encoded
    assert "refresh_token" not in encoded
    assert "token_family" not in encoded

    assert (
        invoke(
            data_root,
            "auth",
            "sessions",
            "revoke",
            first_id,
            "--student-key",
            "20260001",
        )
        == 0
    )
    revoked = output(capsys)["result"]
    assert revoked["session"]["session_id"] == first_id
    assert revoked["session"]["active"] is False
    with pytest.raises(PlatformAPIError, match="invalid or expired"):
        service.get_me(first["access_token"])
    assert service.get_me(second["access_token"])["student_key"] == "20260001"

    assert (
        invoke(
            data_root,
            "auth",
            "sessions",
            "reset",
            "--student-key",
            "20260001",
        )
        == 0
    )
    reset = output(capsys)["result"]
    assert reset["revoked"] == 1
    with pytest.raises(PlatformAPIError, match="invalid or expired"):
        service.get_me(second["access_token"])


@pytest.mark.parametrize("failure_operation", ["write", "fsync"])
def test_platform_cli_revokes_activation_when_private_file_delivery_fails(
    tmp_path,
    capsys,
    monkeypatch,
    failure_operation,
) -> None:
    data_root = tmp_path / "private"
    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
        )
        == 0
    )
    output(capsys)

    def fail_delivery(*_args):
        raise OSError("simulated activation delivery failure")

    monkeypatch.setattr(platform_cli.os, failure_operation, fail_delivery)
    target = tmp_path / "failed.activation"
    assert (
        invoke(
            data_root,
            "auth",
            "issue",
            "20260001",
            "--output",
            str(target),
        )
        == 1
    )
    failure = json.loads(capsys.readouterr().err)
    assert failure["error"]["code"] == "o_s_error"
    assert not target.exists()
    with sqlite3.connect(data_root / "state.sqlite3") as connection:
        states = [
            row[0]
            for row in connection.execute(
                "SELECT state FROM platform_student_activations"
            )
        ]
    assert states == [StudentActivationState.REVOKED.value]


def test_platform_cli_rejects_inactive_or_overlong_activation_issuance(
    tmp_path,
    capsys,
) -> None:
    data_root = tmp_path / "private"
    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
            "--inactive",
        )
        == 0
    )
    output(capsys)
    assert invoke(data_root, "auth", "issue", "20260001", "--show-code") == 1
    inactive = json.loads(capsys.readouterr().err)
    assert inactive["error"]["code"] == "platform_access_denied"

    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
        )
        == 0
    )
    output(capsys)
    assert (
        invoke(
            data_root,
            "auth",
            "issue",
            "20260001",
            "--expires-in-hours",
            "721",
            "--show-code",
        )
        == 1
    )
    overlong = json.loads(capsys.readouterr().err)
    assert overlong["error"]["code"] == "value_error"


def test_platform_cli_inactive_changes_only_current_course_enrollment(
    tmp_path, capsys
) -> None:
    data_root = tmp_path / "private"
    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
        )
        == 0
    )
    output(capsys)
    state = PlatformStateStore(data_root / "state.sqlite3")
    student = state.get_student_by_key("20260001")
    state.upsert_enrollment(
        student_id=student.id,
        course_key="cse202-2026f",
        active=True,
    )

    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "101",
            "--github-login",
            "student-one",
            "--inactive",
        )
        == 0
    )
    result = output(capsys)["result"]
    assert result["active"] is False
    assert state.get_student_by_key("20260001").active is True
    with pytest.raises(PlatformAccessDenied):
        state.require_active_enrollment(
            student_id=student.id,
            course_key="cse101-2026f",
        )
    assert state.require_active_enrollment(
        student_id=student.id,
        course_key="cse202-2026f",
    ).active is True

    assert (
        invoke(
            data_root,
            "student",
            "add",
            "20260001",
            "--github-user-id",
            "202",
            "--github-login",
            "different-account",
        )
        == 1
    )
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "platform_conflict"
    unchanged = state.get_student_by_key("20260001")
    assert unchanged.github_user_id == 101
    assert unchanged.github_login == "student-one"


def test_platform_cli_reports_safe_validation_error(tmp_path, capsys) -> None:
    result = invoke(
        tmp_path / "private",
        "student",
        "add",
        "20260001",
        "--github-user-id",
        "0",
        "--github-login",
        "student-one",
    )

    assert result == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["ok"] is False
    assert "positive" in payload["error"]["message"]


def test_platform_cli_lists_and_shows_only_safe_operator_submission_fields(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    item = OperatorSubmissionView(
        submission_id="sub_01",
        student_key="s001",
        github_login="student-one",
        course_key="cse101-2026f",
        assignment_id="asn_01",
        assignment_key="lab01",
        release_id="lab01-v1",
        repository_owner="school",
        repository_name="lab01-s001",
        github_repository_id=9001,
        requested_sha="a" * 40,
        state=SubmissionState.PUBLISHED,
        received_at="2026-08-22T12:00:00.000000Z",
        updated_at="2026-08-22T12:02:00.000000Z",
        receipt_id="rcp_01",
        commit_sha="a" * 40,
        source_digest="sha256:" + "b" * 64,
        accepted_at="2026-08-22T12:00:01.000000Z",
        result_id="res_01",
        score=8.0,
        max_score=10.0,
        result_created_at="2026-08-22T12:02:00.000000Z",
        published_at="2026-08-22T12:02:01.000000Z",
    )
    calls = []

    def list_submissions(_self, **kwargs):
        calls.append(("list", kwargs))
        return [item]

    def get_submission(_self, submission_id, *, course_key):
        calls.append(("show", submission_id, course_key))
        return item

    monkeypatch.setattr(
        PlatformStateStore,
        "list_operator_submissions",
        list_submissions,
    )
    monkeypatch.setattr(
        PlatformStateStore,
        "get_operator_submission",
        get_submission,
    )
    data_root = tmp_path / "private"

    assert (
        invoke(
            data_root,
            "submission",
            "list",
            "--student-key",
            "s001",
            "--state",
            "published",
            "--limit",
            "25",
        )
        == 0
    )
    listed = output(capsys)["result"]
    assert listed["count"] == 1
    assert listed["submissions"][0]["submission_id"] == "sub_01"
    assert calls[0][1]["limit"] == 25

    assert invoke(data_root, "submission", "show", "sub_01") == 0
    shown = output(capsys)["result"]
    assert shown["receipt"]["source_digest"].startswith("sha256:")
    assert shown["result"] == {
        "created_at": "2026-08-22T12:02:00.000000Z",
        "max_score": 10.0,
        "published_at": "2026-08-22T12:02:01.000000Z",
        "result_id": "res_01",
        "score": 8.0,
    }
    encoded = json.dumps(shown)
    assert "source_path" not in encoded
    assert "idempotency" not in encoded
    assert "rubric" not in encoded
    assert "diagnostics" not in encoded
    assert calls[1] == ("show", "sub_01", "cse101-2026f")


def test_platform_cli_blocks_cross_course_publish_and_process_before_side_effects(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    checked_courses = []
    side_effects = []

    def reject_foreign_submission(_self, submission_id, *, course_key):
        checked_courses.append((submission_id, course_key))
        raise PlatformNotFound("submission was not found in this course")

    def unexpected_publish(_self, submission_id):
        side_effects.append(("publish", submission_id))
        raise AssertionError("publish must not run for another course")

    class UnexpectedGrader:
        def __init__(self, **_kwargs):
            side_effects.append(("grader", None))
            raise AssertionError("grader must not start for another course")

    monkeypatch.setattr(
        PlatformStateStore,
        "get_operator_submission",
        reject_foreign_submission,
    )
    monkeypatch.setattr(
        PlatformStateStore,
        "publish_result",
        unexpected_publish,
    )
    monkeypatch.setattr(platform_cli, "ContainerGrader", UnexpectedGrader)
    data_root = tmp_path / "private"

    for command in ("publish", "process"):
        assert invoke(data_root, "submission", command, "sub_foreign") == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        payload = json.loads(captured.err)
        assert payload["error"]["code"] == "platform_not_found"
        assert payload["error"]["message"] == "submission was not found in this course"

    assert checked_courses == [
        ("sub_foreign", "cse101-2026f"),
        ("sub_foreign", "cse101-2026f"),
    ]
    assert side_effects == []


def test_grader_instance_label_is_stable_scoped_and_non_sensitive(tmp_path) -> None:
    first = AppPaths.from_value(tmp_path / "first").ensure()
    second = AppPaths.from_value(tmp_path / "second").ensure()

    label = platform_cli._grader_instance_label(first, "cse101-2026f")

    assert label == platform_cli._grader_instance_label(first, "cse101-2026f")
    assert label != platform_cli._grader_instance_label(first, "cse102-2026f")
    assert label != platform_cli._grader_instance_label(second, "cse101-2026f")
    assert str(first.root) not in label
    assert "cse101" not in label
    assert label.startswith("service-")
    assert len(label) == len("service-") + 32


def test_course_service_lock_excludes_same_course_and_releases_cleanly(tmp_path) -> None:
    paths = AppPaths.from_value(tmp_path / "private").ensure()
    lock_path = paths.root / (
        ".autograde-platform-"
        f"{platform_cli._grader_instance_label(paths, COURSE)}.lock"
    )

    with platform_cli._exclusive_course_service_lock(paths, COURSE):
        assert os.stat(lock_path).st_mode & 0o777 == 0o600
        with pytest.raises(
            platform_cli.PlatformStateError,
            match="another grading service or manual processor",
        ):
            with platform_cli._exclusive_course_service_lock(paths, COURSE):
                raise AssertionError("same-course lock must not be entered")
        with platform_cli._exclusive_course_service_lock(paths, "other-course"):
            pass

    with platform_cli._exclusive_course_service_lock(paths, COURSE):
        pass


def test_serve_fails_repository_preflight_before_starting_grader(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    data_root = tmp_path / "private"
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("print('ok')\n", encoding="utf-8")
    assessment_digest = platform_cli.WorkspaceBuilder(
        data_root / "workspaces"
    ).digest_instructor_tree(assessment, label="assessment").sha256
    state = PlatformStateStore(data_root / "state.sqlite3")
    student = state.upsert_student(
        student_key="20260001",
        auth_subject="github:101",
        github_user_id=101,
        github_login="student-one",
    )
    state.upsert_enrollment(student_id=student.id, course_key=COURSE)
    state.register_assignment(
        assignment_id="asn_lab01",
        student_id=student.id,
        course_key=COURSE,
        assignment_key="lab01",
        release_id="lab01-v1",
        github_repository_id=9001,
        repository_owner="school",
        repository_name="lab01-student-one",
        clone_url="https://github.com/school/lab01-student-one.git",
        submission_mode="branch",
        target_ref="submission/lab01",
        result_policy="immediate",
        assessment_path=str(assessment),
        assessment_digest=assessment_digest,
        runner_image=RUNNER,
        rubric_version="v1",
        max_score=10,
        ready=True,
    )

    class UnexpectedGrader:
        def __init__(self, **_kwargs):
            raise AssertionError("grader must not start before repository readiness")

    monkeypatch.setattr(platform_cli, "ContainerGrader", UnexpectedGrader)

    assert invoke(data_root, "serve", "--port", "0") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    failure = json.loads(captured.err)
    assert failure["error"]["code"] == "repository_preflight_failed"
    assert failure["result"]["items"][0]["code"] == "github_app_not_configured"


def test_serve_checks_each_unique_ready_runner_before_creating_grader(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    data_root = tmp_path / "private"
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("print('ok')\n", encoding="utf-8")
    assessment_digest = platform_cli.WorkspaceBuilder(
        data_root / "workspaces"
    ).digest_instructor_tree(assessment, label="assessment").sha256
    state = PlatformStateStore(data_root / "state.sqlite3")
    for index in (1, 2):
        student = state.upsert_student(
            student_key=f"2026000{index}",
            auth_subject=f"github:{100 + index}",
            github_user_id=100 + index,
            github_login=f"student-{index}",
        )
        state.upsert_enrollment(student_id=student.id, course_key=COURSE)
        state.register_assignment(
            assignment_id=f"asn_lab01_{index}",
            student_id=student.id,
            course_key=COURSE,
            assignment_key="lab01",
            release_id="lab01-v1",
            github_repository_id=9000 + index,
            repository_owner="school",
            repository_name=f"lab01-student-{index}",
            clone_url=f"https://github.com/school/lab01-student-{index}.git",
            submission_mode="branch",
            target_ref="submission/lab01",
            result_policy="immediate",
            assessment_path=str(assessment),
            assessment_digest=assessment_digest,
            runner_image=RUNNER,
            rubric_version="v1",
            max_score=10,
            ready=True,
        )

    monkeypatch.setattr(
        platform_cli,
        "_repository_preflight",
        lambda *_args, **_kwargs: platform_cli.RepositoryPreflightReport(()),
    )
    inspections = []

    def missing(self, runner_image):
        inspections.append((self.runtime, self.timeout_seconds, runner_image))
        raise platform_cli.RunnerImageAvailabilityError(
            "runner_image_missing",
            "runner image is not available in the configured container runtime",
        )

    monkeypatch.setattr(
        platform_cli.RunnerImageAvailabilityChecker,
        "check",
        missing,
    )

    class UnexpectedGrader:
        def __init__(self, **_kwargs):
            raise AssertionError("grader must not start before runner image readiness")

    monkeypatch.setattr(platform_cli, "ContainerGrader", UnexpectedGrader)

    assert invoke(
        data_root,
        "serve",
        "--port",
        "0",
        "--container-runtime",
        "podman",
        "--runner-image-inspect-timeout",
        "8",
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    failure = json.loads(captured.err)
    assert failure["error"]["code"] == "runner_image_missing"
    assert inspections == [("podman", 8.0, RUNNER)]


@pytest.mark.parametrize(
    ("change", "expected_code"),
    (
        ("mutate", "assessment_digest_mismatch"),
        ("delete", "assessment_input_missing"),
    ),
)
def test_changed_assessment_fails_preflight_and_ready_stays_hidden(
    tmp_path,
    capsys,
    monkeypatch,
    change,
    expected_code,
) -> None:
    data_root = tmp_path / "private"
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    grader_file = assessment / "grade.py"
    grader_file.write_text("print('v1')\n", encoding="utf-8")
    bootstrap_ready_local_assignment(
        data_root,
        assessment,
        tmp_path / "student.git",
        capsys,
        monkeypatch,
    )

    if change == "mutate":
        grader_file.write_text("print('v2')\n", encoding="utf-8")
    else:
        grader_file.unlink()
        assessment.rmdir()

    assert invoke(data_root, "assignment", "preflight", "--jobs", "1") == 1
    preflight = json.loads(capsys.readouterr().err)
    assert preflight["result"]["items"][0]["code"] == expected_code

    assert invoke(data_root, "assignment", "hide", "asn_lab01") == 0
    assert output(capsys)["result"]["ready"] is False
    assert invoke(data_root, "assignment", "ready", "asn_lab01") == 1
    ready = json.loads(capsys.readouterr().err)
    assert ready["result"]["items"][0]["code"] == expected_code
    assignment = PlatformStateStore(data_root / "state.sqlite3").list_operator_assignments(
        course_key=COURSE,
        limit=10,
    )[0]
    assert assignment.ready is False


def test_changed_assessment_blocks_startup_before_grader_creation(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    data_root = tmp_path / "private"
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    grader_file = assessment / "grade.py"
    grader_file.write_text("print('v1')\n", encoding="utf-8")
    bootstrap_ready_local_assignment(
        data_root,
        assessment,
        tmp_path / "student.git",
        capsys,
        monkeypatch,
    )
    grader_file.write_text("print('changed')\n", encoding="utf-8")

    class UnexpectedGrader:
        def __init__(self, **_kwargs):
            raise AssertionError("grader must not start with changed assessment")

    monkeypatch.setattr(platform_cli, "ContainerGrader", UnexpectedGrader)

    assert invoke(data_root, "serve", "--port", "0") == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    failure = json.loads(captured.err)
    assert failure["error"]["code"] == "repository_preflight_failed"
    assert failure["result"]["items"][0]["code"] == (
        "assessment_digest_mismatch"
    )


@pytest.mark.parametrize(
    ("archive_size", "quota", "unsafe", "expected_code"),
    (
        (8, 8, False, "snapshot_store_quota_error"),
        (9, 8, False, "snapshot_store_quota_error"),
        (0, 8, True, "unsafe_path_error"),
    ),
)
def test_serve_checks_snapshot_store_before_creating_grader(
    tmp_path,
    capsys,
    monkeypatch,
    archive_size,
    quota,
    unsafe,
    expected_code,
) -> None:
    data_root = tmp_path / "private"
    snapshots = data_root / "snapshots"
    snapshots.mkdir(parents=True)
    archive = snapshots / "existing.tar.gz"
    if unsafe:
        outside = tmp_path / "outside.tar.gz"
        outside.write_bytes(b"outside")
        archive.symlink_to(outside)
    else:
        archive.write_bytes(b"x" * archive_size)

    class UnexpectedGrader:
        def __init__(self, **_kwargs):
            raise AssertionError("grader must not start before quota readiness")

    monkeypatch.setattr(platform_cli, "ContainerGrader", UnexpectedGrader)

    code = invoke(
        data_root,
        "serve",
        "--port",
        "0",
        "--max-total-snapshot-bytes",
        str(quota),
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    failure = json.loads(captured.err)
    assert failure["error"]["code"] == expected_code


@pytest.mark.parametrize(
    ("shutdown_signal", "expected_code"),
    [(signal.SIGTERM, 0), (signal.SIGINT, 130)],
)
def test_serve_reconciles_and_signal_shutdown_cleans_without_deadlock(
    tmp_path,
    capsys,
    monkeypatch,
    shutdown_signal,
    expected_code,
) -> None:
    events = []
    created = {}

    class FakeGrader:
        def __init__(self, *, runtime, instance_label):
            self.runtime = runtime
            self.instance_label = instance_label
            created["grader"] = self
            events.append("grader-created")

        def reconcile_orphans(self):
            events.append("reconciled")
            return 2

        def close(self):
            events.append("grader-closed")

    class FakeWorker:
        def __init__(
            self,
            processor,
            *,
            course_key,
            max_queue_size,
            recovery_interval_seconds,
        ):
            self.processor = processor
            assert course_key == COURSE
            events.append("worker-created")

        def notify(self, _submission_id):
            return True

        def start(self):
            events.append("worker-started")

        def stop(self, timeout):
            events.append(f"worker-stopped-{timeout:g}")
            return True

    class FakeServer:
        server_address = ("127.0.0.1", 18432)

        def __init__(self):
            self.stopped = threading.Event()

        def serve_forever(self):
            events.append("server-serving")
            os.kill(os.getpid(), shutdown_signal)
            if not self.stopped.wait(timeout=2):
                raise AssertionError("CLI signal coordination deadlocked")
            events.append("server-returned")

        def shutdown(self):
            assert threading.current_thread() is threading.main_thread()
            events.append("server-shutdown")
            self.stopped.set()

        def server_close(self):
            events.append("server-closed")

    server = FakeServer()
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    monkeypatch.delenv("AUTOGRADE_GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(platform_cli, "ContainerGrader", FakeGrader)
    monkeypatch.setattr(platform_cli, "SubmissionWorker", FakeWorker)
    monkeypatch.setattr(platform_cli, "create_server", lambda *_args, **_kwargs: server)

    data_root = tmp_path / "private"
    code = invoke(
        data_root,
        "serve",
        "--port",
        "0",
        "--container-runtime",
        "podman",
    )

    assert code == expected_code
    assert created["grader"].runtime == "podman"
    assert created["grader"].instance_label == platform_cli._grader_instance_label(
        AppPaths.from_value(data_root), "cse101-2026f"
    )
    assert events.index("reconciled") < events.index("worker-started")
    assert events.index("worker-started") < events.index("server-serving")
    assert events.index("server-shutdown") < events.index("server-returned")
    assert events.index("server-closed") < events.index("worker-stopped-30")
    assert events.index("worker-stopped-30") < events.index("grader-closed")
    assert signal.getsignal(signal.SIGTERM) == previous_term
    assert signal.getsignal(signal.SIGINT) == previous_int

    captured = capsys.readouterr()
    startup = json.loads(captured.out)
    assert startup["result"]["listening"] == "127.0.0.1:18432"
    assert startup["result"]["snapshot_store"] == {
        "managed_bytes": 0,
        "max_bytes": 50 * 1024 * 1024 * 1024,
        "remaining_bytes": 50 * 1024 * 1024 * 1024,
    }
    assert startup["result"]["delivery_modes"] == ["bundle", "git"]
    assert startup["result"]["bundle_assignments"] == {
        "ready": 0,
        "worker_count": 4,
        "max_compressed_bytes": 25 * 1024 * 1024,
        "max_expanded_bytes": 100 * 1024 * 1024,
        "max_files": 5_000,
    }
    dashboard = startup["result"]["instructor_dashboard"]
    assert dashboard["url"] == "http://127.0.0.1:8000/instructor"
    assert dashboard["username"] == "instructor"
    assert dashboard["token_file"] == str(
        data_root / "platform-instructor-token"
    )
    assert os.stat(dashboard["token_file"]).st_mode & 0o777 == 0o600
    if shutdown_signal == signal.SIGINT:
        interrupted = json.loads(captured.err)
        assert interrupted["error"]["code"] == "interrupted"
    else:
        assert captured.err == ""


def test_serve_closes_grader_when_startup_reconciliation_fails(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    events = []

    class BrokenReconciliationGrader:
        def __init__(self, *, runtime, instance_label):
            events.append("created")

        def reconcile_orphans(self):
            events.append("reconcile")
            raise InfrastructureGradingError(
                "orphan_reconciliation_failed", "safe failure"
            )

        def close(self):
            events.append("closed")

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    monkeypatch.delenv("AUTOGRADE_GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(
        platform_cli,
        "ContainerGrader",
        BrokenReconciliationGrader,
    )

    assert invoke(
        tmp_path / "private",
        "serve",
        "--port",
        "0",
        "--grading-runtime",
        "docker",
    ) == 1

    assert events == ["created", "reconcile", "closed"]
    assert signal.getsignal(signal.SIGTERM) == previous_term
    assert signal.getsignal(signal.SIGINT) == previous_int
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == "infrastructure_grading_error"


def test_serve_pilot_skips_container_checks_and_reports_unsandboxed_security(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    events = []

    class FakePilotGrader:
        def __init__(self):
            events.append("pilot-created")

        def close(self):
            events.append("pilot-closed")

    class FakeWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        def notify(self, _submission_id):
            return True

        def start(self):
            events.append("worker-started")

        def stop(self, timeout):
            events.append(f"worker-stopped-{timeout}")
            return True

    class FakeServer:
        server_address = ("127.0.0.1", 18080)

        def serve_forever(self):
            events.append("server-served")

        def server_close(self):
            events.append("server-closed")

    class UnexpectedContainer:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("pilot-local must not create a container")

    def unexpected_inspection(*_args, **_kwargs):
        raise AssertionError("pilot-local must not inspect a container")

    monkeypatch.delenv("AUTOGRADE_GRADING_RUNTIME", raising=False)
    monkeypatch.delenv("AUTOGRADE_CONTAINER_RUNTIME", raising=False)
    monkeypatch.delenv("AUTOGRADE_GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(platform_cli, "PilotLocalGrader", FakePilotGrader)
    monkeypatch.setattr(platform_cli, "ContainerGrader", UnexpectedContainer)
    monkeypatch.setattr(
        platform_cli.RunnerImageAvailabilityChecker,
        "check_many",
        unexpected_inspection,
    )
    monkeypatch.setattr(platform_cli, "SubmissionWorker", FakeWorker)
    monkeypatch.setattr(platform_cli, "BundleSubmissionWorker", FakeWorker)
    monkeypatch.setattr(
        platform_cli,
        "create_server",
        lambda *_args, **_kwargs: FakeServer(),
    )

    assert invoke(tmp_path / "private", "serve", "--port", "0") == 0

    captured = capsys.readouterr()
    startup = json.loads(captured.out)["result"]
    warning = json.loads(captured.err)
    assert startup["grading_runtime"] == "pilot-local"
    assert startup["runner_images"] == {
        "code": "not_applicable",
        "checked": 0,
        "runtime": None,
    }
    assert startup["security"]["classification"] == "trusted_code_only"
    assert startup["security"]["sandboxed"] is False
    assert startup["security"]["limits"]["network_isolation"] is False
    assert startup["external_access_mode"] == "disabled"
    assert startup["network_security"] == {
        "encrypted": False,
        "scope": "loopback_or_tls",
        "instructor_dashboard_enabled": True,
    }
    assert startup["instructor_dashboard"]["enabled"] is True
    assert warning == {
        "component": "grader",
        "event": "pilot_local_unsandboxed",
        "level": "warning",
    }
    assert events == [
        "pilot-created",
        "worker-started",
        "worker-started",
        "server-served",
        "server-closed",
        "worker-stopped-30",
        "worker-stopped-30",
        "pilot-closed",
    ]


def test_insecure_http_serve_reports_network_warning_and_hides_dashboard(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    config_path = tmp_path / "pilot.csv"
    config_path.write_text(
        "key,value\n"
        "course_key,cse101-2026f\n"
        "data_root,state\n"
        "public_base_url,http://192.168.50.9:18080\n"
        "listen,192.168.50.9\n"
        "port,18080\n"
        "grading_runtime,pilot-local\n"
        "external_access_mode,insecure-http\n",
        encoding="utf-8",
    )

    class FakePilotGrader:
        def close(self):
            pass

    class FakeWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        def notify(self, _submission_id):
            return True

        def start(self):
            pass

        def stop(self, _timeout=None, **_kwargs):
            return True

    class FakeServer:
        server_address = ("192.168.50.9", 18080)

        def serve_forever(self):
            pass

        def server_close(self):
            pass

    captured_server_options = {}

    def fake_create_server(*_args, **options):
        captured_server_options.update(options)
        return FakeServer()

    monkeypatch.delenv("AUTOGRADE_GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(platform_cli, "PilotLocalGrader", FakePilotGrader)
    monkeypatch.setattr(platform_cli, "SubmissionWorker", FakeWorker)
    monkeypatch.setattr(platform_cli, "BundleSubmissionWorker", FakeWorker)
    monkeypatch.setattr(platform_cli, "create_server", fake_create_server)

    assert main(["--pilot-config", str(config_path), "serve"]) == 0

    captured = capsys.readouterr()
    startup = json.loads(captured.out)["result"]
    warnings = [json.loads(line) for line in captured.err.splitlines()]
    assert captured_server_options["external_access_mode"] == "insecure-http"
    assert startup["external_access_mode"] == "insecure-http"
    assert startup["network_security"] == {
        "encrypted": False,
        "scope": "rfc1918_trusted_lan",
        "instructor_dashboard_enabled": False,
        "warning": (
            "student credentials and submissions cross the trusted LAN "
            "without transport encryption"
        ),
    }
    assert startup["instructor_dashboard"] == {
        "enabled": False,
        "reason": "disabled_over_insecure_http",
    }
    assert warnings == [
        {
            "component": "grader",
            "event": "pilot_local_unsandboxed",
            "level": "warning",
        },
        {
            "component": "http",
            "event": "insecure_http_external_access",
            "level": "warning",
        },
    ]


def test_serve_closes_server_and_grader_when_worker_recovery_fails(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    events = []
    created = {}

    class FakeGrader:
        def __init__(self, *, runtime, instance_label):
            events.append("grader-created")

        def reconcile_orphans(self):
            events.append("reconciled")

        def close(self):
            events.append("grader-closed")

    class FakeServer:
        server_address = ("127.0.0.1", 18432)

        def server_close(self):
            events.append("server-closed")

    real_worker = platform_cli.SubmissionWorker

    def capture_worker(*args, **kwargs):
        worker = real_worker(*args, **kwargs)
        created["worker"] = worker
        return worker

    def fail_recovery(_self, *, course_key):
        assert course_key == COURSE
        raise platform_cli.PlatformStateError("database recovery unavailable")

    monkeypatch.setattr(platform_cli, "ContainerGrader", FakeGrader)
    monkeypatch.setattr(platform_cli, "SubmissionWorker", capture_worker)
    monkeypatch.setattr(
        platform_cli.PlatformStateStore,
        "list_submissions_for_processing",
        fail_recovery,
    )
    monkeypatch.setattr(
        platform_cli,
        "create_server",
        lambda *_args, **_kwargs: FakeServer(),
    )

    assert invoke(
        tmp_path / "private",
        "serve",
        "--port",
        "0",
        "--grading-runtime",
        "docker",
    ) == 1

    worker = created["worker"]
    assert worker._thread is None
    assert worker._stopping.is_set()
    assert events == [
        "grader-created",
        "reconciled",
        "server-closed",
        "grader-closed",
    ]
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "platform_state_error"


def test_serve_reports_unexpected_http_failure_without_exception_detail(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    secret = "database-password-must-not-leak"

    class FakeGrader:
        def __init__(self, *, runtime, instance_label):
            pass

        def reconcile_orphans(self):
            return 0

        def close(self):
            pass

    class FakeWorker:
        def __init__(
            self,
            processor,
            *,
            course_key,
            max_queue_size,
            recovery_interval_seconds,
        ):
            assert course_key == COURSE

        def notify(self, _submission_id):
            return True

        def start(self):
            pass

        def stop(self, timeout):
            return True

    class BrokenServer:
        server_address = ("127.0.0.1", 18432)

        def serve_forever(self):
            raise RuntimeError(secret)

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.delenv("AUTOGRADE_GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(platform_cli, "ContainerGrader", FakeGrader)
    monkeypatch.setattr(platform_cli, "SubmissionWorker", FakeWorker)
    monkeypatch.setattr(
        platform_cli,
        "create_server",
        lambda *_args, **_kwargs: BrokenServer(),
    )

    assert invoke(
        tmp_path / "private",
        "serve",
        "--port",
        "0",
        "--grading-runtime",
        "docker",
    ) == 1

    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    lines = captured.err.splitlines()
    assert len(lines) == 2
    event, cli_error = (json.loads(line) for line in lines)
    assert event == {
        "component": "http",
        "event": "http_server_unexpected_exception",
        "exception_type": "RuntimeError",
        "level": "error",
    }
    assert cli_error["error"] == {
        "code": "platform_state_error",
        "message": "HTTP server stopped unexpectedly",
    }


def test_local_student_add_and_validated_roster_import(
    tmp_path,
    capsys,
) -> None:
    data_root = tmp_path / "private"

    assert invoke(data_root, "student", "add", "20260001") == 0
    local = output(capsys)["result"]
    assert local["identity"] == {"kind": "local"}
    assert local["github_user_id"] is None
    assert local["github_login"] is None

    assert (
        invoke(
            data_root,
            "student",
            "add",
            "bad-pair",
            "--github-user-id",
            "101",
        )
        == 1
    )
    assert "provided together" in json.loads(capsys.readouterr().err)["error"][
        "message"
    ]

    roster = tmp_path / "roster.csv"
    roster.write_text(
        "student_key,github_user_id,github_login,active\n"
        "20260002,,,true\n"
        "20260003,103,student-three,false\n",
        encoding="utf-8",
    )
    assert invoke(data_root, "student", "import", str(roster)) == 0
    imported = output(capsys)["result"]
    assert imported == {
        "atomic": False,
        "count": 2,
        "course_key": COURSE,
        "github_count": 1,
        "local_count": 1,
        "validation": "all_rows_before_apply",
    }
    state = PlatformStateStore(data_root / "state.sqlite3")
    assert state.get_student_by_key("20260002").identity_kind.value == "local"
    assert state.get_student_by_key("20260003").identity_kind.value == "github"
    with state._connection() as connection:
        active = connection.execute(
            "SELECT active FROM platform_enrollments WHERE student_id = ? "
            "AND course_key = ?",
            (state.get_student_by_key("20260003").id, COURSE),
        ).fetchone()["active"]
    assert active == 0


def test_roster_import_validates_all_rows_before_any_write(
    tmp_path,
    capsys,
) -> None:
    data_root = tmp_path / "private"
    roster = tmp_path / "invalid-roster.csv"
    roster.write_text(
        "student_key,github_user_id,github_login\n"
        "20260001,,\n"
        "20260002,not-a-number,student-two\n",
        encoding="utf-8",
    )

    assert invoke(data_root, "student", "import", str(roster)) == 1
    failure = json.loads(capsys.readouterr().err)
    assert "row 3" in failure["error"]["message"]
    state = PlatformStateStore(data_root / "state.sqlite3")
    with pytest.raises(PlatformNotFound):
        state.get_student_by_key("20260001")


def test_bundle_assignment_cli_creates_immutable_release_and_manages_visibility(
    tmp_path,
    capsys,
) -> None:
    data_root = tmp_path / "private"
    starter = tmp_path / "starter"
    assessment = tmp_path / "assessment"
    data = tmp_path / "data"
    starter.mkdir()
    assessment.mkdir()
    data.mkdir()
    (starter / "main.py").write_text("print('student')\n", encoding="utf-8")
    (assessment / "grade.py").write_text("print('grader')\n", encoding="utf-8")
    (data / "input.txt").write_text("example\n", encoding="utf-8")

    assert (
        invoke(
            data_root,
            "assignment",
            "bundle-add",
            "lab01",
            "--assignment-id",
            "basn_lab01",
            "--release-id",
            "lab01-v1",
            "--title",
            "Lab 01",
            "--starter",
            str(starter),
            "--assessment",
            str(assessment),
            "--data",
            str(data),
            "--max-score",
            "10",
        )
        == 0
    )
    created = output(capsys)["result"]
    assert created["assignment_id"] == "basn_lab01"
    assert created["delivery_mode"] == "bundle"
    assert created["grading_runtime"] == "pilot-local"
    assert created["runner_image"] == platform_cli.PILOT_LOCAL_RUNNER
    assert created["starter_digest"].startswith("sha256:")
    assert created["ready"] is True

    state = PlatformStateStore(data_root / "state.sqlite3")
    assignment = state.get_bundle_assignment("basn_lab01")
    assert assignment.runner_image == platform_cli.PILOT_LOCAL_RUNNER
    assert Path(assignment.assessment_path).is_relative_to(
        data_root / "instructor-inputs" / "assessment"
    )
    assert Path(assignment.data_path).is_relative_to(
        data_root / "instructor-inputs" / "data"
    )
    assert Path(assignment.starter_path).is_file()

    assert invoke(data_root, "assignment", "bundle-list") == 0
    listed = output(capsys)["result"]
    assert listed["count"] == 1
    assert listed["assignments"][0]["assignment_id"] == "basn_lab01"

    assert invoke(data_root, "assignment", "bundle-hide", "basn_lab01") == 0
    assert output(capsys)["result"]["ready"] is False
    assert (
        invoke(data_root, "assignment", "bundle-list", "--ready-only") == 0
    )
    assert output(capsys)["result"]["count"] == 0
    assert invoke(data_root, "assignment", "bundle-ready", "basn_lab01") == 0
    assert output(capsys)["result"]["ready"] is True


def test_bundle_assignment_publish_failure_keeps_release_hidden(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    data_root = tmp_path / "private"
    starter = tmp_path / "starter"
    assessment = tmp_path / "assessment"
    starter.mkdir()
    assessment.mkdir()
    (starter / "answer.txt").write_text("TODO\n", encoding="utf-8")
    (assessment / "grade.py").write_text("print(1)\n", encoding="utf-8")

    def missing(_self, _runner_image):
        raise platform_cli.RunnerImageAvailabilityError(
            "runner_image_missing", "runner image is unavailable"
        )

    monkeypatch.setattr(
        platform_cli.RunnerImageAvailabilityChecker,
        "check",
        missing,
    )
    assert (
        invoke(
            data_root,
            "assignment",
            "bundle-add",
            "lab01",
            "--assignment-id",
            "basn_lab01",
            "--release-id",
            "v1",
            "--title",
            "Lab 01",
            "--starter",
            str(starter),
            "--assessment",
            str(assessment),
            "--runner-image",
            RUNNER,
            "--grading-runtime",
            "docker",
            "--max-score",
            "10",
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == (
        "runner_image_missing"
    )
    assert PlatformStateStore(data_root / "state.sqlite3").get_bundle_assignment(
        "basn_lab01"
    ).ready is False
