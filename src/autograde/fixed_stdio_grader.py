"""Fixed C/C++ scoring with every compiler/student process confined to OCI.

Only the installed, trusted evaluator runs on the server. No uploaded Python,
build command, source program or executable runs there. Each disposable OCI
container receives one source and its current input; expected outputs, scores,
the full dataset and server paths are never mounted. Evidence from the
container is untrusted and cannot supply the final score.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import assignment_admin_grader, fixed_stdio_container_driver
from .platform_grader import (
    ContainerGrader, GradeRequest, InfrastructureGradingError,
    sanitize_grade_result,
)
from .platform_runner_image import RunnerImageAvailabilityChecker, RunnerImageAvailabilityError


RECIPE_FILE = "fixed-stdio.json"
RECIPE = (json.dumps({"contract": "autograde.fixed-stdio", "version": 1,
    "evaluator_sha256": hashlib.sha256(Path(assignment_admin_grader.__file__).read_bytes()).hexdigest(),
    "driver_sha256": hashlib.sha256(Path(fixed_stdio_container_driver.__file__).read_bytes()).hexdigest(),
    "backend_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
    sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
ENTRYPOINT = b'# autograde fixed stdio OCI recipe v1\nraise RuntimeError("fixed_stdio_backend_required")\n'


def write_recipe(directory):
    (directory / RECIPE_FILE).write_bytes(RECIPE)
    (directory / "grade.py").write_bytes(ENTRYPOINT)


def _error(code="fixed_stdio_protocol_error"):
    return InfrastructureGradingError(code, "Isolated fixed C/C++ grading is unavailable; no host fallback was attempted.")


def _regular_bytes(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise _error("fixed_stdio_invalid_recipe")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise _error("fixed_stdio_invalid_recipe")
    return value


class FixedStdioContainerGrader(ContainerGrader):
    """Server-owned scoring, per-case unprivileged compiler/execution containers."""

    def grade(self, *, workspace, runner_image, max_score):
        image = self._immutable_image(runner_image)
        # Reuse prepared-workspace checks without granting the container any
        # assessment/data mounts. A receipt cannot choose arbitrary host paths.
        self._mounts(GradeRequest.from_workspace(workspace, runner_image=image, max_score=max_score))
        assessment, dataset = workspace.assessment_path, workspace.data_path
        if assessment is None or dataset is None:
            raise _error("fixed_stdio_invalid_recipe")
        if (set(p.name for p in assessment.iterdir()) != {RECIPE_FILE, "grade.py"}
                or _regular_bytes(assessment / RECIPE_FILE, 1024) != RECIPE
                or _regular_bytes(assessment / "grade.py", 256) != ENTRYPOINT):
            raise _error("fixed_stdio_invalid_recipe")
        try:
            configuration = json.loads(_regular_bytes(dataset / "tests.json", 2 * 1024 * 1024))
            if not isinstance(configuration, dict) or set(configuration) != {"mode", "language", "tests"}:
                raise ValueError("invalid configuration")
            # Same bounded document contract as instructor authoring; never
            # eval/exec/import anything from assessment or submitted files.
            from .assignment_admin import _document
            validated = _document({**configuration, "negative_score": 0})
            configuration = {key: validated[key] for key in ("mode", "language", "tests")}
            expected_maximum = 10 if configuration["mode"] == "template" else sum(t["weight"] for t in configuration["tests"])
            if expected_maximum != max_score or expected_maximum <= 0:
                raise ValueError("score mismatch")
        except (ValueError, TypeError, KeyError) as exc:
            raise _error("fixed_stdio_invalid_recipe") from exc
        try:
            RunnerImageAvailabilityChecker(runtime=self.runtime, executor=self.executor).check(image)
        except RunnerImageAvailabilityError as exc:
            raise _error(exc.code) from exc
        language = configuration["language"]
        filename = "main.c" if language == "c" else "main.cpp"
        with tempfile.TemporaryDirectory(prefix="fixed-stdio-evaluation-") as temporary:
            work = Path(temporary)
            source_bytes = None

            def isolated_run(arguments, _work, label, timeout, stdin=b""):
                nonlocal source_bytes
                if label in {"probe", "compile"}:
                    source = _regular_bytes(Path(arguments[-3]), 1024 * 1024)
                    if label == "compile":
                        source_bytes = source
                    stage = "compile"
                else:
                    if source_bytes is None:
                        raise _error("fixed_stdio_invalid_recipe")
                    source, stage = source_bytes, "run"
                evidence = self._run_case(image, language, source, stdin, stage, timeout)
                compiled = evidence["compile"]
                selected = evidence["run"] if stage == "run" else compiled
                if stage == "run" and (compiled["status"] or compiled["failure"] or selected is None):
                    # Recompilation must agree with the separate compile step.
                    # Infrastructure/memory changes must not award a zero grade.
                    raise _error("fixed_stdio_recompile_failed")
                if label == "compile":
                    # A bounded compiler location only; compiler text never
                    # enters public feedback. Match container path to source.
                    diagnostic = compiled["stderr"].replace(
                        ("/workspace/submission/" + filename).encode(), filename.encode())
                    (work / "compile.err").write_bytes(diagnostic)
                return selected["status"], selected["stdout"], selected["failure"]

            try:
                result = assignment_admin_grader.evaluate(workspace.submission_path, configuration, work,
                    runner=isolated_run, compiler="gcc" if language == "c" else "g++", total_seconds=120)
            except RuntimeError as exc:
                if isinstance(exc, InfrastructureGradingError):
                    raise
                raise _error("fixed_stdio_toolchain_unavailable") from exc
        return sanitize_grade_result(result, assignment_max_score=max_score)

    def _run_case(self, image, language, source, stdin, stage, seconds):
        # No writable host mount: the compiler binary and all runtime files are
        # confined to a bounded container tmpfs, destroyed after each case.
        with tempfile.TemporaryDirectory(prefix="fixed-stdio-input-") as temporary:
            request = Path(temporary)
            if not hasattr(os, "getuid"):
                raise _error("fixed_stdio_unsupported_server")
            owner = int(self.container_user.split(":")[0])
            if os.getuid() not in {0, owner}:
                raise _error("fixed_stdio_input_permissions")
            filename = "main.c" if language == "c" else "main.cpp"
            files = {filename: source, "stdin.in": stdin, "empty.in": b"",
                "request.json": json.dumps({"language": language, "stage": stage, "seconds": max(.01, seconds)}).encode(),
                "driver.py": Path(fixed_stdio_container_driver.__file__).read_bytes()}
            for name, value in files.items():
                (request / name).write_bytes(value)
                (request / name).chmod(0o400)
                if os.getuid() == 0:
                    os.chown(request / name, owner, -1)
            if os.getuid() == 0:
                os.chown(request, owner, -1)
            request.chmod(0o500)
            try:
                raw = self._execute_case(image, request, min(30, 18 + seconds))
            finally:
                request.chmod(0o700)
            try:
                envelope = json.loads(raw)
                if not isinstance(envelope, dict) or set(envelope) != {"compile", "run"}:
                    raise ValueError("invalid evidence")
                for phase in ("compile", "run"):
                    item = envelope[phase]
                    if item is None and phase == "run":
                        continue
                    if not isinstance(item, dict) or set(item) != {"status", "failure", "stdout", "stderr"}:
                        raise ValueError("invalid evidence")
                    if (type(item["status"]) is not int or not -255 <= item["status"] <= 255
                            or item["failure"] not in {None, "time_limit", "output_limit"}):
                        raise ValueError("invalid evidence")
                    for stream in ("stdout", "stderr"):
                        item[stream] = base64.b64decode(item[stream], validate=True)
                    if len(item["stdout"]) + len(item["stderr"]) > assignment_admin_grader.OUTPUT_LIMIT:
                        raise ValueError("unbounded evidence")
                return envelope
            except (ValueError, TypeError, KeyError, binascii.Error) as exc:
                raise _error() from exc

    def _execute_case(self, image, request, timeout):
        name = self._container_name(self._name_factory())
        arguments = list(self._create_args(name=name, image=image, mounts=[(request, "/workspace/submission")]))
        # Docker's tmpfs default is noexec. Compiled binaries must execute only
        # in this disposable, size-bounded filesystem; no host path is writable.
        tmpfs_index = arguments.index("--tmpfs") + 1
        arguments[tmpfs_index] += ",exec"
        # Override image entrypoint and command with our installed fixed driver.
        index = arguments.index(image)
        arguments[index:] = ["--entrypoint", "/usr/local/bin/python3", image, "-I", "-B", "/workspace/submission/driver.py"]
        registered = cleanup_attempted = False
        try:
            with self._lifecycle_lock:
                if self._closed:
                    raise _error("grader_closed")
                if name in self._active_containers:
                    raise _error("container_name_collision")
                self._active_containers.add(name)
                registered = True
                created = self._execute(arguments, timeout_seconds=self.limits.runtime_operation_timeout_seconds)
                if created.returncode or created.timed_out or created.stdout_truncated or created.stderr_truncated:
                    raise _error("sandbox_create_failed")
            execution = self._execute((self.runtime, "start", "--attach", name), timeout_seconds=timeout)
            cleanup = self._remove(name)
            cleanup_attempted = True
            if cleanup:
                raise cleanup
            # OCI timeout/driver death is not a trustworthy student result.
            # Per-program timeout/output limits are reported inside the protocol.
            if (execution.returncode or execution.timed_out or execution.stdout_truncated
                    or execution.stderr_truncated):
                raise _error("fixed_stdio_execution_failed")
            return execution.stdout
        finally:
            if registered:
                if not cleanup_attempted:
                    self._best_effort_remove(name)
                with self._lifecycle_lock:
                    self._active_containers.discard(name)


class PortalContainerGrader(FixedStdioContainerGrader):
    """Route immutable fixed recipes; preserve explicit custom OCI assignments."""

    def grade(self, *, workspace, runner_image, max_score):
        if workspace.assessment_path and (workspace.assessment_path / RECIPE_FILE).exists():
            return super().grade(workspace=workspace, runner_image=runner_image, max_score=max_score)
        return ContainerGrader.grade(self, workspace=workspace, runner_image=runner_image, max_score=max_score)
