from __future__ import annotations

import json
from typing import Sequence

import pytest

from autograde.platform_grader import PILOT_LOCAL_RUNNER, ProcessResult
from autograde.platform_runner_image import (
    RunnerImageAvailabilityChecker,
    RunnerImageAvailabilityError,
    normalize_runner_reference,
    validate_inspect_timeout,
)


DIGEST = "sha256:" + "ab" * 32
IMAGE = f"docker.io/library/busybox@{DIGEST}"


class FakeExecutor:
    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], float, int, int]] = []

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
            raise AssertionError("unexpected image inspection")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


def metadata(*, repo_digests: object = None, os_name: object = "linux") -> bytes:
    if repo_digests is None:
        # Docker may canonicalize docker.io/library/busybox to busybox.  The
        # exact input reference is still used for inspect; RepoDigests confirms
        # the exact digest rather than a potentially different config Id.
        repo_digests = [f"busybox@{DIGEST}"]
    return (
        json.dumps(
            {
                "Os": os_name,
                "RepoDigests": repo_digests,
            }
        )
        + "\n"
    ).encode("utf-8")


def test_exact_digest_is_inspected_with_fixed_bounded_argument_vector() -> None:
    executor = FakeExecutor([ProcessResult(0, stdout=metadata())])
    checker = RunnerImageAvailabilityChecker(
        runtime="podman", timeout_seconds=7, executor=executor
    )

    observation = checker.check(IMAGE)

    assert observation.runtime == "podman"
    assert observation.runner_image == IMAGE
    assert observation.code == "ok"
    assert executor.calls == [
        (
            (
                "podman",
                "image",
                "inspect",
                "--format",
                '{"RepoDigests":{{json .RepoDigests}},"Os":{{json .Os}}}',
                IMAGE,
            ),
            7,
            RunnerImageAvailabilityChecker.MAX_STDOUT_BYTES,
            RunnerImageAvailabilityChecker.MAX_STDERR_BYTES,
        )
    ]


def test_missing_image_uses_stable_code_without_runtime_output() -> None:
    secret = "registry-password=do-not-leak"
    checker = RunnerImageAvailabilityChecker(
        executor=FakeExecutor(
            [ProcessResult(1, stdout=b"\n", stderr=secret.encode("utf-8"))]
        )
    )

    with pytest.raises(RunnerImageAvailabilityError) as caught:
        checker.check(IMAGE)

    assert caught.value.code == "runner_image_missing"
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("result", "code"),
    (
        (ProcessResult(-9, timed_out=True), "runner_image_inspect_timeout"),
        (
            ProcessResult(0, stdout=metadata(), stdout_truncated=True),
            "runner_image_inspect_output_limit",
        ),
        (
            ProcessResult(0, stdout=metadata(), stderr_truncated=True),
            "runner_image_inspect_output_limit",
        ),
    ),
)
def test_timeout_and_output_limits_have_stable_codes(
    result: ProcessResult, code: str
) -> None:
    checker = RunnerImageAvailabilityChecker(executor=FakeExecutor([result]))

    with pytest.raises(RunnerImageAvailabilityError) as caught:
        checker.check(IMAGE)

    assert caught.value.code == code


@pytest.mark.parametrize(
    "payload",
    (
        b"not-json",
        b"[]",
        b'{"Os":"linux","RepoDigests":[],"RepoDigests":[]}',
        b'{"Os":"linux"}',
        b'{"Os":"linux","RepoDigests":"busybox@sha256:bad"}',
        b'{"Os":null,"RepoDigests":[]}',
        b'{"Os":"linux","RepoDigests":[],"unexpected":true}',
        metadata(repo_digests=[f"evil@tag@{DIGEST}"]),
        metadata(repo_digests=[f"busybox:latest@{DIGEST}"]),
        metadata(repo_digests=["busybox@sha256:" + "AB" * 32]),
    ),
)
def test_malformed_inspect_json_is_rejected(payload: bytes) -> None:
    checker = RunnerImageAvailabilityChecker(
        executor=FakeExecutor([ProcessResult(0, stdout=payload)])
    )

    with pytest.raises(RunnerImageAvailabilityError) as caught:
        checker.check(IMAGE)

    assert caught.value.code == "runner_image_inspect_malformed"


def test_repository_digest_mismatch_and_non_linux_image_fail_closed() -> None:
    checker = RunnerImageAvailabilityChecker(
        executor=FakeExecutor(
            [
                ProcessResult(
                    0,
                    stdout=metadata(
                        repo_digests=["busybox@sha256:" + "ef" * 32]
                    ),
                ),
                ProcessResult(0, stdout=metadata(os_name="windows")),
            ]
        )
    )

    with pytest.raises(RunnerImageAvailabilityError) as mismatch:
        checker.check(IMAGE)
    assert mismatch.value.code == "runner_image_digest_mismatch"

    with pytest.raises(RunnerImageAvailabilityError) as platform:
        checker.check(IMAGE)
    assert platform.value.code == "runner_image_platform_mismatch"


@pytest.mark.parametrize(
    "runtime_error",
    (
        FileNotFoundError("/secret/runtime/path"),
        RuntimeError("registry-token=do-not-leak"),
    ),
)
def test_invalid_reference_and_runtime_failure_do_not_leak_details(
    runtime_error: Exception,
) -> None:
    executor = FakeExecutor([runtime_error])
    checker = RunnerImageAvailabilityChecker(executor=executor)

    with pytest.raises(RunnerImageAvailabilityError) as invalid:
        checker.check("busybox:latest")
    assert invalid.value.code == "runner_image_invalid_reference"
    assert executor.calls == []

    with pytest.raises(RunnerImageAvailabilityError) as unavailable:
        checker.check(IMAGE)
    assert unavailable.value.code == "runner_image_runtime_unavailable"
    assert "/secret/runtime/path" not in str(unavailable.value)
    assert "do-not-leak" not in str(unavailable.value)


def test_invalid_executor_protocol_has_stable_code() -> None:
    checker = RunnerImageAvailabilityChecker(
        executor=FakeExecutor([ProcessResult(True)])
    )

    with pytest.raises(RunnerImageAvailabilityError) as caught:
        checker.check(IMAGE)

    assert caught.value.code == "runner_image_inspect_failed"


def test_check_many_deduplicates_canonical_images() -> None:
    executor = FakeExecutor([ProcessResult(0, stdout=metadata())])
    checker = RunnerImageAvailabilityChecker(executor=executor)

    checked = checker.check_many((IMAGE, IMAGE, IMAGE))

    assert len(checked) == 1
    assert len(executor.calls) == 1


def test_pilot_reference_is_valid_for_state_but_not_container_inspection() -> None:
    assert normalize_runner_reference(PILOT_LOCAL_RUNNER) == PILOT_LOCAL_RUNNER
    checker = RunnerImageAvailabilityChecker(executor=FakeExecutor([]))

    with pytest.raises(RunnerImageAvailabilityError) as caught:
        checker.check(PILOT_LOCAL_RUNNER)

    assert caught.value.code == "runner_image_invalid_reference"


@pytest.mark.parametrize("value", (0, 0.09, 60.01, float("inf"), float("nan")))
def test_inspect_timeout_is_bounded(value: float) -> None:
    with pytest.raises(ValueError):
        validate_inspect_timeout(value)

    assert validate_inspect_timeout(0.1) == 0.1
    assert validate_inspect_timeout(60) == 60.0
