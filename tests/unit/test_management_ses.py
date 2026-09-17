"""Configuration-only tests: no production database, scheduler or artifacts."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from autograde.management_ses import ConfigurationError, compile_plan, load_json, main

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "design/ses/management.ses.json"
PROFILE = ROOT / "examples/management/course-profiles.json"


@pytest.fixture
def inputs():
    return load_json(MODEL), load_json(PROFILE)


def nodes(tree):
    yield tree
    for child in tree["children"]:
        yield from nodes(child)


def test_pruning_is_deterministic_isolated_and_does_not_mutate(inputs):
    model, profile = inputs
    original = deepcopy(inputs)
    plan = compile_plan(model, profile)
    assert inputs == original
    assert plan == compile_plan(model, profile)
    assert plan["runtime_ready"] is False and plan["side_effects"] == "none"
    assert plan["protected_policy"]["automatic_deletion"] is False
    by_course = {c["course_key"]: c["modules"] for c in plan["courses"]}
    assert "support.rules.v1" in by_course["come2201"]
    assert "support.disabled.v1" in by_course["come3105"]
    assert {n["type"] for n in nodes(plan["root"])} == {"entity", "aspect"}
    labels = [n["label"] for n in nodes(plan["root"])]
    assert len(labels) == len(set(labels))
    for node in nodes(plan["root"]):
        assert all(c["type"] != node["type"] for c in node["children"])
    profile["courses"].reverse()
    assert compile_plan(model, profile) == plan


@pytest.mark.parametrize("choice,value", [
    ("archive_mode", "NoSuchArchive"),
    ("scheduler_mode", "ManualScheduler"),
    ("report_mode", "ReportsDisabled"),
    ("support_mode", "SupportDisabled"),
    ("lifecycle_mode", "ManualLifecycle"),
])
def test_invalid_or_incompatible_variants_are_rejected(inputs, choice, value):
    model, profile = inputs
    profile["courses"][0]["selections"][choice] = value
    with pytest.raises(ConfigurationError):
        compile_plan(model, profile)


@pytest.mark.parametrize("module,key,value", [
    ("support.rules.v1", "minimum_graded_assignments", 1),
    ("support.rules.v1", "low_score_percent", 101),
    ("support.rules.v1", "low_score_percent", True),
    ("scheduler.pyjevsim.v1", "interval_seconds", 0),
    ("archive.local_tar.v1", "retention_days", -1),
    ("archive.local_tar.v1", "delete_originals", True),
    ("reports.html_csv.v1", "score_basis", "latest_submission_or_zero"),
])
def test_invalid_and_unsafe_parameters_are_rejected(inputs, module, key, value):
    model, profile = inputs
    profile["courses"][0]["parameters"][module][key] = value
    with pytest.raises(ConfigurationError):
        compile_plan(model, profile)


@pytest.mark.parametrize("damage", ["duplicate_course", "unknown_field", "missing_selection",
                                   "unexpected_parameters", "no_courses", "path_course"])
def test_invalid_profile_shape(inputs, damage):
    model, profile = inputs
    if damage == "duplicate_course":
        profile["courses"].append(deepcopy(profile["courses"][0]))
    elif damage == "unknown_field":
        profile["activate"] = True
    elif damage == "missing_selection":
        del profile["courses"][0]["selections"]["archive_mode"]
    elif damage == "unexpected_parameters":
        profile["courses"][1]["parameters"]["support.rules.v1"] = {}
    elif damage == "no_courses":
        profile["courses"] = []
    else:
        profile["courses"][0]["course_key"] = "../production"
    with pytest.raises(ConfigurationError):
        compile_plan(model, profile)


@pytest.mark.parametrize("damage", ["duplicate_label", "no_core", "unknown_module",
                                   "wrong_type", "expression", "wrong_multiplicity", "ambiguous_label"])
def test_invalid_ses_shape(inputs, damage):
    model, profile = inputs
    items = list(nodes(model["root"]))
    core = next(n for n in items if n.get("module_id") == "core.policy.v1")
    if damage == "duplicate_label":
        core["label"] = model["root"]["label"]
    elif damage == "no_core":
        model["root"]["children"][0]["children"].remove(core)
    elif damage == "unknown_module":
        core["module_id"] = "os.system"
    elif damage == "wrong_type":
        core["type"] = []
    elif damage == "expression":
        core["eval"] = "dangerous_expression()"
    elif damage == "ambiguous_label":
        core["label"] = "Lifecycle.come2201"
    else:
        next(n for n in items if n["type"] == "multi_aspect")["multiplicity"] = "eval(courses)"
    with pytest.raises(ConfigurationError):
        compile_plan(model, profile)


@pytest.mark.parametrize("contents", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', "{"])
def test_bad_json(tmp_path, contents):
    path = tmp_path / "invalid.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        load_json(path)


def test_size_limit(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ConfigurationError):
        load_json(path)


def test_parameter_change_changes_hash(inputs):
    model, profile = inputs
    before = compile_plan(model, profile)
    profile["courses"][0]["parameters"]["archive.local_tar.v1"]["retention_days"] = 365
    after = compile_plan(model, profile)
    assert before["plan_sha256"] != after["plan_sha256"]
    assert before["model_sha256"] == after["model_sha256"]


def test_disabling_support_does_not_change_another_course(inputs):
    model, profile = inputs
    before = compile_plan(model, profile)
    course = profile["courses"][0]
    course["selections"]["support_mode"] = "SupportDisabled"
    course["selections"]["notification_mode"] = "NotificationsDisabled"
    del course["parameters"]["support.rules.v1"]
    after = compile_plan(model, profile)
    assert after["courses"][1] == before["courses"][1]
    assert "support.disabled.v1" in after["courses"][0]["modules"]


def test_required_module_cannot_be_removed_from_both_model_and_profile(inputs):
    model, profile = inputs
    composition = next(n for n in nodes(model["root"]) if n["label"] == "ManagementModules")
    composition["children"] = [n for n in composition["children"] if n["label"] != "Reports"]
    for course in profile["courses"]:
        del course["selections"]["report_mode"]
        course["parameters"] = {k: v for k, v in course["parameters"].items() if not k.startswith("reports.")}
    with pytest.raises(ConfigurationError, match="exactly one"):
        compile_plan(model, profile)


def test_cli_is_preview_only(capsys):
    assert main(["--model", str(MODEL), "--profile", str(PROFILE)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["plan"]["runtime_ready"] is False


def test_cli_error_is_nonzero_and_does_not_echo_file(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("private student information")
    assert main(["--model", str(path), "--profile", str(PROFILE)]) == 2
    result = capsys.readouterr()
    assert not result.out and "private student" not in result.err
    assert json.loads(result.err)["ok"] is False
