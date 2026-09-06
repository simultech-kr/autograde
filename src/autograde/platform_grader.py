"""Run one prepared submission with an explicit grading backend.

Future production deployment can use :class:`ContainerGrader`, which invokes a
validated Docker or Podman executable, mounts the three prepared input trees
read-only, and accepts a small JSON result from the instructor-owned runner
image. Local pilots may
explicitly use :class:`PilotLocalGrader`; it applies best-effort POSIX process
limits but is not a security sandbox and must only execute trusted code on a
disposable host.

Raw stdout and stderr are deliberately not part of the public result.  They are
bounded while the process is running and discarded after validation so tokens,
hidden test details, host paths, and arbitrary runner fields cannot flow into
the student result API by accident.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple, Union

from .workspace import PreparedWorkspace


class GradingError(RuntimeError):
    """Base error with a stable, non-sensitive classification code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InfrastructureGradingError(GradingError):
    """The container service or immutable grading configuration failed."""


class AssessmentGradingError(GradingError):
    """The isolated assessment did not produce a valid grade result."""


@dataclass(frozen=True)
class ProcessResult:
    """Bounded output returned by a container-runtime command."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False


class CommandExecutor(Protocol):
    """Injection boundary used by tests and alternate runtime launchers."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        """Execute an argument vector without a shell and return bounded output."""


class Grader(Protocol):
    """Small service-facing protocol; a deterministic fake can implement it."""

    def grade(
        self,
        *,
        workspace: PreparedWorkspace,
        runner_image: str,
        max_score: float,
    ) -> "PublicGradeResult":
        """Grade one immutable prepared submission."""


@dataclass(frozen=True)
class ContainerLimits:
    """Container and runtime limits with conservative MVP defaults."""

    timeout_seconds: float = 30.0
    runtime_operation_timeout_seconds: float = 15.0
    memory_bytes: int = 512 * 1024 * 1024
    cpu_count: float = 1.0
    pids_limit: int = 128
    tmpfs_bytes: int = 64 * 1024 * 1024
    max_stdout_bytes: int = 128 * 1024
    max_stderr_bytes: int = 32 * 1024

    def __post_init__(self) -> None:
        numeric_positive = (
            ("timeout_seconds", self.timeout_seconds),
            ("runtime_operation_timeout_seconds", self.runtime_operation_timeout_seconds),
            ("memory_bytes", self.memory_bytes),
            ("cpu_count", self.cpu_count),
            ("pids_limit", self.pids_limit),
            ("tmpfs_bytes", self.tmpfs_bytes),
            ("max_stdout_bytes", self.max_stdout_bytes),
            ("max_stderr_bytes", self.max_stderr_bytes),
        )
        for field, value in numeric_positive:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{field} must be numeric")
            try:
                finite = math.isfinite(float(value))
            except OverflowError:
                finite = False
            if not finite or value <= 0:
                raise ValueError(f"{field} must be finite and positive")
        for field in (
            "memory_bytes",
            "pids_limit",
            "tmpfs_bytes",
            "max_stdout_bytes",
            "max_stderr_bytes",
        ):
            if not isinstance(getattr(self, field), int):
                raise TypeError(f"{field} must be an integer")


PILOT_LOCAL_RUNNER = "pilot-local:v1"


@dataclass(frozen=True)
class PilotLocalLimits:
    """Best-effort host process limits for non-production pilot grading.

    These per-process POSIX limits reduce accidental damage; they do not
    provide filesystem, network, user, or process-tree isolation.
    """

    timeout_seconds: float = 30.0
    memory_bytes: int = 512 * 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    max_open_files: int = 256
    max_stdout_bytes: int = 128 * 1024
    max_stderr_bytes: int = 32 * 1024

    def __post_init__(self) -> None:
        fields = (
            ("timeout_seconds", self.timeout_seconds),
            ("memory_bytes", self.memory_bytes),
            ("max_file_bytes", self.max_file_bytes),
            ("max_open_files", self.max_open_files),
            ("max_stdout_bytes", self.max_stdout_bytes),
            ("max_stderr_bytes", self.max_stderr_bytes),
        )
        for field, value in fields:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{field} must be numeric")
            try:
                finite = math.isfinite(float(value))
            except OverflowError:
                finite = False
            if not finite or value <= 0:
                raise ValueError(f"{field} must be finite and positive")
        for field in (
            "memory_bytes",
            "max_file_bytes",
            "max_open_files",
            "max_stdout_bytes",
            "max_stderr_bytes",
        ):
            if not isinstance(getattr(self, field), int):
                raise TypeError(f"{field} must be an integer")


@dataclass(frozen=True)
class GradeRequest:
    """Pinned grading inputs passed to a :class:`Grader`."""

    runner_image: str
    max_score: float
    workspace_path: Union[str, Path]
    submission_path: Union[str, Path]
    assessment_path: Optional[Union[str, Path]] = None
    data_path: Optional[Union[str, Path]] = None

    @classmethod
    def from_workspace(
        cls,
        workspace: PreparedWorkspace,
        *,
        runner_image: str,
        max_score: float,
    ) -> "GradeRequest":
        """Create a request from the immutable output of ``WorkspaceBuilder``."""

        return cls(
            runner_image=runner_image,
            max_score=max_score,
            workspace_path=workspace.path,
            submission_path=workspace.submission_path,
            assessment_path=workspace.assessment_path,
            data_path=workspace.data_path,
        )


@dataclass(frozen=True)
class PublicGradeResult:
    """The only grader fields safe for persistence and student publication."""

    score: float
    max_score: float
    rubric: Mapping[str, Mapping[str, Any]]
    diagnostics: Tuple[Mapping[str, Any], ...]

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable copy with the public top-level allowlist."""

        return {
            "score": self.score,
            "max_score": self.max_score,
            "rubric": {key: dict(value) for key, value in self.rubric.items()},
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


class SubprocessExecutor:
    """Run a trusted container-runtime CLI with memory-bounded pipe readers."""

    _ENV_ALLOWLIST = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "CONTAINER_HOST",
        "XDG_RUNTIME_DIR",
    )

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        if not argv or any(
            not isinstance(item, str) or not item or "\x00" in item for item in argv
        ):
            raise ValueError("argv must contain non-empty, NUL-free string arguments")

        environment = {
            key: os.environ[key]
            for key in self._ENV_ALLOWLIST
            if key in os.environ
        }
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
            env=environment,
        )
        if process.stdout is None or process.stderr is None:  # pragma: no cover
            process.kill()
            raise RuntimeError("container runtime pipes were not created")

        stdout = bytearray()
        stderr = bytearray()
        stdout_truncated = [False]
        stderr_truncated = [False]

        def drain(stream: Any, target: bytearray, limit: int, truncated: list[bool]) -> None:
            while True:
                try:
                    chunk = stream.read(64 * 1024)
                except (OSError, ValueError):
                    truncated[0] = True
                    return
                if not chunk:
                    return
                remaining = limit - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    truncated[0] = True

        readers = (
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout, max_stdout_bytes, stdout_truncated),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr, max_stderr_bytes, stderr_truncated),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_process_group(process)
            try:
                returncode = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=2)
        except BaseException:
            # KeyboardInterrupt/SystemExit must not abandon a runtime client
            # that can leave its already-created container running.  Reap the
            # whole process group before preserving the original interruption.
            self._kill_process_group(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            raise
        finally:
            for reader, truncated in (
                (readers[0], stdout_truncated),
                (readers[1], stderr_truncated),
            ):
                reader.join(timeout=1)
                if reader.is_alive():
                    truncated[0] = True

        return ProcessResult(
            returncode=returncode,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            stdout_truncated=stdout_truncated[0],
            stderr_truncated=stderr_truncated[0],
            timed_out=timed_out,
        )

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - the platform target is WSL/Linux.
                process.kill()
        except ProcessLookupError:
            return


_IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._/:+-]*[a-z0-9]$")
_CONTAINER_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_INSTANCE_LABEL_VALUE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_NUMERIC_USER = re.compile(r"^(?P<uid>[0-9]+)(?::(?P<gid>[0-9]+))?$")
_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|private[_-]?key|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization)",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(password|passwd|secret|private[_-]?key|api[_-]?key|"
        r"access[_-]?token|refresh[_-]?token|token)\b\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)

_MAX_RUBRIC_ITEMS = 100
_MAX_DIAGNOSTICS = 100
_MAX_PUBLIC_JSON_BYTES = 64 * 1024
_MAX_ID_CHARS = 128
_MAX_TITLE_CHARS = 256
_MAX_FEEDBACK_CHARS = 2_048
_MAX_MESSAGE_CHARS = 2_048
_MAX_PATH_CHARS = 512


def _finite_score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssessmentGradingError("invalid_grade_result", f"{field} must be numeric")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise AssessmentGradingError(
            "invalid_grade_result", f"{field} must be finite and non-negative"
        ) from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise AssessmentGradingError(
            "invalid_grade_result", f"{field} must be finite and non-negative"
        )
    return 0.0 if normalized == 0 else normalized


def _sanitize_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character
        for character in normalized
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
    ).strip()
    if not normalized:
        return None
    if "-----BEGIN " in normalized.upper() and "PRIVATE KEY-----" in normalized.upper():
        normalized = "[REDACTED]"
    else:
        for pattern in _SECRET_PATTERNS:
            normalized = pattern.sub(
                lambda match: (
                    f"{match.group(1)}=[REDACTED]"
                    if match.lastindex
                    else "[REDACTED]"
                ),
                normalized,
            )
    if len(normalized) > limit:
        normalized = normalized[: max(limit - 1, 0)] + "…"
    return normalized


def _criterion_key(value: Any) -> Optional[str]:
    normalized = _sanitize_text(value, _MAX_ID_CHARS)
    if normalized is None or _SECRET_KEY.search(normalized):
        return None
    if any(character in "\r\n\t/\\" for character in normalized):
        return None
    return normalized


def _sanitize_rubric(
    value: Any,
    *,
    assignment_max_score: float,
) -> Dict[str, Dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AssessmentGradingError(
            "invalid_grade_result", "rubric must be a JSON object"
        )

    public: Dict[str, Dict[str, Any]] = {}
    for raw_key, raw_item in value.items():
        if len(public) >= _MAX_RUBRIC_ITEMS:
            break
        key = _criterion_key(raw_key)
        if key is None or key in public or not isinstance(raw_item, dict):
            continue

        item: Dict[str, Any] = {}
        has_score = "score" in raw_item
        has_max_score = "max_score" in raw_item
        if has_score != has_max_score:
            raise AssessmentGradingError(
                "invalid_grade_result",
                "rubric score and max_score must be provided together",
            )
        if has_score:
            score = _finite_score(raw_item["score"], f"rubric[{key!r}].score")
            maximum = _finite_score(
                raw_item["max_score"], f"rubric[{key!r}].max_score"
            )
            if maximum <= 0 or score > maximum or maximum > assignment_max_score:
                raise AssessmentGradingError(
                    "invalid_grade_result", "rubric score is outside its allowed range"
                )
            item["score"] = score
            item["max_score"] = maximum

        title = _sanitize_text(raw_item.get("title"), _MAX_TITLE_CHARS)
        feedback = _sanitize_text(raw_item.get("feedback"), _MAX_FEEDBACK_CHARS)
        if title is not None:
            item["title"] = title
        if feedback is not None:
            item["feedback"] = feedback
        if item:
            public[key] = item
    return public


def _safe_relative_path(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_CHARS:
        return None
    if "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        return None
    if any(part in ("", ".") for part in path.parts):
        return None
    return path.as_posix()


def _bounded_position(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 10_000_000 else None


def _sanitize_diagnostics(value: Any) -> list[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AssessmentGradingError(
            "invalid_grade_result", "diagnostics must be a JSON array"
        )

    public: list[Dict[str, Any]] = []
    for raw_item in value:
        if len(public) >= _MAX_DIAGNOSTICS:
            break
        if not isinstance(raw_item, dict):
            continue
        message = _sanitize_text(raw_item.get("message"), _MAX_MESSAGE_CHARS)
        if message is None:
            continue

        item: Dict[str, Any] = {"message": message}
        code = _sanitize_text(raw_item.get("code"), _MAX_ID_CHARS)
        if code is not None and not _SECRET_KEY.search(code):
            item["code"] = code
        severity = raw_item.get("severity")
        if severity in {"error", "warning", "info"}:
            item["severity"] = severity
        path = _safe_relative_path(raw_item.get("path"))
        if path is not None:
            item["path"] = path
        for field in ("line", "column"):
            position = _bounded_position(raw_item.get(field))
            if position is not None:
                item[field] = position
        public.append(item)
    return public


def sanitize_grade_result(
    value: Mapping[str, Any],
    *,
    assignment_max_score: float,
) -> PublicGradeResult:
    """Project an untrusted runner object onto the bounded public result schema."""

    try:
        maximum = _finite_score(assignment_max_score, "assignment_max_score")
    except AssessmentGradingError as exc:
        raise InfrastructureGradingError(
            "invalid_grading_configuration", "assignment max_score must be finite"
        ) from exc
    if maximum <= 0:
        raise InfrastructureGradingError(
            "invalid_grading_configuration", "assignment max_score must be positive"
        )
    if not isinstance(value, dict):
        raise AssessmentGradingError(
            "invalid_grade_result", "grader result must be a JSON object"
        )
    if "score" not in value or "max_score" not in value:
        raise AssessmentGradingError(
            "invalid_grade_result", "grader result must include score and max_score"
        )

    score = _finite_score(value["score"], "score")
    reported_maximum = _finite_score(value["max_score"], "max_score")
    if reported_maximum != maximum:
        raise AssessmentGradingError(
            "invalid_grade_result", "grader max_score does not match the assignment"
        )
    if score > maximum:
        raise AssessmentGradingError(
            "invalid_grade_result", "score must not exceed assignment max_score"
        )

    rubric = _sanitize_rubric(value.get("rubric"), assignment_max_score=maximum)
    diagnostics = _sanitize_diagnostics(value.get("diagnostics"))

    payload: Dict[str, Any] = {
        "score": score,
        "max_score": maximum,
        "rubric": rubric,
        "diagnostics": diagnostics,
    }
    while len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ) > _MAX_PUBLIC_JSON_BYTES:
        if diagnostics:
            diagnostics.pop()
        elif rubric:
            rubric.pop(next(reversed(rubric)))
        else:  # Scores alone are far below the bound; retain a defensive guard.
            raise AssessmentGradingError(
                "invalid_grade_result", "public grade result exceeds its size limit"
            )

    return PublicGradeResult(
        score=score,
        max_score=maximum,
        rubric=rubric,
        diagnostics=tuple(diagnostics),
    )


class _DuplicateJsonKey(ValueError):
    pass


def _parse_result(raw: bytes) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")

        def unique_object(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
            result: Dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise _DuplicateJsonKey(key)
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON number: {value}")

        parsed = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise AssessmentGradingError(
            "invalid_grade_result", "grader stdout must contain one valid JSON object"
        ) from exc
    if not isinstance(parsed, dict):
        raise AssessmentGradingError(
            "invalid_grade_result", "grader stdout must contain one JSON object"
        )
    return parsed


class PilotLocalGrader:
    """Run ``assessment/grade.py`` directly on a POSIX pilot host.

    This backend is intentionally explicit and intentionally *not* a sandbox.
    It is suitable only for trusted pilot submissions on a disposable host.
    The assessment and any student process it starts inherit the service
    account's host filesystem and network access.
    """

    ENTRYPOINT = "grade.py"
    SECURITY_CLASSIFICATION = "trusted_code_only"
    _READY_MARKER = b"autograde-pilot-ready-v1\n"
    _PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    def __init__(
        self,
        *,
        limits: Optional[PilotLocalLimits] = None,
        python_executable: Optional[Union[str, Path]] = None,
        launcher_path: Optional[Union[str, Path]] = None,
    ) -> None:
        if os.name != "posix":
            raise ValueError(
                "pilot-local grading requires Linux, macOS, or Linux inside WSL2"
            )
        self.limits = limits or PilotLocalLimits()
        self.python_executable = self._trusted_file(
            python_executable or sys.executable,
            "Python executable",
            preserve_invocation_path=True,
        )
        self.launcher_path = self._trusted_file(
            launcher_path or Path(__file__).with_name("platform_pilot_exec.py"),
            "pilot launcher",
        )
        self._lifecycle_lock = threading.RLock()
        self._active_processes: dict[int, subprocess.Popen[bytes]] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lifecycle_lock:
            return self._closed

    @property
    def active_process_ids(self) -> Tuple[int, ...]:
        with self._lifecycle_lock:
            return tuple(sorted(self._active_processes))

    def close(self) -> None:
        """Reject new grades and terminate active assessment process groups."""

        with self._lifecycle_lock:
            self._closed = True
            active = tuple(self._active_processes.values())
        for process in active:
            self._kill_process_group(process)

    def __enter__(self) -> "PilotLocalGrader":
        with self._lifecycle_lock:
            if self._closed:
                raise InfrastructureGradingError(
                    "grader_closed", "pilot-local grader has already been closed"
                )
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def grade(
        self,
        *,
        workspace: PreparedWorkspace,
        runner_image: str,
        max_score: float,
    ) -> PublicGradeResult:
        """Execute the fixed assessment entrypoint and sanitize its JSON result."""

        try:
            maximum = _finite_score(max_score, "assignment max_score")
        except AssessmentGradingError as exc:
            raise InfrastructureGradingError(
                "invalid_grading_configuration", "assignment max_score must be finite"
            ) from exc
        if maximum <= 0:
            raise InfrastructureGradingError(
                "invalid_grading_configuration", "assignment max_score must be positive"
            )
        if runner_image != PILOT_LOCAL_RUNNER:
            raise InfrastructureGradingError(
                "invalid_pilot_runner",
                f"pilot-local assignments must use {PILOT_LOCAL_RUNNER}",
            )

        root, submission, assessment, data = self._workspace_paths(workspace)
        entrypoint = assessment / self.ENTRYPOINT
        try:
            entry_info = entrypoint.lstat()
        except OSError as exc:
            raise AssessmentGradingError(
                "assessment_entrypoint_missing",
                "pilot assessment must contain assessment/grade.py",
            ) from exc
        if entrypoint.is_symlink() or not stat.S_ISREG(entry_info.st_mode):
            raise AssessmentGradingError(
                "assessment_entrypoint_invalid",
                "pilot assessment entrypoint must be one regular file",
            )

        runtime_dir = Path(tempfile.mkdtemp(prefix="autograde-pilot-"))
        runtime_dir.chmod(0o700)
        try:
            resolved_entrypoint = entrypoint.resolve(strict=True)
            environment = self._environment(
                submission=submission,
                assessment=assessment,
                data=data,
                runtime_dir=runtime_dir.resolve(strict=True),
                maximum=maximum,
            )
            cpu_seconds = max(1, math.ceil(self.limits.timeout_seconds))
            arguments = (
                str(self.python_executable),
                "-I",
                "-B",
                str(self.launcher_path),
                str(resolved_entrypoint),
                str(self.limits.memory_bytes),
                str(cpu_seconds),
                str(self.limits.max_file_bytes),
                str(self.limits.max_open_files),
            )
            execution = self._run(
                arguments,
                cwd=runtime_dir,
                environment=environment,
            )
            if execution.timed_out:
                raise AssessmentGradingError(
                    "assessment_timeout", "pilot assessment exceeded its time limit"
                )
            if execution.stdout_truncated or execution.stderr_truncated:
                raise AssessmentGradingError(
                    "assessment_output_limit", "pilot assessment exceeded its output limit"
                )
            if self._READY_MARKER not in execution.stderr:
                raise InfrastructureGradingError(
                    "pilot_limit_configuration_failed",
                    "pilot-local process limits could not be installed",
                )
            if execution.returncode != 0:
                raise AssessmentGradingError(
                    "assessment_process_failed",
                    "pilot assessment exited unsuccessfully",
                )
            parsed = _parse_result(execution.stdout)
            redacted = self._redact_paths(
                parsed,
                (
                    (str(runtime_dir.resolve(strict=True)), "<work>"),
                    (str(assessment), "<assessment>"),
                    (str(submission), "<submission>"),
                    (str(data), "<data>") if data is not None else ("", ""),
                    (str(root), "<workspace>"),
                ),
            )
            return sanitize_grade_result(redacted, assignment_max_score=maximum)
        finally:
            self._remove_runtime_dir(runtime_dir)

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> ProcessResult:
        """Run and reap one process group with bounded in-memory output."""

        process: Optional[subprocess.Popen[bytes]] = None
        with self._lifecycle_lock:
            if self._closed:
                raise InfrastructureGradingError(
                    "grader_closed", "pilot-local grader has already been closed"
                )
            try:
                process = subprocess.Popen(
                    list(argv),
                    cwd=str(cwd),
                    env=dict(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    close_fds=True,
                    start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                raise InfrastructureGradingError(
                    "pilot_runtime_unavailable",
                    "pilot-local Python process could not be started",
                ) from exc
            self._active_processes[process.pid] = process

        if process.stdout is None or process.stderr is None:  # pragma: no cover
            self._kill_process_group(process)
            process.wait()
            with self._lifecycle_lock:
                self._active_processes.pop(process.pid, None)
            raise InfrastructureGradingError(
                "pilot_runtime_protocol_error", "pilot-local pipes were not created"
            )

        stdout = bytearray()
        stderr = bytearray()
        stdout_truncated = [False]
        stderr_truncated = [False]

        def drain(
            stream: Any,
            target: bytearray,
            limit: int,
            truncated: list[bool],
        ) -> None:
            while True:
                try:
                    chunk = stream.read(64 * 1024)
                except (OSError, ValueError):
                    truncated[0] = True
                    self._kill_process_group(process)
                    return
                if not chunk:
                    return
                remaining = limit - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    truncated[0] = True
                    self._kill_process_group(process)
                    return

        readers = (
            threading.Thread(
                target=drain,
                args=(
                    process.stdout,
                    stdout,
                    self.limits.max_stdout_bytes,
                    stdout_truncated,
                ),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(
                    process.stderr,
                    stderr,
                    self.limits.max_stderr_bytes,
                    stderr_truncated,
                ),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            try:
                returncode = process.wait(timeout=self.limits.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_process_group(process)
                try:
                    returncode = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    returncode = process.wait(timeout=2)
            except BaseException:
                self._kill_process_group(process)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                raise
        finally:
            # The assessment root may exit successfully after leaving children
            # behind. Kill the original process group even after ``wait`` has
            # reaped that root so inherited-pipe and DEVNULL children cannot
            # outlive a completed grade. A child can still escape with setsid;
            # this is one reason pilot-local is explicitly not a sandbox.
            self._kill_process_group(process)
            for reader, truncated in (
                (readers[0], stdout_truncated),
                (readers[1], stderr_truncated),
            ):
                reader.join(timeout=1)
                if reader.is_alive():
                    truncated[0] = True
            with self._lifecycle_lock:
                self._active_processes.pop(process.pid, None)

        return ProcessResult(
            returncode=returncode,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            stdout_truncated=stdout_truncated[0],
            stderr_truncated=stderr_truncated[0],
            timed_out=timed_out,
        )

    def _environment(
        self,
        *,
        submission: Path,
        assessment: Path,
        data: Optional[Path],
        runtime_dir: Path,
        maximum: float,
    ) -> Dict[str, str]:
        environment = {
            "PATH": self._PATH,
            "HOME": str(runtime_dir),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(runtime_dir),
            "TMP": str(runtime_dir),
            "TEMP": str(runtime_dir),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "AUTOGRADE_SUBMISSION_DIR": str(submission),
            "AUTOGRADE_ASSESSMENT_DIR": str(assessment),
            "AUTOGRADE_DATA_DIR": str(data) if data is not None else "",
            "AUTOGRADE_WORK_DIR": str(runtime_dir),
            "AUTOGRADE_MAX_SCORE": format(maximum, ".17g"),
            "AUTOGRADE_PYTHON_EXECUTABLE": str(self.python_executable),
        }
        return environment

    @staticmethod
    def _workspace_paths(
        workspace: PreparedWorkspace,
    ) -> Tuple[Path, Path, Path, Optional[Path]]:
        root = PilotLocalGrader._dedicated_directory(workspace.path, "workspace")
        submission = PilotLocalGrader._dedicated_directory(
            workspace.submission_path, "submission"
        )
        if workspace.assessment_path is None:
            raise AssessmentGradingError(
                "assessment_entrypoint_missing",
                "pilot assessment must contain assessment/grade.py",
            )
        assessment = PilotLocalGrader._dedicated_directory(
            workspace.assessment_path, "assessment"
        )
        data = (
            PilotLocalGrader._dedicated_directory(workspace.data_path, "data")
            if workspace.data_path is not None
            else None
        )
        expected = (
            (submission, root / "submission"),
            (assessment, root / "assessment"),
        )
        if data is not None:
            expected += ((data, root / "data"),)
        if any(actual != wanted for actual, wanted in expected):
            raise InfrastructureGradingError(
                "invalid_workspace",
                "grading inputs must be the prepared workspace directories",
            )
        return root, submission, assessment, data

    @staticmethod
    def _dedicated_directory(value: Union[str, Path], label: str) -> Path:
        try:
            lexical = Path(value)
            if lexical.is_symlink():
                raise InfrastructureGradingError(
                    "invalid_workspace", f"{label} path must not be a symlink"
                )
            resolved = lexical.resolve(strict=True)
        except InfrastructureGradingError:
            raise
        except (OSError, RuntimeError, TypeError) as exc:
            raise InfrastructureGradingError(
                "invalid_workspace", f"{label} path is unavailable"
            ) from exc
        if not resolved.is_dir() or resolved == Path(resolved.anchor):
            raise InfrastructureGradingError(
                "invalid_workspace", f"{label} path must be a dedicated directory"
            )
        return resolved

    @staticmethod
    def _trusted_file(
        value: Union[str, Path],
        label: str,
        *,
        preserve_invocation_path: bool = False,
    ) -> Path:
        try:
            lexical = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
            resolved = lexical.resolve(strict=True)
            info = resolved.stat()
        except (OSError, RuntimeError, TypeError) as exc:
            raise ValueError(f"{label} is unavailable") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} must be a regular file")
        # A virtual environment's Python command is commonly a symlink to the
        # base interpreter. Executing the resolved target loses ``pyvenv.cfg``
        # discovery and therefore the assessment's installed dependencies.
        return lexical if preserve_invocation_path else resolved

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    @staticmethod
    def _remove_runtime_dir(path: Path) -> None:
        def restore_permissions(_function: Any, value: str, _error: Any) -> None:
            try:
                os.chmod(value, 0o700)
            except OSError:
                return
            try:
                if os.path.isdir(value):
                    os.rmdir(value)
                else:
                    os.unlink(value)
            except OSError:
                return

        shutil.rmtree(path, onerror=restore_permissions, ignore_errors=False)

    @staticmethod
    def _redact_paths(
        value: Any,
        replacements: Sequence[Tuple[str, str]],
    ) -> Any:
        active = tuple(
            sorted(
                ((source, target) for source, target in replacements if source),
                key=lambda item: len(item[0]),
                reverse=True,
            )
        )
        if isinstance(value, str):
            for source, target in active:
                value = value.replace(source, target)
            return value
        if isinstance(value, list):
            return [PilotLocalGrader._redact_paths(item, active) for item in value]
        if isinstance(value, dict):
            return {
                PilotLocalGrader._redact_paths(key, active): PilotLocalGrader._redact_paths(
                    item, active
                )
                for key, item in value.items()
            }
        return value


class ContainerGrader:
    """OCI-runtime grader that never invokes submission files on the host."""

    _INFRASTRUCTURE_EXIT_CODES = frozenset({125, 126, 127})
    MANAGED_LABEL = "io.autograde.managed"
    INSTANCE_LABEL = "io.autograde.instance"

    def __init__(
        self,
        *,
        runtime: str = "docker",
        limits: Optional[ContainerLimits] = None,
        container_command: Sequence[str] = (),
        executor: Optional[CommandExecutor] = None,
        container_user: Optional[str] = None,
        name_factory: Optional[Callable[[], str]] = None,
        instance_label: Optional[str] = None,
    ) -> None:
        self.runtime = self._runtime(runtime)
        self.limits = limits or ContainerLimits()
        self.container_command = self._container_command(container_command)
        self.executor = executor or SubprocessExecutor()
        self.container_user = self._container_user(container_user)
        self.instance_label = self._instance_label(
            instance_label or f"ephemeral-{secrets.token_hex(16)}"
        )
        self._name_factory = name_factory or (
            lambda: f"autograde-{secrets.token_hex(12)}"
        )
        self._lifecycle_lock = threading.RLock()
        self._active_containers: set[str] = set()
        self._closed = False

    @property
    def active_container_names(self) -> Tuple[str, ...]:
        """Return a stable snapshot of locally active container names."""

        with self._lifecycle_lock:
            return tuple(sorted(self._active_containers))

    @property
    def closed(self) -> bool:
        with self._lifecycle_lock:
            return self._closed

    def reconcile_orphans(self) -> int:
        """Remove stale containers carrying this service instance's labels.

        Reconciliation is intentionally scoped by both a managed marker and the
        stable service-instance label.  It must run before local grading starts;
        holding the lifecycle lock prevents a new local container from being
        registered while the runtime is queried and cleaned.
        """

        with self._lifecycle_lock:
            if self._closed:
                raise InfrastructureGradingError(
                    "grader_closed", "container grader has already been closed"
                )
            if self._active_containers:
                raise InfrastructureGradingError(
                    "reconciliation_while_active",
                    "cannot reconcile containers while assessments are active",
                )
            listed = self._execute(
                (
                    self.runtime,
                    "ps",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    f"label={self.MANAGED_LABEL}=true",
                    "--filter",
                    f"label={self.INSTANCE_LABEL}={self.instance_label}",
                ),
                timeout_seconds=self.limits.runtime_operation_timeout_seconds,
            )
            if listed.timed_out:
                raise InfrastructureGradingError(
                    "orphan_reconciliation_timeout",
                    "container runtime timed out during orphan reconciliation",
                )
            if listed.stdout_truncated or listed.stderr_truncated:
                raise InfrastructureGradingError(
                    "orphan_reconciliation_output_limit",
                    "container runtime exceeded the reconciliation output limit",
                )
            if listed.returncode != 0:
                raise InfrastructureGradingError(
                    "orphan_reconciliation_failed",
                    "container runtime could not list stale grading sandboxes",
                )
            identifiers = self._container_ids(listed.stdout)
            for identifier in identifiers:
                removed = self._execute(
                    (self.runtime, "rm", "--force", identifier),
                    timeout_seconds=self.limits.runtime_operation_timeout_seconds,
                )
                if removed.timed_out or removed.returncode != 0:
                    raise InfrastructureGradingError(
                        "orphan_reconciliation_failed",
                        "container runtime could not remove a stale grading sandbox",
                    )
            return len(identifiers)

    def close(self) -> None:
        """Prevent new grades and force-remove every locally active sandbox."""

        with self._lifecycle_lock:
            self._closed = True
            active = tuple(self._active_containers)
            self._active_containers.clear()
        for name in active:
            self._best_effort_remove(name)

    def __enter__(self) -> "ContainerGrader":
        with self._lifecycle_lock:
            if self._closed:
                raise InfrastructureGradingError(
                    "grader_closed", "container grader has already been closed"
                )
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def grade(
        self,
        *,
        workspace: PreparedWorkspace,
        runner_image: str,
        max_score: float,
    ) -> PublicGradeResult:
        """Grade a prepared workspace using its pinned runner and score bound."""

        return self._grade_request(
            GradeRequest.from_workspace(
                workspace,
                runner_image=runner_image,
                max_score=max_score,
            )
        )

    def _grade_request(self, request: GradeRequest) -> PublicGradeResult:
        """Validate and execute an internal request built from a workspace."""

        try:
            maximum = _finite_score(request.max_score, "assignment max_score")
        except AssessmentGradingError as exc:
            raise InfrastructureGradingError(
                "invalid_grading_configuration", "assignment max_score must be finite"
            ) from exc
        if maximum <= 0:
            raise InfrastructureGradingError(
                "invalid_grading_configuration", "assignment max_score must be positive"
            )
        image = self._immutable_image(request.runner_image)
        mounts = self._mounts(request)
        name = self._container_name(self._name_factory())

        create_args = self._create_args(
            name=name,
            image=image,
            mounts=mounts,
        )
        registered = False
        cleanup_attempted = False
        try:
            with self._lifecycle_lock:
                if self._closed:
                    raise InfrastructureGradingError(
                        "grader_closed", "container grader has already been closed"
                    )
                if name in self._active_containers:
                    raise InfrastructureGradingError(
                        "container_name_collision", "generated sandbox name is already active"
                    )
                self._active_containers.add(name)
                registered = True
                try:
                    created = self._execute(
                        create_args,
                        timeout_seconds=self.limits.runtime_operation_timeout_seconds,
                    )
                except InfrastructureGradingError:
                    # A client-side failure does not prove that the daemon failed
                    # before creating the named container.
                    self._best_effort_remove(name)
                    raise

            if created.timed_out:
                self._best_effort_remove(name)
                raise InfrastructureGradingError(
                    "sandbox_create_timeout",
                    "container runtime timed out while creating sandbox",
                )
            if created.stdout_truncated or created.stderr_truncated:
                self._best_effort_remove(name)
                raise InfrastructureGradingError(
                    "sandbox_create_output_limit",
                    "container runtime exceeded its output limit",
                )
            if created.returncode != 0:
                self._best_effort_remove(name)
                raise InfrastructureGradingError(
                    "sandbox_create_failed", "container runtime could not create sandbox"
                )

            execution_error: Optional[GradingError] = None
            execution: Optional[ProcessResult] = None
            try:
                execution = self._execute(
                    (self.runtime, "start", "--attach", name),
                    timeout_seconds=self.limits.timeout_seconds,
                )
                if execution.timed_out:
                    execution_error = AssessmentGradingError(
                        "assessment_timeout", "isolated assessment exceeded its time limit"
                    )
                elif execution.stdout_truncated or execution.stderr_truncated:
                    execution_error = AssessmentGradingError(
                        "assessment_output_limit", "isolated assessment exceeded its output limit"
                    )
                elif execution.returncode in self._INFRASTRUCTURE_EXIT_CODES:
                    execution_error = InfrastructureGradingError(
                        "sandbox_start_failed",
                        "container runtime could not start the assessment",
                    )
                elif execution.returncode != 0:
                    execution_error = AssessmentGradingError(
                        "assessment_process_failed",
                        "isolated assessment exited unsuccessfully",
                    )
            except GradingError as exc:
                execution_error = exc

            cleanup_error = self._remove(name)
            cleanup_attempted = True
            if cleanup_error is not None:
                if execution_error is not None:
                    raise cleanup_error from execution_error
                raise cleanup_error
            if execution_error is not None:
                raise execution_error
            if execution is None:  # pragma: no cover - defensive state guard.
                raise InfrastructureGradingError(
                    "sandbox_state_error", "container runtime returned no assessment state"
                )

            parsed = _parse_result(execution.stdout)
            return sanitize_grade_result(parsed, assignment_max_score=maximum)
        except BaseException:
            # Grading may be interrupted by Ctrl-C outside the normal
            # GradingError hierarchy.  Keep the container registered until a
            # best-effort remove has been issued, then re-raise unchanged.
            if registered and not cleanup_attempted:
                self._best_effort_remove(name)
            raise
        finally:
            if registered:
                with self._lifecycle_lock:
                    self._active_containers.discard(name)

    def _execute(self, argv: Sequence[str], *, timeout_seconds: float) -> ProcessResult:
        try:
            result = self.executor.run(
                tuple(argv),
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=self.limits.max_stdout_bytes,
                max_stderr_bytes=self.limits.max_stderr_bytes,
            )
        except Exception as exc:
            raise InfrastructureGradingError(
                "container_runtime_unavailable", "container runtime invocation failed"
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
            raise InfrastructureGradingError(
                "container_runtime_protocol_error", "container runtime returned invalid state"
            )
        return result

    def _remove(self, name: str) -> Optional[InfrastructureGradingError]:
        try:
            removed = self._execute(
                (self.runtime, "rm", "--force", name),
                timeout_seconds=self.limits.runtime_operation_timeout_seconds,
            )
        except InfrastructureGradingError:
            return InfrastructureGradingError(
                "sandbox_cleanup_failed", "container runtime could not remove sandbox"
            )
        if removed.timed_out or removed.returncode != 0:
            return InfrastructureGradingError(
                "sandbox_cleanup_failed", "container runtime could not remove sandbox"
            )
        return None

    def _best_effort_remove(self, name: str) -> None:
        try:
            self.executor.run(
                (self.runtime, "rm", "--force", name),
                timeout_seconds=self.limits.runtime_operation_timeout_seconds,
                max_stdout_bytes=self.limits.max_stdout_bytes,
                max_stderr_bytes=self.limits.max_stderr_bytes,
            )
        except Exception:
            pass

    def _create_args(
        self,
        *,
        name: str,
        image: str,
        mounts: Sequence[Tuple[Path, str]],
    ) -> Tuple[str, ...]:
        limits = self.limits
        arguments = [
            self.runtime,
            "create",
            "--name",
            name,
            "--label",
            f"{self.MANAGED_LABEL}=true",
            "--label",
            f"{self.INSTANCE_LABEL}={self.instance_label}",
            "--pull=never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(limits.pids_limit),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--cpus",
            format(limits.cpu_count, "g"),
            "--user",
            self.container_user,
            "--ulimit",
            "nofile=256:256",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={limits.tmpfs_bytes},mode=1777",
            "--workdir",
            "/workspace/submission",
        ]
        for source, target in mounts:
            arguments.extend(
                (
                    "--mount",
                    f"type=bind,src={source},dst={target},readonly",
                )
            )
        arguments.append(image)
        arguments.extend(self.container_command)
        return tuple(arguments)

    @staticmethod
    def _runtime(value: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("runtime must name docker or podman")
        path = Path(value)
        if path.parent != Path(".") and not path.is_absolute():
            raise ValueError("runtime path must be absolute or use the trusted PATH")
        basename = path.name.casefold()
        if basename not in {"docker", "docker.exe", "podman", "podman.exe"}:
            raise ValueError("runtime must name docker or podman")
        return value

    @staticmethod
    def _instance_label(value: str) -> str:
        if not isinstance(value, str) or _INSTANCE_LABEL_VALUE.fullmatch(value) is None:
            raise ValueError(
                "instance_label must use 1-128 lowercase letters, digits, dots, "
                "underscores, or hyphens"
            )
        return value

    @staticmethod
    def _container_ids(value: bytes) -> Tuple[str, ...]:
        try:
            lines = value.decode("ascii", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise InfrastructureGradingError(
                "orphan_reconciliation_protocol_error",
                "container runtime returned invalid reconciliation output",
            ) from exc
        identifiers = []
        seen = set()
        for line in lines:
            identifier = line.strip().casefold()
            if not identifier:
                continue
            if _CONTAINER_ID.fullmatch(identifier) is None:
                raise InfrastructureGradingError(
                    "orphan_reconciliation_protocol_error",
                    "container runtime returned invalid reconciliation output",
                )
            if identifier not in seen:
                seen.add(identifier)
                identifiers.append(identifier)
        return tuple(identifiers)

    @staticmethod
    def _container_command(value: Sequence[str]) -> Tuple[str, ...]:
        if isinstance(value, (str, bytes)):
            raise TypeError("container_command must be a sequence of arguments")
        normalized = tuple(value)
        if any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in normalized
        ):
            raise ValueError("container_command arguments must be non-empty and NUL-free")
        return normalized

    @staticmethod
    def _container_user(value: Optional[str]) -> str:
        if value is None:
            uid = os.getuid() if hasattr(os, "getuid") else 65534
            gid = os.getgid() if hasattr(os, "getgid") else 65534
            if uid == 0:
                uid = gid = 65534
            value = f"{uid}:{gid}"
        if not isinstance(value, str):
            raise TypeError("container_user must be a numeric uid[:gid]")
        match = _NUMERIC_USER.fullmatch(value)
        if match is None or int(match.group("uid")) == 0:
            raise ValueError("container_user must use a non-root numeric uid")
        return value

    @staticmethod
    def _immutable_image(value: str) -> str:
        if not isinstance(value, str) or len(value) > 512 or any(
            character.isspace() or ord(character) < 32 for character in value
        ):
            raise InfrastructureGradingError(
                "invalid_runner_image", "runner image must be an immutable digest reference"
            )
        marker = "@sha256:"
        if value.count(marker) != 1:
            raise InfrastructureGradingError(
                "invalid_runner_image", "runner image must be an immutable digest reference"
            )
        name, digest = value.rsplit(marker, 1)
        if (
            "/" not in name
            or "@" in name
            or _IMAGE_NAME.fullmatch(name) is None
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            raise InfrastructureGradingError(
                "invalid_runner_image", "runner image must be an immutable digest reference"
            )
        return f"{name}{marker}{digest.lower()}"

    @staticmethod
    def _container_name(value: str) -> str:
        if not isinstance(value, str) or _CONTAINER_NAME.fullmatch(value) is None:
            raise InfrastructureGradingError(
                "invalid_container_name", "generated sandbox name is invalid"
            )
        return value

    @staticmethod
    def _mount_path(value: Union[str, Path], label: str) -> Path:
        try:
            lexical = Path(value)
            if lexical.is_symlink():
                raise InfrastructureGradingError(
                    "invalid_workspace", f"{label} path must not be a symlink"
                )
            resolved = lexical.resolve(strict=True)
        except InfrastructureGradingError:
            raise
        except (OSError, RuntimeError, TypeError) as exc:
            raise InfrastructureGradingError(
                "invalid_workspace", f"{label} path is unavailable"
            ) from exc
        if not resolved.is_dir() or resolved == Path(resolved.anchor):
            raise InfrastructureGradingError(
                "invalid_workspace", f"{label} path must be a dedicated directory"
            )
        path_text = str(resolved)
        if any(character in path_text for character in (",", "\n", "\r", "\x00")):
            raise InfrastructureGradingError(
                "invalid_workspace", f"{label} path cannot be represented safely"
            )
        return resolved

    @classmethod
    def _mounts(cls, request: GradeRequest) -> Tuple[Tuple[Path, str], ...]:
        workspace_root = cls._mount_path(request.workspace_path, "workspace")
        paths = [
            (cls._mount_path(request.submission_path, "submission"), "/workspace/submission")
        ]
        if request.assessment_path is not None:
            paths.append(
                (cls._mount_path(request.assessment_path, "assessment"), "/workspace/assessment")
            )
        if request.data_path is not None:
            paths.append((cls._mount_path(request.data_path, "data"), "/workspace/data"))

        sources = [source for source, _ in paths]
        for source, target in paths:
            if source != workspace_root / PurePosixPath(target).name:
                raise InfrastructureGradingError(
                    "invalid_workspace",
                    "grading inputs must be the prepared workspace directories",
                )
        for index, left in enumerate(sources):
            for right in sources[index + 1 :]:
                if left == right or left in right.parents or right in left.parents:
                    raise InfrastructureGradingError(
                        "invalid_workspace", "grading input directories must not overlap"
                    )
        return tuple(paths)
