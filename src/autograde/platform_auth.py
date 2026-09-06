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
import unicodedata
from typing import Any, Mapping


class PlatformAuthError(RuntimeError):
    """Base class for platform authentication failures."""


class InvalidSignedValue(PlatformAuthError):
    """A signed browser value is malformed, expired, or has a bad signature."""


class AuthSecretError(PlatformAuthError):
    """The server-side authentication secret cannot be used safely."""


_USER_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_ACTIVATION_CODE_CHARACTERS = 26
_ASSIGNMENT_CLAIM_CHARACTERS = 12
_TOKEN_BYTES = 32
_PASSWORD_SALT_BYTES = 16
_PASSWORD_HASH_BYTES = 32
_PASSWORD_SCRYPT_N = 1 << 14
_PASSWORD_SCRYPT_R = 8
_PASSWORD_SCRYPT_P = 1
_PASSWORD_SCRYPT_MAXMEM = 64 * 1024 * 1024
_STUDENT_PASSWORD_DIGITS = 6


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


def new_assignment_claim_code() -> str:
    """Return a short-lived, human-transcribable assignment claim secret."""

    raw = "".join(
        secrets.choice(_USER_CODE_ALPHABET)
        for _ in range(_ASSIGNMENT_CLAIM_CHARACTERS)
    )
    return "AK1-" + "-".join(
        raw[index : index + 4] for index in range(0, len(raw), 4)
    )


def hash_student_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash one dedicated Autograde password using a versioned scrypt format."""

    password_bytes = _student_password_bytes(password, enforce_policy=True)
    actual_salt = secrets.token_bytes(_PASSWORD_SALT_BYTES) if salt is None else salt
    if not isinstance(actual_salt, bytes) or len(actual_salt) != _PASSWORD_SALT_BYTES:
        raise ValueError("password salt must contain exactly 16 bytes")
    digest = hashlib.scrypt(
        password_bytes,
        salt=actual_salt,
        n=_PASSWORD_SCRYPT_N,
        r=_PASSWORD_SCRYPT_R,
        p=_PASSWORD_SCRYPT_P,
        maxmem=_PASSWORD_SCRYPT_MAXMEM,
        dklen=_PASSWORD_HASH_BYTES,
    )
    return "$".join(
        (
            "scrypt",
            "v2",
            str(_PASSWORD_SCRYPT_N),
            str(_PASSWORD_SCRYPT_R),
            str(_PASSWORD_SCRYPT_P),
            _b64url(actual_salt),
            _b64url(digest),
        )
    )


def validate_student_password(password: str) -> str:
    """Validate and return one course-scoped six-digit student password."""

    password_bytes = _student_password_bytes(password, enforce_policy=True)
    return password_bytes.decode("ascii")


def verify_student_password(password: str, encoded_hash: str) -> bool:
    """Verify a password without returning early on malformed stored values."""

    valid_format = True
    try:
        parts = encoded_hash.split("$")
        if (
            len(parts) != 7
            or parts[0] != "scrypt"
            or parts[1] not in {"v1", "v2"}
            or parts[2:5]
            != [
                str(_PASSWORD_SCRYPT_N),
                str(_PASSWORD_SCRYPT_R),
                str(_PASSWORD_SCRYPT_P),
            ]
        ):
            raise ValueError("unsupported password hash")
        current_policy_hash = parts[1] == "v2"
        salt = _b64url_decode(parts[5])
        expected = _b64url_decode(parts[6])
        if len(salt) != _PASSWORD_SALT_BYTES or len(expected) != _PASSWORD_HASH_BYTES:
            raise ValueError("invalid password hash size")
    except (AttributeError, ValueError, binascii.Error):
        valid_format = False
        current_policy_hash = False
        salt = b"\0" * _PASSWORD_SALT_BYTES
        expected = b"\0" * _PASSWORD_HASH_BYTES

    policy_matches = False
    try:
        password_bytes = _student_password_bytes(password, enforce_policy=False)
        normalized = password_bytes.decode("utf-8")
        policy_matches = _is_six_digit_student_password(normalized)
    except (TypeError, ValueError, UnicodeError):
        valid_format = False
        password_bytes = b"invalid-password"
    actual = hashlib.scrypt(
        password_bytes,
        salt=salt,
        n=_PASSWORD_SCRYPT_N,
        r=_PASSWORD_SCRYPT_R,
        p=_PASSWORD_SCRYPT_P,
        maxmem=_PASSWORD_SCRYPT_MAXMEM,
        dklen=_PASSWORD_HASH_BYTES,
    )
    matched = hmac.compare_digest(actual, expected)
    return bool(
        valid_format and current_policy_hash and policy_matches and matched
    )


def _is_six_digit_student_password(value: str) -> bool:
    return len(value) == _STUDENT_PASSWORD_DIGITS and all(
        character in string.digits for character in value
    )


def _student_password_bytes(password: str, *, enforce_policy: bool) -> bytes:
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    normalized = unicodedata.normalize("NFC", password)
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("password must not contain control characters")
    encoded = normalized.encode("utf-8", "strict")
    if not encoded or len(encoded) > 256:
        raise ValueError("password must contain between 1 and 256 UTF-8 bytes")
    if enforce_policy and not _is_six_digit_student_password(normalized):
        raise ValueError("Autograde 전용 비밀번호는 숫자 6자리여야 합니다")
    return encoded


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
