"""Short-lived GitHub App credentials for private repository collection.

The student platform never uses a student's GitHub OAuth token.  This module
authenticates the server as a GitHub App, caches a read-only installation token
until shortly before expiry, and exposes it only to one Git subprocess through
``GIT_ASKPASS`` environment variables.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
from typing import Callable, Mapping, Protocol
from urllib.parse import quote, urlsplit

from .platform_github import GitHubOAuthError, HTTPTransport, UrllibTransport


class GitHubAppError(RuntimeError):
    """GitHub App credentials could not be obtained safely."""


class JWTSigner(Protocol):
    def sign(self, payload: bytes) -> bytes: ...


class OpenSSLRS256Signer:
    """Sign a JWT input with a protected PEM key using the OpenSSL CLI."""

    def __init__(
        self,
        private_key: str | os.PathLike[str],
        *,
        openssl_binary: str = "openssl",
        timeout_seconds: float = 5.0,
    ) -> None:
        key = Path(private_key).expanduser().absolute()
        if os.path.lexists(key) and key.is_symlink():
            raise GitHubAppError("GitHub App private key must not be a symlink")
        try:
            key_stat = key.stat()
        except OSError as exc:
            raise GitHubAppError("GitHub App private key is unavailable") from exc
        if not stat.S_ISREG(key_stat.st_mode):
            raise GitHubAppError("GitHub App private key must be a regular file")
        if stat.S_IMODE(key_stat.st_mode) & 0o077:
            raise GitHubAppError("GitHub App private key permissions must be 0600 or stricter")
        if not openssl_binary or "\x00" in openssl_binary:
            raise ValueError("openssl_binary must be a safe executable name")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.private_key = key
        self.openssl_binary = openssl_binary
        self.timeout_seconds = float(timeout_seconds)

    def sign(self, payload: bytes) -> bytes:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("JWT signing payload must be non-empty bytes")
        try:
            result = subprocess.run(
                (
                    self.openssl_binary,
                    "dgst",
                    "-sha256",
                    "-sign",
                    str(self.private_key),
                ),
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubAppError("GitHub App JWT signing failed") from exc
        if result.returncode != 0 or not result.stdout:
            raise GitHubAppError("GitHub App JWT signing failed")
        return result.stdout


@dataclass(frozen=True, slots=True)
class InstallationToken:
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GitHubRepositoryIdentity:
    """Repository facts returned through the App installation identity."""

    repository_id: int
    full_name: str
    private: bool
    archived: bool
    disabled: bool
    default_branch: str


class GitHubAppInstallationTokenProvider:
    """Generate and cache one installation token with ``contents: read`` only."""

    API_VERSION = "2026-03-10"
    MAX_RESPONSE_BYTES = 1_048_576
    _TOKEN = re.compile(r"^[A-Za-z0-9._~-]{16,4096}$")

    def __init__(
        self,
        *,
        app_id: int,
        installation_id: int,
        signer: JWTSigner,
        askpass_path: str | os.PathLike[str],
        transport: HTTPTransport | None = None,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
        refresh_skew_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        for value, field_name in (
            (app_id, "app_id"),
            (installation_id, "installation_id"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if not callable(getattr(signer, "sign", None)):
            raise TypeError("signer must provide sign(payload)")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(refresh_skew_seconds, bool)
            or not isinstance(refresh_skew_seconds, int)
            or not 30 <= refresh_skew_seconds <= 900
        ):
            raise ValueError("refresh_skew_seconds must be between 30 and 900")

        parsed = urlsplit(api_base_url.rstrip("/"))
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        ):
            raise ValueError("api_base_url must be HTTPS or loopback HTTP")

        helper = Path(askpass_path).expanduser().absolute()
        try:
            if os.path.lexists(helper) and helper.is_symlink():
                raise GitHubAppError(
                    "Git askpass helper must be a private executable file"
                )
            helper_stat = helper.stat()
        except OSError as exc:
            raise GitHubAppError("Git askpass helper is unavailable") from exc
        if (
            not stat.S_ISREG(helper_stat.st_mode)
            or not os.access(helper, os.X_OK)
            or stat.S_IMODE(helper_stat.st_mode) & 0o022
        ):
            raise GitHubAppError(
                "Git askpass helper must be a private executable file"
            )

        self.app_id = app_id
        self.installation_id = installation_id
        self.signer = signer
        self.askpass_path = helper
        self.transport = transport or UrllibTransport()
        self.api_base_url = api_base_url.rstrip("/")
        self.git_host = (
            "github.com"
            if parsed.hostname == "api.github.com"
            else str(parsed.hostname)
        )
        self.timeout_seconds = float(timeout_seconds)
        self.refresh_skew = timedelta(seconds=refresh_skew_seconds)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cached: InstallationToken | None = None
        self._lock = threading.Lock()

    def get_token(self) -> str:
        now = self._aware_now()
        with self._lock:
            cached = self._cached
            if cached is not None and now + self.refresh_skew < cached.expires_at:
                return cached.token
            issued = self._request_token(now)
            self._cached = issued
            return issued.token

    def git_environment(self) -> Mapping[str, str]:
        """Return credentials for one child Git command, never for persistence."""

        return {
            "GIT_ASKPASS": str(self.askpass_path),
            "GIT_ASKPASS_REQUIRE": "force",
            "GIT_TERMINAL_PROMPT": "0",
            "AUTOGRADE_GIT_USERNAME": "x-access-token",
            "AUTOGRADE_GIT_PASSWORD": self.get_token(),
        }

    def get_repository(
        self,
        *,
        owner: str,
        name: str,
    ) -> GitHubRepositoryIdentity:
        """Resolve canonical repository identity with the installation token."""

        owner = self._repository_component(owner, "repository owner")
        name = self._repository_component(name, "repository name")
        token = self.get_token()
        try:
            response = self.transport.request(
                "GET",
                f"{self.api_base_url}/repos/{quote(owner, safe='')}/{quote(name, safe='')}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "school-autograde/0.1",
                    "X-GitHub-Api-Version": self.API_VERSION,
                },
                body=None,
                timeout=self.timeout_seconds,
            )
        except (GitHubOAuthError, OSError, TimeoutError) as exc:
            raise GitHubAppError("GitHub App repository lookup failed") from exc
        finally:
            token = ""
        if response.status != 200 or len(response.body) > self.MAX_RESPONSE_BYTES:
            raise GitHubAppError("GitHub App repository lookup failed")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubAppError("GitHub App repository response is invalid") from exc
        if not isinstance(payload, dict):
            raise GitHubAppError("GitHub App repository response is invalid")

        repository_id = payload.get("id")
        full_name = payload.get("full_name")
        private = payload.get("private")
        archived = payload.get("archived")
        disabled = payload.get("disabled")
        default_branch = payload.get("default_branch")
        expected_full_name = f"{owner}/{name}"
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or not isinstance(full_name, str)
            or full_name.casefold() != expected_full_name.casefold()
            or not isinstance(private, bool)
            or not isinstance(archived, bool)
            or not isinstance(disabled, bool)
            or not isinstance(default_branch, str)
            or not default_branch
            or len(default_branch) > 255
            or any(ord(character) < 32 for character in default_branch)
        ):
            raise GitHubAppError("GitHub App repository response is invalid")
        return GitHubRepositoryIdentity(
            repository_id=repository_id,
            full_name=full_name,
            private=private,
            archived=archived,
            disabled=disabled,
            default_branch=default_branch,
        )

    def _request_token(self, now: datetime) -> InstallationToken:
        jwt = self._jwt(now)
        body = json.dumps(
            {"permissions": {"contents": "read"}},
            separators=(",", ":"),
        ).encode("ascii")
        try:
            response = self.transport.request(
                "POST",
                (
                    f"{self.api_base_url}/app/installations/"
                    f"{self.installation_id}/access_tokens"
                ),
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json",
                    "User-Agent": "school-autograde/0.1",
                    "X-GitHub-Api-Version": self.API_VERSION,
                },
                body=body,
                timeout=self.timeout_seconds,
            )
        except (GitHubOAuthError, OSError, TimeoutError) as exc:
            raise GitHubAppError("GitHub App installation token request failed") from exc
        finally:
            jwt = ""
        if response.status != 201 or len(response.body) > self.MAX_RESPONSE_BYTES:
            raise GitHubAppError("GitHub App installation token request failed")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubAppError("GitHub App token response is invalid") from exc
        if not isinstance(payload, dict):
            raise GitHubAppError("GitHub App token response is invalid")
        token = payload.get("token")
        expires_raw = payload.get("expires_at")
        permissions = payload.get("permissions")
        if not isinstance(token, str) or not self._TOKEN.fullmatch(token):
            raise GitHubAppError("GitHub App token response is invalid")
        if not isinstance(expires_raw, str):
            raise GitHubAppError("GitHub App token response is invalid")
        try:
            expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitHubAppError("GitHub App token response is invalid") from exc
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise GitHubAppError("GitHub App token response is invalid")
        expires_at = expires_at.astimezone(timezone.utc)
        if expires_at <= now + self.refresh_skew:
            raise GitHubAppError("GitHub App token expires too soon")
        if not isinstance(permissions, dict) or permissions.get("contents") != "read":
            raise GitHubAppError("GitHub App token is not contents-read-only")
        return InstallationToken(token=token, expires_at=expires_at)

    def _jwt(self, now: datetime) -> str:
        timestamp = int(now.timestamp())
        header = _base64url(json.dumps(
            {"alg": "RS256", "typ": "JWT"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"))
        payload = _base64url(json.dumps(
            {
                "exp": timestamp + 9 * 60,
                "iat": timestamp - 60,
                "iss": str(self.app_id),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"))
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = self.signer.sign(signing_input)
        return f"{header}.{payload}.{_base64url(signature)}"

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise GitHubAppError("GitHub App clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _repository_component(value: str, label: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 255
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError(f"{label} is invalid")
        return value


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def askpass_main(argv: list[str] | None = None) -> int:
    """Console-script entry point used only as Git's non-interactive askpass."""

    arguments = sys.argv[1:] if argv is None else argv
    prompt = arguments[0].casefold() if arguments else ""
    if len(prompt) > 4096:
        return 2
    if "username" in prompt:
        value = os.environ.get("AUTOGRADE_GIT_USERNAME", "x-access-token")
    elif "password" in prompt:
        value = os.environ.get("AUTOGRADE_GIT_PASSWORD", "")
    else:
        return 2
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        return 2
    sys.stdout.write(value + "\n")
    return 0


__all__ = [
    "GitHubAppError",
    "GitHubAppInstallationTokenProvider",
    "InstallationToken",
    "JWTSigner",
    "OpenSSLRS256Signer",
    "askpass_main",
]
