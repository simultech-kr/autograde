from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from autograde.institution_product import MAX_PROFILE_BYTES, main, validate_profile


EXAMPLE = Path(__file__).resolve().parents[2] / "config/products/institution.example.json"


@pytest.fixture
def profile():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def save(tmp_path, profile):
    path = tmp_path / "institution.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path


def built_client(tmp_path, profile):
    client = profile["clients"][0]
    artifact = tmp_path / "mac.zip"
    artifact.write_bytes(b"synthetic package, never executable")
    client["artifact"] = {"build_status": "built", "path": "mac.zip",
                          "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    client["support_status"] = "evaluation-only"
    return client


def pilot_csv(tmp_path, profile):
    path = tmp_path / "portal.csv"
    path.write_text(
        "key,value\ncourse_key,synthetic\ndata_root,data\nlisten,127.0.0.1\n"
        "port,18080\nweb_port,18081\n"
        f"public_base_url,{profile['origins']['api']}\n"
        f"web_public_base_url,{profile['origins']['web']}\n"
        f"grading_runtime,{profile['runtime']['grading']}\n"
        f"instructor_assignment_web_enabled,{str(profile['runtime']['instructor_web']).lower()}\n",
        encoding="utf-8",
    )
    return path


def test_example_is_definition_only():
    result = validate_profile(EXAMPLE)
    assert result["valid"] is True
    assert result["deployment_ready"] is False
    assert result["runtime_config_changed"] is False
    assert result["evidence_verified"] is False
    assert result["checks"] == {"definition": "passed", "artifacts": "not-requested",
                                "pilot_alignment": "not-requested"}
    assert len(result["clients"]) == 4
    assert result["windows_environment"]["default"] == "windows-native"
    assert "windows_native_acceptance" in result["blocking_gates"]


@pytest.mark.parametrize("location", [None, "deployment", "release", "origins", "storage", "runtime", "teaching_scope", "windows_environment"])
def test_unknown_fields_are_rejected_without_echoing_secrets(tmp_path, profile, location):
    target = profile if location is None else profile[location]
    target["password"] = "must-never-be-echoed"
    result = validate_profile(save(tmp_path, profile))
    assert not result["valid"]
    assert result["errors"][0]["code"] == "fields"
    assert "must-never-be-echoed" not in json.dumps(result)


@pytest.mark.parametrize("origin", [
    "http://school.example", "https://user:private@school.example", "https://school.example/",
    "https://school.example/path", "https://school.example?", "https://school.example#",
    "https://school.example:443", "https://school.example:0", "https://school.example:65536",
    "https://SCHOOL.example", "https://*.example", "https://0.0.0.0", "https://[::]", "https://[fe80::1%en0]",
    "https://127.1", "https://127.0.0.999",
    "https://school.example\\evil", "https://school.example:\n443", "https://school.example:",
])
def test_noncanonical_or_unsafe_origins_are_rejected(tmp_path, profile, origin):
    profile["origins"]["api"] = origin
    result = validate_profile(save(tmp_path, profile))
    assert not result["valid"]
    assert result["deployment_ready"] is False


def test_same_origins_rejected(tmp_path, profile):
    profile["origins"]["api"] = profile["origins"]["web"]
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "separate_origins"


@pytest.mark.parametrize("section,key,value,code", [
    ("storage", "engine", "postgresql", "choice"),
    ("storage", "data_root_ref", "/srv/private", "choice"),
    ("storage", "data_root_ref", "${HOME}", "choice"),
    ("runtime", "grading", "docker", "runtime_mode"),
    ("runtime", "instructor_web", 1, "type"),
    ("runtime", "http_server", "asgi", "choice"),
    ("teaching_scope", "languages", ["python"], "scope"),
    ("deployment", "kind", "shared-multitenant", "choice"),
    ("release", "status", "production-ready", "choice"),
])
def test_unsupported_scope_and_runtime_fail(tmp_path, profile, section, key, value, code):
    profile[section][key] = value
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == code


def test_isolated_mode_allows_fixed_instructor_authoring_but_is_not_deployment_ready(tmp_path, profile):
    profile["runtime"].update(mode="isolated-staging", grading="docker")
    result = validate_profile(save(tmp_path, profile))
    assert result["valid"]
    assert not result["deployment_ready"]
    profile["runtime"]["instructor_web"] = False
    result = validate_profile(save(tmp_path, profile))
    assert result["valid"]
    assert not result["deployment_ready"]


def test_client_target_architecture_uniqueness_and_all_targets(tmp_path, profile):
    profile["clients"][2] = copy.deepcopy(profile["clients"][0])
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "duplicate_client"
    profile["clients"][2]["architecture"] = "x64"
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "targets"


def test_client_compatibility_requires_exact_release(tmp_path, profile):
    profile["clients"][1]["compatibility"]["server_release"] = "some-other-release"
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "server_release"


def test_native_windows_is_required_and_cannot_be_replaced_by_wsl(tmp_path, profile):
    profile["clients"] = [client for client in profile["clients"] if client["target"] != "windows-native"]
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "targets"


def test_optional_wsl_can_be_removed_from_definition(tmp_path, profile):
    profile["windows_environment"]["optional"] = []
    profile["clients"] = [client for client in profile["clients"] if client["target"] != "windows-wsl2"]
    result = validate_profile(save(tmp_path, profile))
    assert result["valid"]
    assert result["windows_environment"]["optional"] == []
    assert len(result["clients"]) == 3
    assert not result["deployment_ready"]


@pytest.mark.parametrize("declared,include_client", [(False, True), (True, False)])
def test_wsl_selection_must_match_client_inventory(tmp_path, profile, declared, include_client):
    profile["windows_environment"]["optional"] = ["windows-wsl2"] if declared else []
    if not include_client:
        profile["clients"] = [client for client in profile["clients"] if client["target"] != "windows-wsl2"]
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "targets"


@pytest.mark.parametrize("default", ["windows-wsl2", "windows", "linux-native"])
def test_windows_native_is_the_only_default(tmp_path, profile, default):
    profile["windows_environment"]["default"] = default
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "choice"


@pytest.mark.parametrize("optional", [["windows-native"], ["windows-wsl2", "windows-wsl2"], "windows-wsl2", None])
def test_optional_windows_selection_is_strict(tmp_path, profile, optional):
    profile["windows_environment"]["optional"] = optional
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "windows_optional"


@pytest.mark.parametrize("field,value", [
    ("toolchain_provisioning", "student-installed"),
    ("node_provisioning", "student-installed"),
    ("lab_sessions", "reuse-previous-student-workspace"),
    ("isolation", "cleanup-is-an-os-sandbox"),
])
def test_native_windows_requirements_cannot_be_weakened(tmp_path, profile, field, value):
    profile["windows_environment"]["native_requirements"][field] = value
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "choice"


def test_native_and_wsl_artifacts_are_distinct(tmp_path, profile):
    for target in ("windows-native", "windows-wsl2"):
        client = next(entry for entry in profile["clients"] if entry["target"] == target)
        client["artifact"] = {"build_status": "built", "path": "windows.zip", "sha256": "a" * 64}
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "duplicate_artifact"


def test_build_and_test_claims_are_independent(tmp_path, profile):
    client = built_client(tmp_path, profile)
    path = save(tmp_path, profile)
    result = validate_profile(path, check_artifacts=True)
    assert result["valid"]
    assert result["clients"][0]["build_status"] == "built"
    assert result["clients"][0]["test_status"] == "not-tested"
    assert result["checked_files"] == {"artifacts": 1, "evidence_references": 0}
    client["test_status"] = "workflow-tested"
    result = validate_profile(save(tmp_path, profile))
    assert not result["valid"]  # No test evidence reference.
    client["test_evidence_ref"] = "acceptance.txt"
    (tmp_path / "acceptance.txt").write_text("Synthetic workflow record", encoding="utf-8")
    result = validate_profile(save(tmp_path, profile), check_artifacts=True)
    assert result["valid"]
    assert result["evidence_verified"] is False
    assert result["deployment_ready"] is False
    assert result["checked_files"] == {"artifacts": 1, "evidence_references": 1}


@pytest.mark.parametrize("field,value,reference", [
    ("test_status", "launch-tested", "test_evidence_ref"),
    ("signature_status", "signed", "signature_evidence_ref"),
])
def test_unbuilt_artifact_cannot_claim_testing_or_signature(tmp_path, profile, field, value, reference):
    profile["clients"][0][field] = value
    profile["clients"][0][reference] = "evidence.txt"
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "unbuilt_claim"


@pytest.mark.parametrize("reference", ["../private", "/tmp/private", "a/../private", "a//file", "./file",
                                       "C:\\private", "$HOME/private", "https://x.example/file", "a/%2e%2e/file"])
def test_artifact_references_cannot_escape_or_expand(tmp_path, profile, reference):
    built_client(tmp_path, profile)["artifact"]["path"] = reference
    result = validate_profile(save(tmp_path, profile))
    assert result["errors"][0]["code"] == "reference"


def test_artifact_mismatch_and_missing_file_fail(tmp_path, profile):
    built_client(tmp_path, profile)
    path = save(tmp_path, profile)
    (tmp_path / "mac.zip").write_bytes(b"changed")
    result = validate_profile(path, check_artifacts=True)
    assert result["errors"][0]["code"] == "artifact_digest"
    (tmp_path / "mac.zip").unlink()
    result = validate_profile(path, check_artifacts=True)
    assert result["errors"][0]["code"] == "reference_file"


@pytest.mark.parametrize("parent_symlink", [False, True])
def test_artifact_symlinks_are_rejected(tmp_path, profile, parent_symlink):
    client = built_client(tmp_path, profile)
    if parent_symlink:
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        client["artifact"]["path"] = "alias/mac.zip"
    else:
        (tmp_path / "alias.zip").symlink_to(tmp_path / "mac.zip")
        client["artifact"]["path"] = "alias.zip"
    result = validate_profile(save(tmp_path, profile), check_artifacts=True)
    assert result["errors"][0]["code"] == "reference_file"


def test_pilot_alignment_checks_without_creating_data(tmp_path, profile):
    source = save(tmp_path, profile)
    csv = pilot_csv(tmp_path, profile)
    before = {entry.name: entry.read_bytes() for entry in tmp_path.iterdir()}
    result = validate_profile(source, pilot_config=csv)
    assert result["valid"]
    assert result["checks"]["pilot_alignment"] == "passed"
    assert {entry.name: entry.read_bytes() for entry in tmp_path.iterdir()} == before
    assert not (tmp_path / "data").exists()
    csv.write_text(csv.read_text().replace(":20000", ":21000"))
    result = validate_profile(source, pilot_config=csv)
    assert result["errors"][0]["code"] == "pilot_mismatch"


def test_pilot_storage_escape_is_rejected(tmp_path, profile):
    csv = pilot_csv(tmp_path, profile)
    csv.write_text(csv.read_text().replace("data_root,data", "data_root,.."))
    result = validate_profile(save(tmp_path, profile), pilot_config=csv)
    assert result["errors"][0]["code"] == "pilot_config"


def test_recording_every_gate_never_certifies_release(tmp_path, profile):
    (tmp_path / "acceptance.txt").write_text("Unverified operator evidence", encoding="utf-8")
    profile["release"].update(status="reviewed", approval_ref="acceptance.txt")
    for gate in profile["release_gates"].values():
        gate.update(status="evidence-recorded", evidence_ref="acceptance.txt")
    result = validate_profile(save(tmp_path, profile), check_artifacts=True)
    assert result["valid"]
    assert len(result["recorded_gates"]) == 9
    assert not result["deployment_ready"]
    assert not result["evidence_verified"]
    assert "independent_release_acceptance" in result["blocking_gates"]


@pytest.mark.parametrize("content", [b'{"schema":1,"schema":2}', b"not JSON", b'\xff', b"[]",
                                    b'{"value":NaN}', b" " * (MAX_PROFILE_BYTES + 1)])
def test_malformed_profiles_return_json_errors(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_bytes(content)
    result = validate_profile(path)
    assert not result["valid"]
    assert not result["deployment_ready"]
    assert result["errors"]


def test_cli_emits_machine_report_for_success_and_failure(tmp_path, capsys):
    assert main(["validate", "--profile", str(EXAMPLE)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert main(["validate", "--profile", str(tmp_path / "missing.json")]) == 1
    failure = json.loads(capsys.readouterr().out)
    assert not failure["valid"]
    assert not failure["deployment_ready"]
