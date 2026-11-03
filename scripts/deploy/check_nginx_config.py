#!/usr/bin/env python3
"""Validate generated Nginx configs in a disposable, network-disabled container.

Requires a locally pulled official Debian-based Nginx image by immutable digest,
Docker and openssl. It never starts a host listener or installs a CA certificate.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from deployment_config import RUNTIME_DIRECTORIES, render_installation


ROOT = Path(__file__).resolve().parents[2]
IMAGE = re.compile(r"(?:docker.io/library/)?nginx@sha256:[0-9a-f]{64}\Z")
CHECK = r'''
set -eu
user=$1; group=$2; uid=$3; root=$4
if ! getent group "$group" >/dev/null; then groupadd "$group"; fi
if ! id "$user" >/dev/null 2>&1; then useradd -M -o -u "$uid" -g "$group" "$user"; fi
nginx -t -p "$root/" -c "$root/config/nginx.conf"
'''


def run(image):
    if not IMAGE.fullmatch(image):
        raise ValueError("--image must name an official Nginx image pinned by SHA-256")
    inspection = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
    evidence = {"image_id": inspection["Id"], "image_digests": inspection["RepoDigests"], "cases": []}
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nginx-config-", dir=build) as temporary:
        temp = Path(temporary).resolve()
        cert, key = temp / "cert.pem", temp / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=classroom.school.example", "-keyout", str(key), "-out", str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600)
        for mode in ("institution", "cloud"):
            root = temp / (mode + " install")
            root.mkdir(mode=0o700)
            profile = {"schema": "autograde.deployment.v1", "deployment": mode, "platform": "ubuntu",
                "installation_id": "syntax-check", "install_dir": str(root),
                "web_url": "https://classroom.school.example", "api_url": "https://api.classroom.school.example",
                "runner_image": "ghcr.io/example/fixed-stdio@sha256:" + "a" * 64,
                "tls_certificate": str(cert), "tls_key": str(key),
                "allowed_cidrs": ["192.168.50.0/24"] if mode == "institution" else [],
                "course_keys": ["come2201"]}
            if mode == "cloud":
                profile.update(ssh_target="ubuntu@cloud.example", web_url="https://grade.school.example:20010",
                               api_url="https://grade.school.example:20000")
            rendered = render_installation(profile, ROOT, root)
            for name, contents in rendered.items():
                output = root / name
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                output.write_text(contents, encoding="utf-8")
                output.chmod(0o600)
            for name in RUNTIME_DIRECTORIES:
                (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
            identity = next(line for line in rendered["config/nginx.conf"].splitlines() if line.startswith("user "))
            user, group = identity.removeprefix("user ").rstrip(";").split()
            # The exact generated absolute paths are visible in the container.
            result = subprocess.run(["docker", "run", "--rm", "--pull", "never", "--network", "none",
                "--memory", "128m", "--pids-limit", "64", "--cpus", "1", "--entrypoint", "/bin/sh",
                "--mount", f"type=bind,source={temp},target={temp}", image, "-c", CHECK, "--",
                user, group, str(os.getuid()), str(root)], capture_output=True, text=True, timeout=40)
            if result.returncode:
                raise RuntimeError(mode + " Nginx configuration failed: " + result.stderr[-4000:])
            evidence["cases"].append({"deployment": mode, "nginx_configuration": "passed",
                                      "paths_with_spaces": True, "started_host_listener": False})
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.image), indent=2))
