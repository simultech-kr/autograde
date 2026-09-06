from pathlib import Path

import pytest

from autograde.roster import RosterEntry, RosterError, import_roster, read_roster
from autograde.state import IdempotencyConflict, StateStore


def test_minimal_roster_maps_school_id_and_github_id_to_repository(
    tmp_path: Path,
) -> None:
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "student_key,github_id,github_repository_id\n"
        "20260001,octocat,12345\n",
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state.sqlite3")

    repositories = import_roster(
        store,
        read_roster(roster),
        organization="school-org",
        repository_template="python-2026-{github_id}",
    )

    assert len(repositories) == 1
    repository = repositories[0]
    assert repository.repository_key == "school-org/python-2026-octocat"
    assert repository.student_key == "20260001"
    assert repository.github_repository_id == 12345
    assert repository.clone_url == (
        "https://github.com/school-org/python-2026-octocat.git"
    )
    assert repository.metadata == {"github_id": "octocat"}


def test_roster_row_can_override_repository_policy(tmp_path: Path) -> None:
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "student_key,github_id,repository_name,clone_url,target_ref\n"
        "s1,alice,custom-repo,/srv/git/alice.git,submission\n",
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state.sqlite3")

    repository = import_roster(
        store,
        read_roster(roster),
        organization="school-org",
    )[0]

    assert repository.name == "custom-repo"
    assert repository.clone_url == "/srv/git/alice.git"
    assert repository.target_ref == "submission"


def test_roster_rejects_missing_identity_columns(tmp_path: Path) -> None:
    roster = tmp_path / "roster.csv"
    roster.write_text("student_key\ns1\n", encoding="utf-8")

    with pytest.raises(RosterError, match="github_id"):
        read_roster(roster)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            "s1,alice,repo-one\ns1,bob,repo-two\n",
            "duplicate student_key",
        ),
        (
            "s1,Alice,repo-one\ns2,alice,repo-two\n",
            "duplicate github_id",
        ),
    ],
)
def test_roster_rejects_duplicate_school_or_github_identity(
    tmp_path: Path,
    rows: str,
    message: str,
) -> None:
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "student_key,github_id,repository_name\n" + rows,
        encoding="utf-8",
    )

    with pytest.raises(RosterError, match=message):
        read_roster(roster)


def test_programmatic_duplicate_roster_is_validated_before_any_write(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    entries = (
        RosterEntry("s1", "alice", repository_name="repo-one"),
        RosterEntry("s1", "bob", repository_name="repo-two"),
    )

    with pytest.raises(RosterError, match="duplicate student_key"):
        import_roster(store, entries, organization="school-org")

    assert store.list_repositories() == []


def test_existing_database_conflict_rolls_back_the_entire_roster(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.upsert_repository(
        repository_key="school-org/existing",
        student_key="s2",
        owner="school-org",
        name="existing",
        clone_url="https://github.com/school-org/existing.git",
    )
    entries = (
        RosterEntry("s1", "alice", repository_name="new-one"),
        RosterEntry("s2", "bob", repository_name="conflict-two"),
    )

    with pytest.raises(IdempotencyConflict, match="repository identity conflicts"):
        import_roster(store, entries, organization="school-org")

    assert [item.repository_key for item in store.list_repositories()] == [
        "school-org/existing"
    ]
