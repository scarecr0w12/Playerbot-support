"""Async web crawler for RAG ingestion.

Fetches one or more web pages, strips HTML to plain text, and splits
the result into overlapping chunks suitable for embedding.
"""

from __future__ import annotations

import logging
import re
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse

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

_SKIP_TAGS = {
    "script", "style", "noscript", "nav", "footer", "header",
    "aside", "form", "button", "svg", "img",
}

_USER_AGENT = (
    "Mozilla/5.0 (compatible; DiscordBot-RAGCrawler/1.0; +https://github.com)"
)


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
