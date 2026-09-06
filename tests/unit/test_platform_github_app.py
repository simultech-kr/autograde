from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from autograde.gitops import GitCollector, GitCollectorError
from autograde.platform_github import HTTPResponse
from autograde.platform_github_app import (
    GitHubAppError,
    GitHubAppInstallationTokenProvider,
    OpenSSLRS256Signer,
    askpass_main,
)


NOW = datetime(2026, 8, 30, 3, tzinfo=timezone.utc)


class FakeSigner:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def sign(self, payload: bytes) -> bytes:
        self.payloads.append(payload)
        return b"test-signature"


class FakeTransport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        return self.responses.pop(0)


def helper(tmp_path: Path) -> Path:
    path = tmp_path / "askpass"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def response(token: str, expires_at: datetime, *, contents: str = "read") -> HTTPResponse:
    return HTTPResponse(
        status=201,
        headers={},
        body=json.dumps(
            {
                "token": token,
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                "permissions": {"contents": contents},
            }
        ).encode("utf-8"),
    )


def repository_response(
    *,
    repository_id: int = 987654321,
    full_name: str = "school/lab01-student",
    private: bool = True,
    archived: bool = False,
    disabled: bool = False,
) -> HTTPResponse:
    return HTTPResponse(
        status=200,
        headers={},
        body=json.dumps(
            {
                "id": repository_id,
                "full_name": full_name,
                "private": private,
                "archived": archived,
                "disabled": disabled,
                "default_branch": "main",
            }
        ).encode("utf-8"),
    )


def decode_segment(value: str) -> dict:
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + padding))


def test_installation_token_is_read_only_cached_and_refreshed(tmp_path: Path) -> None:
    clock = [NOW]
    signer = FakeSigner()
    transport = FakeTransport(
        [
            response("ghs_first_token_123456", NOW + timedelta(hours=1)),
            response(
                "ghs_second_token_12345",
                NOW + timedelta(hours=2),
            ),
        ]
    )
    provider = GitHubAppInstallationTokenProvider(
        app_id=123,
        installation_id=456,
        signer=signer,
        askpass_path=helper(tmp_path),
        transport=transport,
        now=lambda: clock[0],
    )

    first = provider.git_environment()
    assert first["AUTOGRADE_GIT_PASSWORD"] == "ghs_first_token_123456"
    assert first["AUTOGRADE_GIT_USERNAME"] == "x-access-token"
    assert first["GIT_ASKPASS_REQUIRE"] == "force"
    assert provider.get_token() == "ghs_first_token_123456"
    assert len(transport.calls) == 1

    method, url, headers, body, timeout = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/app/installations/456/access_tokens")
    assert json.loads(body) == {"permissions": {"contents": "read"}}
    assert headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert timeout == 10
    encoded_jwt = headers["Authorization"].removeprefix("Bearer ")
    encoded_header, encoded_payload, _signature = encoded_jwt.split(".")
    assert decode_segment(encoded_header) == {"alg": "RS256", "typ": "JWT"}
    assert decode_segment(encoded_payload) == {
        "exp": int(NOW.timestamp()) + 540,
        "iat": int(NOW.timestamp()) - 60,
        "iss": "123",
    }
    assert signer.payloads == [f"{encoded_header}.{encoded_payload}".encode("ascii")]

    clock[0] = NOW + timedelta(minutes=56)
    assert provider.get_token() == "ghs_second_token_12345"
    assert len(transport.calls) == 2


def test_token_provider_rejects_write_permission_or_bad_expiry(tmp_path: Path) -> None:
    write_provider = GitHubAppInstallationTokenProvider(
        app_id=1,
        installation_id=2,
        signer=FakeSigner(),
        askpass_path=helper(tmp_path),
        transport=FakeTransport(
            [response("ghs_write_token_123456", NOW + timedelta(hours=1), contents="write")]
        ),
        now=lambda: NOW,
    )
    with pytest.raises(GitHubAppError, match="read-only"):
        write_provider.get_token()

    expiring_provider = GitHubAppInstallationTokenProvider(
        app_id=1,
        installation_id=2,
        signer=FakeSigner(),
        askpass_path=helper(tmp_path),
        transport=FakeTransport(
            [response("ghs_short_token_123456", NOW + timedelta(minutes=4))]
        ),
        now=lambda: NOW,
    )
    with pytest.raises(GitHubAppError, match="expires too soon"):
        expiring_provider.get_token()

    enterprise_provider = GitHubAppInstallationTokenProvider(
        app_id=1,
        installation_id=2,
        signer=FakeSigner(),
        askpass_path=helper(tmp_path),
        transport=FakeTransport([]),
        api_base_url="https://github.school.example/api/v3",
        now=lambda: NOW,
    )
    assert enterprise_provider.git_host == "github.school.example"


def test_installation_token_resolves_canonical_repository_identity(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        [
            response("ghs_repository_token_12345", NOW + timedelta(hours=1)),
            repository_response(),
        ]
    )
    provider = GitHubAppInstallationTokenProvider(
        app_id=1,
        installation_id=2,
        signer=FakeSigner(),
        askpass_path=helper(tmp_path),
        transport=transport,
        now=lambda: NOW,
    )

    repository = provider.get_repository(owner="school", name="lab01-student")

    assert repository.repository_id == 987654321
    assert repository.full_name == "school/lab01-student"
    assert repository.private is True
    assert provider.git_host == "github.com"
    method, url, headers, body, _timeout = transport.calls[1]
    assert method == "GET"
    assert url.endswith("/repos/school/lab01-student")
    assert headers["Authorization"] == "Bearer ghs_repository_token_12345"
    assert headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert body is None


@pytest.mark.parametrize(
    "payload",
    [
        repository_response(repository_id=0),
        repository_response(full_name="school/renamed"),
    ],
)
def test_repository_identity_rejects_invalid_or_redirected_metadata(
    tmp_path: Path,
    payload: HTTPResponse,
) -> None:
    provider = GitHubAppInstallationTokenProvider(
        app_id=1,
        installation_id=2,
        signer=FakeSigner(),
        askpass_path=helper(tmp_path),
        transport=FakeTransport(
            [response("ghs_repository_token_12345", NOW + timedelta(hours=1)), payload]
        ),
        now=lambda: NOW,
    )

    with pytest.raises(GitHubAppError, match="response is invalid"):
        provider.get_repository(owner="school", name="lab01-student")


def test_private_key_and_askpass_require_safe_values(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    private_key = tmp_path / "app.pem"
    private_key.write_text("not-a-real-key", encoding="utf-8")
    private_key.chmod(0o644)
    with pytest.raises(GitHubAppError, match="permissions"):
        OpenSSLRS256Signer(private_key)

    monkeypatch.setenv("AUTOGRADE_GIT_USERNAME", "x-access-token")
    monkeypatch.setenv("AUTOGRADE_GIT_PASSWORD", "ghs_secret_token_12345")
    assert askpass_main(["Username for https://github.com:"]) == 0
    assert capsys.readouterr().out == "x-access-token\n"
    assert askpass_main(["Password for https://github.com:"]) == 0
    assert capsys.readouterr().out == "ghs_secret_token_12345\n"
    assert askpass_main(["Unexpected prompt"]) == 2
    assert capsys.readouterr().out == ""


def test_askpass_helper_rejects_symlink_and_writable_file(tmp_path: Path) -> None:
    target = helper(tmp_path)
    symlink = tmp_path / "askpass-link"
    symlink.symlink_to(target)
    with pytest.raises(GitHubAppError, match="private executable"):
        GitHubAppInstallationTokenProvider(
            app_id=1,
            installation_id=2,
            signer=FakeSigner(),
            askpass_path=symlink,
        )

    target.chmod(0o722)
    with pytest.raises(GitHubAppError, match="private executable"):
        GitHubAppInstallationTokenProvider(
            app_id=1,
            installation_id=2,
            signer=FakeSigner(),
            askpass_path=target,
        )


def test_openssl_signer_produces_a_verifiable_rs256_signature(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is unavailable")
    private_key = tmp_path / "app.pem"
    public_key = tmp_path / "app.pub.pem"
    subprocess.run(
        (openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private_key)),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    private_key.chmod(0o600)
    subprocess.run(
        (openssl, "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    payload = b"header.payload"
    signature = OpenSSLRS256Signer(private_key, openssl_binary=openssl).sign(payload)
    signature_path = tmp_path / "signature.bin"
    signature_path.write_bytes(signature)
    verified = subprocess.run(
        (openssl, "dgst", "-sha256", "-verify", str(public_key), "-signature", str(signature_path)),
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert verified.returncode == 0


def test_git_collector_uses_ephemeral_environment_and_hides_provider_errors(
    tmp_path: Path,
) -> None:
    collector = GitCollector(
        tmp_path / "cache",
        tmp_path / "snapshots",
        git_environment_provider=lambda: {"AUTOGRADE_GIT_PASSWORD": "secret"},
    )
    environment = collector._command_environment()
    assert environment["AUTOGRADE_GIT_PASSWORD"] == "secret"
    assert "AUTOGRADE_GIT_PASSWORD" not in collector._git_environment

    broken = GitCollector(
        tmp_path / "broken-cache",
        tmp_path / "broken-snapshots",
        git_environment_provider=lambda: (_ for _ in ()).throw(RuntimeError("token-secret")),
    )
    with pytest.raises(GitCollectorError, match="credential provider failed") as captured:
        broken._command_environment()
    assert "token-secret" not in str(captured.value)
