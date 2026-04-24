"""Async web crawler for RAG ingestion.

Fetches one or more web pages, strips HTML to plain text, and splits
the result into overlapping chunks suitable for embedding.
"""

from __future__ import annotations

import logging
import os
import re
from typing import AsyncIterator
from urllib.parse import quote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CHUNK_SIZE = 800        # characters per chunk
DEFAULT_CHUNK_OVERLAP = 150     # overlap between consecutive chunks
DEFAULT_MAX_PAGES = 20          # hard ceiling on recursive crawls
DEFAULT_TIMEOUT = 15            # seconds per HTTP request
MAX_REPO_FILE_BYTES = 200_000

_SKIP_TAGS = {
    "script", "style", "noscript", "nav", "footer", "header",
    "aside", "form", "button", "svg", "img",
}

_USER_AGENT = (
    "Mozilla/5.0 (compatible; DiscordBot-RAGCrawler/1.0; +https://github.com)"
)

_TEXT_FILE_EXTENSIONS = {
    ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs", ".css", ".csv", ".env",
    ".go", ".h", ".hpp", ".html", ".ini", ".java", ".js", ".json", ".jsx",
    ".kt", ".kts", ".log", ".lua", ".md", ".php", ".properties", ".py",
    ".rb", ".rs", ".rst", ".scss", ".sh", ".sql", ".svg", ".toml", ".ts",
    ".tsx", ".txt", ".xml", ".yaml", ".yml",
}

_TEXT_FILE_NAMES = {
    "dockerfile", "makefile", "readme", "license", "copying", "authors", "notice",
    ".gitignore", ".gitattributes", "requirements.txt", "pyproject.toml", "poetry.lock",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "cargo.toml",
    "cargo.lock", "go.mod", "go.sum",
}

_SKIP_REPO_DIR_MARKERS = {
    ".git", ".github", ".idea", ".next", ".nuxt", ".venv", "__pycache__", "build",
    "coverage", "dist", "node_modules", "site-packages", "target", "vendor",
}

_GITHUB_HOSTS = {"github.com", "www.github.com"}


def _gitlab_hosts() -> set[str]:
    hosts = {"gitlab.com", "www.gitlab.com"}
    configured = os.getenv("GITLAB_URL", "").strip()
    if configured:
        parsed = urlparse(configured)
        if parsed.netloc:
            hosts.add(parsed.netloc.lower())
    return hosts


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _extract_text(html: str) -> str:
    """Strip HTML to readable plain text, removing boilerplate tags."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_SKIP_TAGS):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split *text* into overlapping chunks of at most *chunk_size* chars."""
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def _same_origin(base: str, url: str) -> bool:
    """Return True if *url* shares the same scheme+host as *base*."""
    b = urlparse(base)
    u = urlparse(url)
    return b.scheme == u.scheme and b.netloc == u.netloc


def _normalise_url(url: str) -> str:
    """Strip fragment identifiers and trailing slashes for dedup."""
    p = urlparse(url)
    return p._replace(fragment="").geturl().rstrip("/")


def _path_suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1]


def _looks_like_text_repo_file(path: str) -> bool:
    parts = [part.lower() for part in path.split("/") if part]
    if not parts:
        return False
    if any(part in _SKIP_REPO_DIR_MARKERS for part in parts[:-1]):
        return False
    name = parts[-1]
    if name.endswith((".min.js", ".min.css")):
        return False
    if name in _TEXT_FILE_NAMES:
        return True
    return _path_suffix(name) in _TEXT_FILE_EXTENSIONS


def _repo_file_title(repo: str, path: str) -> str:
    return f"{repo}:{path}"


def _repo_file_body(repo: str, path: str, text: str) -> str:
    body = text.replace("\r\n", "\n").strip()
    return f"Repository: {repo}\nPath: {path}\n\n{body}" if body else ""


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class CrawlResult:
    """Container returned for each successfully crawled page."""

    __slots__ = ("url", "title", "chunks")

    def __init__(self, url: str, title: str, chunks: list[str]) -> None:
        self.url = url
        self.title = title
        self.chunks = chunks


class WebCrawler:
    """Async web crawler that extracts text and yields :class:`CrawlResult` objects."""

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        timeout: int = DEFAULT_TIMEOUT,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.timeout = timeout
        self.max_pages = max_pages

    async def _request_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict | list | str | None]:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                allow_redirects=True,
                headers={"User-Agent": _USER_AGENT, **(headers or {})},
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = await resp.text(errors="replace")
                return resp.status, body
        except Exception as exc:
            logger.warning("Crawler: JSON request failed for %s — %s", url, exc)
            return 0, None

    async def _fetch_text_url(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                allow_redirects=True,
                headers={"User-Agent": _USER_AGENT, **(headers or {})},
            ) as resp:
                if resp.status != 200:
                    return None
                if resp.content_length and resp.content_length > MAX_REPO_FILE_BYTES:
                    return None
                return await resp.text(errors="replace")
        except Exception as exc:
            logger.warning("Crawler: failed to fetch text file %s — %s", url, exc)
            return None

    async def _github_api_json(
        self,
        session: aiohttp.ClientSession,
        path: str,
    ) -> tuple[int, dict | list | str | None]:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.getenv("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await self._request_json(session, f"https://api.github.com{path}", headers=headers)

    async def _gitlab_api_json(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        path: str,
    ) -> tuple[int, dict | list | str | None]:
        headers: dict[str, str] = {}
        token = os.getenv("GITLAB_TOKEN", "").strip()
        if token:
            headers["PRIVATE-TOKEN"] = token
        return await self._request_json(session, f"{base_url}/api/v4{path}", headers=headers)

    def _github_repo_spec(self, url: str) -> tuple[str, str] | None:
        parsed = urlparse(url)
        if parsed.netloc.lower() not in _GITHUB_HOSTS:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        if not owner or not repo:
            return None
        return owner, repo

    def _gitlab_repo_spec(self, url: str) -> tuple[str, str] | None:
        parsed = urlparse(url)
        if parsed.netloc.lower() not in _gitlab_hosts():
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            return None
        if "-" in parts:
            dash_index = parts.index("-")
            project_parts = parts[:dash_index]
        else:
            project_parts = parts
        if len(project_parts) < 2:
            return None
        project = "/".join(project_parts)
        return parsed.scheme + "://" + parsed.netloc, project

    async def _crawl_github_repository(
        self,
        url: str,
        session: aiohttp.ClientSession,
        limit: int,
    ) -> list[CrawlResult] | None:
        spec = self._github_repo_spec(url)
        if spec is None:
            return None

        owner, repo = spec
        status, repo_data = await self._github_api_json(session, f"/repos/{owner}/{repo}")
        if status != 200 or not isinstance(repo_data, dict):
            return None
        branch = str(repo_data.get("default_branch") or "main")
        status, tree_data = await self._github_api_json(
            session,
            f"/repos/{owner}/{repo}/git/trees/{quote(branch, safe='')}?recursive=1",
        )
        if status != 200 or not isinstance(tree_data, dict):
            return None

        results: list[CrawlResult] = []
        repo_name = f"{owner}/{repo}"
        tree = tree_data.get("tree")
        if not isinstance(tree, list):
            return None
        for item in tree:
            if len(results) >= limit:
                break
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            size = item.get("size")
            if not path or not _looks_like_text_repo_file(path):
                continue
            if isinstance(size, int) and size > MAX_REPO_FILE_BYTES:
                continue
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(branch, safe='')}/{quote(path, safe='/')}"
            text = await self._fetch_text_url(session, raw_url)
            if text is None:
                continue
            body = _repo_file_body(repo_name, path, text)
            chunks = chunk_text(body, self.chunk_size, self.chunk_overlap)
            if not chunks:
                continue
            results.append(
                CrawlResult(
                    url=f"https://github.com/{owner}/{repo}/blob/{quote(branch, safe='')}/{quote(path, safe='/')}",
                    title=_repo_file_title(repo_name, path),
                    chunks=chunks,
                )
            )
        return results or None

    async def _crawl_gitlab_repository(
        self,
        url: str,
        session: aiohttp.ClientSession,
        limit: int,
    ) -> list[CrawlResult] | None:
        spec = self._gitlab_repo_spec(url)
        if spec is None:
            return None

        base_url, project = spec
        encoded_project = quote(project, safe="")
        status, project_data = await self._gitlab_api_json(session, base_url, f"/projects/{encoded_project}")
        if status != 200 or not isinstance(project_data, dict):
            return None
        branch = str(project_data.get("default_branch") or "main")

        items: list[dict] = []
        page = 1
        while len(items) < limit:
            status, tree_data = await self._gitlab_api_json(
                session,
                base_url,
                f"/projects/{encoded_project}/repository/tree?ref={quote(branch, safe='')}&recursive=true&per_page=100&page={page}",
            )
            if status != 200 or not isinstance(tree_data, list):
                return None if not items else []
            if not tree_data:
                break
            items.extend(item for item in tree_data if isinstance(item, dict))
            if len(tree_data) < 100:
                break
            page += 1

        results: list[CrawlResult] = []
        for item in items:
            if len(results) >= limit:
                break
            if item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            if not path or not _looks_like_text_repo_file(path):
                continue
            raw_url = f"{base_url}/{project}/-/raw/{quote(branch, safe='')}/{quote(path, safe='/')}"
            headers: dict[str, str] = {}
            token = os.getenv("GITLAB_TOKEN", "").strip()
            if token:
                headers["PRIVATE-TOKEN"] = token
            text = await self._fetch_text_url(session, raw_url, headers=headers)
            if text is None:
                continue
            body = _repo_file_body(project, path, text)
            chunks = chunk_text(body, self.chunk_size, self.chunk_overlap)
            if not chunks:
                continue
            results.append(
                CrawlResult(
                    url=f"{base_url}/{project}/-/blob/{quote(branch, safe='')}/{quote(path, safe='/')}",
                    title=_repo_file_title(project, path),
                    chunks=chunks,
                )
            )
        return results or None

    async def _crawl_repository(
        self,
        start_url: str,
        session: aiohttp.ClientSession,
        limit: int,
    ) -> list[CrawlResult] | None:
        github_results = await self._crawl_github_repository(start_url, session, limit)
        if github_results is not None:
            return github_results
        return await self._crawl_gitlab_repository(start_url, session, limit)

    async def fetch_page(self, url: str, session: aiohttp.ClientSession) -> tuple[str, str] | None:
        """Fetch *url* and return *(title, text)* or None on failure."""
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                allow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            ) as resp:
                if resp.status != 200:
                    logger.warning("Crawler: %s returned HTTP %d", url, resp.status)
                    return None
                content_type = resp.headers.get("Content-Type", "")
                if "text/html" not in content_type and "text/plain" not in content_type:
                    logger.debug("Crawler: skipping non-HTML %s (%s)", url, content_type)
                    return None
                html = await resp.text(errors="replace")
        except Exception as exc:
            logger.warning("Crawler: failed to fetch %s — %s", url, exc)
            return None

        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        title = title_tag.get_text().strip() if title_tag else url
        text = _extract_text(html)
        return title, text

    async def crawl_one(self, url: str) -> CrawlResult | None:
        """Fetch and chunk a single URL."""
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            result = await self.fetch_page(url, session)
        if result is None:
            return None
        title, text = result
        chunks = chunk_text(text, self.chunk_size, self.chunk_overlap)
        if not chunks:
            return None
        return CrawlResult(url=url, title=title, chunks=chunks)

    async def crawl_site(
        self,
        start_url: str,
        *,
        max_pages: int | None = None,
        same_origin_only: bool = True,
    ) -> AsyncIterator[CrawlResult]:
        """Breadth-first crawl starting from *start_url*.

        Yields :class:`CrawlResult` for each successfully crawled page.
        Stays within the same origin by default.
        """
        limit = max_pages if max_pages is not None else self.max_pages
        visited: set[str] = set()
        queue: list[str] = [_normalise_url(start_url)]

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            repo_results = await self._crawl_repository(start_url, session, limit)
            if repo_results is not None:
                for result in repo_results:
                    yield result
                return

            while queue and len(visited) < limit:
                url = queue.pop(0)
                norm = _normalise_url(url)
                if norm in visited:
                    continue
                visited.add(norm)

                result = await self.fetch_page(url, session)
                if result is None:
                    continue

                title, text = result
                chunks = chunk_text(text, self.chunk_size, self.chunk_overlap)
                if chunks:
                    yield CrawlResult(url=url, title=title, chunks=chunks)

                # Discover more links on the same page
                if len(visited) < limit:
                    try:
                        html_again = text  # already extracted; re-parse raw HTML for links
                        # We need HTML for link discovery — re-fetch is wasteful, so
                        # we embedded link discovery in a second BeautifulSoup pass above.
                        # Instead, use a lightweight approach: fetch raw HTML once per page.
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(total=self.timeout),
                            headers={"User-Agent": _USER_AGENT},
                        ) as resp:
                            if resp.status == 200:
                                raw_html = await resp.text(errors="replace")
                                soup = BeautifulSoup(raw_html, "html.parser")
                                for a in soup.find_all("a", href=True):
                                    href = urljoin(url, a["href"])
                                    href = _normalise_url(href)
                                    parsed = urlparse(href)
                                    if parsed.scheme not in ("http", "https"):
                                        continue
                                    if same_origin_only and not _same_origin(start_url, href):
                                        continue
                                    if href not in visited and href not in queue:
                                        queue.append(href)
                    except Exception:
                        pass

    # Needed to make the async generator work as an `async for` target
    # when called as a method — Python handles this automatically.
