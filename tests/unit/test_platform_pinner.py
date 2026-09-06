from __future__ import annotations

from types import SimpleNamespace

import pytest

from autograde.gitops import (
    AssignmentNotFoundError,
    SnapshotLimitError,
    UnsupportedGitLFSObjectError,
    UnsupportedGitSubmoduleError,
)
from autograde.platform_pinner import GitSubmissionPinner, SubmissionSourceInvalid


def assignment():
    return SimpleNamespace(
        github_repository_id=9001,
        clone_url="/srv/git/lab01.git",
        assignment_id="asn_01",
        assignment_path=".",
        target_ref="submission/lab01",
    )


class BrokenCollector:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def collect_exact(self, *_args, **_kwargs):
        raise self.error


@pytest.mark.parametrize(
    ("error", "code", "public_message"),
    [
        (
            UnsupportedGitLFSObjectError("secret file path"),
            "git_lfs_object_unsupported",
            "Git LFS pointers are not supported for submissions",
        ),
        (
            UnsupportedGitSubmoduleError("secret module path"),
            "git_submodule_unsupported",
            "Git submodules are not supported for submissions",
        ),
        (
            SnapshotLimitError("secret size"),
            "repository_limit_exceeded",
            "the submitted source exceeds repository limits",
        ),
        (
            AssignmentNotFoundError("secret assignment path"),
            "assignment_path_missing",
            "the configured assignment path has no tracked files",
        ),
    ],
)
def test_pinner_maps_known_content_failures_to_safe_actionable_codes(
    error: Exception,
    code: str,
    public_message: str,
) -> None:
    pinner = GitSubmissionPinner(BrokenCollector(error))  # type: ignore[arg-type]

    with pytest.raises(SubmissionSourceInvalid) as captured:
        pinner.pin(
            assignment=assignment(),  # type: ignore[arg-type]
            requested_sha="a" * 40,
            submission_id="sub_01",
        )

    assert captured.value.code == code
    assert captured.value.public_message == public_message
    assert "secret" not in captured.value.public_message
