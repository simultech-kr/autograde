"""Operator CLI for the student-platform MVP."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import errno
import fcntl
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import sys
import threading
from typing import Any, Iterator, Mapping, Optional, Sequence

from .gitops import GitCollector, GitCollectorError
from .platform_auth import (
    PlatformAuthError,
    create_or_load_auth_secret,
    create_or_load_instructor_token,
    hash_student_password,
    new_public_id,
    validate_student_password,
    verify_student_password,
)
from .platform_bundle import BundleError, BundleStore
from .platform_bundle_worker import (
    BundleSubmissionProcessor,
    BundleSubmissionWorker,
)
from .platform_events import emit_operator_event
from .platform_github import GitHubOAuthClient
from .platform_github_app import (
    GitHubAppError,
    GitHubAppInstallationTokenProvider,
    OpenSSLRS256Signer,
)
from .platform_grader import (
    ContainerGrader,
    Grader,
    GradingError,
    PILOT_LOCAL_RUNNER,
    PilotLocalGrader,
)
from .platform_http import create_server
from .platform_pinner import GitSubmissionPinner
from .pilot_config import apply_pilot_config, load_pilot_config
from .platform_repository import RepositoryPreflightReport, preflight_assignments
from .platform_runner_image import (
    RunnerImageAvailability,
    RunnerImageAvailabilityChecker,
    RunnerImageAvailabilityError,
    normalize_runner_image,
    validate_inspect_timeout,
)
from .platform_service import (
    PlatformAPIError,
    StudentPlatformService,
    SubmissionPolicy,
    TokenPolicy,
)
from .platform_state import (
    BundleAssignmentRelease,
    CourseRosterImportEntry,
    OperatorSubmissionView,
    PlatformAssignment,
    PlatformConflict,
    PlatformNotFound,
    PlatformStateError,
    PlatformStateStore,
    StudentIdentityKind,
    SubmissionState,
)
from .platform_worker import SubmissionProcessor, SubmissionWorker
from .settings import AppPaths
from .workspace import WorkspaceBuilder, WorkspaceError


DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_BUNDLE_EXPANDED_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_BUNDLE_FILES = 5_000


class RepositoryPreflightFailed(RuntimeError):
    """One or more registered repositories failed the operator readiness gate."""

    def __init__(self, report: RepositoryPreflightReport) -> None:
        super().__init__("one or more repositories failed preflight")
        self.report = report


def _container_runtime(value: str) -> str:
    if value not in {"docker", "podman"}:
        raise argparse.ArgumentTypeError("container runtime must be docker or podman")
    return value


def _grading_runtime(value: str) -> str:
    if value not in {"pilot-local", "docker", "podman"}:
        raise argparse.ArgumentTypeError(
            "grading runtime must be pilot-local, docker, or podman"
        )
    return value


def _runner_image_inspect_timeout(value: str) -> float:
    try:
        return validate_inspect_timeout(float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_score(value: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("max score must be a number") from exc
    if not math.isfinite(score) or score <= 0:
        raise argparse.ArgumentTypeError("max score must be finite and positive")
    return score


def _add_runner_image_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--grading-runtime",
        type=_grading_runtime,
        choices=("pilot-local", "docker", "podman"),
        default=os.environ.get(
            "AUTOGRADE_GRADING_RUNTIME",
            os.environ.get("AUTOGRADE_CONTAINER_RUNTIME", "pilot-local"),
        ),
        help="pilot-local (unsafe pilot default) or an isolated OCI runtime",
    )
    parser.add_argument(
        "--container-runtime",
        dest="grading_runtime",
        type=_container_runtime,
        choices=("docker", "podman"),
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runner-image-inspect-timeout",
        type=_runner_image_inspect_timeout,
        default=os.environ.get(
            "AUTOGRADE_RUNNER_IMAGE_INSPECT_TIMEOUT",
            str(RunnerImageAvailabilityChecker.DEFAULT_TIMEOUT_SECONDS),
        ),
        metavar="SECONDS",
        help="bounded local runner image inspection timeout (0.1-60 seconds)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autograde-platform",
        description="Course-scoped student submission and grading service",
    )
    parser.add_argument(
        "--pilot-config",
        type=Path,
        help=(
            "local non-secret key,value CSV; command-line values take precedence"
        ),
    )
    parser.add_argument(
        "--data-root",
        default=os.environ.get("AUTOGRADE_DATA_ROOT", ".autograde-data"),
        help="private state/artifact root (default: .autograde-data)",
    )
    parser.add_argument(
        "--course-key",
        default=os.environ.get("AUTOGRADE_COURSE_KEY"),
        help="course identifier (or AUTOGRADE_COURSE_KEY)",
    )
    parser.add_argument(
        "--public-base-url",
        default=os.environ.get("AUTOGRADE_PUBLIC_BASE_URL", "http://127.0.0.1:8000"),
        help="browser-visible HTTPS origin; loopback HTTP is allowed for development",
    )
    parser.add_argument(
        "--github-app-id",
        type=int,
        default=os.environ.get("AUTOGRADE_GITHUB_APP_ID"),
        help="read-only collection GitHub App ID",
    )
    parser.add_argument(
        "--github-app-installation-id",
        type=int,
        default=os.environ.get("AUTOGRADE_GITHUB_APP_INSTALLATION_ID"),
        help="course repository GitHub App installation ID",
    )
    parser.add_argument(
        "--github-app-private-key",
        type=Path,
        default=os.environ.get("AUTOGRADE_GITHUB_APP_PRIVATE_KEY"),
        help="mode-0600 GitHub App PEM key path",
    )
    parser.add_argument(
        "--github-api-base-url",
        default=os.environ.get("AUTOGRADE_GITHUB_API_BASE_URL", "https://api.github.com"),
        help="GitHub API origin (or GHES API base URL)",
    )
    parser.add_argument(
        "--git-askpass-path",
        type=Path,
        default=os.environ.get("AUTOGRADE_GIT_ASKPASS_PATH"),
        help="installed autograde-git-askpass executable",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="initialize private platform state")

    course = commands.add_parser("course", help="inspect managed courses")
    course_commands = course.add_subparsers(dest="course_command", required=True)
    list_courses = course_commands.add_parser(
        "list", help="list courses with roster, assignment, acceptance, and submission counts"
    )
    list_courses.add_argument("--limit", type=int, default=1_000)
    show_course = course_commands.add_parser(
        "show", help="show one course and its student and assignment status"
    )
    show_course.add_argument("managed_course_key", nargs="?")

    student = commands.add_parser("student", help="manage a roster student")
    student_commands = student.add_subparsers(dest="student_command", required=True)
    add_student = student_commands.add_parser(
        "add", help="register a new global identity or manage this course enrollment"
    )
    add_student.add_argument("student_key")
    add_student.add_argument(
        "--github-user-id",
        type=int,
        help="optional GitHub numeric identity; must be paired with --github-login",
    )
    add_student.add_argument(
        "--github-login",
        help="optional GitHub login; omit both GitHub options for local identity",
    )
    add_student.add_argument("--inactive", action="store_true")
    import_students = student_commands.add_parser(
        "import", help="validate and import a course roster CSV"
    )
    import_students.add_argument("csv_file", type=Path)
    import_students.add_argument(
        "--replace-passwords",
        action="store_true",
        help=(
            "replace passwords that differ from existing credentials; without "
            "this flag, mismatches fail safely"
        ),
    )
    list_students = student_commands.add_parser(
        "list", help="list enrolled students and aggregate status"
    )
    list_students.add_argument("--limit", type=int, default=10_000)
    show_student = student_commands.add_parser(
        "show", help="show one enrolled student's aggregate status"
    )
    show_student.add_argument("student_key")
    password_set = student_commands.add_parser(
        "password-set", help="set a course-scoped six-digit Autograde password"
    )
    password_set.add_argument("student_key")
    password_set.add_argument(
        "--password-file",
        type=Path,
        help="read one six-digit password from a private mode-0600 UTF-8 file",
    )

    assignment = commands.add_parser("assignment", help="manage student assignments")
    assignment_commands = assignment.add_subparsers(
        dest="assignment_command", required=True
    )
    add_assignment = assignment_commands.add_parser(
        "add", help="register one already-provisioned student repository"
    )
    add_assignment.add_argument("assignment_key")
    add_assignment.add_argument("student_key")
    add_assignment.add_argument("--assignment-id")
    add_assignment.add_argument("--release-id", required=True)
    add_assignment.add_argument("--github-repository-id", type=int, required=True)
    add_assignment.add_argument("--repository-owner", required=True)
    add_assignment.add_argument("--repository-name", required=True)
    add_assignment.add_argument("--clone-url", required=True)
    add_assignment.add_argument("--target-ref", default="main")
    add_assignment.add_argument("--assignment-path", default=".")
    add_assignment.add_argument("--assessment", type=Path, required=True)
    add_assignment.add_argument("--data", type=Path)
    add_assignment.add_argument(
        "--runner-image",
        help="required only with --grading-runtime docker or podman",
    )
    add_assignment.add_argument("--rubric-version", default="v1")
    add_assignment.add_argument("--max-score", type=_positive_score, required=True)
    add_assignment.add_argument(
        "--result-policy",
        choices=("immediate", "score_only", "after_deadline", "manual"),
        default="immediate",
    )
    add_assignment.add_argument("--opens-at")
    add_assignment.add_argument("--due-at")
    add_assignment.add_argument("--not-ready", action="store_true")
    add_assignment.add_argument("--git-timeout", type=float, default=60.0)
    _add_runner_image_options(add_assignment)

    ready = assignment_commands.add_parser("ready", help="publish an assignment to its student")
    ready.add_argument("assignment_id")
    ready.add_argument("--git-timeout", type=float, default=60.0)
    _add_runner_image_options(ready)
    hide = assignment_commands.add_parser("hide", help="hide an assignment from its student")
    hide.add_argument("assignment_id")
    bundle_add = assignment_commands.add_parser(
        "bundle-add",
        help="register one course-wide direct-download assignment release",
    )
    bundle_add.add_argument("assignment_key")
    bundle_add.add_argument("--assignment-id")
    bundle_add.add_argument("--release-id", required=True)
    bundle_add.add_argument("--title", required=True)
    bundle_add.add_argument("--starter", type=Path, required=True)
    bundle_add.add_argument("--assessment", type=Path, required=True)
    bundle_add.add_argument("--data", type=Path)
    bundle_add.add_argument(
        "--runner-image",
        help="required only with --grading-runtime docker or podman",
    )
    bundle_add.add_argument("--rubric-version", default="v1")
    bundle_add.add_argument("--max-score", type=_positive_score, required=True)
    bundle_add.add_argument(
        "--result-policy",
        choices=("immediate", "score_only", "after_deadline", "manual"),
        default="immediate",
    )
    bundle_add.add_argument("--opens-at")
    bundle_add.add_argument("--due-at")
    bundle_add.add_argument("--not-ready", action="store_true")
    _add_runner_image_options(bundle_add)
    bundle_ready = assignment_commands.add_parser(
        "bundle-ready", help="publish a direct-download assignment release"
    )
    bundle_ready.add_argument("assignment_id")
    _add_runner_image_options(bundle_ready)
    bundle_hide = assignment_commands.add_parser(
        "bundle-hide", help="hide a direct-download assignment release"
    )
    bundle_hide.add_argument("assignment_id")
    bundle_list = assignment_commands.add_parser(
        "bundle-list", help="list direct-download assignment releases"
    )
    bundle_list.add_argument("--ready-only", action="store_true")
    bundle_list.add_argument("--include-inactive", action="store_true")
    bundle_list.add_argument("--limit", type=int, default=1_000)
    preflight = assignment_commands.add_parser(
        "preflight",
        help="verify GitHub identity, branch access, and repository content safety",
    )
    preflight.add_argument("--ready-only", action="store_true")
    preflight.add_argument("--jobs", type=int, default=4)
    preflight.add_argument("--git-timeout", type=float, default=60.0)

    auth = commands.add_parser("auth", help="manage student activation and pairing")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    issue = auth_commands.add_parser(
        "issue", help="issue a one-time code for one active course student"
    )
    issue.add_argument("student_key")
    issue.add_argument("--expires-in-hours", type=int, default=168)
    issue_output = issue.add_mutually_exclusive_group(required=True)
    issue_output.add_argument(
        "--output",
        type=Path,
        help="create a mode-0600 file containing the code; refuses overwrite",
    )
    issue_output.add_argument(
        "--show-code",
        action="store_true",
        help="explicitly reveal the one-time code in JSON stdout",
    )
    revoke_activation = auth_commands.add_parser(
        "revoke", help="revoke the currently issued code for one student"
    )
    revoke_activation.add_argument("student_key")
    sessions = auth_commands.add_parser(
        "sessions", help="recover or inspect one student's course sessions"
    )
    session_commands = sessions.add_subparsers(
        dest="session_command", required=True
    )
    list_sessions = session_commands.add_parser(
        "list", help="list retained sessions without credentials"
    )
    list_sessions.add_argument("--student-key", required=True)
    list_sessions.add_argument("--limit", type=int, default=100)
    revoke_session = session_commands.add_parser(
        "revoke", help="revoke one student session"
    )
    revoke_session.add_argument("session_id")
    revoke_session.add_argument("--student-key", required=True)
    reset_sessions = session_commands.add_parser(
        "reset", help="revoke all sessions after a student loses credentials"
    )
    reset_sessions.add_argument("--student-key", required=True)
    approve = auth_commands.add_parser(
        "approve", help="recovery-only approval of a displayed connection code"
    )
    approve.add_argument("user_code")
    approve.add_argument("--github-user-id", type=int, required=True)

    submission = commands.add_parser("submission", help="operate durable submissions")
    submission_commands = submission.add_subparsers(
        dest="submission_command", required=True
    )
    list_submissions = submission_commands.add_parser(
        "list", help="list recent course submissions"
    )
    list_submissions.add_argument("--student-key")
    list_submissions.add_argument("--assignment-key")
    list_submissions.add_argument(
        "--state", choices=tuple(item.value for item in SubmissionState)
    )
    list_submissions.add_argument("--limit", type=int, default=100)
    show_submission = submission_commands.add_parser(
        "show", help="show one course submission"
    )
    show_submission.add_argument("submission_id")
    process = submission_commands.add_parser("process", help="process one pending submission")
    process.add_argument("submission_id")
    _add_runner_image_options(process)
    publish = submission_commands.add_parser("publish", help="publish one graded result")
    publish.add_argument("submission_id")

    serve = commands.add_parser("serve", help="run HTTP API and the grading worker")
    serve.add_argument("--listen", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    _add_runner_image_options(serve)
    serve.add_argument(
        "--git-timeout",
        type=float,
        default=60.0,
        help="per-command timeout for synchronous submission collection",
    )
    serve.add_argument("--repository-preflight-jobs", type=int, default=4)
    serve.add_argument("--max-snapshot-files", type=int, default=10_000)
    serve.add_argument(
        "--max-snapshot-bytes",
        type=int,
        default=GitCollector.DEFAULT_MAX_SNAPSHOT_BYTES,
    )
    serve.add_argument(
        "--max-total-snapshot-bytes",
        type=int,
        default=50 * 1024 * 1024 * 1024,
        help="hard quota for all managed source archives (default: 50 GiB)",
    )
    serve.add_argument("--worker-queue-size", type=int, default=256)
    serve.add_argument(
        "--bundle-worker-count",
        type=int,
        default=4,
        help="parallel direct-bundle grading workers (default: 4)",
    )
    serve.add_argument("--recovery-interval", type=float, default=10.0)
    serve.add_argument(
        "--max-bundle-compressed-bytes",
        type=int,
        default=DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES,
        help="maximum starter/submission bundle size (default: 25 MiB)",
    )
    serve.add_argument(
        "--max-bundle-expanded-bytes",
        type=int,
        default=DEFAULT_MAX_BUNDLE_EXPANDED_BYTES,
        help="maximum expanded bundle payload (default: 100 MiB)",
    )
    serve.add_argument(
        "--max-bundle-files",
        type=int,
        default=DEFAULT_MAX_BUNDLE_FILES,
        help="maximum files/directories per bundle (default: 5000)",
    )
    serve.add_argument("--max-outstanding-per-student", type=int, default=3)
    serve.add_argument("--max-daily-submissions-per-student", type=int, default=50)
    serve.add_argument("--max-active-sessions-per-student", type=int, default=5)
    serve.add_argument("--max-daily-session-issuances-per-student", type=int, default=20)
    serve.add_argument("--max-retained-sessions-per-student", type=int, default=1_000)
    serve.add_argument("--max-refresh-rotations-per-session", type=int, default=2_048)
    serve.add_argument("--max-activation-attempts", type=int, default=5)
    serve.add_argument(
        "--max-concurrent-password-verifications",
        type=int,
        default=4,
        help="bound concurrent password hashing work (default: 4)",
    )
    serve.add_argument("--session-list-limit", type=int, default=50)
    serve.add_argument(
        "--auth-history-retention-seconds",
        type=int,
        default=30 * 24 * 60 * 60,
    )
    serve.add_argument(
        "--github-client-id",
        default=os.environ.get("AUTOGRADE_GITHUB_CLIENT_ID"),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        if args.pilot_config is not None:
            pilot_config = load_pilot_config(args.pilot_config)
            apply_pilot_config(args, pilot_config, argv=raw_argv)
        if args.command == "course" and args.course_command == "list":
            course_key = (
                args.course_key.strip()
                if isinstance(args.course_key, str) and args.course_key.strip()
                else ""
            )
        elif args.command == "course" and args.course_command == "show":
            selected = args.managed_course_key or args.course_key
            if not isinstance(selected, str) or not selected.strip():
                parser.error(
                    "course show requires COURSE_KEY or --course-key/AUTOGRADE_COURSE_KEY"
                )
            course_key = selected.strip()
        else:
            course_key = _course_key(args, parser)
        paths = AppPaths.from_value(args.data_root).ensure()
        state = PlatformStateStore(paths.database)
        secret = create_or_load_auth_secret(paths.platform_auth_secret)
        instructor_token = create_or_load_instructor_token(
            paths.platform_instructor_token
        )
        result = _dispatch(
            args,
            paths,
            state,
            secret,
            instructor_token,
            course_key,
        )
        if result is not None:
            _print_json({"ok": True, "result": result})
        return 0
    except RepositoryPreflightFailed as exc:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "repository_preflight_failed",
                    "message": "one or more repositories failed preflight",
                },
                "result": exc.report.as_dict(),
            },
            stream=sys.stderr,
        )
        return 1
    except RunnerImageAvailabilityError as exc:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": _safe_cli_message(exc),
                },
            },
            stream=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        _print_json({"ok": False, "error": {"code": "interrupted", "message": "interrupted"}}, stream=sys.stderr)
        return 130
    except (
        ValueError,
        OSError,
        BundleError,
        PlatformAuthError,
        PlatformAPIError,
        PlatformStateError,
        GitCollectorError,
        GitHubAppError,
        GradingError,
        WorkspaceError,
    ) as exc:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": _error_code(exc),
                    "message": _safe_cli_message(exc),
                },
            },
            stream=sys.stderr,
        )
        return 1


def _ensure_roster_student(
    state: PlatformStateStore,
    *,
    student_key: str,
    github_user_id: int | None,
    github_login: str | None,
):
    has_github_id = github_user_id is not None
    has_github_login = github_login is not None
    if has_github_id != has_github_login:
        raise ValueError(
            "github_user_id and github_login must be provided together"
        )
    identity_kind = (
        StudentIdentityKind.GITHUB
        if has_github_id
        else StudentIdentityKind.LOCAL
    )
    expected_subject = (
        f"github:{github_user_id}" if has_github_id else f"local:{student_key}"
    )
    try:
        existing = state.get_student_by_key(student_key)
    except PlatformNotFound:
        existing = None
    if existing is not None:
        differs = (
            existing.identity_kind != identity_kind
            or existing.auth_subject != expected_subject
        )
        if identity_kind == StudentIdentityKind.GITHUB:
            differs = differs or (
                existing.github_user_id != github_user_id
                or existing.github_login != github_login
            )
        if differs:
            raise PlatformConflict(
                "global student identity differs; an identity administrator must update it"
            )
        return existing
    if identity_kind == StudentIdentityKind.GITHUB:
        assert github_user_id is not None and github_login is not None
        return state.upsert_student(
            student_key=student_key,
            auth_subject=expected_subject,
            github_user_id=github_user_id,
            github_login=github_login,
            active=True,
        )
    return state.upsert_local_student(
        student_key=student_key,
        auth_subject=expected_subject,
        active=True,
    )


def _read_roster_csv(
    path: Path,
    *,
    state: PlatformStateStore,
    course_key: str,
    replace_passwords: bool = False,
) -> list[dict[str, Any]]:
    """Parse and validate every roster row before the first database write."""

    source = path.expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValueError("roster CSV is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("roster CSV must be one regular file")
        if info.st_size > 4 * 1024 * 1024:
            raise ValueError("roster CSV exceeds the 4 MiB limit")
        with os.fdopen(
            descriptor, "r", encoding="utf-8-sig", newline=""
        ) as stream:
            descriptor = -1
            reader = csv.DictReader(stream, skipinitialspace=False)
            fields = reader.fieldnames
            if fields is None:
                raise ValueError("roster CSV must include a header")
            if len(fields) != len(set(fields)):
                raise ValueError("roster CSV has duplicate columns")
            allowed = {
                "student_key",
                "github_user_id",
                "github_login",
                "active",
                "password",
            }
            if "student_key" not in fields or set(fields) - allowed:
                raise ValueError(
                    "roster CSV columns must be student_key and optional "
                    "github_user_id, github_login, active, password"
                )
            if ("github_user_id" in fields) != ("github_login" in fields):
                raise ValueError(
                    "roster CSV must include both GitHub columns or neither"
                )
            has_password_column = "password" in fields
            if has_password_column and os.name == "posix" and (
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError(
                    "roster CSV containing passwords must be owned by the current "
                    "user and mode 0600"
                )
            raw_rows = list(reader)
    except UnicodeError as exc:
        raise ValueError("roster CSV must be UTF-8") from exc
    except csv.Error as exc:
        raise ValueError("roster CSV is malformed") from exc
    except OSError as exc:
        raise ValueError("cannot read roster CSV") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not raw_rows:
        raise ValueError("roster CSV must contain at least one student")
    if len(raw_rows) > 10_000:
        raise ValueError("roster CSV exceeds the 10000-student limit")

    parsed: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_github_ids: set[int] = set()
    seen_passwords: set[str] = set()
    for line_number, row in enumerate(raw_rows, start=2):
        if None in row:
            raise ValueError(f"roster CSV row {line_number} has extra fields")
        student_key = (row.get("student_key") or "").strip()
        if not student_key:
            raise ValueError(f"roster CSV row {line_number} has no student_key")
        if student_key in seen_keys:
            raise ValueError(
                f"roster CSV row {line_number} duplicates student_key"
            )
        seen_keys.add(student_key)

        raw_id = (row.get("github_user_id") or "").strip()
        github_login = (row.get("github_login") or "").strip() or None
        github_user_id: int | None = None
        if raw_id:
            try:
                github_user_id = int(raw_id, 10)
            except ValueError as exc:
                raise ValueError(
                    f"roster CSV row {line_number} has invalid github_user_id"
                ) from exc
            if github_user_id <= 0:
                raise ValueError(
                    f"roster CSV row {line_number} has invalid github_user_id"
                )
        if (github_user_id is None) != (github_login is None):
            raise ValueError(
                f"roster CSV row {line_number} must provide both GitHub fields or neither"
            )
        if github_user_id is not None:
            if github_user_id in seen_github_ids:
                raise ValueError(
                    f"roster CSV row {line_number} duplicates github_user_id"
                )
            seen_github_ids.add(github_user_id)

        raw_active = (row.get("active") or "true").strip().lower()
        if raw_active in {"true", "1", "yes"}:
            active = True
        elif raw_active in {"false", "0", "no"}:
            active = False
        else:
            raise ValueError(f"roster CSV row {line_number} has invalid active")

        password: str | None = None
        if has_password_column:
            raw_password = row.get("password") or ""
            if active and not raw_password:
                raise ValueError(
                    f"roster CSV row {line_number} has no password for an active "
                    "enrollment"
                )
            if not active and raw_password:
                raise ValueError(
                    f"roster CSV row {line_number} must leave password blank for an "
                    "inactive enrollment"
                )
            if raw_password:
                try:
                    password = validate_student_password(raw_password)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"roster CSV row {line_number} has invalid password: {exc}"
                    ) from None
                if password in seen_passwords:
                    raise ValueError(
                        f"roster CSV row {line_number} duplicates another password"
                    )
                seen_passwords.add(password)

        expected_subject = (
            f"github:{github_user_id}"
            if github_user_id is not None
            else f"local:{student_key}"
        )
        try:
            subject_owner = state.get_student_by_subject(expected_subject)
        except PlatformNotFound:
            subject_owner = None
        if subject_owner is not None and subject_owner.student_key != student_key:
            raise PlatformConflict(
                f"roster CSV row {line_number} uses an identity owned by another student"
            )
        try:
            existing = state.get_student_by_key(student_key)
        except PlatformNotFound:
            existing = None
        if existing is not None:
            expected_kind = (
                StudentIdentityKind.GITHUB
                if github_user_id is not None
                else StudentIdentityKind.LOCAL
            )
            if (
                existing.identity_kind != expected_kind
                or existing.auth_subject != expected_subject
                or (
                    expected_kind == StudentIdentityKind.GITHUB
                    and (
                        existing.github_user_id != github_user_id
                        or existing.github_login != github_login
                    )
                )
            ):
                raise PlatformConflict(
                    f"roster CSV row {line_number} conflicts with an existing student"
                )
        password_action: str | None = None
        current_password = None
        if active and password is not None:
            if existing is not None:
                current_password = state.find_student_password_credential(
                    student_key=student_key,
                    course_key=course_key,
                )
            if current_password is None:
                password_action = "set"
            elif current_password.password_hash.startswith(
                "scrypt$v2$"
            ) and verify_student_password(password, current_password.password_hash):
                password_action = "unchanged"
            elif replace_passwords:
                password_action = "replace"
            else:
                raise PlatformConflict(
                    f"roster CSV row {line_number} password differs from the "
                    "existing credential; pass --replace-passwords to replace it"
                )
        parsed.append(
            {
                "student_key": student_key,
                "github_user_id": github_user_id,
                "github_login": github_login,
                "active": active,
                "password": password,
                "password_action": password_action,
                "expected_password_hash": (
                    current_password.password_hash
                    if active and password is not None and current_password is not None
                    else None
                ),
            }
        )
    return parsed


def _dispatch(
    args: argparse.Namespace,
    paths: AppPaths,
    state: PlatformStateStore,
    secret: bytes,
    instructor_token: str,
    course_key: str,
) -> Optional[Mapping[str, Any]]:
    if args.command == "course" and args.course_command == "list":
        courses = state.list_course_summaries(limit=args.limit)
        return {"count": len(courses), "courses": courses}
    if args.command == "course" and args.course_command == "show":
        summary = dict(state.get_course_summary(course_key=course_key))
        students = state.list_course_student_summaries(course_key=course_key)
        assignments = state.list_course_assignment_summaries(course_key=course_key)
        return {
            **summary,
            "students": students,
            "assignment_details": assignments,
        }
    if args.command == "init":
        return {
            "course_key": course_key,
            "database": str(paths.database),
            "platform_schema_version": state.schema_version(),
            "instructor_token_file": str(paths.platform_instructor_token),
        }
    if args.command == "student" and args.student_command == "add":
        has_github_id = args.github_user_id is not None
        has_github_login = args.github_login is not None
        if has_github_id != has_github_login:
            raise ValueError(
                "--github-user-id and --github-login must be provided together"
            )
        student = _ensure_roster_student(
            state,
            student_key=args.student_key,
            github_user_id=args.github_user_id,
            github_login=args.github_login,
        )
        enrollment = state.upsert_enrollment(
            student_id=student.id,
            course_key=course_key,
            active=not args.inactive,
        )
        return {
            "student_key": student.student_key,
            "identity": {"kind": student.identity_kind.value},
            "github_user_id": (
                student.github_user_id
                if student.identity_kind == StudentIdentityKind.GITHUB
                else None
            ),
            "github_login": (
                student.github_login
                if student.identity_kind == StudentIdentityKind.GITHUB
                else None
            ),
            "course_key": enrollment.course_key,
            "active": student.active and enrollment.active,
        }
    if args.command == "student" and args.student_command == "list":
        students = state.list_course_student_summaries(
            course_key=course_key, limit=args.limit
        )
        return {"course_key": course_key, "count": len(students), "students": students}
    if args.command == "student" and args.student_command == "show":
        return state.get_course_student_summary(
            course_key=course_key, student_key=args.student_key
        )
    if args.command == "student" and args.student_command == "password-set":
        service = StudentPlatformService(
            state=state,
            server_secret=secret,
            course_key=course_key,
            public_base_url=args.public_base_url,
        )
        return service.set_student_password(
            student_key=args.student_key,
            password=_read_student_password(args.password_file),
        )
    if args.command == "student" and args.student_command == "import":
        rows = _read_roster_csv(
            args.csv_file,
            state=state,
            course_key=course_key,
            replace_passwords=args.replace_passwords,
        )
        entries = []
        for row in rows:
            github_user_id = row["github_user_id"]
            identity_kind = (
                StudentIdentityKind.GITHUB
                if github_user_id is not None
                else StudentIdentityKind.LOCAL
            )
            password = row["password"]
            new_password_hash = (
                hash_student_password(password)
                if row["password_action"] in {"set", "replace"}
                and password is not None
                else None
            )
            entries.append(
                CourseRosterImportEntry(
                    student_key=row["student_key"],
                    auth_subject=(
                        f"github:{github_user_id}"
                        if github_user_id is not None
                        else f"local:{row['student_key']}"
                    ),
                    identity_kind=identity_kind,
                    github_user_id=github_user_id,
                    github_login=row["github_login"],
                    active=row["active"],
                    password_managed=(row["password_action"] is not None),
                    expected_password_hash=row["expected_password_hash"],
                    new_password_hash=new_password_hash,
                )
            )
        imported = state.import_course_roster(
            course_key=course_key,
            entries=entries,
        )
        return {
            "course_key": course_key,
            "count": len(imported),
            "local_count": sum(
                student.identity_kind == StudentIdentityKind.LOCAL
                for student, _ in imported
            ),
            "github_count": sum(
                student.identity_kind == StudentIdentityKind.GITHUB
                for student, _ in imported
            ),
            "atomic": True,
            "validation": "all_rows_before_apply",
        }
    if args.command == "assignment":
        if args.assignment_command == "add":
            return _add_assignment(args, paths, state, course_key)
        if args.assignment_command == "bundle-add":
            return _add_bundle_assignment(args, paths, state, course_key)
        if args.assignment_command == "bundle-list":
            assignments = state.list_operator_bundle_assignments(
                course_key=course_key,
                ready_only=args.ready_only,
                active_only=not args.include_inactive,
                limit=args.limit,
            )
            return {
                "course_key": course_key,
                "count": len(assignments),
                "assignments": [
                    _bundle_assignment_summary(item) for item in assignments
                ],
            }
        if args.assignment_command == "bundle-ready":
            assignment = state.set_bundle_assignment_availability(
                args.assignment_id,
                course_key=course_key,
                ready=False,
            )
            _validate_bundle_assignment(paths, assignment)
            _check_runner_images(args, (assignment.runner_image,))
            assignment = state.set_bundle_assignment_availability(
                args.assignment_id,
                course_key=course_key,
                ready=True,
            )
            return _bundle_assignment_summary(assignment)
        if args.assignment_command == "bundle-hide":
            assignment = state.set_bundle_assignment_availability(
                args.assignment_id,
                course_key=course_key,
                ready=False,
            )
            return _bundle_assignment_summary(assignment)
        if args.assignment_command == "preflight":
            report = _repository_preflight(
                args,
                paths,
                state,
                course_key=course_key,
                ready_only=args.ready_only,
                jobs=args.jobs,
                git_timeout=args.git_timeout,
            )
            if not report.go:
                raise RepositoryPreflightFailed(report)
            return report.as_dict()
        if args.assignment_command == "ready":
            assignment = state.set_assignment_availability(
                args.assignment_id,
                course_key=course_key,
                ready=False,
            )
            report = _preflight_registered_assignments(
                args,
                paths,
                (assignment,),
                jobs=1,
                git_timeout=args.git_timeout,
            )
            if not report.go:
                raise RepositoryPreflightFailed(report)
            _check_runner_images(args, (assignment.runner_image,))
            assignment = state.set_assignment_availability(
                args.assignment_id,
                course_key=course_key,
                ready=True,
            )
            return {
                "assignment_id": assignment.assignment_id,
                "ready": assignment.ready,
                "active": assignment.active,
            }
        assignment = state.set_assignment_availability(
            args.assignment_id,
            course_key=course_key,
            ready=False,
        )
        return {
            "assignment_id": assignment.assignment_id,
            "ready": assignment.ready,
            "active": assignment.active,
        }
    if args.command == "auth":
        service = StudentPlatformService(
            state=state,
            server_secret=secret,
            course_key=course_key,
            public_base_url=args.public_base_url,
            external_access_mode=getattr(
                args, "external_access_mode", "disabled"
            ),
        )
        if args.auth_command == "issue":
            lifetime_seconds = args.expires_in_hours * 60 * 60
            if args.output is not None:
                return _issue_student_activation_to_file(
                    service,
                    student_key=args.student_key,
                    lifetime_seconds=lifetime_seconds,
                    path=args.output,
                )
            return dict(
                service.issue_student_activation(
                    student_key=args.student_key,
                    lifetime_seconds=lifetime_seconds,
                )
            )
        if args.auth_command == "revoke":
            return {
                "student_key": args.student_key,
                "course_key": course_key,
                "revoked": service.revoke_student_activation(
                    student_key=args.student_key
                ),
            }
        if args.auth_command == "sessions":
            if args.session_command == "list":
                return service.list_student_sessions(
                    student_key=args.student_key,
                    limit=args.limit,
                )
            if args.session_command == "revoke":
                return service.revoke_student_session(
                    student_key=args.student_key,
                    session_id=args.session_id,
                )
            return service.reset_student_sessions(student_key=args.student_key)
        service.approve_user_code(
            user_code=args.user_code,
            github_user_id=args.github_user_id,
        )
        return {"approved": True, "github_user_id": args.github_user_id}
    if args.command == "submission" and args.submission_command == "list":
        submissions = state.list_operator_submissions(
            course_key=course_key,
            student_key=args.student_key,
            assignment_key=args.assignment_key,
            state=args.state,
            limit=args.limit,
        )
        return {
            "course_key": course_key,
            "count": len(submissions),
            "submissions": [
                _operator_submission_summary(item) for item in submissions
            ],
        }
    if args.command == "submission" and args.submission_command == "show":
        if args.submission_id.startswith("bsub_"):
            request = _get_course_bundle_submission(
                state, args.submission_id, course_key=course_key
            )
            return _bundle_submission_detail(state, request)
        return _operator_submission_detail(
            state.get_operator_submission(
                args.submission_id,
                course_key=course_key,
            )
        )
    if args.command == "submission" and args.submission_command == "publish":
        if args.submission_id.startswith("bsub_"):
            _get_course_bundle_submission(
                state, args.submission_id, course_key=course_key
            )
            request, result = state.publish_bundle_result(args.submission_id)
            return {
                "submission_id": request.submission_id,
                "delivery_mode": "bundle",
                "state": request.state.value,
                "result_id": result.result_id,
            }
        state.get_operator_submission(args.submission_id, course_key=course_key)
        request, result = state.publish_result(args.submission_id)
        return {
            "submission_id": request.submission_id,
            "state": request.state.value,
            "result_id": result.result_id,
        }
    if args.command == "submission" and args.submission_command == "process":
        if args.submission_id.startswith("bsub_"):
            submission = _get_course_bundle_submission(
                state, args.submission_id, course_key=course_key
            )
            assignment = state.get_bundle_assignment(submission.assignment_id)
            _check_runner_images(args, (assignment.runner_image,))
            with _exclusive_course_service_lock(paths, course_key):
                grader = _new_grader(args, paths, course_key)
                try:
                    if isinstance(grader, ContainerGrader):
                        grader.reconcile_orphans()
                    processor = _bundle_processor(
                        paths,
                        state,
                        course_key=course_key,
                        grader=grader,
                    )
                    outcome = processor.process(args.submission_id)
                finally:
                    grader.close()
            return {
                "submission_id": outcome.submission_id,
                "delivery_mode": "bundle",
                "state": outcome.state.value,
                "changed": outcome.changed,
            }
        submission = state.get_operator_submission(
            args.submission_id, course_key=course_key
        )
        assignment = state.get_assignment(submission.assignment_id)
        _check_runner_images(args, (assignment.runner_image,))
        with _exclusive_course_service_lock(paths, course_key):
            grader = _new_grader(args, paths, course_key)
            try:
                if isinstance(grader, ContainerGrader):
                    grader.reconcile_orphans()
                processor = _processor(
                    paths,
                    state,
                    course_key=course_key,
                    runtime=args.grading_runtime,
                    grader=grader,
                )
                outcome = processor.process(args.submission_id)
            finally:
                grader.close()
        return {
            "submission_id": outcome.submission_id,
            "state": outcome.state.value,
            "changed": outcome.changed,
        }
    if args.command == "serve":
        _serve(args, paths, state, secret, instructor_token, course_key)
        return None
    raise RuntimeError("unhandled command")


def _operator_submission_summary(
    submission: OperatorSubmissionView,
) -> Mapping[str, Any]:
    return {
        "submission_id": submission.submission_id,
        "student_key": submission.student_key,
        "assignment_id": submission.assignment_id,
        "assignment_key": submission.assignment_key,
        "release_id": submission.release_id,
        "state": submission.state.value,
        "requested_sha": submission.requested_sha,
        "pull_request_number": submission.pull_request_number,
        "received_at": submission.received_at,
        "updated_at": submission.updated_at,
        "failure_code": submission.failure_code,
    }


def _operator_submission_detail(
    submission: OperatorSubmissionView,
) -> Mapping[str, Any]:
    return {
        **_operator_submission_summary(submission),
        "course_key": submission.course_key,
        "github_login": submission.github_login,
        "repository": {
            "github_repository_id": submission.github_repository_id,
            "owner": submission.repository_owner,
            "name": submission.repository_name,
        },
        "receipt": (
            None
            if submission.receipt_id is None
            else {
                "receipt_id": submission.receipt_id,
                "commit_sha": submission.commit_sha,
                "source_digest": submission.source_digest,
                "accepted_at": submission.accepted_at,
            }
        ),
        "result": (
            None
            if submission.result_id is None
            else {
                "result_id": submission.result_id,
                "score": submission.score,
                "max_score": submission.max_score,
                "created_at": submission.result_created_at,
                "published_at": submission.published_at,
            }
        ),
    }


def _get_course_bundle_submission(
    state: PlatformStateStore,
    submission_id: str,
    *,
    course_key: str,
):
    request = state.get_bundle_submission(submission_id)
    assignment = state.get_bundle_assignment(request.assignment_id)
    if assignment.course_key != course_key:
        raise PlatformNotFound("submission was not found in this course")
    return request


def _bundle_submission_detail(state: PlatformStateStore, request) -> Mapping[str, Any]:
    receipt = state.get_bundle_receipt(request.submission_id)
    return {
        "submission_id": request.submission_id,
        "delivery_mode": "bundle",
        "assignment_id": request.assignment_id,
        "state": request.state.value,
        "source_digest": request.source_digest,
        "source_size_bytes": request.source_size_bytes,
        "received_at": request.received_at,
        "updated_at": request.updated_at,
        "failure_code": request.failure_code,
        "receipt": {
            "receipt_id": receipt.receipt_id,
            "assignment_key": receipt.assignment_key,
            "release_id": receipt.release_id,
            "accepted_at": receipt.accepted_at,
        },
    }


def _add_assignment(
    args: argparse.Namespace,
    paths: AppPaths,
    state: PlatformStateStore,
    course_key: str,
) -> Mapping[str, Any]:
    runner_reference = _assignment_runner_reference(args)
    student = state.get_student_by_key(args.student_key)
    builder = WorkspaceBuilder(paths.workspaces)
    assessment = builder.digest_instructor_tree(args.assessment, label="assessment")
    data = (
        builder.digest_instructor_tree(args.data, label="data")
        if args.data is not None
        else None
    )
    assignment = state.register_assignment(
        assignment_id=args.assignment_id or new_public_id("asn"),
        student_id=student.id,
        course_key=course_key,
        assignment_key=args.assignment_key,
        release_id=args.release_id,
        github_repository_id=args.github_repository_id,
        repository_owner=args.repository_owner,
        repository_name=args.repository_name,
        clone_url=args.clone_url,
        submission_mode="branch",
        target_ref=args.target_ref,
        assignment_path=args.assignment_path,
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
        # New releases are never exposed before remote identity/tree preflight.
        ready=False,
    )
    assignment = state.set_assignment_availability(
        assignment.assignment_id,
        course_key=course_key,
        ready=False,
    )
    if not args.not_ready:
        report = _preflight_registered_assignments(
            args,
            paths,
            (assignment,),
            jobs=1,
            git_timeout=args.git_timeout,
        )
        if not report.go:
            raise RepositoryPreflightFailed(report)
        _check_runner_images(args, (assignment.runner_image,))
        assignment = state.set_assignment_availability(
            assignment.assignment_id,
            course_key=course_key,
            ready=True,
        )
    return {
        "assignment_id": assignment.assignment_id,
        "assignment_key": assignment.assignment_key,
        "student_key": student.student_key,
        "repository": f"{assignment.repository_owner}/{assignment.repository_name}",
        "github_repository_id": assignment.github_repository_id,
        "assessment_digest": assignment.assessment_digest,
        "dataset_digest": assignment.dataset_digest,
        "runner_image": assignment.runner_image,
        "grading_runtime": (
            "pilot-local"
            if assignment.runner_image == PILOT_LOCAL_RUNNER
            else "container"
        ),
        "ready": assignment.ready,
    }


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
    assignment = state.set_bundle_assignment_availability(
        assignment.assignment_id,
        course_key=course_key,
        ready=False,
    )
    if not args.not_ready:
        _validate_bundle_assignment(paths, assignment, store=store)
        _check_runner_images(args, (assignment.runner_image,))
        assignment = state.set_bundle_assignment_availability(
            assignment.assignment_id,
            course_key=course_key,
            ready=True,
        )
    return _bundle_assignment_summary(assignment)


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


def _github_app_provider(
    args: argparse.Namespace,
) -> GitHubAppInstallationTokenProvider | None:
    app_id = args.github_app_id
    installation_id = args.github_app_installation_id
    private_key = args.github_app_private_key
    configured = (
        app_id is not None,
        installation_id is not None,
        private_key is not None,
    )
    if any(configured) and not all(configured):
        raise ValueError(
            "GitHub App ID, installation ID, and private key must be configured together"
        )
    if not any(configured):
        return None

    askpass_path = args.git_askpass_path
    if askpass_path is None:
        askpass_path = Path(sys.executable).resolve().parent / "autograde-git-askpass"
    signer = OpenSSLRS256Signer(Path(private_key))
    return GitHubAppInstallationTokenProvider(
        app_id=app_id,
        installation_id=installation_id,
        signer=signer,
        askpass_path=askpass_path,
        api_base_url=args.github_api_base_url,
    )


def _platform_collector(
    paths: AppPaths,
    *,
    command_timeout: float,
    identity_provider: GitHubAppInstallationTokenProvider | None,
    max_files: int = GitCollector.DEFAULT_MAX_FILES,
    max_snapshot_bytes: int = GitCollector.DEFAULT_MAX_SNAPSHOT_BYTES,
    max_total_snapshot_bytes: int | None = None,
) -> GitCollector:
    return GitCollector(
        paths.cache,
        paths.snapshots,
        command_timeout=command_timeout,
        max_files=max_files,
        max_snapshot_bytes=max_snapshot_bytes,
        max_total_snapshot_bytes=max_total_snapshot_bytes,
        git_environment_provider=(
            identity_provider.git_environment
            if identity_provider is not None
            else None
        ),
    )


def _repository_preflight(
    args: argparse.Namespace,
    paths: AppPaths,
    state: PlatformStateStore,
    *,
    course_key: str,
    ready_only: bool,
    jobs: int,
    git_timeout: float,
    collector: GitCollector | None = None,
    identity_provider: GitHubAppInstallationTokenProvider | None = None,
) -> RepositoryPreflightReport:
    assignments = state.list_operator_assignments(
        course_key=course_key,
        ready_only=ready_only,
        limit=10_000,
    )
    return _preflight_registered_assignments(
        args,
        paths,
        assignments,
        jobs=jobs,
        git_timeout=git_timeout,
        collector=collector,
        identity_provider=identity_provider,
    )


def _preflight_registered_assignments(
    args: argparse.Namespace,
    paths: AppPaths,
    assignments: Sequence[PlatformAssignment],
    *,
    jobs: int,
    git_timeout: float,
    collector: GitCollector | None = None,
    identity_provider: GitHubAppInstallationTokenProvider | None = None,
) -> RepositoryPreflightReport:
    provider = identity_provider
    if provider is None:
        provider = _github_app_provider(args)
    active_collector = collector or _platform_collector(
        paths,
        command_timeout=git_timeout,
        identity_provider=provider,
        max_files=getattr(args, "max_snapshot_files", GitCollector.DEFAULT_MAX_FILES),
        max_snapshot_bytes=getattr(
            args,
            "max_snapshot_bytes",
            GitCollector.DEFAULT_MAX_SNAPSHOT_BYTES,
        ),
        max_total_snapshot_bytes=getattr(args, "max_total_snapshot_bytes", None),
    )
    return preflight_assignments(
        assignments,
        collector=active_collector,
        identity_provider=provider,
        instructor_input_digester=_instructor_input_digester(paths),
        jobs=jobs,
    )


def _instructor_input_digester(paths: AppPaths):
    builder = WorkspaceBuilder(paths.workspaces)

    def digest(source_path: str, *, label: str) -> str:
        return builder.digest_instructor_tree(source_path, label=label).sha256

    return digest


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


def _processor(
    paths: AppPaths,
    state: PlatformStateStore,
    *,
    course_key: str,
    runtime: str,
    grader: Optional[Grader] = None,
    collector: Optional[GitCollector] = None,
) -> SubmissionProcessor:
    return SubmissionProcessor(
        state=state,
        course_key=course_key,
        collector=(
            collector
            if collector is not None
            else GitCollector(paths.cache, paths.snapshots)
        ),
        workspace_builder=WorkspaceBuilder(paths.workspaces),
        grader=(
            grader
            if grader is not None
            else (
                PilotLocalGrader()
                if runtime == "pilot-local"
                else ContainerGrader(runtime=runtime)
            )
        ),
    )


def _bundle_processor(
    paths: AppPaths,
    state: PlatformStateStore,
    *,
    course_key: str,
    grader: Grader,
) -> BundleSubmissionProcessor:
    return BundleSubmissionProcessor(
        state=state,
        course_key=course_key,
        workspace_builder=WorkspaceBuilder(paths.workspaces),
        grader=grader,
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


@contextmanager
def _exclusive_course_service_lock(
    paths: AppPaths, course_key: str
) -> Iterator[None]:
    """Hold the per-data-root/course grader lifecycle lock until shutdown."""

    instance_label = _grader_instance_label(paths, course_key)
    lock_path = paths.root / f".autograde-platform-{instance_label}.lock"
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise PlatformStateError("cannot safely open the course service lock") from exc

    locked = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PlatformStateError("course service lock must be a private regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise PlatformStateError(
                    "another grading service or manual processor is active for this course"
                ) from exc
            raise PlatformStateError("cannot acquire the course service lock") from exc
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _serve(
    args: argparse.Namespace,
    paths: AppPaths,
    state: PlatformStateStore,
    secret: bytes,
    instructor_token: str,
    course_key: str,
) -> None:
    with _exclusive_course_service_lock(paths, course_key):
        _serve_locked(
            args,
            paths,
            state,
            secret,
            instructor_token,
            course_key,
        )


def _serve_locked(
    args: argparse.Namespace,
    paths: AppPaths,
    state: PlatformStateStore,
    secret: bytes,
    instructor_token: str,
    course_key: str,
) -> None:
    external_access_mode = getattr(args, "external_access_mode", "disabled")
    client_id = args.github_client_id
    client_secret = os.environ.get("AUTOGRADE_GITHUB_CLIENT_SECRET")
    github_oauth = None
    if bool(client_id) != bool(client_secret):
        raise ValueError(
            "AUTOGRADE_GITHUB_CLIENT_ID and AUTOGRADE_GITHUB_CLIENT_SECRET must be configured together"
        )
    if client_id and client_secret:
        github_oauth = GitHubOAuthClient(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=f"{args.public_base_url.rstrip('/')}/oauth/github/callback",
        )

    github_app = _github_app_provider(args)
    collector = _platform_collector(
        paths,
        command_timeout=args.git_timeout,
        identity_provider=github_app,
        max_files=args.max_snapshot_files,
        max_snapshot_bytes=args.max_snapshot_bytes,
        max_total_snapshot_bytes=args.max_total_snapshot_bytes,
    )
    snapshot_store = collector.check_snapshot_store_quota_readiness()
    repository_preflight = _repository_preflight(
        args,
        paths,
        state,
        course_key=course_key,
        ready_only=True,
        jobs=args.repository_preflight_jobs,
        git_timeout=args.git_timeout,
        collector=collector,
        identity_provider=github_app,
    )
    if not repository_preflight.go:
        raise RepositoryPreflightFailed(repository_preflight)

    ready_assignments = state.list_operator_assignments(
        course_key=course_key,
        ready_only=True,
        active_only=True,
        limit=10_000,
    )
    bundle_store = _bundle_store(
        paths,
        max_compressed_bytes=args.max_bundle_compressed_bytes,
        max_expanded_bytes=args.max_bundle_expanded_bytes,
        max_files=args.max_bundle_files,
    )
    ready_bundle_assignments = state.list_operator_bundle_assignments(
        course_key=course_key,
        ready_only=True,
        active_only=True,
        limit=10_000,
    )
    for assignment in ready_bundle_assignments:
        _validate_bundle_assignment(paths, assignment, store=bundle_store)
    runner_images = _check_runner_images(
        args,
        tuple(assignment.runner_image for assignment in ready_assignments)
        + tuple(
            assignment.runner_image for assignment in ready_bundle_assignments
        ),
    )

    grader = _new_grader(args, paths, course_key)
    worker: Optional[SubmissionWorker] = None
    bundle_worker: Optional[BundleSubmissionWorker] = None
    server: Any = None
    server_thread: Optional[threading.Thread] = None
    server_failures: list[BaseException] = []
    server_finished = threading.Event()
    shutdown_requested = threading.Event()
    shutdown_signal: Optional[signal.Signals] = None
    previous_signal_handlers: dict[signal.Signals, Any] = {}
    worker_started = False
    bundle_worker_started = False

    def request_shutdown(signum: int, _frame: Any) -> None:
        nonlocal shutdown_signal
        shutdown_signal = signal.Signals(signum)
        shutdown_requested.set()

    def run_server() -> None:
        try:
            server.serve_forever()
        except BaseException as exc:
            emit_operator_event(
                "http_server_unexpected_exception",
                component="http",
                exception=exc,
            )
            server_failures.append(exc)
        finally:
            server_finished.set()

    def stop_before_serving() -> bool:
        if not shutdown_requested.is_set():
            return False
        if shutdown_signal == signal.SIGINT:
            raise KeyboardInterrupt
        return True

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_signal_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_shutdown)

        if isinstance(grader, ContainerGrader):
            grader.reconcile_orphans()
        if stop_before_serving():
            return
        processor = _processor(
            paths,
            state,
            course_key=course_key,
            runtime=args.grading_runtime,
            grader=grader,
            collector=collector,
        )
        worker = SubmissionWorker(
            processor,
            course_key=course_key,
            max_queue_size=args.worker_queue_size,
            recovery_interval_seconds=args.recovery_interval,
        )
        bundle_processor = _bundle_processor(
            paths,
            state,
            course_key=course_key,
            grader=grader,
        )
        bundle_worker = BundleSubmissionWorker(
            bundle_processor,
            course_key=course_key,
            worker_count=args.bundle_worker_count,
            max_queue_size=args.worker_queue_size,
            recovery_interval_seconds=args.recovery_interval,
        )
        service = StudentPlatformService(
            state=state,
            server_secret=secret,
            course_key=course_key,
            public_base_url=args.public_base_url,
            external_access_mode=external_access_mode,
            github_oauth=github_oauth,
            submission_pinner=GitSubmissionPinner(collector),
            submission_policy=SubmissionPolicy(
                max_outstanding_per_student=args.max_outstanding_per_student,
                max_daily_per_student=args.max_daily_submissions_per_student,
            ),
            token_policy=TokenPolicy(
                max_active_sessions=args.max_active_sessions_per_student,
                max_daily_session_issuances=(
                    args.max_daily_session_issuances_per_student
                ),
                max_retained_sessions=args.max_retained_sessions_per_student,
                max_refresh_rotations=args.max_refresh_rotations_per_session,
                max_activation_attempts=args.max_activation_attempts,
                max_concurrent_password_verifications=(
                    args.max_concurrent_password_verifications
                ),
                session_list_limit=args.session_list_limit,
                auth_history_retention_seconds=(
                    args.auth_history_retention_seconds
                ),
            ),
            notify_submission=worker.notify,
            bundle_store=bundle_store,
            notify_bundle_submission=bundle_worker.notify,
            instructor_token=instructor_token,
        )
        server = create_server(
            (args.listen, args.port),
            service,
            public_base_url=args.public_base_url,
            external_access_mode=external_access_mode,
            max_bundle_request_bytes=args.max_bundle_compressed_bytes,
            max_file_response_bytes=args.max_bundle_compressed_bytes,
        )
        if external_access_mode == "insecure-http":
            emit_operator_event(
                "insecure_http_external_access",
                component="http",
                level="warning",
            )
        if stop_before_serving():
            return
        worker.start()
        worker_started = True
        bundle_worker.start()
        bundle_worker_started = True
        _print_json(
            {
                "ok": True,
                "result": {
                    "listening": f"{args.listen}:{server.server_address[1]}",
                    "public_base_url": args.public_base_url,
                    "external_access_mode": external_access_mode,
                    "network_security": (
                        {
                            "encrypted": False,
                            "scope": "rfc1918_trusted_lan",
                            "instructor_dashboard_enabled": False,
                            "warning": (
                                "student credentials and submissions cross the trusted "
                                "LAN without transport encryption"
                            ),
                        }
                        if external_access_mode == "insecure-http"
                        else {
                            "encrypted": args.public_base_url.startswith("https://"),
                            "scope": "loopback_or_tls",
                            "instructor_dashboard_enabled": True,
                        }
                    ),
                    "course_key": course_key,
                    "delivery_modes": ["bundle", "git"],
                    "student_activation": True,
                    "github_oauth": github_oauth is not None,
                    "github_app_collection": github_app is not None,
                    "snapshot_store": {
                        "managed_bytes": snapshot_store.managed_bytes,
                        "max_bytes": snapshot_store.max_bytes,
                        "remaining_bytes": snapshot_store.remaining_bytes,
                    },
                    "repository_preflight": {
                        "count": len(repository_preflight.items),
                        "passed": repository_preflight.passed,
                        "failed": repository_preflight.failed,
                    },
                    "bundle_assignments": {
                        "ready": len(ready_bundle_assignments),
                        "worker_count": args.bundle_worker_count,
                        "max_compressed_bytes": args.max_bundle_compressed_bytes,
                        "max_expanded_bytes": args.max_bundle_expanded_bytes,
                        "max_files": args.max_bundle_files,
                    },
                    "instructor_dashboard": (
                        {
                            "enabled": False,
                            "reason": "disabled_over_insecure_http",
                        }
                        if external_access_mode == "insecure-http"
                        else {
                            "enabled": True,
                            "url": f"{args.public_base_url.rstrip('/')}/instructor",
                            "username": "instructor",
                            "token_file": str(paths.platform_instructor_token),
                        }
                    ),
                    "runner_images": {
                        "code": (
                            "not_applicable"
                            if args.grading_runtime == "pilot-local"
                            else "ok"
                        ),
                        "checked": len(runner_images),
                        "runtime": (
                            None
                            if args.grading_runtime == "pilot-local"
                            else args.grading_runtime
                        ),
                    },
                    "grading_runtime": args.grading_runtime,
                    "security": (
                        {
                            "classification": "trusted_code_only",
                            "sandboxed": False,
                            "warning": (
                                "pilot-local executes assessment and student code with "
                                "the service account's host filesystem and network access"
                            ),
                            "limits": {
                                "wall_time": True,
                                "output": True,
                                "cpu_per_process": True,
                                "file_size_per_file": True,
                                "memory_per_process": sys.platform != "darwin",
                                "process_tree_isolation": False,
                                "network_isolation": False,
                                "filesystem_isolation": False,
                            },
                        }
                        if args.grading_runtime == "pilot-local"
                        else {
                            "classification": "container_isolated",
                            "sandboxed": True,
                        }
                    ),
                },
            }
        )
        server_thread = threading.Thread(
            target=run_server,
            name="autograde-platform-http",
            daemon=True,
        )
        server_thread.start()
        while not server_finished.is_set() and not shutdown_requested.wait(0.2):
            pass
        if shutdown_requested.is_set() and not server_finished.is_set():
            # BaseServer.shutdown must run from a thread other than the one in
            # serve_forever.  The CLI thread is therefore the safe coordinator.
            server.shutdown()
        server_thread.join()
        if server_failures:
            raise PlatformStateError("HTTP server stopped unexpectedly")
    finally:
        try:
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
                server_thread.join()
            if server is not None:
                server.server_close()
        finally:
            worker_stopped = not worker_started
            bundle_worker_stopped = not bundle_worker_started
            try:
                if worker_started and worker is not None:
                    worker_stopped = worker.stop(timeout=30)
            finally:
                try:
                    if bundle_worker_started and bundle_worker is not None:
                        bundle_worker_stopped = bundle_worker.stop(timeout=30)
                finally:
                    grader.close()
                    if not worker_stopped and worker is not None:
                        worker.stop(timeout=5)
                    if not bundle_worker_stopped and bundle_worker is not None:
                        bundle_worker.stop(timeout=5)
                    for signum, previous_handler in previous_signal_handlers.items():
                        signal.signal(signum, previous_handler)

    if shutdown_signal == signal.SIGINT:
        raise KeyboardInterrupt


def _course_key(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    value = args.course_key
    if not isinstance(value, str) or not value.strip():
        parser.error("--course-key or AUTOGRADE_COURSE_KEY is required")
    return value.strip()


def _read_student_password(path: Optional[Path]) -> str:
    """Read a password without accepting it in command-line arguments."""

    if path is None:
        password = getpass.getpass("Autograde 전용 비밀번호(숫자 6자리): ")
        confirmation = getpass.getpass(
            "Autograde 전용 비밀번호 확인(숫자 6자리): "
        )
        if password != confirmation:
            raise ValueError("Autograde 전용 비밀번호 확인이 일치하지 않습니다")
        return validate_student_password(password)

    source = path.expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("password file must be one regular file")
        if info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
            raise ValueError("password file must be owned by the current user and mode 0600")
        raw = os.read(descriptor, 1_025)
        if len(raw) > 1_024:
            raise ValueError("password file exceeds the 1024-byte limit")
    finally:
        os.close(descriptor)
    try:
        password = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("password file must be UTF-8") from exc
    if password.endswith("\r\n"):
        password = password[:-2]
    elif password.endswith("\n"):
        password = password[:-1]
    return validate_student_password(password)


def _issue_student_activation_to_file(
    service: StudentPlatformService,
    *,
    student_key: str,
    lifetime_seconds: int,
    path: Path,
) -> Mapping[str, Any]:
    """Reserve a private file before issuing, then write the code exactly once."""

    target = path.expanduser().absolute()
    parent = target.parent
    if os.path.lexists(parent) and parent.is_symlink():
        raise OSError("activation code output parent must not be a symlink")
    if not parent.is_dir():
        raise OSError("activation code output parent must be an existing directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    complete = False
    issued_activation_id: Optional[str] = None
    try:
        result = dict(
            service.issue_student_activation(
                student_key=student_key,
                lifetime_seconds=lifetime_seconds,
            )
        )
        issued_activation_id = str(result["activation_id"])
        value = str(result.pop("activation_code"))
        payload = (value + "\n").encode("ascii")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("activation code output write was incomplete")
            offset += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        complete = True
    finally:
        try:
            if not complete and issued_activation_id is not None:
                try:
                    service.revoke_issued_student_activation(
                        activation_id=issued_activation_id
                    )
                except (PlatformAPIError, PlatformStateError, ValueError, OSError):
                    pass
        finally:
            try:
                os.close(descriptor)
            finally:
                if not complete:
                    try:
                        os.unlink(target)
                    except FileNotFoundError:
                        pass
    result["activation_code_file"] = str(target)
    return result


def _print_json(value: Mapping[str, Any], *, stream: Any = None) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False),
        file=sys.stdout if stream is None else stream,
        flush=True,
    )


def _error_code(exc: Exception) -> str:
    name = exc.__class__.__name__
    output = []
    for index, character in enumerate(name):
        if character.isupper() and index:
            output.append("_")
        output.append(character.lower())
    return "".join(output)


def _safe_cli_message(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return message[:512] or "operation failed"


if __name__ == "__main__":
    raise SystemExit(main())
