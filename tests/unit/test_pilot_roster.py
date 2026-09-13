from concurrent.futures import ThreadPoolExecutor

import pytest

from autograde.pilot_roster import initialize_student_roster, RosterBootstrapError
from autograde.platform_auth import hash_student_password, verify_student_password
from autograde.platform_state import PlatformStateStore, PlatformNotFound

HEADER = "course_key,student_key,active,password\n"
GOOD = "come3105,001234,true,042731\ncome2201,001234,true,072731\n"


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "student_roster.csv"
    path.write_text(HEADER + GOOD)
    path.chmod(0o600)
    return PlatformStateStore(tmp_path / "state.sqlite3"), path


def test_initializes_two_courses_once_and_preserves_changed_password(roster):
    state, path = roster
    assert initialize_student_roster(state, path) == {"status": "initialized", "enrollment_count": 2}
    first = state.get_student_password_credential(student_key="001234", course_key="come3105")
    assert verify_student_password("042731", first.password_hash)
    second = state.get_student_password_credential(student_key="001234", course_key="come2201")
    assert verify_student_password("072731", second.password_hash)
    state.set_student_password_hash(student_id=first.student_id, course_key="come3105", password_hash=hash_student_password("032789"))
    path.unlink()
    assert initialize_student_roster(state, path)["status"] == "already_initialized"
    current = state.get_student_password_credential(student_key="001234", course_key="come3105")
    assert verify_student_password("032789", current.password_hash)


@pytest.mark.parametrize("bad", [
    "unknown,s2,true,123789\n", "come2201,s2,true,12345\n", "come2201,s2,true,１２３４５６\n",
    "come2201,s2,true,042731,extra\n", "come3105,001234,true,042731\n",
    "come3105,s2,true,042731\n", "come2201,s2,false,123789\n", "come2201,s2,,123789\n",
    "come2201,REPLACE_STUDENT_2,false,\n",
])
def test_invalid_later_row_does_not_import_earlier_students(roster, bad):
    state, path = roster
    path.write_text(HEADER + GOOD.splitlines()[0] + "\n" + bad)
    with pytest.raises(RosterBootstrapError):
        initialize_student_roster(state, path)
    assert state.roster_bootstrap_status() is None
    with pytest.raises(PlatformNotFound):
        state.get_student_by_key("001234")


def test_database_conflict_in_second_course_rolls_back_first_course(roster):
    state, path = roster
    # Local bootstrap must not rewrite a pre-existing GitHub identity.
    state.upsert_student(student_key="other", auth_subject="github:987654", github_user_id=987654, github_login="other")
    path.write_text(HEADER + GOOD.splitlines()[0] + "\ncome2201,other,true,072731\n")
    with pytest.raises(RosterBootstrapError):
        initialize_student_roster(state, path)
    with pytest.raises(PlatformNotFound):
        state.get_student_by_key("001234")
    assert state.roster_bootstrap_status() is None


def test_bootstrap_race_imports_once(roster):
    state, path = roster
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: initialize_student_roster(state, path), range(2)))
    assert sorted(r["status"] for r in results) == ["already_initialized", "initialized"]


def test_empty_missing_permission_symlink_and_bom(roster, tmp_path):
    state, path = roster
    path.chmod(0o644)
    with pytest.raises(RosterBootstrapError, match="chmod"):
        initialize_student_roster(state, path)
    path.chmod(0o600)
    linked = tmp_path / "linked.csv"
    linked.symlink_to(path)
    with pytest.raises(RosterBootstrapError):
        initialize_student_roster(state, linked)
    path.write_text(HEADER)
    with pytest.raises(RosterBootstrapError, match="1~10000"):
        initialize_student_roster(state, path)
    path.write_bytes((HEADER + GOOD).encode("utf-8-sig"))
    assert initialize_student_roster(state, path)["status"] == "initialized"


def test_twenty_five_students_initialize_with_distinct_credentials(roster):
    state, path = roster
    path.write_text(HEADER + "".join(
        f"{('come3105', 'come2201')[i % 2]},00{i:04d},true,{i + 1230:06d}\n" for i in range(25)))
    assert initialize_student_roster(state, path)["enrollment_count"] == 25
    for i in range(25):
        credential = state.get_student_password_credential(student_key=f"00{i:04d}", course_key=("come3105", "come2201")[i % 2])
        assert verify_student_password(f"{i + 1230:06d}", credential.password_hash)
