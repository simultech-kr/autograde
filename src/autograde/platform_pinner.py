"""Synchronous exact-SHA admission pinning for student submissions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .gitops import (
    AssignmentNotFoundError,
    ExpectedCommitMismatchError,
    GitCollector,
    GitCollectorError,
    RemoteBranchNotFoundError,
    SnapshotLimitError,
    UnsupportedGitLFSObjectError,
    UnsupportedGitSubmoduleError,
    UnsafePathError,
    UnsafeRepositoryError,
)
from .platform_state import PlatformAssignment


class SubmissionPinError(RuntimeError):
    """Base class for failures before durable submission admission."""


class SubmissionSourceUnavailable(SubmissionPinError):
    """The requested SHA is not the currently pushed target-branch tip."""


class SubmissionSourceInvalid(SubmissionPinError):
    """The pushed source cannot be safely represented as a snapshot."""

    def __init__(
        self,
        message: str = "submission source is invalid",
        *,
        code: str = "invalid_submission_source",
        public_message: str = "the submitted source cannot be safely collected",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = public_message


class SubmissionPinInfrastructureError(SubmissionPinError):
    """Repository collection infrastructure failed transiently."""


@dataclass(frozen=True)
class PinnedSubmission:
    commit_sha: str
    source_path: str
    source_digest: str
    snapshot_key: str


class SubmissionPinner(Protocol):
    def pin(
        self,
        *,
        assignment: PlatformAssignment,
        requested_sha: str,
        submission_id: str,
    ) -> PinnedSubmission: ...


class GitSubmissionPinner:
    """Observe a configured remote branch and preserve its exact tip.

    Fetch, exact-tip comparison, immutable ref creation, and archive export run
    under one repository lock.  A mismatched request creates no candidate ref
    or archive; a matching snapshot uses the candidate submission ID.  Nothing
    is persisted to the platform database until this method succeeds.
    """

    def __init__(self, collector: GitCollector) -> None:
        self.collector = collector

    def pin(
        self,
        *,
        assignment: PlatformAssignment,
        requested_sha: str,
        submission_id: str,
    ) -> PinnedSubmission:
        try:
            collected = self.collector.collect_exact(
                str(assignment.github_repository_id),
                assignment.clone_url,
                assignment_id=assignment.assignment_id,
                assignment_subpath=assignment.assignment_path,
                snapshot_label=submission_id,
                expected_sha=requested_sha,
                target_ref=assignment.target_ref,
            )
        except (RemoteBranchNotFoundError, ExpectedCommitMismatchError) as exc:
            raise SubmissionSourceUnavailable(
                "requested SHA is not the pushed target-branch tip"
            ) from exc
        except UnsupportedGitLFSObjectError as exc:
            raise SubmissionSourceInvalid(
                code="git_lfs_object_unsupported",
                public_message="Git LFS pointers are not supported for submissions",
            ) from exc
        except UnsupportedGitSubmoduleError as exc:
            raise SubmissionSourceInvalid(
                code="git_submodule_unsupported",
                public_message="Git submodules are not supported for submissions",
            ) from exc
        except SnapshotLimitError as exc:
            raise SubmissionSourceInvalid(
                code="repository_limit_exceeded",
                public_message="the submitted source exceeds repository limits",
            ) from exc
        except AssignmentNotFoundError as exc:
            raise SubmissionSourceInvalid(
                code="assignment_path_missing",
                public_message="the configured assignment path has no tracked files",
            ) from exc
        except (UnsafePathError, UnsafeRepositoryError) as exc:
            raise SubmissionSourceInvalid("submission source is invalid") from exc
        except (GitCollectorError, OSError) as exc:
            raise SubmissionPinInfrastructureError(
                "repository collection failed"
            ) from exc

        snapshot = collected.snapshot
        if snapshot.commit_sha != requested_sha:
            raise SubmissionPinInfrastructureError(
                "collector returned a snapshot for an unexpected commit"
            )
        return PinnedSubmission(
            commit_sha=snapshot.commit_sha,
            source_path=str(snapshot.archive_path),
            source_digest=snapshot.archive_sha256,
            snapshot_key=snapshot.snapshot_ref,
        )
