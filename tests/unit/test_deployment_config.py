"""Deployment profiles keep runtime origins, private data and proxy rules aligned."""
import csv
import importlib.util
import io
import json
from pathlib import Path

import pytest

from autograde.pilot_config import load_pilot_config


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("deployment_config", ROOT / "scripts/deploy/deployment_config.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


@pytest.fixture
def profile(tmp_path):
    return {
        "schema": "autograde.deployment.v1", "deployment": "institution", "platform": "ubuntu",
        "installation_id": "campus-lab", "install_dir": str(tmp_path / "install"),
        "web_url": "https://classroom.school.example", "api_url": "https://api.classroom.school.example",
        "runner_image": "ghcr.io/example/fixed-stdio@sha256:" + "a" * 64,
        "tls_certificate": str(tmp_path / "certs/fullchain.pem"),
        "tls_key": str(tmp_path / "certs/privkey.pem"),
        "allowed_cidrs": ["192.168.50.0/24"],
    }


def test_load_profile_defaults_and_unknown_or_duplicate_fields(tmp_path, profile):
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(profile))
    normalized = deploy.load_profile(path)
    assert normalized["worker_count"] == 4
    assert normalized["ssh_port"] == 22
    assert normalized["course_keys"] == []
    assert "worker_count" not in profile
    assert deploy.validate_profile(normalized) == normalized
    path.write_text(json.dumps({**profile, "admin_password": "must-not-be-in-a-profile"}))
    with pytest.raises(deploy.DeploymentConfigError, match="unsupported"):
        deploy.load_profile(path)
    path.write_text('{"schema":"one","schema":"two"}')
    with pytest.raises(deploy.DeploymentConfigError, match="duplicate"):
        deploy.load_profile(path)


@pytest.mark.parametrize("field,value", [
    ("web_url", "http://classroom.school.example"),
    ("web_url", "https://classroom.school.example/"),
    ("web_url", "https://classroom.school.example/api"),
    ("web_url", "https://classroom.school.example:443"),
    ("web_url", "https://classroom.school.example:18080"),
    ("api_url", "https://api.classroom.school.example:18082"),
    ("web_url", "https://Classroom.school.example"),
    ("web_url", "https://name:password@classroom.school.example"),
    ("web_url", "https://classroom.school.example?secret=value"),
    ("web_url", "https://classroom.school.example#fragment"),
    ("web_url", "https://classroom.school.example:70000"),
    ("web_url", "https://classroom.school.example:0"),
    ("web_url", "https://0.0.0.0"),
    ("web_url", "https://224.0.0.1"),
    ("web_url", "https://999.0.0.1"),
    ("web_url", "https://classroom.school.example\n"),
    ("web_url", "https://example;include.school.example"),
    ("web_url", "https://[::1]"),
    ("installation_id", "campus;include"),
    ("installation_id", "--campus"),
    ("installation_id", "campus\nlab"),
    ("installation_id", "x" * 49),
    ("deployment", []),
    ("platform", {}),
    ("install_dir", "relative/install"),
    ("install_dir", "/"),
    ("install_dir", "/srv/../etc"),
    ("install_dir", "/srv//autograde"),
    ("install_dir", "/srv/autograde/"),
    ("install_dir", "/srv/autograde\ninclude"),
    ("tls_key", "/etc/$secret.pem"),
    ("tls_certificate", '/etc/cert";include.pem'),
    ("tls_key", "/etc/$(command).pem"),
    ("tls_key", "C:\\certs\\key.pem"),
    ("runner_image", "ghcr.io/example/fixed-stdio:latest"),
    ("runner_image", "ghcr.io/example/fixed-stdio@sha256:" + "F" * 64),
    ("runner_image", "ghcr.io/example/fixed-stdio@sha256:" + "a" * 63),
    ("runner_image", "ghcr.io:70000/example/fixed-stdio@sha256:" + "a" * 64),
    ("runner_image", "ghcr.io/example/fixed-stdio@sha256:" + "a" * 64 + ";id"),
    ("worker_count", True), ("worker_count", 0), ("worker_count", 17),
    ("ssh_port", True), ("ssh_port", 0),
    ("course_keys", ["come2201", "come2201"]),
    ("course_keys", ["COME2201"]),
    ("course_keys", ["come2201;all"]),
    ("allowed_cidrs", []),
    ("allowed_cidrs", ["192.168.50.1/24"]),
    ("allowed_cidrs", ["192.168.50.0/24", "192.168.50.0/24"]),
    ("allowed_cidrs", ["0.0.0.0/0"]),
    ("allowed_cidrs", ["::/0"]),
    ("allowed_cidrs", ["224.0.0.0/24"]),
    ("allowed_cidrs", ["192.168.0.0/16;allow all"]),
    ("ssh_target", "ubuntu@school.example"),
])
def test_invalid_profiles_fail_before_rendering(profile, field, value):
    with pytest.raises(deploy.DeploymentConfigError):
        deploy.validate_profile({**profile, field: value})


def test_separate_origins_and_certificate_files_required(profile):
    with pytest.raises(deploy.DeploymentConfigError, match="separate origins"):
        deploy.validate_profile({**profile, "api_url": profile["web_url"]})
    with pytest.raises(deploy.DeploymentConfigError, match="different files"):
        deploy.validate_profile({**profile, "tls_key": profile["tls_certificate"]})


def test_cloud_is_an_ubuntu_target_not_the_workstation_platform(profile):
    cloud = {**profile, "deployment": "cloud", "allowed_cidrs": [], "ssh_target": "ubuntu@grade.example.edu"}
    assert deploy.validate_profile(cloud)["platform"] == "ubuntu"
    for value in (None, "-oProxyCommand=evil", "ubuntu@host;id", "ubuntu@host:2222", "ubuntu@host\ncommand"):
        with pytest.raises(deploy.DeploymentConfigError):
            deploy.validate_profile({**cloud, "ssh_target": value})
    for platform in ("macos", "windows-wsl2"):
        with pytest.raises(deploy.DeploymentConfigError):
            deploy.validate_profile({**cloud, "platform": platform})
    assert deploy.validate_profile({**cloud, "ssh_target": "autograde-staging"})["ssh_target"] == "autograde-staging"


def test_rendered_csv_is_accepted_by_actual_application_and_preserves_private_root(profile):
    profile["course_keys"] = ["come2201", "come3105"]
    destination = Path(profile["install_dir"])
    rendered = deploy.render_installation(profile, ROOT, destination)
    assert not destination.exists(), "rendering is side-effect free"
    destination.mkdir()
    (destination / "pilot.csv").write_text(rendered["pilot.csv"])
    actual = load_pilot_config(destination / "pilot.csv").values
    assert actual["data_root"] == str(destination / "data")
    assert actual["instructor_auth_mode"] == "personal"
    assert actual["grading_runtime"] == "docker"
    assert actual["roster_bootstrap_mode"] == "web"
    assert actual["instructor_next_course_keys"] == ("come2201", "come3105")
    assert actual["instructor_next_results_enabled"] is True
    assert actual["port"] == 18080 and actual["web_port"] == 18081
    assert actual["listen"] == "127.0.0.1"
    ide = json.loads(rendered["ide/institution.json"])
    assert ide["apiUrl"] == actual["public_base_url"]
    assert ide["portalUrl"] == actual["web_public_base_url"]


def test_fresh_install_does_not_invent_course_allowlist(profile):
    rendered = deploy.render_installation(profile, ROOT, Path(profile["install_dir"]))
    csv_values = dict(list(csv.reader(io.StringIO(rendered["pilot.csv"])))[1:])
    assert csv_values["instructor_next_results_enabled"] == "false"
    assert csv_values["instructor_next_course_keys"] == ""


def test_proxy_preserves_upload_limits_cookies_and_event_streaming(profile):
    rendered = deploy.render_installation(profile, ROOT, Path(profile["install_dir"]))
    proxy, next_routes = rendered["config/nginx.conf"], rendered["config/nginx-next.locations.conf"]
    assert proxy.count("listen 443 ssl;") == 2
    assert "listen 20000" not in proxy and "listen 20010" not in proxy
    assert proxy.count("deny all;") == 2
    assert proxy.count("allow 192.168.50.0/24;") == 2
    assert proxy.count("allow 127.0.0.1/32;") == 2
    assert 'include "' + profile["install_dir"] + '/config/nginx-next.locations.conf";' in proxy
    assert proxy.count("nginx-next.locations.conf") == 1
    for size in ("2m", "6m", "1100k", "11m", "26m", "101m", "70m", "256k"):
        assert "client_max_body_size " + size + ";" in proxy
    assert "proxy_set_header Origin" not in proxy + next_routes
    assert "proxy_set_header Host $http_host;" in proxy
    assert "proxy_hide_header Referrer-Policy;" in proxy
    assert 'add_header Referrer-Policy "same-origin" always;' in proxy
    assert 'proxy_set_header Authorization "";' in next_routes
    assert 'proxy_set_header Cookie "";' in next_routes
    instructor_api = next_routes.split("location ^~ /instructor/api/v1/ {")[1].split("}")[0]
    assert "proxy_buffering off;" in instructor_api
    assert "proxy_intercept_errors off;" in instructor_api
    assert 'proxy_set_header Cookie "";' not in instructor_api
    assert 'proxy_set_header Authorization "";' not in instructor_api
    assert "daemon " not in proxy
    assert re_user(proxy) != "root"


def re_user(text):
    return next(line.split()[1].rstrip(";") for line in text.splitlines() if line.startswith("user "))


def test_same_hostname_different_public_ports_is_supported(profile):
    updated = {**profile, "web_url": "https://grade.school.example:20010", "api_url": "https://grade.school.example:20000"}
    rendered = deploy.render_installation(updated, ROOT, Path(profile["install_dir"]))
    proxy = rendered["config/nginx.conf"]
    assert "listen 20010 ssl;" in proxy
    assert "listen 20000 ssl;" in proxy
    assert proxy.count("server_name grade.school.example;") == 2
    assert rendered["config/DNS-records.txt"].count("grade.school.example  A") == 1


def test_cloud_network_and_mac_paths_are_rendered_without_shell_expansion(profile):
    updated = {**profile, "deployment": "cloud", "allowed_cidrs": [], "ssh_target": "ubuntu@grade.example.edu"}
    cloud = deploy.render_installation(updated, ROOT, Path(profile["install_dir"]))
    assert "deny all;" not in cloud["config/nginx.conf"]
    assert "authoritative DNS" in cloud["config/DNS-records.txt"]
    mac = {**profile, "platform": "macos", "install_dir": "/Users/교수/Autograde Server", "tls_key": "/Users/교수/Server Certificates/key.pem"}
    generated = deploy.render_installation(mac, ROOT, Path(mac["install_dir"]))
    assert 'pid "/Users/교수/Autograde Server/run/nginx.pid";' in generated["config/nginx.conf"]
    assert 'ssl_certificate_key "/Users/교수/Server Certificates/key.pem";' in generated["config/nginx.conf"]


def test_render_does_not_allow_profile_directory_substitution(profile, tmp_path):
    with pytest.raises(deploy.DeploymentConfigError, match="match"):
        deploy.render_installation(profile, ROOT, tmp_path / "other")


def test_profile_file_limits_and_symlinks(tmp_path, profile):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(deploy.DeploymentConfigError):
        deploy.load_profile(link)
    path.write_bytes(b"x" * (deploy.MAX_PROFILE_BYTES + 1))
    with pytest.raises(deploy.DeploymentConfigError):
        deploy.load_profile(path)
    path.write_bytes(b"\xff")
    with pytest.raises(deploy.DeploymentConfigError):
        deploy.load_profile(path)


def test_changed_proxy_template_fails_instead_of_silently_dropping_auth_rules(profile, tmp_path):
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    for filename in ("nginx-autograde-portal.conf.example", "nginx-autograde-next.locations.conf.example"):
        contents = (ROOT / "config" / filename).read_text()
        if "portal" in filename:
            contents = contents.replace("listen 20010 ssl;", "listen 20011 ssl;")
        (source / "config" / filename).write_text(contents)
    with pytest.raises(deploy.DeploymentConfigError, match="template changed"):
        deploy.render_installation(profile, source, Path(profile["install_dir"]))
