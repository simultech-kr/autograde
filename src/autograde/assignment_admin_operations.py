"""Shared immutable assignment registration and validation operations.

CLI and instructor web call this service boundary without invoking one another.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .platform_auth import new_public_id
from .platform_bundle import BundleStore, BundleError
from .platform_events import emit_operator_event
from .platform_grader import Grader, ContainerGrader, PilotLocalGrader, PILOT_LOCAL_RUNNER
from .platform_runner_image import RunnerImageAvailability, RunnerImageAvailabilityChecker, normalize_runner_image
from .platform_state import BundleAssignmentRelease, PlatformStateStore, PlatformNotFound
from .settings import AppPaths
from .workspace import WorkspaceBuilder, WorkspaceError

DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_BUNDLE_EXPANDED_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_BUNDLE_FILES = 5000

def _add_bundle_assignment(
    args: argparse.Namespace,
    paths: AppPaths,
    state: PlatformStateStore,
    course_key: str,
) -> Mapping[str, Any]:
    """Create immutable delivery/input artifacts, then register one release."""

    runner_reference = _assignment_runner_reference(args)
    store = _bundle_store(paths)
    starter = store.create_from_directory(args.starter, kind="starter")
    assessment_artifact = store.create_from_directory(
        args.assessment, kind="assessment"
    )
    builder = WorkspaceBuilder(paths.workspaces)
    expected_assessment = builder.digest_instructor_tree(
        args.assessment, label="assessment"
    )
    assessment = _materialize_instructor_bundle(
        paths,
        store,
        assessment_artifact.archive_sha256,
        kind="assessment",
        expected_tree_digest=expected_assessment.sha256,
    )
    data = None
    if args.data is not None:
        data_artifact = store.create_from_directory(args.data, kind="data")
        expected_data = builder.digest_instructor_tree(args.data, label="data")
        data = _materialize_instructor_bundle(
            paths,
            store,
            data_artifact.archive_sha256,
            kind="data",
            expected_tree_digest=expected_data.sha256,
        )

    assignment = state.register_bundle_assignment_release(
        assignment_id=args.assignment_id or new_public_id("basn"),
        course_key=course_key,
        assignment_key=args.assignment_key,
        release_id=args.release_id,
        title=args.title,
        starter_path=str(starter.path),
        starter_digest=starter.archive_sha256,
        starter_size_bytes=starter.compressed_bytes,
        assessment_path=str(assessment.path),
        assessment_digest=assessment.sha256,
        data_path=str(data.path) if data is not None else None,
        dataset_digest=data.sha256 if data is not None else "",
        runner_image=runner_reference,
        rubric_version=args.rubric_version,
        max_score=args.max_score,
        result_policy=args.result_policy,
        opens_at=args.opens_at,
        due_at=args.due_at,
        ready=False,
    )
    # Registration is always a draft. Replaying an existing registration must
    # not silently hide a release that is already public.
    return _bundle_assignment_summary(assignment)


def _check_bundle_assignment(args, paths, state, course_key, *, grader=None):
    assignment = state.get_bundle_assignment(args.assignment_id)
    if assignment.course_key != course_key:
        raise PlatformNotFound("assignment was not found in this course")
    if not math.isfinite(args.negative_score) or not 0 <= args.negative_score < assignment.max_score:
        raise ValueError("negative-score must be finite, nonnegative and below max-score")
    check_id = state.begin_bundle_release_check(assignment.assignment_id, course_key=course_key)
    details = {"cases": []}
    try:
        _validate_bundle_assignment(paths, assignment)
        _check_runner_images(args, (assignment.runner_image,))
        grader = grader or _new_grader(args, paths, course_key)
        store = _bundle_store(paths)
        for label, source, expected in (("solution", args.solution, assignment.max_score),
                                        ("negative", args.negative_solution, args.negative_score)):
            artifact = store.create_from_directory(source, kind="submission")
            workspace = WorkspaceBuilder(paths.workspaces).prepare(
                new_public_id("validation"), artifact.path, assignment.assessment_path, assignment.data_path,
                expected_source_sha256=artifact.digest, expected_assessment_sha256=assignment.assessment_digest,
                expected_data_sha256=assignment.dataset_digest or None,
            )
            result = grader.grade(workspace=workspace, runner_image=assignment.runner_image, max_score=assignment.max_score)
            passed = math.isclose(result.score, expected, rel_tol=0, abs_tol=1e-9) and result.max_score == assignment.max_score
            details["cases"].append({"case": label, "source_sha256": artifact.digest, "expected_score": expected,
                                     "score": result.score, "passed": passed})
            if not passed:
                raise ValueError(f"{label} test did not receive its expected score")
        state.finish_bundle_release_check(check_id, passed=True, details=details)
        return {"assignment_id": assignment.assignment_id, "check_id": check_id, "status": "passed", **details}
    except BaseException as exc:
        details["error_type"] = type(exc).__name__
        state.finish_bundle_release_check(check_id, passed=False, details=details)
        raise
    finally:
        if grader is not None:
            grader.close()


def _bundle_store(
    paths: AppPaths,
    *,
    max_compressed_bytes: int = DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES,
    max_expanded_bytes: int = DEFAULT_MAX_BUNDLE_EXPANDED_BYTES,
    max_files: int = DEFAULT_MAX_BUNDLE_FILES,
) -> BundleStore:
    return BundleStore(
        paths.bundles,
        max_compressed_bytes=max_compressed_bytes,
        max_expanded_bytes=max_expanded_bytes,
        max_files=max_files,
    )


def _materialize_instructor_bundle(
    paths: AppPaths,
    store: BundleStore,
    digest: str,
    *,
    kind: str,
    expected_tree_digest: str,
):
    kind_root = paths.instructor_inputs / kind
    if os.path.lexists(kind_root) and kind_root.is_symlink():
        raise OSError(f"instructor input directory must not be a symlink: {kind_root}")
    kind_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not kind_root.is_dir():
        raise NotADirectoryError(kind_root)
    kind_root.chmod(0o700)
    digest_hex = digest.removeprefix("sha256:")
    destination = kind_root / digest_hex
    if not os.path.lexists(destination):
        store.materialize_directory(digest, destination, read_only=True)
    elif destination.is_symlink() or not destination.is_dir():
        raise OSError(
            f"materialized instructor input must be a directory: {destination}"
        )
    materialized = WorkspaceBuilder(paths.workspaces).digest_instructor_tree(
        destination, label=kind
    )
    if materialized.sha256 != expected_tree_digest:
        raise WorkspaceError(
            f"materialized {kind} bundle does not match its instructor source"
        )
    return materialized


def _validate_bundle_assignment(
    paths: AppPaths,
    assignment: BundleAssignmentRelease,
    *,
    store: BundleStore | None = None,
) -> None:
    active_store = store or _bundle_store(paths)
    starter = active_store.get(assignment.starter_digest)
    if (
        starter.path != Path(assignment.starter_path)
        or starter.compressed_bytes != assignment.starter_size_bytes
        or starter.metadata.kind != "starter"
    ):
        raise BundleError("registered starter bundle metadata does not match storage")
    builder = WorkspaceBuilder(paths.workspaces)
    assessment_path = Path(assignment.assessment_path).resolve(strict=True)
    assessment_root = (paths.instructor_inputs / "assessment").resolve(strict=True)
    if not assessment_path.is_relative_to(assessment_root):
        raise WorkspaceError("registered assessment is outside managed storage")
    entrypoint = assessment_path / "grade.py"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise WorkspaceError("assessment must contain a regular grade.py entrypoint")
    assessment = builder.digest_instructor_tree(
        assessment_path, label="assessment"
    )
    if assessment.sha256 != assignment.assessment_digest:
        raise WorkspaceError("registered assessment digest does not match storage")
    if assignment.data_path is not None:
        data_path = Path(assignment.data_path).resolve(strict=True)
        data_root = (paths.instructor_inputs / "data").resolve(strict=True)
        if not data_path.is_relative_to(data_root):
            raise WorkspaceError("registered dataset is outside managed storage")
        data = builder.digest_instructor_tree(data_path, label="data")
        if data.sha256 != assignment.dataset_digest:
            raise WorkspaceError("registered dataset digest does not match storage")
    elif assignment.dataset_digest:
        raise WorkspaceError("registered dataset digest has no data directory")


def _bundle_assignment_summary(
    assignment: BundleAssignmentRelease,
) -> Mapping[str, Any]:
    return {
        "assignment_id": assignment.assignment_id,
        "assignment_key": assignment.assignment_key,
        "release_id": assignment.release_id,
        "title": assignment.title,
        "delivery_mode": "bundle",
        "starter_digest": assignment.starter_digest,
        "starter_size_bytes": assignment.starter_size_bytes,
        "assessment_digest": assignment.assessment_digest,
        "dataset_digest": assignment.dataset_digest,
        "runner_image": assignment.runner_image,
        "grading_runtime": (
            "pilot-local"
            if assignment.runner_image == PILOT_LOCAL_RUNNER
            else "container"
        ),
        "max_score": assignment.max_score,
        "result_policy": assignment.result_policy.value,
        "opens_at": assignment.opens_at,
        "due_at": assignment.due_at,
        "ready": assignment.ready,
        "active": assignment.active,
    }


def _check_runner_images(
    args: argparse.Namespace,
    runner_images: Sequence[str],
) -> tuple[RunnerImageAvailability, ...]:
    """Inspect unique local runner digests using the command's runtime policy."""

    runtime = args.grading_runtime
    if runtime == "pilot-local":
        if any(reference != PILOT_LOCAL_RUNNER for reference in runner_images):
            raise ValueError(
                "ready container assignments require explicit --grading-runtime docker or podman"
            )
        return ()
    if any(reference == PILOT_LOCAL_RUNNER for reference in runner_images):
        raise ValueError(
            "pilot-local assignments require --grading-runtime pilot-local"
        )
    checker = RunnerImageAvailabilityChecker(
        runtime=runtime,
        timeout_seconds=args.runner_image_inspect_timeout,
    )
    return checker.check_many(runner_images)


def _assignment_runner_reference(args: argparse.Namespace) -> str:
    """Resolve the persisted runner contract for one assignment command."""

    runtime = args.grading_runtime
    runner_image = args.runner_image
    if runtime == "pilot-local":
        if runner_image not in {None, PILOT_LOCAL_RUNNER}:
            raise ValueError(
                "--runner-image is not used by pilot-local; select docker or podman explicitly"
            )
        return PILOT_LOCAL_RUNNER
    if runner_image is None:
        raise ValueError(
            "--runner-image is required with --grading-runtime docker or podman"
        )
    return normalize_runner_image(runner_image)


def _new_grader(
    args: argparse.Namespace,
    paths: AppPaths,
    course_key: str,
) -> Grader:
    """Create only the explicitly selected grading implementation."""

    if args.grading_runtime == "pilot-local":
        emit_operator_event(
            "pilot_local_unsandboxed",
            component="grader",
            level="warning",
        )
        return PilotLocalGrader()
    return ContainerGrader(
        runtime=args.grading_runtime,
        instance_label=_grader_instance_label(paths, course_key),
    )


def _grader_instance_label(paths: AppPaths, course_key: str) -> str:
    """Return a non-sensitive, restart-stable label for one course data root."""

    material = (
        b"autograde-container-instance-v1\0"
        + os.fsencode(paths.root.resolve(strict=True))
        + b"\0"
        + course_key.encode("utf-8", "strict")
    )
    return f"service-{hashlib.sha256(material).hexdigest()[:32]}"
