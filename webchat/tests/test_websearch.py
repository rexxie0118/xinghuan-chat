"""Tests for web search/fetch/ranking/conflict tools without network."""
from __future__ import annotations

import unittest

from webchat.tools import ToolRegistry
from webchat.websearch import (
    FetchedPage,
    SearchResult,
    WebResearchService,
    WebSearchError,
    conflict_detect,
    fetch_page,
    register_web_tools,
    source_rank,
)


class FakeSearch:
    def search(self, query, *, freshness="week", limit=5):
        return [
            SearchResult("官方天气", "https://www.cma.gov.cn/a", "北京 0℃", published_at="2026-11-24T08:00:00+00:00", domain="www.cma.gov.cn"),
            SearchResult("博客天气", "https://example.com/b", "北京 20℃", published_at="2026-01-01T00:00:00+00:00", domain="example.com"),
        ]


class FakeFetcher:
    def __call__(self, url):
        return FetchedPage(url=url, title="页面", text="北京当前气温 0℃。" * 10, published_at="2026-11-24T08:00:00+00:00", domain="www.cma.gov.cn")


class WebToolsTest(unittest.TestCase):
    def test_source_rank_prefers_official_recent(self):
        ranked = source_rank([
            {"title": "官方", "url": "https://www.cma.gov.cn/a", "snippet": "0℃", "published_at": "2026-11-24T08:00:00+00:00", "domain": "www.cma.gov.cn"},
            {"title": "博客", "url": "https://example.com/b", "snippet": "20℃", "published_at": "2026-01-01T00:00:00+00:00", "domain": "example.com"},
        ])
        self.assertEqual(ranked[0]["domain"], "www.cma.gov.cn")
        self.assertGreater(ranked[0]["trust_score"], ranked[1]["trust_score"])

    def test_conflict_detection_for_different_numbers(self):
        result = conflict_detect([
            {"title": "价格 10 元", "snippet": "当前价格 10 元", "domain": "a.com", "url": "https://a.com"},
            {"title": "价格 12 元", "snippet": "当前价格 12 元", "domain": "b.com", "url": "https://b.com"},
        ])
        self.assertTrue(result["conflict"])
        self.assertTrue(result["details"])

    def test_research_combines_sources_and_conflicts(self):
        service = WebResearchService(search=FakeSearch(), fetcher=FakeFetcher())
        data = service.research({"query": "北京天气", "limit": 2})
        self.assertEqual(data["query"], "北京天气")
        self.assertEqual(len(data["sources"]), 2)
        self.assertIn("conflicts", data)

    def test_fetch_rejects_private_address(self):
        with self.assertRaises(WebSearchError):
            fetch_page("https://127.0.0.1/secret")

    def test_web_tools_are_registered(self):
        tools = ToolRegistry({})
        register_web_tools(tools, object())
        names = [spec.name for spec in tools.list_tools()]
        self.assertIn("web.search", names)
        self.assertIn("web.fetch", names)
        self.assertIn("web.research", names)


if __name__ == "__main__":
    unittest.main()
