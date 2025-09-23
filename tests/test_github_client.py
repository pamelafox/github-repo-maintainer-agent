import asyncio
from collections import deque

import httpx
import pytest

from github_client import GitHubClient
from models import Repository


class DummyResponse:
    """Minimal httpx-like response for testing."""

    def __init__(
        self,
        payload: list[dict],
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ):
        self._payload = payload
        self.headers = headers or {}
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> list[dict]:
        return self._payload


class DummyAsyncClient:
    """Async context manager that yields predictable responses."""

    def __init__(
        self,
        responses: deque[DummyResponse | Exception],
    ):
        self._responses = responses

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def get(self, url: str, headers: dict[str, str] | None = None):
        if not self._responses:
            raise AssertionError("No more responses queued for DummyAsyncClient")
        result = self._responses.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo() -> Repository:
    return Repository(name="example", owner="octocat", archived=False)


def test_issue_exists_with_title_returns_true(monkeypatch, repo):
    matching_title = "Check failing"
    responses = deque([
        DummyResponse([
            {"title": matching_title, "number": 7},
        ])
    ])

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: DummyAsyncClient(responses),
    )

    client = GitHubClient(auth_token="test-token")
    assert _run(client.issue_exists_with_title(repo, matching_title)) is True


def test_issue_exists_with_title_checks_pagination(monkeypatch, repo):
    matching_title = "Pattern detected"
    responses = deque([
        DummyResponse(
            payload=[{"title": "Other issue"}],
            headers={"link": "<https://next-page>; rel=\"next\""},
        ),
        DummyResponse(
            payload=[{"title": matching_title, "number": 42}],
            headers={},
        ),
    ])

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: DummyAsyncClient(responses),
    )

    client = GitHubClient(auth_token="test-token")
    assert _run(client.issue_exists_with_title(repo, matching_title)) is True


def test_issue_exists_with_title_handles_http_error(monkeypatch, repo):
    request = httpx.Request("GET", "https://api.github.com")
    error = httpx.HTTPStatusError(
        "boom",
        request=request,
        response=httpx.Response(500, request=request),
    )
    responses = deque([
        DummyResponse([], error=error)
    ])

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: DummyAsyncClient(responses),
    )

    client = GitHubClient(auth_token="test-token")
    assert _run(client.issue_exists_with_title(repo, "Missing")) is False
