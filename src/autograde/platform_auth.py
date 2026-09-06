"""Security primitives for the student-platform device pairing flow.

This module deliberately contains no HTTP framework dependencies. Raw activation,
device, access, and refresh secrets are returned to the caller exactly once;
persistence adapters receive only domain-separated HMAC digests.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import string
import time
from typing import Any, Mapping


class PlatformAuthError(RuntimeError):
    """Base class for platform authentication failures."""


class InvalidSignedValue(PlatformAuthError):
    """A signed browser value is malformed, expired, or has a bad signature."""


class AuthSecretError(PlatformAuthError):
    """The server-side authentication secret cannot be used safely."""


_USER_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_ACTIVATION_CODE_CHARACTERS = 26
_TOKEN_BYTES = 32


def create_or_load_auth_secret(path: str | Path) -> bytes:
    """Create or load one 256-bit server secret from a mode-0600 regular file."""

    secret_path = Path(path).expanduser().absolute()
    parent = secret_path.parent
    if os.path.lexists(parent) and parent.is_symlink():
        raise AuthSecretError(f"auth secret parent must not be a symlink: {parent}")
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not parent.is_dir():
        raise AuthSecretError(f"auth secret parent is not a directory: {parent}")
    parent.chmod(0o700)

    if os.path.lexists(secret_path):
        info = secret_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AuthSecretError(f"auth secret must be a regular file: {secret_path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise AuthSecretError(f"auth secret permissions must be 0600: {secret_path}")
        value = secret_path.read_bytes()
        if len(value) != _TOKEN_BYTES:
            raise AuthSecretError("auth secret must contain exactly 32 bytes")
        return value

    value = secrets.token_bytes(_TOKEN_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(secret_path, flags, 0o600)
    except FileExistsError:
        return create_or_load_auth_secret(secret_path)
    try:
        written = os.write(descriptor, value)
        if written != len(value):
            raise AuthSecretError("auth secret file write was incomplete")
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return value


def create_or_load_instructor_token(path: str | Path) -> str:
    """Create or load a browser Basic-auth password from a private file.

    The raw value is intentionally never written to SQLite or operator JSON.
    An instructor who can read the server-local mode-0600 file can enter it as
    the dashboard password; remote use still requires HTTPS.
    """

    token_path = Path(path).expanduser().absolute()
    parent = token_path.parent
    if os.path.lexists(parent) and parent.is_symlink():
        raise AuthSecretError(f"instructor token parent must not be a symlink: {parent}")
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not parent.is_dir():
        raise AuthSecretError(f"instructor token parent is not a directory: {parent}")
    parent.chmod(0o700)

    if os.path.lexists(token_path):
        info = token_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AuthSecretError(
                f"instructor token must be a regular file: {token_path}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise AuthSecretError(
                f"instructor token permissions must be 0600: {token_path}"
            )
        try:
            value = token_path.read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            raise AuthSecretError("instructor token is unreadable") from exc
        if (
            len(value) < 32
            or len(value) > 256
            or not value.isascii()
            or any(character.isspace() or ord(character) < 33 for character in value)
        ):
            raise AuthSecretError("instructor token has an invalid format")
        return value

    value = new_api_token()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(token_path, flags, 0o600)
    except FileExistsError:
        return create_or_load_instructor_token(token_path)
    try:
        payload = value.encode("ascii")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise AuthSecretError("instructor token file write was incomplete")
            offset += written
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return value


def secret_digest(server_secret: bytes, purpose: str, value: str) -> str:
    """Return a domain-separated HMAC digest suitable for persistence."""

    if len(server_secret) < _TOKEN_BYTES:
        raise ValueError("server_secret must contain at least 32 bytes")
    if not purpose or any(character not in string.ascii_lowercase + "-_" for character in purpose):
        raise ValueError("purpose must contain lowercase ASCII letters, '-' or '_'")
    if not value or "\x00" in value:
        raise ValueError("secret value must be non-empty")
    payload = purpose.encode("ascii") + b"\x00" + value.encode("utf-8")
    return hmac.new(server_secret, payload, hashlib.sha256).hexdigest()


def new_device_code() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def new_api_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def new_user_code() -> str:
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def new_activation_code() -> str:
    """Return a human-transcribable one-time credential with 130 bits of entropy."""

    raw = "".join(
        secrets.choice(_USER_CODE_ALPHABET)
        for _ in range(_ACTIVATION_CODE_CHARACTERS)
    )
    groups = [raw[index : index + 4] for index in range(0, len(raw), 4)]
    return "AG1-" + "-".join(groups)


def new_public_id(prefix: str) -> str:
    if not prefix or any(character not in string.ascii_lowercase for character in prefix):
        raise ValueError("public id prefix must contain lowercase ASCII letters")
    return f"{prefix}_{secrets.token_urlsafe(18).rstrip('=')}"


def sign_browser_value(
    server_secret: bytes,
    purpose: str,
    payload: Mapping[str, Any],
    *,
    lifetime_seconds: int,
    now: int | None = None,
) -> str:
    """Sign a small JSON value for OAuth state or an HttpOnly web session."""

    if lifetime_seconds <= 0:
        raise ValueError("lifetime_seconds must be positive")
    issued_at = int(time.time() if now is None else now)
    body = dict(payload)
    body["iat"] = issued_at
    body["exp"] = issued_at + lifetime_seconds
    encoded = _b64url(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    )
    signature = secret_digest(server_secret, f"signed-{purpose}", encoded)
    return f"{encoded}.{signature}"


def verify_browser_value(
    server_secret: bytes,
    purpose: str,
    value: str,
    *,
    now: int | None = None,
) -> Mapping[str, Any]:
    """Verify and decode a value created by :func:`sign_browser_value`."""

    try:
        encoded, supplied_signature = value.split(".", 1)
    except ValueError as exc:
        raise InvalidSignedValue("signed value is malformed") from exc
    expected_signature = secret_digest(server_secret, f"signed-{purpose}", encoded)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise InvalidSignedValue("signed value has an invalid signature")
    try:
        decoded = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise InvalidSignedValue("signed value payload is malformed") from exc
    if not isinstance(decoded, dict):
        raise InvalidSignedValue("signed value payload must be an object")
    expires_at = decoded.get("exp")
    issued_at = decoded.get("iat")
    current = int(time.time() if now is None else now)
    if not isinstance(expires_at, int) or not isinstance(issued_at, int):
        raise InvalidSignedValue("signed value timestamps are missing")
    if issued_at > current + 60:
        raise InvalidSignedValue("signed value was issued in the future")
    if expires_at <= current:
        raise InvalidSignedValue("signed value has expired")
    return decoded


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
