"""Web scraper: extract content from web pages."""

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


def _is_safe_url(url: str) -> tuple[bool, str]:
    """S1: 防 SSRF — 只允许 http/https，且禁止内网/回环/链路本地/元数据地址。"""
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"无效的 URL: {e}"
    if parsed.scheme not in ("http", "https"):
        return False, f"不允许的协议: {parsed.scheme}"
    host = parsed.hostname
    if not host:
        return False, "URL 缺少主机名"
    # 解析所有 IP，任何一个落在私有/回环网段都拒绝
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"DNS 解析失败: {e}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False, f"禁止访问内网/特殊地址: {ip_str}"
        # 拒绝云元数据服务地址
        if str(ip) in ("169.254.169.254", "100.100.100.200", "fd00:ec2::254"):
            return False, "禁止访问元数据服务"
    return True, ""


class WebScraper:
    @staticmethod
    async def _fetch(url: str) -> str:
        """Fetch URL HTML. Uses curl (bypasses TLS fingerprinting) with httpx fallback."""
        import asyncio, shutil, subprocess
        curl = shutil.which("curl")
        if curl:
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    [
                        curl, "-sL", "--max-time", "15", "--max-redirs", "5",
                        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
                        url,
                    ],
                    capture_output=True,
                    timeout=20,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="ignore")
            except Exception:
                pass
        # Fallback to httpx
        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=_headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text

    @staticmethod
    async def scrape(url: str) -> dict:
        """Scrape a URL and extract main content."""
        # S1: 校验 URL 安全性
        ok, reason = _is_safe_url(url)
        if not ok:
            return {
                "title": "", "content": "", "url": url,
                "success": False, "error": f"拒绝抓取: {reason}",
            }
        try:
            html = await WebScraper._fetch(url)
            if not html:
                return {
                    "title": "", "content": "", "url": url,
                    "success": False, "error": "请求返回空内容",
                }

            soup = BeautifulSoup(html, "html.parser")

            # Remove obvious non-content elements globally
            for tag in soup(["script", "style", "nav", "footer", "header",
                             "aside", "iframe", "noscript", "form", "svg",
                             "button", "input", "select", "textarea"]):
                tag.decompose()

            import re as _re

            # Find main content area first (site-specific + generic)
            main = (
                # Baidu Baike
                soup.find("div", class_="main-content")
                or soup.find("div", attrs={"class": _re.compile(r"lemma[-_]?content|J-lemma", _re.I)})
                # Wikipedia
                or soup.find("div", id="mw-content-text")
                or soup.find("div", id="bodyContent")
                # Common patterns
                or soup.find("article")
                or soup.find("main")
                or soup.find("div", attrs={"class": _re.compile(r"(article|post|entry|content)[-_]?(body|text|main)?", _re.I)})
                or soup.find("div", id=_re.compile(r"(article|post|entry|content)", _re.I))
                or soup.find("body")
            )

            # Within main, strip noisy sub-elements (sidebars, share bars, ads, related)
            if main:
                _noise_pattern = _re.compile(
                    r'(side[-_]?bar|menu|breadcrumb|comment|share|social'
                    r'|related|recommend|advertisement|ad[-_]banner|banner'
                    r'|popup|modal|cookie|login|signup|toolbar|toc[-_]'
                    r'|catalog|footer|copyright)',
                    _re.IGNORECASE,
                )
                for tag in main.find_all(True):
                    classes = " ".join(tag.get("class") or [])
                    tag_id = tag.get("id", "")
                    if _noise_pattern.search(classes) or _noise_pattern.search(tag_id):
                        tag.decompose()

            title = ""
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)

            raw = main.get_text(separator="\n", strip=True) if main else ""
            # Filter out short navigation-like lines (most nav links are ≤ 4 chars)
            lines = [ln.strip() for ln in raw.split("\n") if len(ln.strip()) > 4]
            content = "\n".join(lines)
            # Truncate very long content
            if len(content) > 20000:
                content = content[:20000] + "..."

            return {
                "title": title,
                "content": content,
                "url": url,
                "success": True,
            }
        except Exception as e:
            return {
                "title": "",
                "content": "",
                "url": url,
                "success": False,
                "error": str(e),
            }
