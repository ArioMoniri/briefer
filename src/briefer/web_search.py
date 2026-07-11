"""General web search to find extra, up-to-date details about an item.

Default provider is DuckDuckGo's HTML endpoint (no API key). Optionally use
Brave or SerpAPI with a key. Results are (title, url, snippet); the caller
safety-gates and verifies them before use.
"""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

log = logging.getLogger("briefer.search")

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def search(query: str, max_results: int = 5, provider: str = "ddg",
           api_key: str = "") -> list[dict]:
    if not query.strip():
        return []
    try:
        if provider == "brave" and api_key:
            return _brave(query, max_results, api_key)
        if provider == "serpapi" and api_key:
            return _serpapi(query, max_results, api_key)
        return _ddg(query, max_results)
    except Exception as exc:  # noqa: BLE001
        log.warning("web search failed (%s): %s", provider, exc)
        return []


def _ddg(query: str, n: int) -> list[dict]:
    with httpx.Client(timeout=15, headers={"User-Agent": _UA},
                      follow_redirects=True) as c:
        r = c.get("https://html.duckduckgo.com/html/", params={"q": query})
    if r.status_code != 200:
        return []
    out: list[dict] = []
    # Result anchors: <a class="result__a" href="/l/?uddg=<encoded>">title</a>
    for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            r.text, re.DOTALL):
        href, title = m.group(1), _strip(m.group(2))
        url = _ddg_target(href)
        if url:
            out.append({"title": title, "url": url, "snippet": ""})
        if len(out) >= n:
            break
    # Attach snippets in order if present.
    snippets = [_strip(s) for s in re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL)]
    for i, s in enumerate(snippets[:len(out)]):
        out[i]["snippet"] = s
    return out


def _ddg_target(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com/l/" in href or parsed.path.endswith("/l/"):
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    if href.startswith("http"):
        return href
    return ""


def _brave(query: str, n: int, key: str) -> list[dict]:
    with httpx.Client(timeout=15, headers={
            "X-Subscription-Token": key, "Accept": "application/json"}) as c:
        r = c.get("https://api.search.brave.com/res/v1/web/search",
                  params={"q": query, "count": n})
    r.raise_for_status()
    results = (r.json().get("web") or {}).get("results") or []
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "snippet": x.get("description", "")} for x in results[:n]]


def _serpapi(query: str, n: int, key: str) -> list[dict]:
    with httpx.Client(timeout=15) as c:
        r = c.get("https://serpapi.com/search",
                  params={"q": query, "num": n, "api_key": key, "engine": "google"})
    r.raise_for_status()
    results = r.json().get("organic_results") or []
    return [{"title": x.get("title", ""), "url": x.get("link", ""),
             "snippet": x.get("snippet", "")} for x in results[:n]]


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
