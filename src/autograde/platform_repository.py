"""Operator preflight for registered student repositories.

The check is deliberately read-only with respect to GitHub and grading state.
It verifies the configured numeric repository identity through a read-only
GitHub App installation, then fetches and inspects the configured submission
branch without publishing a submission snapshot.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .gitops import (
    AssignmentNotFoundError,
    GitCollectorError,
    RemoteBranchNotFoundError,
    SnapshotLimitError,
    SnapshotStoreQuotaError,
    UnsupportedGitLFSObjectError,
    UnsupportedGitSubmoduleError,
    UnsafePathError,
    UnsafeRepositoryError,
)
from .platform_github_app import GitHubAppError, GitHubRepositoryIdentity
from .platform_state import PlatformAssignment


class RepositoryTreePreflighter(Protocol):
    def preflight(
        self,
        repository_id: str | int,
        remote_url: str,
        *,
        assignment_subpath: str,
        target_ref: str,
    ) -> object: ...


class RepositoryIdentityProvider(Protocol):
    git_host: str

    def get_repository(
        self,
        *,
        owner: str,
        name: str,
    ) -> GitHubRepositoryIdentity: ...


class InstructorInputDigester(Protocol):
    def __call__(self, source_path: str, *, label: str) -> str: ...


@dataclass(frozen=True, slots=True)
class RepositoryPreflightItem:
    assignment_id: str
    assignment_key: str
    github_repository_id: int
    repository: str
    target_ref: str
    passed: bool
    code: str
    commit_sha: str | None = None

    def as_dict(self) -> Mapping[str, object]:
        return {
            "assignment_id": self.assignment_id,
            "assignment_key": self.assignment_key,
            "github_repository_id": self.github_repository_id,
            "repository": self.repository,
            "target_ref": self.target_ref,
            "passed": self.passed,
            "code": self.code,
            "commit_sha": self.commit_sha,
        }


@dataclass(frozen=True, slots=True)
class RepositoryPreflightReport:
    items: tuple[RepositoryPreflightItem, ...]

    @property
    def passed(self) -> int:
        return sum(item.passed for item in self.items)

    @property
    def failed(self) -> int:
        return len(self.items) - self.passed

    @property
    def go(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> Mapping[str, object]:
        return {
            "go": self.go,
            "count": len(self.items),
            "passed": self.passed,
            "failed": self.failed,
            "items": [item.as_dict() for item in self.items],
        }


class _PreflightIssue(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _InstructorInputObservation:
    state: str
    digest: str | None = None


def preflight_assignments(
    assignments: Sequence[PlatformAssignment],
    *,
    collector: RepositoryTreePreflighter,
    identity_provider: RepositoryIdentityProvider | None,
    instructor_input_digester: InstructorInputDigester | None = None,
    jobs: int = 4,
) -> RepositoryPreflightReport:
    """Check assignment repositories concurrently and return stable-order results."""

    if isinstance(jobs, bool) or not isinstance(jobs, int) or not 1 <= jobs <= 32:
        raise ValueError("jobs must be an integer between 1 and 32")
    normalized = tuple(assignments)
    if not normalized:
        return RepositoryPreflightReport(())
    instructor_inputs = _observe_unique_instructor_inputs(
        normalized,
        instructor_input_digester,
    )

    results: list[RepositoryPreflightItem | None] = [None] * len(normalized)
    with ThreadPoolExecutor(
        max_workers=min(jobs, len(normalized)),
        thread_name_prefix="repository-preflight",
    ) as executor:
        pending = {
            executor.submit(
                _preflight_one,
                assignment,
                collector=collector,
                identity_provider=identity_provider,
                instructor_inputs=instructor_inputs,
            ): index
            for index, assignment in enumerate(normalized)
        }
        for future in as_completed(pending):
            results[pending[future]] = future.result()

    return RepositoryPreflightReport(
        tuple(item for item in results if item is not None)
    )


def _preflight_one(
    assignment: PlatformAssignment,
    *,
    collector: RepositoryTreePreflighter,
    identity_provider: RepositoryIdentityProvider | None,
    instructor_inputs: Mapping[str, _InstructorInputObservation] | None,
) -> RepositoryPreflightItem:
    repository = f"{assignment.repository_owner}/{assignment.repository_name}"
    base = {
        "assignment_id": assignment.assignment_id,
        "assignment_key": assignment.assignment_key,
        "github_repository_id": assignment.github_repository_id,
        "repository": repository,
        "target_ref": assignment.target_ref,
    }
    try:
        if instructor_inputs is not None:
            _validate_assignment_instructor_inputs(assignment, instructor_inputs)
        parsed = urlsplit(assignment.clone_url)
        http_remote = parsed.scheme.casefold() in {"http", "https"}
        local_remote = (
            not parsed.scheme
            and not parsed.netloc
            and assignment.clone_url.startswith("/")
        )
        if not http_remote and not local_remote:
            # The built-in GitHub App credential path is HTTPS/askpass-only.
            # Do not silently treat SSH, git://, SCP-style, or relative remotes
            # as development-local and bypass numeric repository identity.
            raise _PreflightIssue("repository_clone_url_mismatch")
        if http_remote:
            if identity_provider is None:
                raise _PreflightIssue("github_app_not_configured")
            _validate_http_clone_url(
                assignment,
                expected_host=getattr(identity_provider, "git_host", parsed.hostname),
            )

        # Local repositories remain available for deterministic development
        # tests.  Every HTTP(S) production repository uses the App identity.
        if http_remote and identity_provider is not None:
            identity = identity_provider.get_repository(
                owner=assignment.repository_owner,
                name=assignment.repository_name,
            )
            if identity.repository_id != assignment.github_repository_id:
                raise _PreflightIssue("repository_identity_mismatch")
            if not identity.private:
                raise _PreflightIssue("repository_not_private")
            if identity.archived or identity.disabled:
                raise _PreflightIssue("repository_not_writable")

        fetched = collector.preflight(
            str(assignment.github_repository_id),
            assignment.clone_url,
            assignment_subpath=assignment.assignment_path,
            target_ref=assignment.target_ref,
        )
        commit_sha = getattr(fetched, "commit_sha", None)
        if commit_sha is None:
            # Keep the small protocol compatible with fetch-only test doubles.
            commit_sha = getattr(fetched, "new_sha", None)
        if not isinstance(commit_sha, str) or not commit_sha:
            raise _PreflightIssue("target_ref_missing")
        return RepositoryPreflightItem(
            **base,
            passed=True,
            code="ok",
            commit_sha=commit_sha,
        )
    except _PreflightIssue as exc:
        code = exc.code
    except GitHubAppError:
        code = "github_app_access_failed"
    except RemoteBranchNotFoundError:
        code = "target_ref_missing"
    except AssignmentNotFoundError:
        code = "assignment_path_missing"
    except UnsupportedGitSubmoduleError:
        code = "git_submodule_unsupported"
    except UnsupportedGitLFSObjectError:
        code = "git_lfs_object_unsupported"
    except (SnapshotLimitError, SnapshotStoreQuotaError):
        code = "repository_limit_exceeded"
    except (UnsafePathError, ValueError):
        code = "repository_configuration_invalid"
    except UnsafeRepositoryError:
        code = "repository_content_unsafe"
    except (GitCollectorError, OSError):
        code = "git_fetch_failed"
    except Exception:
        # A provider/collector implementation is an internal trust boundary.
        # Never leak its exception text into operator JSON or logs by default.
        code = "repository_preflight_failed"

    return RepositoryPreflightItem(
        **base,
        passed=False,
        code=code,
    )


def _observe_unique_instructor_inputs(
    assignments: Sequence[PlatformAssignment],
    digester: InstructorInputDigester | None,
) -> Mapping[str, _InstructorInputObservation] | None:
    if digester is None:
        return None
    observations: dict[str, _InstructorInputObservation] = {}
    for assignment in assignments:
        inputs = [(assignment.assessment_path, "assessment")]
        if assignment.data_path is not None:
            inputs.append((assignment.data_path, "data"))
        for source_path, label in inputs:
            key = _instructor_input_key(source_path)
            if key in observations:
                continue
            observations[key] = _observe_instructor_input(
                key,
                label=label,
                digester=digester,
            )
    return observations


def _observe_instructor_input(
    source_path: str,
    *,
    label: str,
    digester: InstructorInputDigester,
) -> _InstructorInputObservation:
    path = Path(source_path)
    try:
        if not os.path.lexists(path):
            return _InstructorInputObservation("missing")
        if path.is_symlink() or not path.is_dir():
            return _InstructorInputObservation("unsafe")
        digest = _normalize_sha256(digester(source_path, label=label))
    except Exception:
        # The canonical WorkspaceBuilder check is the authority.  Re-check
        # existence only to preserve a useful stable missing-vs-unsafe code;
        # exception details and private paths never enter the report.
        try:
            missing = not os.path.lexists(path)
        except OSError:
            missing = False
        return _InstructorInputObservation("missing" if missing else "unsafe")
    if digest is None:
        return _InstructorInputObservation("unsafe")
    return _InstructorInputObservation("ok", digest)


def _validate_assignment_instructor_inputs(
    assignment: PlatformAssignment,
    observations: Mapping[str, _InstructorInputObservation],
) -> None:
    inputs = [
        (
            "assessment",
            assignment.assessment_path,
            assignment.assessment_digest,
        )
    ]
    if assignment.data_path is not None:
        inputs.append(("data", assignment.data_path, assignment.dataset_digest))
    for label, source_path, expected_digest in inputs:
        observation = observations[_instructor_input_key(source_path)]
        if observation.state == "missing":
            raise _PreflightIssue(f"{label}_input_missing")
        if observation.state != "ok":
            raise _PreflightIssue(f"{label}_input_unsafe")
        if observation.digest != _normalize_sha256(expected_digest):
            raise _PreflightIssue(f"{label}_digest_mismatch")


def _instructor_input_key(source_path: str) -> str:
    return str(Path(source_path).expanduser().absolute())


def _normalize_sha256(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if normalized.startswith("sha256:"):
        normalized = normalized[len("sha256:") :]
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        return None
    return f"sha256:{normalized}"


def _validate_http_clone_url(
    assignment: PlatformAssignment,
    *,
    expected_host: str | None,
) -> None:
    parsed = urlsplit(assignment.clone_url)
    expected_paths = {
        f"/{assignment.repository_owner}/{assignment.repository_name}",
        f"/{assignment.repository_owner}/{assignment.repository_name}.git",
    }
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or not isinstance(expected_host, str)
        or parsed.hostname.casefold() != expected_host.casefold()
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.casefold() not in {path.casefold() for path in expected_paths}
    ):
        raise _PreflightIssue("repository_clone_url_mismatch")


__all__ = [
    "InstructorInputDigester",
    "RepositoryIdentityProvider",
    "RepositoryPreflightItem",
    "RepositoryPreflightReport",
    "RepositoryTreePreflighter",
    "preflight_assignments",
]
