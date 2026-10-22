#!/usr/bin/env python3
"""Prepare an isolated, trusted-code-only Code-OSS end-to-end pilot.

This creates a NEW private directory, two synthetic students, a separate SQLite
database and a validated C++17 Hello World release. It neither starts a listener
nor reads an existing roster/database. Credentials stay in a mode-0600 file;
stdout contains only non-secret connection information and the launch command.
Run with the repository's editable Python environment, without AUTOGRADE_* vars.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[1]
COURSE = "come2201"
ASSIGNMENT = "basn_codeoss_cpp_e2e"
OBSERVER_ASSIGNMENT = "basn_codeoss_observer_e2e"


def check_ports(api_port: int, web_port: int) -> None:
    if api_port == web_port or any(not 1024 <= port <= 65535 for port in (api_port, web_port)):
        raise ValueError("use two distinct unprivileged loopback ports")
    with socket.socket() as api, socket.socket() as web:
        api.bind(("127.0.0.1", api_port))
        web.bind(("127.0.0.1", web_port))


def private_text(path: Path, text: str) -> None:
    # Exclusive creation also prevents accidental replacement of a prior fixture.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)


def cli(config: Path, *arguments: str) -> dict:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AUTOGRADE_")}
    result = subprocess.run(
        [sys.executable, "-m", "autograde.platform_cli", "--pilot-config", str(config), *arguments],
        cwd=REPOSITORY, env=environment, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=180, check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"fixture CLI {arguments[0]} failed without a JSON response") from None
    if result.returncode or not payload.get("ok"):
        # The CLI response excludes authentication secrets. Never echo stderr or
        # subprocess environment, which may contain unrelated caller data.
        raise RuntimeError(f"fixture CLI failed: {json.dumps(payload, ensure_ascii=False)}")
    return payload["result"]


def prepare(root: Path | None, api_port: int, web_port: int) -> dict:
    check_ports(api_port, web_port)
    if root is None:
        build = REPOSITORY / "build"
        build.mkdir(exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="codeoss-e2e-", dir=build))
    else:
        root = root.expanduser().absolute()
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
    root.chmod(0o700)
    config = root / "pilot.csv"
    private_text(config, "key,value\n" + "\n".join((
        f"course_key,{COURSE}", "data_root,state",
        f"public_base_url,http://127.0.0.1:{api_port}", "listen,127.0.0.1", f"port,{api_port}",
        f"web_public_base_url,http://127.0.0.1:{web_port}", f"web_port,{web_port}",
        "grading_runtime,pilot-local", "bundle_worker_count,2", "instructor_assignment_web_enabled,true",
    )) + "\n")
    students = [{"student_key": key, "password": f"{secrets.randbelow(1_000_000):06d}"}
                for key in ("codeoss-gui-001", "codeoss-http-002")]
    while students[0]["password"] == students[1]["password"]:
        students[1]["password"] = f"{secrets.randbelow(1_000_000):06d}"
    credentials = root / "synthetic-credentials.json"
    private_text(credentials, json.dumps({"course_key": COURSE, "students": students}, indent=2) + "\n")
    private_text(root / "roster.csv", "student_key,active,password\n" + "".join(
        f"{entry['student_key']},true,{entry['password']}\n" for entry in students))
    private_text(root / "student_roster.csv", "course_key,student_key,active,password\n" + "".join(
        f"{COURSE},{entry['student_key']},true,{entry['password']}\n" for entry in students))
    cli(config, "init")
    cli(config, "student", "import", str(root / "roster.csv"))
    example = REPOSITORY / "examples" / "hello-world"
    cli(config, "assignment", "bundle-add", "codeoss-cpp-e2e", "--assignment-id", ASSIGNMENT,
        "--release-id", "codeoss-cpp-e2e-v1", "--title", "[격리 E2E] C++ Hello World",
        "--starter", str(example / "linux" / "starter"), "--assessment", str(example / "assessment"),
        "--data", str(example / "data"), "--max-score", "10", "--due-at",
        (datetime.now(timezone.utc) + timedelta(days=7)).isoformat())
    validation = cli(config, "assignment", "bundle-check", ASSIGNMENT,
        "--solution", str(example / "solution"), "--negative-solution", str(example / "linux" / "starter"),
        "--negative-score", "5")
    if validation.get("status") != "passed":
        raise RuntimeError("trusted C++ release validation did not pass")
    cli(config, "assignment", "bundle-ready", ASSIGNMENT)
    metadata = {
        "fixture_root": str(root), "config": str(config), "credentials_file": str(credentials),
        "course_key": COURSE, "assignment_id": ASSIGNMENT,
        "api_url": f"http://127.0.0.1:{api_port}", "web_url": f"http://127.0.0.1:{web_port}",
        "student_url": f"http://127.0.0.1:{web_port}/courses/{COURSE}",
        "solution": str(example / "solution"), "negative_solution": str(example / "linux" / "starter"),
        "expected_scores": [5, 10, 5], "validation": validation,
        "serve_command": shlex.join([sys.executable, "-m", "autograde.pilot_portal_cli", "--config", str(config)]),
        "warning": "Loopback only; trusted synthetic code; pilot-local is NOT a sandbox.",
    }
    private_text(root / "fixture.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return metadata


def add_observer(fixture_path: Path) -> dict:
    """Add the existing diagnostic-rich C++ lab to our isolated fixture only."""
    root = fixture_path.resolve().parent
    metadata = json.loads(fixture_path.read_text())
    if (Path(metadata["fixture_root"]).resolve() != root
            or Path(metadata["config"]).resolve() != root / "pilot.csv"
            or metadata["assignment_id"] != ASSIGNMENT
            or metadata["course_key"] != COURSE
            or "observer" in metadata):
        raise ValueError("expected a prepared fixture without an Observer release")
    config = root / "pilot.csv"
    from autograde.pilot_config import load_pilot_config
    values = load_pilot_config(config).values
    if (Path(values["data_root"]).resolve() != root / "state"
            or values["course_key"] != COURSE or values["listen"] != "127.0.0.1"
            or values["grading_runtime"] != "pilot-local"
            or not values["public_base_url"].startswith("http://127.0.0.1:")
            or not values.get("web_public_base_url", "").startswith("http://127.0.0.1:")):
        raise ValueError("fixture config no longer describes an isolated loopback pilot")
    # Reuse the known full-score implementation without importing test modules
    # or executing their decorators/compiler probes.
    reference = ast.parse((REPOSITORY / "tests/integration/test_observer_cpp_assessment.py").read_text())
    implementations = [ast.literal_eval(node.value) for node in reference.body
                       if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                       and target.id == "CORRECT_IMPLEMENTATION" for target in node.targets)]
    if len(implementations) != 1 or not isinstance(implementations[0], str):
        raise ValueError("verified Observer reference implementation unavailable")
    example = REPOSITORY / "examples/observer-cpp"
    solution = root / "observer-solution"
    shutil.copytree(example / "starter", solution)
    (solution / "observer.cpp").write_text(implementations[0], encoding="utf-8")
    cli(config, "assignment", "bundle-add", "codeoss-observer-e2e", "--assignment-id", OBSERVER_ASSIGNMENT,
        "--release-id", "codeoss-observer-e2e-v1", "--title", "[격리 E2E] C++ Observer 진단",
        "--starter", str(example / "starter"), "--assessment", str(example / "assessment"),
        "--data", str(example / "data"), "--max-score", "10", "--due-at",
        (datetime.now(timezone.utc) + timedelta(days=7)).isoformat())
    validation = cli(config, "assignment", "bundle-check", OBSERVER_ASSIGNMENT,
        "--solution", str(solution), "--negative-solution", str(example / "starter"), "--negative-score", "2")
    if validation.get("status") != "passed":
        raise RuntimeError("Observer reference validation did not pass")
    cli(config, "assignment", "bundle-ready", OBSERVER_ASSIGNMENT)
    metadata["observer"] = {"assignment_id": OBSERVER_ASSIGNMENT, "solution": str(solution),
        "negative_solution": str(example / "starter"), "expected_scores": [2, 10], "validation": validation}
    fixture_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return metadata["observer"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="new private fixture directory; parent must exist")
    parser.add_argument("--api-port", type=int, default=28000)
    parser.add_argument("--web-port", type=int, default=28010)
    parser.add_argument("--add-observer", type=Path, metavar="FIXTURE_JSON", help="add the diagnostic-rich Observer lab to an existing isolated fixture")
    arguments = parser.parse_args()
    metadata = (add_observer(arguments.add_observer) if arguments.add_observer
                else prepare(arguments.root, arguments.api_port, arguments.web_port))
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
