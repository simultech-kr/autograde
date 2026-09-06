from __future__ import annotations

import os
import re

import pytest

from autograde.platform_auth import (
    AuthSecretError,
    InvalidSignedValue,
    create_or_load_auth_secret,
    create_or_load_instructor_token,
    hash_student_password,
    new_activation_code,
    new_api_token,
    new_assignment_claim_code,
    new_device_code,
    new_public_id,
    new_user_code,
    secret_digest,
    sign_browser_value,
    verify_student_password,
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


def test_assignment_claim_codes_have_the_versioned_human_transcribable_shape() -> None:
    codes = {new_assignment_claim_code() for _ in range(100)}

    assert len(codes) == 100
    assert all(
        re.fullmatch(
            r"AK1-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}"
            r"(?:-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}){2}",
            code,
        )
        for code in codes
    )


def test_student_password_hash_round_trip_uses_nfc_and_a_versioned_scrypt_format() -> None:
    decomposed = "e\u0301" + ("a" * 14)
    composed = "\u00e9" + ("a" * 14)

    encoded = hash_student_password(decomposed, salt=b"s" * 16)

    assert encoded.startswith("scrypt$v1$16384$8$1$")
    assert decomposed not in encoded
    assert composed not in encoded
    assert verify_student_password(composed, encoded)
    assert not verify_student_password(composed + "wrong", encoded)
    assert not verify_student_password(composed, "not-a-supported-hash")


def test_student_password_policy_counts_nfc_characters_and_caps_utf8_bytes() -> None:
    with pytest.raises(ValueError, match="15"):
        hash_student_password("a" * 14)

    assert verify_student_password(
        "a" * 15,
        hash_student_password("a" * 15, salt=b"m" * 16),
    )
    assert verify_student_password(
        "a" * 256,
        hash_student_password("a" * 256, salt=b"x" * 16),
    )
    with pytest.raises(ValueError, match="256 UTF-8 bytes"):
        hash_student_password("a" * 257)
    with pytest.raises(ValueError, match="256 UTF-8 bytes"):
        hash_student_password(("\uac00" * 85) + "ab")


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
