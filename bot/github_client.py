"""Minimal async GitHub REST API client and shared constants."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
GITHUB_COLOR = 0x24292E
POLL_INTERVAL_SECONDS = 60
MAX_ISSUES_LISTED = 8
MAX_RELEASES_LISTED = 5
MAX_SEARCH_RESULTS = 6
MAX_REVIEW_QUEUE_PRS = 10
MAX_TRIAGE_ITEMS = 5
DEFAULT_REVIEW_DIGEST_HOUR_UTC = 13
ISSUE_TEMPLATE_KEYS = ("bug", "feature", "docs")

_VALID_EVENTS = {"push", "pull_request", "issues", "release"}
_DEFAULT_EVENTS = "push,pull_request,issues,release"


# ---------------------------------------------------------------------------
# GitHub API client
# ---------------------------------------------------------------------------

class GitHubClient:
    """Minimal async GitHub REST API client."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    def _headers(self, extra: dict | None = None) -> dict:
        h: dict = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "DiscordBot-GitHubCog/1.0",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if extra:
            h.update(extra)
        return h

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def request(
        self,
        method: str,
        path: str,
        *,
        extra_headers: dict | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, dict | list | str | None, dict]:
        """Perform an HTTP request relative to the GitHub API."""
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        session = await self._session_get()
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(extra_headers),
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp_headers = dict(resp.headers)
                if resp.status == 304:
                    return 304, None, resp_headers
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text()
                return resp.status, body, resp_headers
        except Exception as exc:
            logger.warning("GitHub API error for %s: %s", url, exc)
            return 0, None, {}

    async def get(self, path: str, *, extra_headers: dict | None = None) -> tuple[int, dict | list | str | None, dict]:
        """GET *path* (relative to GITHUB_API). Returns (status, body, response_headers)."""
        return await self.request("GET", path, extra_headers=extra_headers)

    async def post(self, path: str, *, json_body: dict[str, Any]) -> tuple[int, dict | list | str | None, dict]:
        return await self.request("POST", path, json_body=json_body)

    async def patch(self, path: str, *, json_body: dict[str, Any]) -> tuple[int, dict | list | str | None, dict]:
        return await self.request("PATCH", path, json_body=json_body)

    async def delete(self, path: str, *, json_body: dict[str, Any] | None = None) -> tuple[int, dict | list | str | None, dict]:
        return await self.request("DELETE", path, json_body=json_body)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
