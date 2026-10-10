from pathlib import Path

import pytest

from autograde.pilot_config import NextResultsPolicy, PilotConfigError, load_pilot_config


def config(path: Path, *, enabled="true", courses="come2201;come3105"):
    path.write_text(
        "key,value\ncourse_key,come2201\ndata_root,state\n"
        "public_base_url,http://127.0.0.1:18080\nlisten,127.0.0.1\nport,18080\n"
        "web_public_base_url,http://127.0.0.1:18081\nweb_port,18081\n"
        "instructor_assignment_web_enabled,true\n"
        f"instructor_next_results_enabled,{enabled}\n"
        + (f"instructor_next_course_keys,{courses}\n" if courses is not None else ""),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("enabled,courses", [
    ("yes", "come2201"), ("true", None), ("true", "*"),
    ("true", "come2201;come2201"), ("true", "come2201;../other"),
    ("true", "come2201; come3105"), ("true", "COME2201"),
])
def test_invalid_next_policy_is_rejected(tmp_path, enabled, courses):
    with pytest.raises(PilotConfigError):
        load_pilot_config(config(tmp_path / "pilot.csv", enabled=enabled, courses=courses))


def test_next_requires_existing_instructor_and_web(tmp_path):
    path = config(tmp_path / "pilot.csv")
    path.write_text(path.read_text().replace("instructor_assignment_web_enabled,true",
                                           "instructor_assignment_web_enabled,false"))
    with pytest.raises(PilotConfigError):
        load_pilot_config(path)


def test_hot_reload_disable_reenable_course_scope_and_fail_closed(tmp_path):
    path = config(tmp_path / "pilot.csv")
    policy = NextResultsPolicy(load_pilot_config(path))
    assert policy() == {"come2201", "come3105"}
    config(path, enabled="false")
    assert policy() == frozenset()
    config(path, courses="come3105")
    assert policy() == {"come3105"}
    path.write_text("key,value\ninvalid,partial-edit\n")
    assert policy() == frozenset()
    config(path)
    assert policy() == {"come2201", "come3105"}
    path.unlink()
    assert policy() == frozenset()


def test_policy_does_not_hot_switch_data_origin_or_authentication(tmp_path):
    path = config(tmp_path / "pilot.csv")
    policy = NextResultsPolicy(load_pilot_config(path))
    for old, new in [("data_root,state", "data_root,different"),
                     ("web_public_base_url,http://127.0.0.1:18081", "web_public_base_url,https://example.test"),
                     ("instructor_assignment_web_enabled,true", "instructor_assignment_web_enabled,false")]:
        config(path)
        path.write_text(path.read_text().replace(old, new))
        assert policy() == frozenset()


def test_off_without_course_list_is_valid_and_not_enabled_by_default(tmp_path):
    parsed = load_pilot_config(config(tmp_path / "pilot.csv", enabled="false", courses=None))
    assert NextResultsPolicy(parsed)() == frozenset()
