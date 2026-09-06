from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from autograde.gitops import UnsupportedGitLFSObjectError
from autograde.platform_github_app import GitHubRepositoryIdentity
from autograde.platform_repository import preflight_assignments
from autograde.platform_state import PlatformAssignment, ResultPolicy, SubmissionMode


def assignment(**overrides) -> PlatformAssignment:
    base = PlatformAssignment(
        assignment_id="asn_01",
        student_id=1,
        enrollment_id=1,
        course_key="cse101-2026f",
        assignment_key="lab01",
        release_id="lab01-v1",
        github_repository_id=987654321,
        repository_owner="school",
        repository_name="lab01-student",
        clone_url="https://github.com/school/lab01-student.git",
        submission_mode=SubmissionMode.BRANCH,
        target_ref="submission/lab01",
        allowed_base_ref=None,
        assignment_path=".",
        assessment_path="/srv/assessment",
        data_path=None,
        assessment_digest="sha256:" + "a" * 64,
        dataset_digest="",
        runner_image="ghcr.io/school/grader@sha256:" + "b" * 64,
        rubric_version="v1",
        max_score=10,
        result_policy=ResultPolicy.IMMEDIATE,
        active=True,
        ready=True,
        created_at="2026-08-30T00:00:00.000000Z",
        updated_at="2026-08-30T00:00:00.000000Z",
    )
    return replace(base, **overrides)


class FakeCollector:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = []

    def preflight(
        self,
        repository_id,
        remote_url,
        *,
        assignment_subpath,
        target_ref,
    ):
        self.calls.append(
            (repository_id, remote_url, assignment_subpath, target_ref)
        )
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(new_sha="a" * 40)


class FakeIdentityProvider:
    def __init__(
        self,
        identity: GitHubRepositoryIdentity,
        *,
        git_host: str = "github.com",
    ) -> None:
        self.identity = identity
        self.git_host = git_host
        self.calls = []

    def get_repository(self, *, owner, name):
        self.calls.append((owner, name))
        return self.identity


def identity(**overrides) -> GitHubRepositoryIdentity:
    base = GitHubRepositoryIdentity(
        repository_id=987654321,
        full_name="school/lab01-student",
        private=True,
        archived=False,
        disabled=False,
        default_branch="main",
    )
    return replace(base, **overrides)


def test_preflight_verifies_numeric_identity_private_state_and_tree() -> None:
    collector = FakeCollector()
    provider = FakeIdentityProvider(identity())

    report = preflight_assignments(
        [assignment()],
        collector=collector,
        identity_provider=provider,
    )

    assert report.go is True
    assert report.as_dict()["passed"] == 1
    assert report.items[0].commit_sha == "a" * 40
    assert provider.calls == [("school", "lab01-student")]
    assert collector.calls == [
        (
            "987654321",
            "https://github.com/school/lab01-student.git",
            ".",
            "submission/lab01",
        )
    ]


@pytest.mark.parametrize(
    ("repository_identity", "code"),
    [
        (identity(repository_id=1), "repository_identity_mismatch"),
        (identity(private=False), "repository_not_private"),
        (identity(archived=True), "repository_not_writable"),
        (identity(disabled=True), "repository_not_writable"),
    ],
)
def test_preflight_blocks_unsafe_github_repository_state(
    repository_identity: GitHubRepositoryIdentity,
    code: str,
) -> None:
    collector = FakeCollector()
    report = preflight_assignments(
        [assignment()],
        collector=collector,
        identity_provider=FakeIdentityProvider(repository_identity),
    )

    assert report.go is False
    assert report.items[0].code == code
    assert collector.calls == []


def test_github_repository_requires_app_and_exact_clone_coordinates() -> None:
    missing = preflight_assignments(
        [assignment()],
        collector=FakeCollector(),
        identity_provider=None,
    )
    mismatched = preflight_assignments(
        [assignment(clone_url="https://github.com/school/other.git")],
        collector=FakeCollector(),
        identity_provider=FakeIdentityProvider(identity()),
    )
    wrong_host = preflight_assignments(
        [assignment(clone_url="https://mirror.example/school/lab01-student.git")],
        collector=FakeCollector(),
        identity_provider=FakeIdentityProvider(identity()),
    )

    assert missing.items[0].code == "github_app_not_configured"
    assert mismatched.items[0].code == "repository_clone_url_mismatch"
    assert wrong_host.items[0].code == "repository_clone_url_mismatch"


@pytest.mark.parametrize(
    "clone_url",
    (
        "git@github.com:school/lab01-student.git",
        "ssh://git@github.com/school/lab01-student.git",
        "git://github.com/school/lab01-student.git",
        "school/lab01-student.git",
    ),
)
def test_non_https_remotes_cannot_bypass_github_app_identity(
    clone_url: str,
) -> None:
    collector = FakeCollector()
    provider = FakeIdentityProvider(identity())

    report = preflight_assignments(
        [assignment(clone_url=clone_url)],
        collector=collector,
        identity_provider=provider,
    )

    assert report.go is False
    assert report.items[0].code == "repository_clone_url_mismatch"
    assert collector.calls == []
    assert provider.calls == []


def test_local_preflight_is_available_without_github_credentials() -> None:
    collector = FakeCollector()
    report = preflight_assignments(
        [assignment(clone_url="/srv/git/lab01-student.git")],
        collector=collector,
        identity_provider=None,
    )

    assert report.go is True
    assert len(collector.calls) == 1


def test_instructor_input_preflight_hashes_each_shared_path_once(
    tmp_path,
) -> None:
    bundle = tmp_path / "shared-bundle"
    bundle.mkdir()
    calls = []

    def digest(source_path: str, *, label: str) -> str:
        calls.append((source_path, label))
        return "sha256:" + "a" * 64

    assignments = (
        assignment(
            assignment_id="asn_01",
            clone_url="/srv/git/lab01-student.git",
            assessment_path=str(bundle),
            data_path=str(bundle),
            dataset_digest="sha256:" + "a" * 64,
        ),
        assignment(
            assignment_id="asn_02",
            assignment_key="lab02",
            clone_url="/srv/git/lab02-student.git",
            assessment_path=str(bundle),
        ),
    )

    report = preflight_assignments(
        assignments,
        collector=FakeCollector(),
        identity_provider=None,
        instructor_input_digester=digest,
        jobs=2,
    )

    assert report.go is True
    assert calls == [(str(bundle.absolute()), "assessment")]


@pytest.mark.parametrize(
    ("kind", "state", "expected_code"),
    (
        ("assessment", "missing", "assessment_input_missing"),
        ("assessment", "unsafe", "assessment_input_unsafe"),
        ("assessment", "mismatch", "assessment_digest_mismatch"),
        ("data", "missing", "data_input_missing"),
        ("data", "unsafe", "data_input_unsafe"),
        ("data", "mismatch", "data_digest_mismatch"),
    ),
)
def test_instructor_input_preflight_uses_stable_non_secret_codes(
    tmp_path,
    kind: str,
    state: str,
    expected_code: str,
) -> None:
    valid = tmp_path / "valid"
    valid.mkdir()
    selected = tmp_path / f"secret-{kind}-{state}"
    if state == "unsafe":
        selected.symlink_to(valid, target_is_directory=True)
    elif state == "mismatch":
        selected.mkdir()

    values = {
        "clone_url": "/srv/git/lab01-student.git",
        "assessment_path": str(valid),
    }
    if kind == "assessment":
        values["assessment_path"] = str(selected)
    else:
        values["data_path"] = str(selected)
        values["dataset_digest"] = "sha256:" + "a" * 64

    report = preflight_assignments(
        [assignment(**values)],
        collector=FakeCollector(),
        identity_provider=None,
        instructor_input_digester=(
            lambda _path, *, label: "sha256:" + (
                "b" * 64 if label == kind and state == "mismatch" else "a" * 64
            )
        ),
    )

    assert report.go is False
    assert report.items[0].code == expected_code
    assert "secret-" not in str(report.as_dict())


def test_content_failures_are_stable_and_do_not_leak_exception_text() -> None:
    report = preflight_assignments(
        [assignment(clone_url="/srv/git/lab01-student.git")],
        collector=FakeCollector(
            UnsupportedGitLFSObjectError("secret internal repository path")
        ),
        identity_provider=None,
    )

    payload = report.as_dict()
    assert report.go is False
    assert report.items[0].code == "git_lfs_object_unsupported"
    assert "secret internal repository path" not in str(payload)


def test_preflight_preserves_input_order_and_bounds_parallelism() -> None:
    assignments = [
        assignment(
            assignment_id=f"asn_{index:02d}",
            assignment_key=f"lab{index:02d}",
            github_repository_id=1000 + index,
            clone_url=f"/srv/git/lab{index:02d}.git",
        )
        for index in range(20)
    ]

    report = preflight_assignments(
        assignments,
        collector=FakeCollector(),
        identity_provider=None,
        jobs=4,
    )

    assert report.passed == 20
    assert [item.assignment_id for item in report.items] == [
        f"asn_{index:02d}" for index in range(20)
    ]
    with pytest.raises(ValueError, match="between 1 and 32"):
        preflight_assignments(
            [],
            collector=FakeCollector(),
            identity_provider=None,
            jobs=0,
        )
