"""Non-secret deployment profiles and deterministic runtime configuration.

This module deliberately uses only Python's standard library: it must work
before the application and its dependencies have been installed. Rendering
does not write files, provision DNS, issue certificates or start services.
"""
from __future__ import annotations

import csv
import getpass
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit


SCHEMA = "autograde.deployment.v1"
MAX_PROFILE_BYTES = 65536
FIELDS = frozenset({
    "schema", "deployment", "platform", "installation_id", "install_dir",
    "web_url", "api_url", "runner_image", "tls_certificate", "tls_key",
    "allowed_cidrs", "course_keys", "worker_count", "ssh_target", "ssh_port",
})
REQUIRED = FIELDS - {"allowed_cidrs", "course_keys", "worker_count", "ssh_target", "ssh_port"}
IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?\Z")
COURSE = re.compile(r"[a-z0-9_-]{1,96}\Z")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9._-]*(?::[1-9][0-9]{0,4})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[0-9a-f]{64}\Z")
SSH = re.compile(r"(?:[a-z_][a-z0-9_.-]{0,63}@)?[A-Za-z0-9][A-Za-z0-9._-]{0,252}\Z")
INTERNAL_PORTS = frozenset({18080, 18081, 18082})
RUNTIME_DIRECTORIES = ("run", "logs", "run/nginx", "run/nginx/body", "run/nginx/proxy",
                       "run/nginx/fastcgi", "run/nginx/uwsgi", "run/nginx/scgi")


class DeploymentConfigError(ValueError):
    """A safe, operator-facing profile validation failure."""


def _fail(message):
    raise DeploymentConfigError(message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON key: " + str(key))
        result[key] = value
    return result


def _constant(value):
    _fail("non-finite JSON numbers are not supported")


def _path(value, field):
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 2048:
        _fail(field + " must be an absolute POSIX path")
    # All command execution uses argv, but these paths also enter Nginx config.
    # Keep a small shared alphabet and quote them there. Unicode letters and
    # spaces are useful for macOS user directories; expansion syntax is not.
    if any(not (c.isalnum() or c in "/_. -") for c in value):
        _fail(field + " contains unsupported path characters")
    parts = value.split("/")[1:]
    if not parts or any(not p or p in {".", ".."} or p.strip() != p for p in parts):
        _fail(field + " must be a canonical path without traversal or empty components")
    if str(PurePosixPath(value)) != value:
        _fail(field + " must use a canonical POSIX path")
    return value


def _origin(value, field):
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        _fail(field + " must be a canonical HTTPS origin")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port or 443
    except ValueError:
        _fail(field + " has an invalid host or port")
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or not host):
        _fail(field + " must contain only https://host and an optional non-default port")
    if not 1 <= port <= 65535 or port in INTERNAL_PORTS:
        _fail(field + " port conflicts with a backend listener or is invalid")
    if re.fullmatch(r"[0-9.]+", host):
        try:
            address = ipaddress.IPv4Address(host)
        except ValueError:
            _fail(field + " contains an invalid IPv4 address")
        if address.is_unspecified or address.is_multicast:
            _fail(field + " cannot use an unspecified or multicast address")
    elif len(host) > 253 or not all(LABEL.fullmatch(label) for label in host.split(".")):
        _fail(field + " must use a lowercase DNS name or IPv4 address")
    canonical = "https://" + host + (":" + str(port) if port != 443 else "")
    if value != canonical:
        _fail(field + " must be lowercase, without a trailing slash or explicit :443")
    return value


def _integer(value, field, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def validate_profile(raw):
    """Return a normalized copy; unknown/secret fields and injections fail closed."""
    if not isinstance(raw, dict):
        _fail("deployment profile must be a JSON object")
    if set(raw) - FIELDS:
        _fail("unsupported deployment profile fields")
    if REQUIRED - set(raw):
        _fail("missing deployment profile fields: " + ", ".join(sorted(REQUIRED - set(raw))))
    profile = dict(raw)
    if profile["schema"] != SCHEMA:
        _fail("unsupported deployment profile schema")
    if not isinstance(profile["deployment"], str) or profile["deployment"] not in {"institution", "cloud"}:
        _fail("deployment must be institution or cloud")
    if not isinstance(profile["platform"], str) or profile["platform"] not in {"ubuntu", "macos", "windows-wsl2"}:
        _fail("platform must be ubuntu, macos or windows-wsl2")
    if profile["deployment"] == "cloud" and profile["platform"] != "ubuntu":
        _fail("cloud targets use Ubuntu; the deployment workstation may use any supported OS")
    if not isinstance(profile["installation_id"], str) or not IDENTIFIER.fullmatch(profile["installation_id"]):
        _fail("installation_id must be 1-48 lowercase letters, digits or interior hyphens")
    for key in ("install_dir", "tls_certificate", "tls_key"):
        profile[key] = _path(profile[key], key)
    if profile["tls_certificate"] == profile["tls_key"]:
        _fail("TLS certificate and private key must be different files")
    for key in ("web_url", "api_url"):
        profile[key] = _origin(profile[key], key)
    if profile["web_url"] == profile["api_url"]:
        _fail("web_url and api_url must use separate origins")
    image = profile["runner_image"]
    if not isinstance(image, str) or len(image) > 512 or not IMAGE.fullmatch(image):
        _fail("runner_image must be a repository pinned to a lowercase sha256 digest")
    registry = image.split("/", 1)[0]
    if ":" in registry and int(registry.rsplit(":", 1)[1]) > 65535:
        _fail("runner_image registry port is invalid")
    profile["worker_count"] = _integer(profile.get("worker_count", 4), "worker_count", 1, 16)
    profile["ssh_port"] = _integer(profile.get("ssh_port", 22), "ssh_port", 1, 65535)
    target = profile.get("ssh_target")
    if profile["deployment"] == "cloud":
        if not isinstance(target, str) or not SSH.fullmatch(target):
            _fail("cloud deployment requires an SSH host alias or user@host without shell syntax")
    elif target is not None:
        _fail("ssh_target is only available for cloud deployment")
    # No fake default public network: an institution installer needs an explicit
    # reviewed client range before exposing its listeners.
    cidrs = profile.get("allowed_cidrs", [])
    if not isinstance(cidrs, list) or len(cidrs) > 64:
        _fail("allowed_cidrs must be a list of up to 64 client networks")
    if profile["deployment"] == "institution" and not cidrs:
        _fail("institution deployment requires allowed_cidrs")
    normalized = []
    for value in cidrs:
        try:
            if not isinstance(value, str):
                raise ValueError()
            network = ipaddress.ip_network(value, strict=True)
            if value != str(network) or network.prefixlen == 0 or network.is_multicast:
                raise ValueError()
        except ValueError:
            _fail("allowed_cidrs entries must be canonical, bounded unicast CIDRs")
        normalized.append(str(network))
    if len(set(normalized)) != len(normalized):
        _fail("allowed_cidrs must not contain duplicates")
    profile["allowed_cidrs"] = normalized
    courses = profile.get("course_keys", [])
    if (not isinstance(courses, list) or len(courses) > 100
            or any(not isinstance(c, str) or not COURSE.fullmatch(c) for c in courses)
            or len(set(courses)) != len(courses)):
        _fail("course_keys must be distinct lowercase course keys, up to 100 entries")
    profile["course_keys"] = list(courses)
    return profile


def load_profile(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PROFILE_BYTES:
        _fail("profile must be a regular UTF-8 JSON file up to 64 KiB")
    try:
        with path.open(encoding="utf-8") as stream:
            raw = json.load(stream, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError):
        _fail("profile must be valid UTF-8 JSON")
    return validate_profile(raw)


def _nginx_quote(path):
    # Called only for paths already validated by _path, not arbitrary strings.
    return '"' + str(path) + '"'


def _replace_once(text, old, new):
    if text.count(old) != 1:
        _fail("Nginx source template changed; review the deployment renderer")
    return text.replace(old, new, 1)


def _worker_user(install_dir):
    try:
        import grp
        import pwd
        uid = install_dir.stat().st_uid if install_dir.exists() else os.getuid()
        account = pwd.getpwnam("nobody") if uid == 0 else pwd.getpwuid(uid)
        user, group = account.pw_name, grp.getgrgid(account.pw_gid).gr_name
    except ImportError:
        # Useful for a Windows planning run; the actual WSL/cloud installation
        # renders again on its POSIX target before starting the proxy.
        user = group = getpass.getuser()
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", value)
           for value in (user, group)):
        _fail("the deployment owner must have Nginx-compatible OS account and group names")
    return user + " " + group


def _render_nginx(profile, source_dir, install_dir):
    template = (source_dir / "config/nginx-autograde-portal.conf.example").read_text(encoding="utf-8")
    next_template = (source_dir / "config/nginx-autograde-next.locations.conf.example").read_text(encoding="utf-8")
    sections = template.split("\nserver {\n")
    if len(sections) != 3:
        _fail("Nginx portal template must contain exactly two servers")
    preamble, web, api = sections
    # The source is an http{} include; the generated file owns its own prefix.
    preamble = "\n".join(line for line in preamble.splitlines() if not line.startswith("#"))
    acl = ""
    if profile["allowed_cidrs"]:
        networks = list(dict.fromkeys(["127.0.0.1/32", "::1/128", *profile["allowed_cidrs"]]))
        acl = "\n".join("    allow " + value + ";" for value in networks) + "\n    deny all;\n"
    servers = []
    for body, origin, original_port in ((web, profile["web_url"], 20010), (api, profile["api_url"], 20000)):
        address = urlsplit(origin)
        body = _replace_once(body, f"    listen {original_port} ssl;", f"    listen {address.port or 443} ssl;")
        body = _replace_once(body, "    server_name grade.example.edu;", "    server_name " + address.hostname + ";\n" + acl)
        body = _replace_once(body, "    ssl_certificate /etc/letsencrypt/live/grade.example.edu/fullchain.pem;",
                             "    ssl_certificate " + _nginx_quote(profile["tls_certificate"]) + ";")
        body = _replace_once(body, "    ssl_certificate_key /etc/letsencrypt/live/grade.example.edu/privkey.pem;",
                             "    ssl_certificate_key " + _nginx_quote(profile["tls_key"]) + ";")
        if original_port == 20010:
            body = _replace_once(body, "    # include /opt/autograde/config/nginx-autograde-next.locations.conf.example;",
                                 "    include " + _nginx_quote(install_dir / "config/nginx-next.locations.conf") + ";")
        servers.append("server {\n" + body)
    prefix = ("# Generated Autograde standalone proxy. Does not modify system Nginx.\n"
              "user " + _worker_user(install_dir) + ";\n"
              "worker_processes 1;\n"
              "pid " + _nginx_quote(install_dir / "run/nginx.pid") + ";\n"
              "error_log " + _nginx_quote(install_dir / "logs/nginx-error.log") + " crit;\n"
              "events { worker_connections 1024; }\n"
              "http {\n"
              "    default_type application/octet-stream;\n"
              "    server_tokens off;\n"
              "    access_log off;\n"
              "    client_body_temp_path " + _nginx_quote(install_dir / "run/nginx/body") + ";\n"
              "    proxy_temp_path " + _nginx_quote(install_dir / "run/nginx/proxy") + ";\n"
              "    fastcgi_temp_path " + _nginx_quote(install_dir / "run/nginx/fastcgi") + ";\n"
              "    uwsgi_temp_path " + _nginx_quote(install_dir / "run/nginx/uwsgi") + ";\n"
              "    scgi_temp_path " + _nginx_quote(install_dir / "run/nginx/scgi") + ";\n")
    return prefix + preamble + "\n" + "\n".join(servers) + "\n}\n", next_template


def render_installation(profile, source_dir, install_dir):
    """Return relative path -> content for a fresh deployment directory.

    ``pilot.csv`` belongs at the installation root because the application
    requires its data directory to be a child of the CSV's own directory.
    No source checkout path is embedded in any generated runtime config.
    """
    profile = validate_profile(profile)
    source_dir, install_dir = Path(source_dir), Path(install_dir)
    if str(install_dir) != profile["install_dir"]:
        _fail("installation directory must match the deployment profile")
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows([
        ("key", "value"), ("course_key", "institution"), ("data_root", "data"),
        ("public_base_url", profile["api_url"]), ("web_public_base_url", profile["web_url"]),
        ("listen", "127.0.0.1"), ("port", "18080"), ("web_port", "18081"),
        ("external_access_mode", "disabled"), ("grading_runtime", "docker"),
        ("instructor_runner_image", profile["runner_image"]),
        ("bundle_worker_count", str(profile["worker_count"])),
        ("instructor_auth_mode", "personal"), ("instructor_assignment_web_enabled", "true"),
        ("roster_bootstrap_mode", "web"),
        ("instructor_next_results_enabled", "true" if profile["course_keys"] else "false"),
        ("instructor_next_course_keys", ";".join(profile["course_keys"])),
    ])
    nginx, next_locations = _render_nginx(profile, source_dir, install_dir)
    hosts = list(dict.fromkeys(urlsplit(profile[name]).hostname for name in ("web_url", "api_url")))
    dns = ["Autograde DNS planning record — no DNS/DHCP settings were changed."]
    if profile["deployment"] == "institution":
        dns += ["Register these names in the institution's internal DNS with the server's LAN address.",
                "Distribute that resolver through the existing DHCP policy; do not start a second DHCP server.",
                "Permit client networks to the configured HTTPS ports. DNS is not an access-control boundary."]
    else:
        dns += ["Register these names in the authoritative DNS zone with the Ubuntu server/load-balancer address.",
                "Review the cloud firewall for the configured HTTPS ports; backend ports stay loopback-only."]
    for host in hosts:
        try:
            ipaddress.IPv4Address(host)
            dns.append(host + " — IP origin; certificate SAN must cover this address.")
        except ValueError:
            dns.append(host + "  A  <reviewed server IPv4 address>")
    dns += ["The certificate must cover both origins. Arrange certificate renewal separately.",
            "For an internal service, DNS-01 can validate a domain you own without public HTTP ingress.",
            "A .local name uses mDNS; prefer an institution-owned DNS subdomain.",
            "The IDE profile in ide/institution.json uses the same origins as pilot.csv."]
    return {
        "pilot.csv": out.getvalue(),
        "config/nginx.conf": nginx,
        "config/nginx-next.locations.conf": next_locations,
        "config/DNS-records.txt": "\n".join(dns) + "\n",
        "ide/institution.json": json.dumps({
            "schema": "autograde.ide-institution.v1", "institutionId": profile["installation_id"],
            "apiUrl": profile["api_url"], "portalUrl": profile["web_url"],
        }, ensure_ascii=False, indent=2) + "\n",
    }
