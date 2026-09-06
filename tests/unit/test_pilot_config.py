from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import autograde.platform_cli as platform_cli
from autograde.pilot_config import (
    MAX_PILOT_CONFIG_BYTES,
    PilotConfigError,
    apply_pilot_config,
    load_pilot_config,
)


def write_config(path: Path, *, extra: str = "") -> Path:
    path.write_text(
        "key,value\n"
        "course_key,cse101-2026f\n"
        "data_root,state\n"
        "public_base_url,http://127.0.0.1:18080\n"
        "listen,127.0.0.1\n"
        "port,18080\n"
        + extra,
        encoding="utf-8",
    )
    return path


def test_loads_typed_config_and_anchors_relative_data_root_at_csv_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "pilot"
    elsewhere = tmp_path / "elsewhere"
    folder.mkdir()
    elsewhere.mkdir()
    config_path = write_config(
        folder / "pilot.csv",
        extra="grading_runtime,pilot-local\nbundle_worker_count,7\n",
    )
    monkeypatch.chdir(elsewhere)

    config = load_pilot_config(config_path)

    assert config.source == config_path
    assert dict(config.values) == {
        "course_key": "cse101-2026f",
        "data_root": str(folder / "state"),
        "public_base_url": "http://127.0.0.1:18080",
        "listen": "127.0.0.1",
        "port": 18080,
        "grading_runtime": "pilot-local",
        "bundle_worker_count": 7,
        "external_access_mode": "disabled",
    }


def test_optional_pilot_values_have_local_defaults(tmp_path: Path) -> None:
    config = load_pilot_config(write_config(tmp_path / "pilot.csv"))

    assert config.values["grading_runtime"] == "pilot-local"
    assert config.values["bundle_worker_count"] == 4
    assert config.values["external_access_mode"] == "disabled"


def test_accepts_windows_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "pilot.csv"
    content = (
        "key,value\n"
        "course_key,cse101-2026f\n"
        "data_root,state\n"
        "public_base_url,http://localhost:18080\n"
        "listen,localhost\n"
        "port,18080\n"
    )
    path.write_bytes(content.encode("utf-8-sig"))

    assert load_pilot_config(path).values["listen"] == "localhost"


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("key,name\n", "header"),
        ("key,value,extra\n", "header"),
        ("key,value\ncourse_key,cse101,extra\n", "exactly two"),
        ("key,value\ncourse_key,cse101\ncourse_key,cse102\n", "duplicates"),
        ("key,value\nunknown,value\n", "unknown key"),
        ("key,value\nserver_token,do-not-store-this\n", "must not contain secrets"),
        ("key,value\ncourse_key,\n", "empty value"),
        ("key,value\nCourse_Key,cse101\n", "invalid key"),
    ],
)
def test_rejects_invalid_csv_structure_and_keys(
    tmp_path: Path, replacement: str, message: str
) -> None:
    path = write_config(tmp_path / "pilot.csv")
    path.write_text(replacement, encoding="utf-8")

    with pytest.raises(PilotConfigError, match=message):
        load_pilot_config(path)


def test_rejects_non_utf8_symlink_hardlink_and_oversize(
    tmp_path: Path,
) -> None:
    invalid_utf8 = tmp_path / "invalid.csv"
    invalid_utf8.write_bytes(b"key,value\ncourse_key,\xff\n")
    with pytest.raises(PilotConfigError, match="UTF-8"):
        load_pilot_config(invalid_utf8)

    source = write_config(tmp_path / "source.csv")
    symlink = tmp_path / "symlink.csv"
    symlink.symlink_to(source)
    with pytest.raises(PilotConfigError, match="unavailable|regular"):
        load_pilot_config(symlink)

    hardlink = tmp_path / "hardlink.csv"
    os.link(source, hardlink)
    with pytest.raises(PilotConfigError, match="regular"):
        load_pilot_config(hardlink)

    oversized = tmp_path / "oversized.csv"
    oversized.write_bytes(b"x" * (MAX_PILOT_CONFIG_BYTES + 1))
    with pytest.raises(PilotConfigError, match="64 KiB"):
        load_pilot_config(oversized)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("course_key", "contains a space", "course_key"),
        ("public_base_url", "http://grade.example:18080", "HTTPS"),
        ("public_base_url", "http://user:pass@localhost:18080", "credentials"),
        ("public_base_url", "http://localhost:18080/path", "without credentials"),
        ("public_base_url", "https://bad host", "listen host"),
        ("listen", "bad_host", "listen host"),
        ("port", "18.080", "integer"),
        ("port", "65536", "between"),
        ("grading_runtime", "shell", "pilot-local"),
        ("bundle_worker_count", "0", "between"),
        ("bundle_worker_count", "many", "integer"),
        ("external_access_mode", "enabled", "disabled or insecure-http"),
    ],
)
def test_rejects_invalid_typed_values(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    path = write_config(tmp_path / "pilot.csv")
    lines = path.read_text(encoding="utf-8").splitlines()
    rewritten = [
        f"{key},{value}" if line.startswith(key + ",") else line
        for line in lines
    ]
    if not any(line.startswith(key + ",") for line in lines):
        rewritten.append(f"{key},{value}")
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    with pytest.raises(PilotConfigError, match=message):
        load_pilot_config(path)


def test_rejects_mismatched_loopback_origin_and_listener(tmp_path: Path) -> None:
    path = write_config(tmp_path / "pilot.csv")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("port,18080", "port,18081"), encoding="utf-8")
    with pytest.raises(PilotConfigError, match="must match"):
        load_pilot_config(path)

    path.write_text(
        text.replace("listen,127.0.0.1", "listen,0.0.0.0"), encoding="utf-8"
    )
    with pytest.raises(PilotConfigError, match="127.0.0.1 or localhost"):
        load_pilot_config(path)


def test_pilot_local_accepts_https_proxy_and_rejects_unsupported_ipv6_listener(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path / "pilot.csv")
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "http://127.0.0.1:18080",
            "https://grade.example.test",
        ),
        encoding="utf-8",
    )
    config = load_pilot_config(path)
    assert config.values["public_base_url"] == "https://grade.example.test"
    assert config.values["listen"] == "127.0.0.1"
    assert config.values["grading_runtime"] == "pilot-local"

    path.write_text(
        text.replace(
            "http://127.0.0.1:18080",
            "http://[::1]:18080",
        ).replace("listen,127.0.0.1", "listen,::1"),
        encoding="utf-8",
    )
    with pytest.raises(PilotConfigError, match="127.0.0.1 or localhost"):
        load_pilot_config(path)


@pytest.mark.parametrize(
    "address",
    ("10.23.4.5", "172.16.0.1", "172.31.255.254", "192.168.50.9"),
)
def test_insecure_http_accepts_only_an_explicit_matching_rfc1918_binding(
    tmp_path: Path, address: str
) -> None:
    path = write_config(
        tmp_path / "pilot.csv",
        extra="external_access_mode,insecure-http\n",
    )
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("127.0.0.1", address),
        encoding="utf-8",
    )

    config = load_pilot_config(path)

    assert config.values["public_base_url"] == f"http://{address}:18080"
    assert config.values["listen"] == address
    assert config.values["external_access_mode"] == "insecure-http"


@pytest.mark.parametrize(
    ("public_url", "listen", "message"),
    (
        ("http://grade.lan:18080", "192.168.50.9", "RFC1918 IPv4 literal"),
        ("http://203.0.113.10:18080", "203.0.113.10", "RFC1918 IPv4 literal"),
        ("http://127.0.0.1:18080", "127.0.0.1", "RFC1918 IPv4 literal"),
        ("http://0.0.0.0:18080", "0.0.0.0", "RFC1918 IPv4 literal"),
        ("http://192.168.50.9:18080", "0.0.0.0", "RFC1918 IPv4 interface"),
        ("http://192.168.50.9:18080", "192.168.50.10", "must match"),
        ("https://192.168.50.9:18080", "192.168.50.9", "requires an HTTP"),
        ("http://192.168.50.9:18081", "192.168.50.9", "port must match"),
    ),
)
def test_insecure_http_rejects_unsafe_or_incoherent_bindings(
    tmp_path: Path, public_url: str, listen: str, message: str
) -> None:
    path = write_config(
        tmp_path / "pilot.csv",
        extra="external_access_mode,insecure-http\n",
    )
    text = path.read_text(encoding="utf-8")
    text = text.replace("http://127.0.0.1:18080", public_url)
    text = text.replace("listen,127.0.0.1", f"listen,{listen}")
    path.write_text(text, encoding="utf-8")

    with pytest.raises(PilotConfigError, match=message):
        load_pilot_config(path)


def test_private_http_binding_remains_rejected_without_explicit_mode(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path / "pilot.csv")
    path.write_text(
        path.read_text(encoding="utf-8").replace("127.0.0.1", "192.168.50.9"),
        encoding="utf-8",
    )

    with pytest.raises(PilotConfigError, match="127.0.0.1 or localhost"):
        load_pilot_config(path)


def test_rejects_unsafe_data_root_targets(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "pilot.csv")
    regular = tmp_path / "not-a-directory"
    regular.write_text("occupied", encoding="utf-8")
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "data_root,state", "data_root,not-a-directory"
        ),
        encoding="utf-8",
    )
    with pytest.raises(PilotConfigError, match="directory or a new path"):
        load_pilot_config(config_path)

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "data_root,not-a-directory", f"data_root,{Path('/')}"
        ),
        encoding="utf-8",
    )
    with pytest.raises(PilotConfigError, match="filesystem root"):
        load_pilot_config(config_path)

    lexical_root = Path(tmp_path.anchor) / "temporary" / ".."
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f"data_root,{Path('/')}", f"data_root,{lexical_root}"
        ),
        encoding="utf-8",
    )
    with pytest.raises(PilotConfigError, match="filesystem root"):
        load_pilot_config(config_path)

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f"data_root,{lexical_root}", "data_root,.."
        ),
        encoding="utf-8",
    )
    with pytest.raises(PilotConfigError, match="dedicated child"):
        load_pilot_config(config_path)


def test_rejects_absolute_or_symlinked_data_root_outside_config_directory(
    tmp_path: Path,
) -> None:
    config_directory = tmp_path / "config"
    external = tmp_path / "external"
    config_directory.mkdir()
    external.mkdir()
    config_path = write_config(config_directory / "pilot.csv")
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "data_root,state", f"data_root,{external}"
        ),
        encoding="utf-8",
    )
    with pytest.raises(PilotConfigError, match="dedicated child"):
        load_pilot_config(config_path)

    link = config_directory / "linked-state"
    link.symlink_to(external, target_is_directory=True)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f"data_root,{external}", "data_root,linked-state"
        ),
        encoding="utf-8",
    )
    with pytest.raises(PilotConfigError, match="must not be a symlink"):
        load_pilot_config(config_path)


def test_merge_precedence_is_cli_then_csv_then_existing_default(tmp_path: Path) -> None:
    config = load_pilot_config(
        write_config(
            tmp_path / "pilot.csv",
            extra="grading_runtime,pilot-local\nbundle_worker_count,8\n",
        )
    )
    namespace = SimpleNamespace(
        course_key="from-cli",
        data_root="from-default",
        public_base_url="http://127.0.0.1:8000",
        listen="localhost",
        port=18080,
        grading_runtime="docker",
        bundle_worker_count=2,
    )

    apply_pilot_config(
        namespace,
        config,
        argv=(
            "--course-key=from-cli",
            "serve",
            "--listen",
            "localhost",
            "--port=18080",
        ),
    )

    assert namespace.course_key == "from-cli"
    assert namespace.data_root == str(tmp_path / "state")
    assert namespace.public_base_url == "http://127.0.0.1:18080"
    assert namespace.listen == "localhost"
    assert namespace.port == 18080
    assert namespace.grading_runtime == "pilot-local"
    assert namespace.bundle_worker_count == 8


def test_explicit_port_override_must_also_keep_origin_coherent(tmp_path: Path) -> None:
    config = load_pilot_config(write_config(tmp_path / "pilot.csv"))
    namespace = SimpleNamespace(
        public_base_url="http://127.0.0.1:8000",
        listen="127.0.0.1",
        port=19090,
    )

    with pytest.raises(PilotConfigError, match="must match"):
        apply_pilot_config(namespace, config, argv=("serve", "--port", "19090"))


@pytest.mark.parametrize(
    "public_url",
    (
        "http://evil.example.test:18080",
        "http://127.0.0.1:18080/path",
        "http://user:pass@127.0.0.1:18080",
        "http://[::1]:18080",
    ),
)
def test_explicit_public_url_cannot_bypass_pilot_validation(
    tmp_path: Path,
    public_url: str,
) -> None:
    config = load_pilot_config(write_config(tmp_path / "pilot.csv"))
    namespace = SimpleNamespace(
        course_key="cse101-2026f",
        data_root=str(tmp_path / "state"),
        public_base_url=public_url,
        listen="127.0.0.1",
        port=18080,
        grading_runtime="pilot-local",
        bundle_worker_count=4,
    )

    with pytest.raises(PilotConfigError):
        apply_pilot_config(
            namespace,
            config,
            argv=("--public-base-url", public_url, "serve"),
        )


def test_explicit_data_root_and_worker_count_remain_inside_pilot_bounds(
    tmp_path: Path,
) -> None:
    config = load_pilot_config(write_config(tmp_path / "pilot.csv"))
    namespace = SimpleNamespace(
        course_key="cse101-2026f",
        data_root=str(tmp_path.parent),
        public_base_url="http://127.0.0.1:18080",
        listen="127.0.0.1",
        port=18080,
        grading_runtime="pilot-local",
        bundle_worker_count=4,
    )
    with pytest.raises(PilotConfigError, match="dedicated child"):
        apply_pilot_config(
            namespace,
            config,
            argv=("--data-root", str(tmp_path.parent), "serve"),
        )

    namespace.data_root = str(tmp_path / "state")
    namespace.bundle_worker_count = 33
    with pytest.raises(PilotConfigError, match="between 1 and 32"):
        apply_pilot_config(
            namespace,
            config,
            argv=("serve", "--bundle-worker-count", "33"),
        )


def test_https_public_url_override_supports_loopback_reverse_proxy(
    tmp_path: Path,
) -> None:
    config = load_pilot_config(write_config(tmp_path / "pilot.csv"))
    namespace = SimpleNamespace(
        course_key="cse101-2026f",
        data_root=str(tmp_path / "state"),
        public_base_url="https://grade.example.test",
    )

    apply_pilot_config(
        namespace,
        config,
        argv=(
            "--public-base-url",
            "https://grade.example.test",
            "auth",
            "issue",
            "s001",
        ),
    )
    assert namespace.public_base_url == "https://grade.example.test"
    assert namespace.external_access_mode == "disabled"


def test_pilot_local_https_proxy_may_use_a_different_public_port(
    tmp_path: Path,
) -> None:
    config = load_pilot_config(write_config(tmp_path / "pilot.csv"))
    namespace = SimpleNamespace(
        course_key="cse101-2026f",
        data_root=str(tmp_path / "state"),
        public_base_url="https://grade.example.test:8443",
        listen="127.0.0.1",
        port=18080,
        grading_runtime="pilot-local",
        bundle_worker_count=4,
    )

    apply_pilot_config(
        namespace,
        config,
        argv=(
            "--public-base-url",
            "https://grade.example.test:8443",
            "serve",
        ),
    )

    assert namespace.public_base_url == "https://grade.example.test:8443"
    assert namespace.listen == "127.0.0.1"
    assert namespace.port == 18080
    assert namespace.grading_runtime == "pilot-local"


def test_non_serve_command_preserves_insecure_http_config_validation(
    tmp_path: Path,
) -> None:
    path = write_config(
        tmp_path / "pilot.csv",
        extra="external_access_mode,insecure-http\n",
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "127.0.0.1", "192.168.50.9"
        ),
        encoding="utf-8",
    )
    config = load_pilot_config(path)
    namespace = SimpleNamespace(
        course_key="cse101-2026f",
        data_root=str(tmp_path / "state"),
        public_base_url="http://127.0.0.1:8000",
    )

    apply_pilot_config(
        namespace,
        config,
        argv=("auth", "issue", "s001"),
    )

    assert namespace.public_base_url == "http://192.168.50.9:18080"
    assert namespace.external_access_mode == "insecure-http"


def test_platform_cli_initializes_from_csv_without_environment_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path / "pilot.csv")
    for key in (
        "AUTOGRADE_COURSE_KEY",
        "AUTOGRADE_DATA_ROOT",
        "AUTOGRADE_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    assert platform_cli.main(["--pilot-config", str(config_path), "init"]) == 0

    result = json.loads(capsys.readouterr().out)["result"]
    assert result["course_key"] == "cse101-2026f"
    assert Path(result["database"]) == tmp_path / "state" / "state.sqlite3"
    assert (tmp_path / "state" / "platform-auth-secret").is_file()


def test_platform_cli_imports_local_roster_with_the_same_pilot_csv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path / "pilot.csv")
    roster_path = tmp_path / "roster.csv"
    roster_path.write_text(
        "student_key,active\n"
        "20260001,true\n"
        "20260002,true\n",
        encoding="utf-8",
    )
    prefix = ["--pilot-config", str(config_path)]

    assert platform_cli.main([*prefix, "init"]) == 0
    capsys.readouterr()
    assert (
        platform_cli.main([*prefix, "student", "import", str(roster_path)])
        == 0
    )

    result = json.loads(capsys.readouterr().out)["result"]
    assert result["course_key"] == "cse101-2026f"
    assert result["count"] == 2
    assert result["local_count"] == 2
    assert result["github_count"] == 0


def test_explicit_global_cli_values_override_pilot_csv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path / "pilot.csv")
    override_root = tmp_path / "override-state"

    assert (
        platform_cli.main(
            [
                "--pilot-config",
                str(config_path),
                "--course-key",
                "explicit-course",
                "--data-root",
                str(override_root),
                "init",
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)["result"]
    assert result["course_key"] == "explicit-course"
    assert Path(result["database"]) == override_root / "state.sqlite3"
    assert not (tmp_path / "state").exists()


def test_platform_cli_reports_invalid_pilot_csv_without_creating_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path / "pilot.csv")
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "password,exposed\n",
        encoding="utf-8",
    )

    assert platform_cli.main(["--pilot-config", str(config_path), "init"]) == 1

    failure = json.loads(capsys.readouterr().err)
    assert failure["error"]["code"] == "pilot_config_error"
    assert "must not contain secrets" in failure["error"]["message"]
    assert not (tmp_path / "state").exists()


def test_platform_serve_namespace_receives_pilot_values_and_explicit_overrides(
    tmp_path: Path,
) -> None:
    config_path = write_config(
        tmp_path / "pilot.csv",
        extra="grading_runtime,pilot-local\nbundle_worker_count,6\n",
    )
    config = load_pilot_config(config_path)
    parser = platform_cli.build_parser()

    raw = ("--pilot-config", str(config_path), "serve")
    args = parser.parse_args(raw)
    apply_pilot_config(args, config, argv=raw)
    assert args.listen == "127.0.0.1"
    assert args.port == 18080
    assert args.grading_runtime == "pilot-local"
    assert args.bundle_worker_count == 6

    explicit = (
        "--pilot-config",
        str(config_path),
        "--public-base-url",
        "http://127.0.0.1:19090",
        "serve",
        "--listen=localhost",
        "--port",
        "19090",
        "--grading-runtime",
        "docker",
        "--bundle-worker-count=2",
    )
    args = parser.parse_args(explicit)
    apply_pilot_config(args, config, argv=explicit)
    assert args.public_base_url == "http://127.0.0.1:19090"
    assert args.listen == "localhost"
    assert args.port == 19090
    assert args.grading_runtime == "docker"
    assert args.bundle_worker_count == 2

    legacy = (
        "--pilot-config",
        str(config_path),
        "serve",
        "--container-runtime",
        "podman",
    )
    args = parser.parse_args(legacy)
    apply_pilot_config(args, config, argv=legacy)
    assert args.grading_runtime == "podman"


def test_pilot_csv_overrides_environment_runtime_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_config(tmp_path / "pilot.csv")
    config = load_pilot_config(config_path)
    monkeypatch.setenv("AUTOGRADE_GRADING_RUNTIME", "podman")
    parser = platform_cli.build_parser()
    raw = ("--pilot-config", str(config_path), "serve")

    args = parser.parse_args(raw)
    assert args.grading_runtime == "podman"
    apply_pilot_config(args, config, argv=raw)

    assert args.grading_runtime == "pilot-local"
