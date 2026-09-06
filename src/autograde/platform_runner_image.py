"""Fail-closed availability checks for immutable grading runner images.

The service never pulls an image on a student request.  Operators preload an
exact digest and this module verifies that the configured Docker or Podman
runtime can resolve that exact repository digest locally.  Runtime output is
strictly bounded and is never included in public/operator error messages.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from .platform_grader import (
    CommandExecutor,
    PILOT_LOCAL_RUNNER,
    ProcessResult,
    SubprocessExecutor,
)


_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = rf"{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*(?::[0-9]{{1,5}})?"
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_IMAGE_NAME = re.compile(
    rf"^(?:(?:{_DOMAIN})/)?{_COMPONENT}(?:/{_COMPONENT})*$"
)
_RUNTIMES = frozenset({"docker", "podman"})
_INSPECT_FORMAT = '{"RepoDigests":{{json .RepoDigests}},"Os":{{json .Os}}}'


class RunnerImageAvailabilityError(RuntimeError):
    """A runner image failed a local, non-pulling readiness check."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RunnerImageAvailability:
    """A non-sensitive successful availability observation."""

    runtime: str
    runner_image: str
    code: str = "ok"


class RunnerImageAvailabilityChecker:
    """Inspect exact local runner digests without a shell or implicit pull."""

    DEFAULT_TIMEOUT_SECONDS = 15.0
    MIN_TIMEOUT_SECONDS = 0.1
    MAX_TIMEOUT_SECONDS = 60.0
    MAX_STDOUT_BYTES = 64 * 1024
    MAX_STDERR_BYTES = 16 * 1024

    def __init__(
        self,
        *,
        runtime: str = "docker",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        executor: CommandExecutor | None = None,
    ) -> None:
        if runtime not in _RUNTIMES:
            raise ValueError("container runtime must be docker or podman")
        self.runtime = runtime
        self.timeout_seconds = validate_inspect_timeout(timeout_seconds)
        self.executor = executor or SubprocessExecutor()

    def check(self, runner_image: str) -> RunnerImageAvailability:
        """Require one exact digest in the runtime's local ``RepoDigests``."""

        image = normalize_runner_image(runner_image)
        argv = (
            self.runtime,
            "image",
            "inspect",
            "--format",
            _INSPECT_FORMAT,
            image,
        )
        try:
            result = self.executor.run(
                argv,
                timeout_seconds=self.timeout_seconds,
                max_stdout_bytes=self.MAX_STDOUT_BYTES,
                max_stderr_bytes=self.MAX_STDERR_BYTES,
            )
        except Exception as exc:
            raise RunnerImageAvailabilityError(
                "runner_image_runtime_unavailable",
                "container runtime is unavailable for runner image inspection",
            ) from exc

        if (
            not isinstance(result, ProcessResult)
            or isinstance(result.returncode, bool)
            or not isinstance(result.returncode, int)
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
            or not isinstance(result.stdout_truncated, bool)
            or not isinstance(result.stderr_truncated, bool)
            or not isinstance(result.timed_out, bool)
        ):
            raise RunnerImageAvailabilityError(
                "runner_image_inspect_failed",
                "container runtime returned an invalid image inspection result",
            )
        if result.timed_out:
            raise RunnerImageAvailabilityError(
                "runner_image_inspect_timeout",
                "runner image inspection timed out",
            )
        if result.stdout_truncated or result.stderr_truncated:
            raise RunnerImageAvailabilityError(
                "runner_image_inspect_output_limit",
                "runner image inspection exceeded its output limit",
            )
        if result.returncode != 0:
            # Docker may still emit a newline on stdout for a missing image.
            # Return status is authoritative and raw stderr is never exposed.
            raise RunnerImageAvailabilityError(
                "runner_image_missing",
                "runner image is not available in the configured container runtime",
            )

        observation = _strict_json_object(result.stdout)
        operating_system = observation.get("Os")
        if not isinstance(operating_system, str):
            raise RunnerImageAvailabilityError(
                "runner_image_inspect_malformed",
                "runner image inspection returned malformed metadata",
            )
        if operating_system != "linux":
            raise RunnerImageAvailabilityError(
                "runner_image_platform_mismatch",
                "runner image is not a Linux image",
            )

        repo_digests = observation.get("RepoDigests")
        if not isinstance(repo_digests, list):
            raise RunnerImageAvailabilityError(
                "runner_image_inspect_malformed",
                "runner image inspection returned malformed metadata",
            )
        requested_digest = image.rsplit("@", 1)[1]
        try:
            observed_digests = tuple(
                _repo_digest(item) for item in repo_digests
            )
        except (TypeError, ValueError) as exc:
            raise RunnerImageAvailabilityError(
                "runner_image_inspect_malformed",
                "runner image inspection returned malformed metadata",
            ) from exc
        if requested_digest not in observed_digests:
            raise RunnerImageAvailabilityError(
                "runner_image_digest_mismatch",
                "local runner image does not expose the configured repository digest",
            )
        return RunnerImageAvailability(runtime=self.runtime, runner_image=image)

    def check_many(
        self, runner_images: Iterable[str]
    ) -> tuple[RunnerImageAvailability, ...]:
        """Check each unique canonical reference once, preserving input order."""

        unique: dict[str, None] = {}
        for runner_image in runner_images:
            unique.setdefault(normalize_runner_image(runner_image), None)
        return tuple(self.check(runner_image) for runner_image in unique)


def validate_inspect_timeout(value: float) -> float:
    """Return a finite bounded timeout accepted by operator entry points."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("runner image inspect timeout must be numeric")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("runner image inspect timeout must be finite") from exc
    if not math.isfinite(normalized):
        raise ValueError("runner image inspect timeout must be finite")
    if not (
        RunnerImageAvailabilityChecker.MIN_TIMEOUT_SECONDS
        <= normalized
        <= RunnerImageAvailabilityChecker.MAX_TIMEOUT_SECONDS
    ):
        raise ValueError("runner image inspect timeout must be between 0.1 and 60 seconds")
    return normalized


def normalize_runner_image(value: str) -> str:
    """Validate and canonicalize an exact ``name@sha256:<64hex>`` reference."""

    if not isinstance(value, str) or len(value) > 512 or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        raise RunnerImageAvailabilityError(
            "runner_image_invalid_reference",
            "runner image must be an immutable digest reference",
        )
    marker = "@sha256:"
    if value.count(marker) != 1:
        raise RunnerImageAvailabilityError(
            "runner_image_invalid_reference",
            "runner image must be an immutable digest reference",
        )
    name, digest = value.rsplit(marker, 1)
    if (
        "/" not in name
        or "@" in name
        or _IMAGE_NAME.fullmatch(name) is None
        or len(digest) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in digest)
    ):
        raise RunnerImageAvailabilityError(
            "runner_image_invalid_reference",
            "runner image must be an immutable digest reference",
        )
    return f"{name}{marker}{digest.lower()}"


def normalize_runner_reference(value: str) -> str:
    """Accept either an immutable OCI image or the explicit pilot sentinel."""

    if value == PILOT_LOCAL_RUNNER:
        return value
    return normalize_runner_image(value)


def _repo_digest(value: object) -> str:
    """Return a strictly structured lowercase digest from ``RepoDigests``."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character.isspace() or ord(character) < 32 for character in value)
        or value.count("@") != 1
        or value.count("@sha256:") != 1
    ):
        raise ValueError("invalid repository digest")
    name, digest = value.rsplit("@sha256:", 1)
    if (
        "@" in name
        or _IMAGE_NAME.fullmatch(name) is None
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("invalid repository digest")
    return f"sha256:{digest}"


def _strict_json_object(value: bytes) -> Mapping[str, Any]:
    def unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON constant")

    try:
        decoded = value.decode("utf-8", "strict")
        parsed = json.loads(
            decoded,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise RunnerImageAvailabilityError(
            "runner_image_inspect_malformed",
            "runner image inspection returned malformed metadata",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise RunnerImageAvailabilityError(
            "runner_image_inspect_malformed",
            "runner image inspection returned malformed metadata",
        )
    if set(parsed) != {"RepoDigests", "Os"}:
        raise RunnerImageAvailabilityError(
            "runner_image_inspect_malformed",
            "runner image inspection returned malformed metadata",
        )
    return parsed
