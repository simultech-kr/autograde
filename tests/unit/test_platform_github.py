from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from autograde.platform_github import (
    GitHubOAuthClient,
    GitHubOAuthError,
    HTTPResponse,
)


class FakeTransport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append((method, url, dict(headers), body, timeout))
        return self.responses.pop(0)


def response(payload, status=200):
    return HTTPResponse(status=status, headers={}, body=json.dumps(payload).encode())


def client(transport: FakeTransport) -> GitHubOAuthClient:
    return GitHubOAuthClient(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="https://grade.example/oauth/github/callback",
        transport=transport,
    )


def test_authorization_url_has_exact_callback_state_and_minimal_scope() -> None:
    url = client(FakeTransport([])).authorization_url(state="signed-state")
    query = parse_qs(urlsplit(url).query)
    assert query == {
        "client_id": ["client-id"],
        "redirect_uri": ["https://grade.example/oauth/github/callback"],
        "scope": ["read:user"],
        "state": ["signed-state"],
    }


def test_exchange_returns_numeric_identity_without_exposing_token() -> None:
    transport = FakeTransport(
        [response({"access_token": "secret-token"}), response({"id": 123, "login": "alice"})]
    )
    identity = client(transport).exchange_identity(code="temporary-code")

    assert identity.user_id == 123
    assert identity.login == "alice"
    assert transport.requests[1][2]["Authorization"] == "Bearer secret-token"


@pytest.mark.parametrize(
    "payload",
    [{"id": "123", "login": "alice"}, {"id": 123, "login": ""}],
)
def test_exchange_rejects_malformed_identity(payload) -> None:
    transport = FakeTransport([response({"access_token": "token"}), response(payload)])
    with pytest.raises(GitHubOAuthError, match="GitHub user response"):
        client(transport).exchange_identity(code="code")


def test_exchange_rejects_token_error_without_echoing_secret() -> None:
    transport = FakeTransport([response({"error": "bad_verification_code"}, status=400)])
    with pytest.raises(GitHubOAuthError, match="token request failed") as error:
        client(transport).exchange_identity(code="do-not-leak")
    assert "do-not-leak" not in str(error.value)
