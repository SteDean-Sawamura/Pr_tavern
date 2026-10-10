"""Multi-source search engine."""

import logging
import asyncio
from search.web_scraper import WebScraper
from search.ai_knowledge import AIKnowledge

logger = logging.getLogger("tavern.search")


class SearchEngine:
    def __init__(self, ai_provider=None):
        self.scraper = WebScraper()
        self.ai_knowledge = AIKnowledge(ai_provider) if ai_provider else None

    async def search(
        self,
        query: str,
        sources: list[str] | None = None,
        scrape_url: str | None = None,
        scrape_urls: list[str] | None = None,
        max_results: int = 5,
    ) -> list[dict]:
        """Run multi-source search and return combined results."""
        if sources is None:
            sources = ["web", "ai"]

        tasks = []

        if "web" in sources:
            tasks.append(self._web_search(query, max_results=max_results))

        if "ai" in sources and self.ai_knowledge:
            tasks.append(self._ai_search(query))

        # Support both single URL and multi-URL
        all_urls = list(scrape_urls or [])
        if scrape_url and scrape_url not in all_urls:
            all_urls.append(scrape_url)

        if "scrape" in sources and all_urls:
            for url in all_urls:
                url = url.strip()
                if url:
                    tasks.append(self._scrape_search(url))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        combined = []
        for result in results:
            if isinstance(result, list):
                combined.extend(result)
            elif isinstance(result, Exception):
                continue

        return combined

    async def _web_search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search using Bing China (cn.bing.com) — HTML scraping, no API key needed."""
        import re
        from urllib.parse import quote_plus, urlparse, parse_qs, unquote
        import base64
        import httpx
        from bs4 import BeautifulSoup

        def _unwrap_bing_url(href: str) -> str:
            """Bing wraps result URLs as bing.com/ck/a?...&u=a1<base64>. Decode to real URL."""
            try:
                parsed = urlparse(href)
                if "bing.com" in (parsed.netloc or "") and parsed.path.startswith("/ck/"):
                    qs = parse_qs(parsed.query)
                    u = qs.get("u", [""])[0]
                    if u.startswith("a1"):
                        b64 = u[2:]
                        # Bing uses URL-safe base64 without padding
                        b64 += "=" * (-len(b64) % 4)
                        try:
                            return base64.urlsafe_b64decode(b64).decode("utf-8", errors="ignore")
                        except Exception:
                            return href
                return href
            except Exception:
                return href

        try:
            url = f"https://cn.bing.com/search?q={quote_plus(query)}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=headers) as client:
                resp = await client.get(url)
                resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            results = []
            for item in soup.select("li.b_algo"):
                if len(results) >= max_results:
                    break
                a = item.select_one("h2 a")
                if not a:
                    continue
                title = a.get_text(strip=True)
                href = _unwrap_bing_url(a.get("href", ""))
                if not href.startswith(("http://", "https://")):
                    continue
                snippet_el = item.select_one(".b_caption p") or item.select_one("p")
                snippet = snippet_el.get_text(separator=" ", strip=True) if snippet_el else ""
                # Strip leading "<日期> · " prefix common in Bing snippets
                snippet = re.sub(r"^[\d年月日\-/\.\s]+·\s*", "", snippet)
                results.append({
                    "title": title,
                    "content": snippet,
                    "url": href,
                    "source_type": "web_search",
                    "relevance_score": 0.8,
                })
            return results
        except Exception as e:
            logger.warning("Bing搜索失败: %s", e)
            return []

    async def _ai_search(self, query: str) -> list[dict]:
        """Generate knowledge using AI."""
        if not self.ai_knowledge:
            return []
        result = await self.ai_knowledge.generate(query)
        if result.get("success"):
            return [{
                "title": result["title"],
                "content": result["content"],
                "url": None,
                "source_type": "ai_generated",
                "relevance_score": 0.7,
            }]
        return []

    async def _scrape_search(self, url: str) -> list[dict]:
        """Scrape a specific URL."""
        result = await self.scraper.scrape(url)
        if result.get("success"):
            if not result.get("content", "").strip():
                logger.warning("抓取成功但内容为空 — url=%s", url)
                return [{
                    "title": f"抓取失败: {url}",
                    "content": "页面抓取成功但未提取到正文内容（可能是动态渲染页面）。",
                    "url": url,
                    "source_type": "web_scrape",
                    "relevance_score": 0,
                }]
            return [{
                "title": result["title"],
                "content": result["content"],
                "url": result["url"],
                "source_type": "web_scrape",
                "relevance_score": 0.9,
            }]
        error = result.get("error", "未知错误")
        logger.warning("网页抓取失败 — url=%s, error=%s", url, error)
        return [{
            "title": f"抓取失败: {url}",
            "content": f"无法抓取此网页: {error}",
            "url": url,
            "source_type": "web_scrape",
            "relevance_score": 0,
        }]
