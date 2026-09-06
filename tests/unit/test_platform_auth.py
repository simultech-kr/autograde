from __future__ import annotations

import os
import re

import pytest

from autograde.platform_auth import (
    AuthSecretError,
    InvalidSignedValue,
    create_or_load_auth_secret,
    create_or_load_instructor_token,
    new_activation_code,
    new_api_token,
    new_device_code,
    new_public_id,
    new_user_code,
    secret_digest,
    sign_browser_value,
    verify_browser_value,
)


def test_auth_secret_is_created_once_with_private_permissions(tmp_path) -> None:
    path = tmp_path / "private" / "auth-secret"
    first = create_or_load_auth_secret(path)
    second = create_or_load_auth_secret(path)

    assert first == second
    assert len(first) == 32
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_auth_secret_rejects_overly_broad_permissions(tmp_path) -> None:
    path = tmp_path / "auth-secret"
    path.write_bytes(b"x" * 32)
    path.chmod(0o644)

    with pytest.raises(AuthSecretError, match="0600"):
        create_or_load_auth_secret(path)


def test_instructor_token_is_stable_private_and_rejects_unsafe_files(tmp_path) -> None:
    path = tmp_path / "private" / "instructor-token"
    first = create_or_load_instructor_token(path)
    second = create_or_load_instructor_token(path)

    assert first == second
    assert len(first) >= 32
    assert os.stat(path).st_mode & 0o777 == 0o600

    path.chmod(0o644)
    with pytest.raises(AuthSecretError, match="0600"):
        create_or_load_instructor_token(path)


def test_instructor_token_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("x" * 40, encoding="ascii")
    link = tmp_path / "token"
    link.symlink_to(target)

    with pytest.raises(AuthSecretError, match="regular file"):
        create_or_load_instructor_token(link)


def test_secret_digests_are_purpose_separated() -> None:
    key = b"k" * 32
    assert secret_digest(key, "access", "same") != secret_digest(
        key, "refresh", "same"
    )


def test_random_values_have_expected_public_shape() -> None:
    assert len(new_device_code()) >= 40
    assert len(new_api_token()) >= 40
    code = new_user_code()
    assert len(code) == 9 and code[4] == "-"
    assert new_public_id("sub").startswith("sub_")


def test_student_activation_codes_are_high_entropy_and_human_transcribable() -> None:
    codes = {new_activation_code() for _ in range(100)}

    assert len(codes) == 100
    assert all(
        re.fullmatch(
            r"AG1-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}"
            r"(?:-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}){5}"
            r"-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{2}",
            code,
        )
        for code in codes
    )
    assert secret_digest(b"k" * 32, "activation-code", next(iter(codes))) != (
        secret_digest(b"k" * 32, "user-code", next(iter(codes)))
    )


def test_signed_browser_value_round_trip_and_expiry() -> None:
    key = b"s" * 32
    signed = sign_browser_value(
        key,
        "oauth-state",
        {"device": "dev_1", "nonce": "n"},
        lifetime_seconds=300,
        now=1_000,
    )

    assert verify_browser_value(key, "oauth-state", signed, now=1_100)["device"] == "dev_1"
    with pytest.raises(InvalidSignedValue, match="expired"):
        verify_browser_value(key, "oauth-state", signed, now=1_300)


def test_signed_browser_value_rejects_tampering_and_wrong_purpose() -> None:
    key = b"s" * 32
    signed = sign_browser_value(
        key,
        "oauth-state",
        {"device": "dev_1"},
        lifetime_seconds=300,
        now=1_000,
    )

    with pytest.raises(InvalidSignedValue, match="signature"):
        verify_browser_value(key, "web-session", signed, now=1_001)
    with pytest.raises(InvalidSignedValue, match="signature"):
        verify_browser_value(key, "oauth-state", signed[:-1] + "0", now=1_001)
