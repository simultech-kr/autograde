"""Minimal GitHub OAuth identity adapter for the student web login."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GitHubOAuthError(RuntimeError):
    """GitHub did not return a usable OAuth identity."""


@dataclass(frozen=True, slots=True)
class GitHubIdentity:
    user_id: int
    login: str


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse: ...


class UrllibTransport:
    """Small bounded urllib adapter that does not expose response bodies in errors."""

    MAX_RESPONSE_BYTES = 1_048_576

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                payload = response.read(self.MAX_RESPONSE_BYTES + 1)
                if len(payload) > self.MAX_RESPONSE_BYTES:
                    raise GitHubOAuthError("GitHub response exceeded the size limit")
                return HTTPResponse(
                    status=int(response.status),
                    headers={key: value for key, value in response.headers.items()},
                    body=payload,
                )
        except HTTPError as exc:
            # OAuth responses can contain token-adjacent diagnostics.  Keep the
            # externally visible error intentionally generic.
            raise GitHubOAuthError(f"GitHub returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GitHubOAuthError("GitHub OAuth request failed") from exc


class GitHubOAuthClient:
    AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    USER_URL = "https://api.github.com/user"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: HTTPTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("GitHub OAuth client id and secret are required")
        if not redirect_uri.startswith("https://") and not redirect_uri.startswith(
            "http://127.0.0.1:"
        ):
            raise ValueError("redirect_uri must use HTTPS or loopback HTTP")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.transport = transport or UrllibTransport()
        self.timeout = timeout

    def authorization_url(self, *, state: str) -> str:
        if not state:
            raise ValueError("OAuth state is required")
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": "read:user",
                "state": state,
            }
        )
        return f"{self.AUTHORIZE_URL}?{query}"

    def exchange_identity(self, *, code: str) -> GitHubIdentity:
        if not code or len(code) > 1024 or any(ord(character) < 32 for character in code):
            raise GitHubOAuthError("GitHub authorization code is invalid")
        token_response = self.transport.request(
            "POST",
            self.TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "school-autograde/0.1",
            },
            body=urlencode(
                {
                    "client_id": self.client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                }
            ).encode("ascii"),
            timeout=self.timeout,
        )
        token_payload = self._json_object(token_response, "token")
        access_token = token_payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise GitHubOAuthError("GitHub token response is missing an access token")
        try:
            user_response = self.transport.request(
                "GET",
                self.USER_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "school-autograde/0.1",
                },
                body=None,
                timeout=self.timeout,
            )
        finally:
            # Drop our last local binding as soon as the identity lookup ends.
            access_token = ""
        user_payload = self._json_object(user_response, "user")
        user_id = user_payload.get("id")
        login = user_payload.get("login")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise GitHubOAuthError("GitHub user response has an invalid numeric id")
        if not isinstance(login, str) or not login.strip() or len(login) > 255:
            raise GitHubOAuthError("GitHub user response has an invalid login")
        return GitHubIdentity(user_id=user_id, login=login.strip())

    @staticmethod
    def _json_object(response: HTTPResponse, label: str) -> Mapping[str, object]:
        if response.status != 200:
            raise GitHubOAuthError(f"GitHub {label} request failed")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubOAuthError(f"GitHub {label} response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubOAuthError(f"GitHub {label} response must be an object")
        return payload

