from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from bot.crawler import WebCrawler


class RepoAwareCrawlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_github_repository_crawl_uses_raw_file_contents(self) -> None:
        crawler = WebCrawler(chunk_size=500, max_pages=5)
        crawler._github_api_json = AsyncMock(
            side_effect=[
                (200, {"default_branch": "main"}),
                (
                    200,
                    {
                        "tree": [
                            {"path": "README.md", "type": "blob", "size": 42},
                            {"path": "src/app.py", "type": "blob", "size": 64},
                            {"path": "assets/logo.png", "type": "blob", "size": 2048},
                        ]
                    },
                ),
            ]
        )
        crawler._fetch_text_url = AsyncMock(side_effect=["# Title\nhello repo", "print('hello')"])

        results = await crawler._crawl_github_repository("https://github.com/octo/example", session=object(), limit=5)

        self.assertIsNotNone(results)
        assert results is not None
        self.assertEqual(len(results), 2)
        self.assertEqual(
            [result.url for result in results],
            [
                "https://github.com/octo/example/blob/main/README.md",
                "https://github.com/octo/example/blob/main/src/app.py",
            ],
        )
        self.assertEqual(results[0].title, "octo/example:README.md")
        self.assertIn("Repository: octo/example", results[0].chunks[0])
        self.assertIn("Path: README.md", results[0].chunks[0])
        self.assertIn("hello repo", results[0].chunks[0])

    async def test_gitlab_repository_crawl_uses_tree_and_raw_blob_urls(self) -> None:
        crawler = WebCrawler(chunk_size=500, max_pages=5)
        crawler._gitlab_api_json = AsyncMock(
            side_effect=[
                (200, {"default_branch": "main"}),
                (
                    200,
                    [
                        {"path": "README.md", "type": "blob"},
                        {"path": "docs/setup.md", "type": "blob"},
                        {"path": "dist/app.js", "type": "blob"},
                    ],
                ),
            ]
        )
        crawler._fetch_text_url = AsyncMock(side_effect=["intro", "setup guide"])

        results = await crawler._crawl_gitlab_repository("https://gitlab.com/group/project", session=object(), limit=5)

        self.assertIsNotNone(results)
        assert results is not None
        self.assertEqual(len(results), 2)
        self.assertEqual(
            [result.url for result in results],
            [
                "https://gitlab.com/group/project/-/blob/main/README.md",
                "https://gitlab.com/group/project/-/blob/main/docs/setup.md",
            ],
        )
        self.assertEqual(results[1].title, "group/project:docs/setup.md")
        self.assertIn("Repository: group/project", results[1].chunks[0])
        self.assertIn("Path: docs/setup.md", results[1].chunks[0])
        self.assertIn("setup guide", results[1].chunks[0])