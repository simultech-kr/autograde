"""Strict, non-secret CSV configuration for a local platform pilot.

The file is intentionally small and boring: it has exactly two columns named
``key`` and ``value``.  It is configuration, not a credential store.  Student
activation codes and server secrets therefore remain in the private files
managed below ``data_root``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import ipaddress
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


MAX_PILOT_CONFIG_BYTES = 64 * 1024
DEFAULT_GRADING_RUNTIME = "pilot-local"
DEFAULT_BUNDLE_WORKER_COUNT = 4
DEFAULT_EXTERNAL_ACCESS_MODE = "disabled"

_REQUIRED_KEYS = frozenset(
    {"course_key", "data_root", "public_base_url", "listen", "port"}
)
_OPTIONAL_KEYS = frozenset(
    {"grading_runtime", "bundle_worker_count", "external_access_mode", "web_public_base_url", "web_port",
     "instructor_assignment_web_enabled", "instructor_rubric_web_enabled", "instructor_auth_mode", "roster_bootstrap_mode"}
)
_ALLOWED_KEYS = _REQUIRED_KEYS | _OPTIONAL_KEYS
_SECRET_KEY_PARTS = frozenset(
    {
        "activation_code",
        "api_key",
        "credential",
        "password",
        "private_key",
        "secret",
        "token",
    }
)
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_COURSE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_INTEGER = re.compile(r"^[0-9]+$")
_CLI_OPTION_ALIASES = {
    "grading_runtime": ("--grading-runtime", "--container-runtime"),
}


class PilotConfigError(ValueError):
    """The local pilot configuration is unavailable or invalid."""


@dataclass(frozen=True)
class PilotConfig:
    """A validated pilot file and the typed values it contributes to the CLI."""

    source: Path
    values: Mapping[str, Any]


def load_pilot_config(path: str | Path) -> PilotConfig:
    """Read and fully validate one UTF-8 ``key,value`` pilot CSV.

    Relative ``data_root`` values are anchored at the configuration file's
    parent, not at the process working directory.  This makes a pilot folder
    movable and prevents two invocations from silently choosing different
    databases merely because they were launched from different directories.
    """

    source = _config_path(path)
    text = _read_regular_utf8_file(source)
    raw_values = _read_rows(text)

    missing = sorted(_REQUIRED_KEYS - raw_values.keys())
    if missing:
        raise PilotConfigError(
            "pilot config is missing required keys: " + ", ".join(missing)
        )

    values: dict[str, Any] = {
        "course_key": _course_key(raw_values["course_key"]),
        "data_root": str(_data_root(raw_values["data_root"], source=source)),
        "public_base_url": _public_base_url(raw_values["public_base_url"]),
        "listen": _listen(raw_values["listen"]),
        "port": _bounded_integer(
            raw_values["port"], key="port", minimum=1, maximum=65535
        ),
        "grading_runtime": _grading_runtime(
            raw_values.get("grading_runtime", DEFAULT_GRADING_RUNTIME)
        ),
        "bundle_worker_count": _bounded_integer(
            raw_values.get(
                "bundle_worker_count", str(DEFAULT_BUNDLE_WORKER_COUNT)
            ),
            key="bundle_worker_count",
            minimum=1,
            maximum=32,
        ),
        "external_access_mode": _external_access_mode(
            raw_values.get(
                "external_access_mode", DEFAULT_EXTERNAL_ACCESS_MODE
            )
        ),
    }
    enabled = raw_values.get("instructor_assignment_web_enabled", "false")
    if enabled not in {"true", "false"}:
        raise PilotConfigError("instructor_assignment_web_enabled must be true or false")
    values["instructor_assignment_web_enabled"] = enabled == "true"
    instructor_mode = raw_values.get('instructor_auth_mode', 'shared')
    if instructor_mode not in {'shared', 'personal'}:
        raise PilotConfigError('instructor_auth_mode must be shared or personal')
    if instructor_mode == 'personal' and (not values['instructor_assignment_web_enabled'] or not {'web_port', 'web_public_base_url'} <= raw_values.keys()):
        raise PilotConfigError('personal instructor auth requires instructor web and web origin/port')
    values['instructor_auth_mode'] = instructor_mode
    rubric_enabled = raw_values.get("instructor_rubric_web_enabled", "false")
    if rubric_enabled not in {"true", "false"}:
        raise PilotConfigError("instructor_rubric_web_enabled must be true or false")
    values["instructor_rubric_web_enabled"] = rubric_enabled == "true"
    if values["instructor_rubric_web_enabled"] and not values["instructor_assignment_web_enabled"]:
        raise PilotConfigError("rubric web requires instructor_assignment_web_enabled=true")
    if values["instructor_rubric_web_enabled"] and not {"web_port", "web_public_base_url"} <= raw_values.keys():
        raise PilotConfigError("rubric web requires web_port and web_public_base_url")
    mode = raw_values.get("roster_bootstrap_mode", "csv")
    if mode not in {"csv", "web"}:
        raise PilotConfigError("roster_bootstrap_mode must be csv or web")
    if mode == "web" and not values["instructor_assignment_web_enabled"]:
        raise PilotConfigError("web roster bootstrap requires instructor_assignment_web_enabled=true")
    values["roster_bootstrap_mode"] = mode
    if "web_public_base_url" in raw_values or "web_port" in raw_values:
        if not {"web_public_base_url", "web_port"} <= raw_values.keys():
            raise PilotConfigError("web_public_base_url and web_port must be provided together")
        values["web_public_base_url"] = _public_base_url(raw_values["web_public_base_url"])
        values["web_port"] = _bounded_integer(raw_values["web_port"], key="web_port", minimum=1, maximum=65535)
        _validate_local_http_binding({**values, "public_base_url": values["web_public_base_url"], "port": values["web_port"]})
        if values["web_port"] == values["port"] or values["web_public_base_url"] == values["public_base_url"]:
            raise PilotConfigError("web and API must use separate ports and origins")
        if urlsplit(values["web_public_base_url"]).scheme != urlsplit(values["public_base_url"]).scheme:
            raise PilotConfigError("web and API must use the same transport scheme")
    _validate_local_http_binding(values)
    return PilotConfig(source=source, values=MappingProxyType(values))


def apply_pilot_config(
    namespace: Any,
    config: PilotConfig,
    *,
    argv: Sequence[str],
) -> None:
    """Merge a pilot file into a parsed CLI namespace.

    Argparse has already resolved environment and built-in defaults at this
    point.  A value explicitly present on the command line is left untouched;
    all other applicable attributes receive the CSV value.  The resulting
    precedence is therefore CLI > pilot CSV > environment > built-in default.
    """

    if config.values.get('instructor_auth_mode') == 'personal' and getattr(namespace, 'command', None) == 'serve':
        raise PilotConfigError('personal instructor auth requires autograde.pilot_portal_cli, not legacy platform serve')
    for key, value in config.values.items():
        if not hasattr(namespace, key):
            # ``listen`` and other serve-only options are intentionally ignored
            # by commands which do not expose them.
            continue
        options = _CLI_OPTION_ALIASES.get(
            key, ("--" + key.replace("_", "-"),)
        )
        if not any(_option_is_explicit(argv, option) for option in options):
            setattr(namespace, key, value)

    # There is deliberately no environment or CLI shortcut for weakening the
    # network boundary. The reviewed CSV is the only source of this opt-in.
    namespace.external_access_mode = config.values["external_access_mode"]

    # Argparse validates only options which declare an explicit ``type`` and
    # cannot apply the config-relative data-root rule. Re-validate the complete
    # effective profile so an explicit override cannot bypass CSV safeguards.
    if hasattr(namespace, "course_key"):
        namespace.course_key = _course_key(str(namespace.course_key))
    if hasattr(namespace, "data_root"):
        namespace.data_root = str(
            _data_root(str(namespace.data_root), source=config.source)
        )
    if hasattr(namespace, "public_base_url"):
        namespace.public_base_url = _public_base_url(
            str(namespace.public_base_url)
        )
    if hasattr(namespace, "listen"):
        namespace.listen = _listen(str(namespace.listen))
    if hasattr(namespace, "port"):
        namespace.port = _bounded_integer(
            str(namespace.port), key="port", minimum=1, maximum=65535
        )
    if hasattr(namespace, "grading_runtime"):
        namespace.grading_runtime = _grading_runtime(
            str(namespace.grading_runtime)
        )
    if hasattr(namespace, "bundle_worker_count"):
        namespace.bundle_worker_count = _bounded_integer(
            str(namespace.bundle_worker_count),
            key="bundle_worker_count",
            minimum=1,
            maximum=32,
        )

    # Commands other than ``serve`` do not carry listener fields in their
    # argparse namespace, but they still use ``public_base_url`` for device
    # activation responses. Validate them against the selected CSV endpoint so
    # a command-specific override cannot silently drift from the server.
    effective = {
        "public_base_url": namespace.public_base_url,
        "listen": getattr(namespace, "listen", config.values["listen"]),
        "port": getattr(namespace, "port", config.values["port"]),
        "grading_runtime": getattr(
            namespace,
            "grading_runtime",
            config.values["grading_runtime"],
        ),
        # This safety opt-in intentionally comes only from the pilot file. It
        # therefore remains effective for commands such as ``auth issue``
        # whose argparse namespace has no listener or external-access field.
        "external_access_mode": namespace.external_access_mode,
    }
    _validate_local_http_binding(effective)


def _option_is_explicit(argv: Sequence[str], option: str) -> bool:
    return any(
        argument == option or argument.startswith(option + "=")
        for argument in argv
    )


def _config_path(path: str | Path) -> Path:
    try:
        source = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise PilotConfigError("pilot config path is invalid") from exc
    return source


def _read_regular_utf8_file(source: Path) -> str:
    try:
        before = source.lstat()
    except OSError as exc:
        raise PilotConfigError("pilot config is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise PilotConfigError("pilot config must be one regular file")
    if before.st_size > MAX_PILOT_CONFIG_BYTES:
        raise PilotConfigError("pilot config exceeds the 64 KiB limit")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise PilotConfigError("pilot config is unavailable") from exc

    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PilotConfigError("pilot config must be one regular file")
        if info.st_size > MAX_PILOT_CONFIG_BYTES:
            raise PilotConfigError("pilot config exceeds the 64 KiB limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(MAX_PILOT_CONFIG_BYTES + 1)
        if len(payload) > MAX_PILOT_CONFIG_BYTES:
            raise PilotConfigError("pilot config exceeds the 64 KiB limit")
    finally:
        os.close(descriptor)

    try:
        # A UTF-8 BOM is accepted because Windows spreadsheet applications
        # commonly emit it; every other decoding error remains fatal.
        return payload.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise PilotConfigError("pilot config must be UTF-8") from exc


def _read_rows(text: str) -> dict[str, str]:
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise PilotConfigError("pilot config CSV is malformed") from exc
    if not rows:
        raise PilotConfigError("pilot config CSV must include a key,value header")
    if rows[0] != ["key", "value"]:
        raise PilotConfigError("pilot config CSV header must be exactly key,value")

    values: dict[str, str] = {}
    for line_number, row in enumerate(rows[1:], start=2):
        if len(row) != 2:
            raise PilotConfigError(
                f"pilot config row {line_number} must contain exactly two fields"
            )
        key, value = row
        if not key or key != key.strip() or key.casefold() != key:
            raise PilotConfigError(
                f"pilot config row {line_number} has an invalid key"
            )
        if _looks_like_secret_key(key):
            raise PilotConfigError(
                f"pilot config row {line_number} must not contain secrets or credentials"
            )
        if key not in _ALLOWED_KEYS:
            raise PilotConfigError(
                f"pilot config row {line_number} has unknown key {key!r}"
            )
        if key in values:
            raise PilotConfigError(
                f"pilot config row {line_number} duplicates key {key!r}"
            )
        normalized_value = value.strip()
        if not normalized_value:
            raise PilotConfigError(
                f"pilot config row {line_number} has an empty value"
            )
        if _has_disallowed_control(normalized_value):
            raise PilotConfigError(
                f"pilot config row {line_number} contains control characters"
            )
        values[key] = normalized_value
    return values


def _looks_like_secret_key(key: str) -> bool:
    normalized = key.casefold()
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _has_disallowed_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _course_key(value: str) -> str:
    if not _COURSE_KEY.fullmatch(value):
        raise PilotConfigError(
            "pilot config course_key must be a portable 1-128 character identifier"
        )
    return value


def _data_root(value: str, *, source: Path) -> Path:
    if "\\" in value:
        raise PilotConfigError(
            "pilot config data_root must use this host's native path syntax"
        )
    try:
        configured = Path(value).expanduser()
        config_directory = source.parent.resolve(strict=True)
        lexical_candidate = Path(
            os.path.abspath(
                os.fspath(
                    configured
                    if configured.is_absolute()
                    else config_directory / configured
                )
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise PilotConfigError("pilot config data_root is invalid") from exc
    if os.path.lexists(lexical_candidate):
        try:
            lexical_info = lexical_candidate.lstat()
        except OSError as exc:
            raise PilotConfigError("pilot config data_root is unavailable") from exc
        if stat.S_ISLNK(lexical_info.st_mode):
            raise PilotConfigError("pilot config data_root must not be a symlink")
    # Resolve existing parent symlinks after rejecting a final symlink.  A
    # pilot state root must be one dedicated child of the config directory;
    # otherwise ``AppPaths.ensure`` could chmod or populate a broad directory
    # such as the repository, home directory, or ``/tmp``.
    candidate = Path(os.path.realpath(os.fspath(lexical_candidate)))
    if candidate == Path(candidate.anchor):
        raise PilotConfigError("pilot config data_root must not be a filesystem root")
    if candidate == config_directory or not candidate.is_relative_to(
        config_directory
    ):
        raise PilotConfigError(
            "pilot config data_root must be a dedicated child of the config directory"
        )
    if candidate == source.resolve(strict=True):
        raise PilotConfigError("pilot config data_root must not be the config file")
    if os.path.lexists(candidate):
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise PilotConfigError("pilot config data_root is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise PilotConfigError(
                "pilot config data_root must be a directory or a new path"
            )
    return candidate


def _public_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError as exc:
        raise PilotConfigError("pilot config public_base_url is invalid") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        raise PilotConfigError(
            "pilot config public_base_url must be an HTTP(S) origin without credentials, path, query, or fragment"
        )
    _listen(hostname)
    # Accessing ``parsed.port`` above validates its syntax and range.
    del parsed_port
    return value.rstrip("/")


def _listen(value: str) -> str:
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if len(value) > 253 or value.endswith("."):
        raise PilotConfigError("pilot config listen host is invalid")
    labels = value.split(".")
    if not labels or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise PilotConfigError("pilot config listen host is invalid")
    return value.casefold()


def _grading_runtime(value: str) -> str:
    if value not in {"pilot-local", "docker", "podman"}:
        raise PilotConfigError(
            "pilot config grading_runtime must be pilot-local, docker, or podman"
        )
    return value


def _external_access_mode(value: str) -> str:
    if value not in {"disabled", "insecure-http"}:
        raise PilotConfigError(
            "pilot config external_access_mode must be disabled or insecure-http"
        )
    return value


def _bounded_integer(value: str, *, key: str, minimum: int, maximum: int) -> int:
    if not _INTEGER.fullmatch(value):
        raise PilotConfigError(f"pilot config {key} must be an integer")
    parsed = int(value, 10)
    if parsed < minimum or parsed > maximum:
        raise PilotConfigError(
            f"pilot config {key} must be between {minimum} and {maximum}"
        )
    return parsed


def _validate_local_http_binding(values: Mapping[str, Any]) -> None:
    try:
        parsed = urlsplit(str(values["public_base_url"]))
        url_port = parsed.port
    except (KeyError, ValueError) as exc:
        raise PilotConfigError("pilot config public_base_url is invalid") from exc
    listen = str(values["listen"])
    external_access_mode = _external_access_mode(
        str(
            values.get(
                "external_access_mode", DEFAULT_EXTERNAL_ACCESS_MODE
            )
        )
    )
    if external_access_mode == "insecure-http":
        if parsed.scheme != "http":
            raise PilotConfigError(
                "insecure-http external access requires an HTTP public_base_url"
            )
        public_address = _rfc1918_ipv4_address(parsed.hostname)
        listen_address = _rfc1918_ipv4_address(listen)
        if public_address is None:
            raise PilotConfigError(
                "insecure-http public_base_url must use an RFC1918 IPv4 literal"
            )
        if listen_address is None:
            raise PilotConfigError(
                "insecure-http listen must use an RFC1918 IPv4 interface"
            )
        if listen_address != public_address:
            raise PilotConfigError(
                "insecure-http listen must match the public_base_url IPv4 address"
            )
    else:
        # The default stays loopback-only. The explicit insecure mode above is
        # the sole path that can expose the built-in plain-HTTP server to a
        # trusted RFC1918 network.
        if listen.casefold() not in {"127.0.0.1", "localhost"}:
            raise PilotConfigError(
                "pilot config listen must be 127.0.0.1 or localhost"
            )
        if parsed.scheme == "http" and (
            parsed.hostname is None
            or parsed.hostname.casefold() not in {"127.0.0.1", "localhost"}
        ):
            raise PilotConfigError(
                "pilot config public_base_url must use HTTPS or loopback HTTP"
            )
    if parsed.scheme != "http":
        # HTTPS is terminated by a reverse proxy while the built-in server and
        # even a pilot-local grader remain loopback-only.  The public port does
        # not need to match the private listener in this topology.
        return
    expected_port = url_port if url_port is not None else 80
    try:
        serve_port = int(values["port"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PilotConfigError("pilot config port must be an integer") from exc
    if serve_port != expected_port:
        raise PilotConfigError(
            "pilot config public_base_url port must match the local serve port"
        )


def _rfc1918_ipv4_address(value: str | None) -> ipaddress.IPv4Address | None:
    if value is None:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv4Address):
        return None
    private_networks = (
        ipaddress.IPv4Network("10.0.0.0/8"),
        ipaddress.IPv4Network("172.16.0.0/12"),
        ipaddress.IPv4Network("192.168.0.0/16"),
    )
    return address if any(address in network for network in private_networks) else None


__all__ = [
    "DEFAULT_BUNDLE_WORKER_COUNT",
    "DEFAULT_EXTERNAL_ACCESS_MODE",
    "DEFAULT_GRADING_RUNTIME",
    "MAX_PILOT_CONFIG_BYTES",
    "PilotConfig",
    "PilotConfigError",
    "apply_pilot_config",
    "load_pilot_config",
]
