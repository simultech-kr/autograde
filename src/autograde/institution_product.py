"""Validate an institution release definition without configuring the server.

This deliberately does not import the portal, open a database, execute a build,
or treat recorded acceptance evidence as independently verified approval.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import stat
from typing import Any, Sequence
from urllib.parse import urlsplit

from .pilot_config import load_pilot_config


SCHEMA = "autograde.institution-product.v1"
REPORT_SCHEMA = "autograde.institution-product-report.v1"
MAX_PROFILE_BYTES = 128 * 1024
GATES = (
    "isolated_grading", "instructor_isolated_authoring", "production_http",
    "identity_and_authorization", "backup_restore_and_upgrade", "capacity_and_recovery",
    "signed_ide_releases", "windows_native_acceptance", "institution_acceptance",
)
REQUIRED_TARGETS = {"macos-native", "windows-native", "linux-native"}
TARGETS = REQUIRED_TARGETS | {"windows-wsl2"}
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_RELATIVE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class ProductValidationError(ValueError):
    """A safe, fixed diagnostic; never contains supplied field values."""

    def __init__(self, code: str, location: str, message: str):
        self.code = code
        self.location = location
        super().__init__(message)


def _fail(code: str, location: str, message: str) -> None:
    raise ProductValidationError(code, location, message)


def _object(value: Any, fields: set[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("type", location, "Expected an object.")
    if value.keys() != fields:
        _fail("fields", location, "Missing or unknown fields; this schema is strict.")
    return value


def _text(value: Any, location: str, maximum: int = 160) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail("text", location, "Expected bounded non-empty text without surrounding whitespace or controls.")
    return value


def _choice(value: Any, choices: set[str], location: str) -> str:
    if not isinstance(value, str) or value not in choices:
        _fail("choice", location, "Value is not supported by this schema.")
    return value


def _identifier(value: Any, location: str) -> str:
    if not _IDENTIFIER.fullmatch(_text(value, location, 96)):
        _fail("identifier", location, "Expected a lowercase release or deployment identifier.")
    return value


def _reference(value: Any, location: str) -> str:
    text = _text(value, location, 256)
    if (not _RELATIVE_PATH.fullmatch(text)
            or any(part in {".", ".."} for part in text.split("/"))):
        _fail("reference", location, "Use a relative file reference without traversal, expansion, or credentials.")
    return text


def _origin(value: Any, location: str) -> str:
    text = _text(value, location, 256)
    try:
        parsed = urlsplit(text)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        _fail("origin", location, "Expected a canonical HTTPS origin.")
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or "?" in text or "#" in text or "%" in text or port == 0):
        _fail("origin", location, "Expected HTTPS scheme, host and optional port only, without credentials.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (len(host) > 253 or "." not in host or re.fullmatch(r"[0-9.]+", host)
                or any(not _HOST_LABEL.fullmatch(label) for label in host.split("."))):
            _fail("origin", location, "Expected a valid DNS name or IP address.")
        canonical_host = host
    else:
        if address.is_unspecified or address.is_multicast:
            _fail("origin", location, "Unspecified and multicast addresses are not service origins.")
        canonical_host = f"[{address.compressed}]" if address.version == 6 else str(address)
    canonical = f"https://{canonical_host}" + (f":{port}" if port not in {None, 443} else "")
    if text != canonical:
        _fail("origin", location, "Use the canonical HTTPS origin: lowercase host, no slash or default port.")
    return canonical


def validate_definition(value: Any) -> dict[str, Any]:
    """Validate schema and cross-field constraints; do not access artifact files."""
    profile = _object(value, {"schema", "deployment", "release", "origins", "storage",
                              "runtime", "teaching_scope", "windows_environment", "clients", "release_gates"}, "$")
    _choice(profile["schema"], {SCHEMA}, "schema")
    deployment = _object(profile["deployment"], {"id", "kind"}, "deployment")
    _identifier(deployment["id"], "deployment.id")
    _choice(deployment["kind"], {"institution-dedicated"}, "deployment.kind")
    release = _object(profile["release"], {"id", "status", "approval_ref"}, "release")
    _identifier(release["id"], "release.id")
    _choice(release["status"], {"draft", "reviewed"}, "release.status")
    if release["status"] == "reviewed":
        _reference(release["approval_ref"], "release.approval_ref")
    elif release["approval_ref"] is not None:
        _fail("approval", "release.approval_ref", "A draft cannot carry a release-definition approval reference.")
    origins = _object(profile["origins"], {"web", "api"}, "origins")
    if _origin(origins["web"], "origins.web") == _origin(origins["api"], "origins.api"):
        _fail("separate_origins", "origins", "The current portal requires separate web and API origins.")
    storage = _object(profile["storage"], {"engine", "data_root_ref", "isolation"}, "storage")
    _choice(storage["engine"], {"sqlite"}, "storage.engine")
    _choice(storage["data_root_ref"], {"pilot-config:data_root"}, "storage.data_root_ref")
    _choice(storage["isolation"], {"dedicated-per-institution"}, "storage.isolation")
    runtime = _object(profile["runtime"], {"mode", "grading", "instructor_web", "http_server"}, "runtime")
    _choice(runtime["mode"], {"trusted-pilot", "isolated-staging"}, "runtime.mode")
    _choice(runtime["grading"], {"pilot-local", "docker", "podman"}, "runtime.grading")
    _choice(runtime["http_server"], {"builtin-pilot"}, "runtime.http_server")
    if type(runtime["instructor_web"]) is not bool:
        _fail("type", "runtime.instructor_web", "Expected a boolean.")
    if (runtime["mode"] == "trusted-pilot") != (runtime["grading"] == "pilot-local"):
        _fail("runtime_mode", "runtime", "Trusted pilot requires pilot-local; isolated staging requires Docker or Podman.")
    # Runtime startup separately requires a locally available immutable
    # instructor_runner_image for fixed C/C++ web authoring. An offline product
    # manifest does not establish that deployment acceptance gate by itself.
    scope = _object(profile["teaching_scope"], {"languages", "c_standard", "cpp_standard"}, "teaching_scope")
    if scope["languages"] != ["c", "cpp"]:
        _fail("scope", "teaching_scope.languages", "Version 1 defines C and C++ in that order.")
    _choice(scope["c_standard"], {"c17"}, "teaching_scope.c_standard")
    _choice(scope["cpp_standard"], {"c++17"}, "teaching_scope.cpp_standard")
    windows = _object(profile["windows_environment"], {"default", "optional", "native_requirements"}, "windows_environment")
    _choice(windows["default"], {"windows-native"}, "windows_environment.default")
    if windows["optional"] not in ([], ["windows-wsl2"]):
        _fail("windows_optional", "windows_environment.optional", "WSL2 is an explicit optional environment, never the native default.")
    requirements = _object(windows["native_requirements"], {
        "toolchain_provisioning", "node_provisioning", "lab_sessions", "isolation"
    }, "windows_environment.native_requirements")
    for field, expected in (
        ("toolchain_provisioning", "operator-msvc-windows-sdk"),
        ("node_provisioning", "bundled"),
        ("lab_sessions", "ephemeral-with-verified-cleanup"),
        ("isolation", "operator-managed-os"),
    ):
        _choice(requirements[field], {expected}, "windows_environment.native_requirements." + field)
    clients = profile["clients"]
    if not isinstance(clients, list) or not 3 <= len(clients) <= 8:
        _fail("clients", "clients", "Declare three to eight target/architecture combinations.")
    seen: set[tuple[str, str]] = set()
    artifact_paths: set[str] = set()
    for index, entry in enumerate(clients):
        loc = f"clients[{index}]"
        client = _object(entry, {"target", "architecture", "artifact", "compatibility",
                                 "test_status", "test_evidence_ref", "support_status",
                                 "signature_status", "signature_evidence_ref"}, loc)
        target = _choice(client["target"], TARGETS, loc + ".target")
        architecture = _choice(client["architecture"], {"arm64", "x64"}, loc + ".architecture")
        if (target, architecture) in seen:
            _fail("duplicate_client", loc, "Each target and architecture must be unique.")
        seen.add((target, architecture))
        artifact = _object(client["artifact"], {"build_status", "path", "sha256"}, loc + ".artifact")
        built = _choice(artifact["build_status"], {"not-built", "built"}, loc + ".artifact.build_status") == "built"
        if built:
            path = _reference(artifact["path"], loc + ".artifact.path")
            if path in artifact_paths:
                _fail("duplicate_artifact", loc + ".artifact.path", "Each client package must have its own artifact reference.")
            artifact_paths.add(path)
            if not isinstance(artifact["sha256"], str) or not _DIGEST.fullmatch(artifact["sha256"]):
                _fail("digest", loc + ".artifact.sha256", "Built artifacts require a lowercase SHA-256 digest.")
        elif artifact["path"] is not None or artifact["sha256"] is not None:
            _fail("unbuilt_artifact", loc + ".artifact", "Unbuilt packages cannot declare an artifact or digest.")
        compatibility = _object(client["compatibility"], {"server_release", "extension_version",
                                                          "os_requirement", "toolchain_requirement"}, loc + ".compatibility")
        if compatibility["server_release"] != release["id"]:
            _fail("server_release", loc + ".compatibility.server_release", "Client compatibility must name this exact server release.")
        if not _VERSION.fullmatch(_text(compatibility["extension_version"], loc + ".compatibility.extension_version", 64)):
            _fail("version", loc + ".compatibility.extension_version", "Declare an exact extension version.")
        _text(compatibility["os_requirement"], loc + ".compatibility.os_requirement")
        _text(compatibility["toolchain_requirement"], loc + ".compatibility.toolchain_requirement")
        test = _choice(client["test_status"], {"not-tested", "launch-tested", "workflow-tested"}, loc + ".test_status")
        support = _choice(client["support_status"], {"planned", "evaluation-only"}, loc + ".support_status")
        signature = _choice(client["signature_status"], {"unsigned", "signed"}, loc + ".signature_status")
        for claimed, reference, field in (
            (test != "not-tested", client["test_evidence_ref"], "test_evidence_ref"),
            (signature == "signed", client["signature_evidence_ref"], "signature_evidence_ref"),
        ):
            if claimed:
                if not built:
                    _fail("unbuilt_claim", loc, "Testing and signing claims require a built artifact.")
                _reference(reference, loc + "." + field)
            elif reference is not None:
                _fail("evidence", loc + "." + field, "Evidence is only accepted with its corresponding status.")
        if support == "evaluation-only" and not built:
            _fail("support", loc + ".support_status", "Evaluation availability requires a built artifact.")
    if {target for target, _ in seen} != REQUIRED_TARGETS | set(windows["optional"]):
        _fail("targets", "clients", "Declare macOS, Windows and Linux native targets, plus exactly the selected optional WSL2 target.")
    gates = _object(profile["release_gates"], set(GATES), "release_gates")
    for name in GATES:
        gate = _object(gates[name], {"status", "evidence_ref"}, "release_gates." + name)
        _choice(gate["status"], {"pending", "evidence-recorded"}, "release_gates." + name + ".status")
        if gate["status"] == "evidence-recorded":
            _reference(gate["evidence_ref"], "release_gates." + name + ".evidence_ref")
        elif gate["evidence_ref"] is not None:
            _fail("evidence", "release_gates." + name, "Pending gates cannot claim evidence.")
    return profile


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate_field", "$", "Duplicate JSON fields are not allowed.")
        value[key] = item
    return value


def _read_profile(source: Path) -> dict[str, Any]:
    try:
        if source.is_symlink() or not stat.S_ISREG(source.stat().st_mode):
            _fail("profile_file", "$", "Profile must be a regular file, not a symlink.")
        with source.open("rb") as stream:
            data = stream.read(MAX_PROFILE_BYTES + 1)
        if len(data) > MAX_PROFILE_BYTES:
            _fail("profile_size", "$", "Profile exceeds the size limit.")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: _fail("json", "$", "Non-finite JSON numbers are forbidden."))
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, ProductValidationError):
            raise
        _fail("profile_file", "$", "Profile must be a readable UTF-8 JSON file.")
    return validate_definition(value)


def _reference_file(root: Path, reference: str, location: str) -> Path:
    path = root
    for part in reference.split("/"):
        path = path / part
        if path.is_symlink():
            _fail("reference_file", location, "Referenced files and their parent directories must not be symlinks.")
    try:
        if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
            _fail("reference_file", location, "Referenced file must exist within the profile directory.")
    except (OSError, RuntimeError):
        _fail("reference_file", location, "Referenced file is unavailable.")
    return path


def _check_files(profile: dict[str, Any], root: Path) -> dict[str, int]:
    references: list[tuple[str, str]] = []
    artifact_count = 0
    if profile["release"]["approval_ref"] is not None:
        references.append((profile["release"]["approval_ref"], "release.approval_ref"))
    for index, client in enumerate(profile["clients"]):
        loc = f"clients[{index}]"
        artifact = client["artifact"]
        if artifact["build_status"] == "built":
            path = _reference_file(root, artifact["path"], loc + ".artifact.path")
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError:
                _fail("artifact_file", loc + ".artifact.path", "Artifact could not be read.")
            if digest.hexdigest() != artifact["sha256"]:
                _fail("artifact_digest", loc + ".artifact.sha256", "Artifact does not match its declared SHA-256 digest.")
            artifact_count += 1
        for field in ("test_evidence_ref", "signature_evidence_ref"):
            if client[field] is not None:
                references.append((client[field], loc + "." + field))
    for name, gate in profile["release_gates"].items():
        if gate["evidence_ref"] is not None:
            references.append((gate["evidence_ref"], "release_gates." + name + ".evidence_ref"))
    for reference, location in references:
        _reference_file(root, reference, location)
    return {"artifacts": artifact_count, "evidence_references": len(references)}


def _check_pilot(profile: dict[str, Any], path: str | Path) -> None:
    try:
        values = load_pilot_config(path).values
    except (ValueError, OSError, RuntimeError):
        _fail("pilot_config", "pilot_config", "Pilot CSV is unavailable or invalid; review it separately.")
    for key, expected in (
        ("public_base_url", profile["origins"]["api"]),
        ("web_public_base_url", profile["origins"]["web"]),
        ("grading_runtime", profile["runtime"]["grading"]),
        ("instructor_assignment_web_enabled", profile["runtime"]["instructor_web"]),
        ("external_access_mode", "disabled"),
    ):
        if values.get(key) != expected:
            _fail("pilot_mismatch", "pilot_config." + key, "Pilot CSV differs from the declared institution release.")


def validate_profile(path: str | Path, *, check_artifacts: bool = False,
                     pilot_config: str | Path | None = None) -> dict[str, Any]:
    """Return a JSON-safe report. All operations are read-only and offline."""
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA, "valid": False, "deployment_ready": False,
        "runtime_config_changed": False, "evidence_verified": False,
        "checks": {"definition": "not-run", "artifacts": "not-requested",
                   "pilot_alignment": "not-requested"},
        "errors": [], "blocking_gates": ["independent_release_acceptance"],
    }
    try:
        source = Path(path).absolute()
        profile = _read_profile(source)
        report["checks"]["definition"] = "passed"
        report["deployment_id"] = profile["deployment"]["id"]
        report["release_id"] = profile["release"]["id"]
        report["windows_environment"] = profile["windows_environment"]
        report["blocking_gates"] = list(GATES) + ["independent_release_acceptance"]
        report["recorded_gates"] = [name for name, gate in profile["release_gates"].items()
                                    if gate["status"] == "evidence-recorded"]
        report["clients"] = [{"target": entry["target"], "architecture": entry["architecture"],
                              "build_status": entry["artifact"]["build_status"],
                              "test_status": entry["test_status"],
                              "signature_status": entry["signature_status"],
                              "support_status": entry["support_status"]} for entry in profile["clients"]]
        if check_artifacts:
            report["checks"]["artifacts"] = "failed"
            report["checked_files"] = _check_files(profile, source.parent.resolve(strict=True))
            report["checks"]["artifacts"] = "passed"
        if pilot_config is not None:
            report["checks"]["pilot_alignment"] = "failed"
            _check_pilot(profile, pilot_config)
            report["checks"]["pilot_alignment"] = "passed"
        report["valid"] = True
    except ProductValidationError as exc:
        if report["checks"]["definition"] == "not-run":
            report["checks"]["definition"] = "failed"
        report["errors"].append({"code": exc.code, "location": exc.location, "message": str(exc)})
    except (OSError, ValueError, RuntimeError):
        report["errors"].append({"code": "unavailable", "location": "$", "message": "A required local file is unavailable."})
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a release definition; never activate it.")
    validate.add_argument("--profile", required=True)
    validate.add_argument("--check-artifacts", action="store_true", help="Check referenced files and package hashes.")
    validate.add_argument("--pilot-config", help="Compare an existing runtime CSV without changing it.")
    args = parser.parse_args(argv)
    report = validate_profile(args.profile, check_artifacts=args.check_artifacts, pilot_config=args.pilot_config)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
