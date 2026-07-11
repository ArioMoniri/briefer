"""Optional headless-browser fallback (Playwright / Chromium).

Some pages (LinkedIn, JS-rendered SPAs, soft paywalls) return almost no
text to a plain HTTP GET. When that happens and Playwright + a browser are
installed, we render the page and extract the visible text.

Fully optional: if Playwright or the browser binary isn't present, every
entry point returns "" and the caller falls back to whatever it had.
Install on the server with:  ./manage.sh enable-browser
"""
from __future__ import annotations

import importlib.util
import logging

from .security import safe_resolve

log = logging.getLogger("briefer.browser")


def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _load_netscape_cookies(path: str) -> list[dict]:
    """Parse a Netscape cookies.txt into Playwright cookie dicts."""
    cookies: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                raw = line.rstrip("\n")
                if raw.startswith("#HttpOnly_"):
                    raw = raw[len("#HttpOnly_"):]
                elif not raw or raw.startswith("#"):
                    continue
                parts = raw.split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, cpath, secure, expiry, name, value = parts[:7]
                cookie = {"name": name, "value": value, "domain": domain,
                          "path": cpath or "/",
                          "secure": secure.strip().upper() == "TRUE"}
                if expiry.strip().isdigit() and int(expiry) > 0:
                    cookie["expires"] = int(expiry)
                cookies.append(cookie)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not parse cookies file %s: %s", path, exc)
    return cookies


def fetch_rendered(url: str, timeout_ms: int = 20000,
                   max_chars: int = 12000, cookies_file: str = "") -> str:
    """Render `url` in headless Chromium and return 'title\\n\\ntext'.

    Runs the SYNC Playwright API, so it must be called from a worker thread
    (it is — enrichment runs inside asyncio.to_thread), never the event loop.
    Returns "" on any failure or if Playwright/browser is unavailable.
    If cookies_file is given, the page is loaded logged-in (LinkedIn, etc.).
    """
    if not available():
        return ""
    ok, reason, _ = safe_resolve(url)
    if not ok:
        log.warning("browser refused unsafe URL %s: %s", url, reason)
        return ""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return ""

    ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-gpu"],
            )
            try:
                ctx = browser.new_context(user_agent=ua, locale="en-US")
                if cookies_file:
                    cookies = _load_netscape_cookies(cookies_file)
                    if cookies:
                        try:
                            ctx.add_cookies(cookies)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("could not add cookies: %s", exc)
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_timeout(1500)  # let client-side content settle
                except Exception:  # noqa: BLE001
                    pass
                title = page.title() or ""
                text = page.evaluate(
                    "() => document.body ? document.body.innerText : ''") or ""
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("browser render failed for %s: %s", url, exc)
        return ""
    text = " ".join(text.split())
    combined = (f"{title}\n\n{text}").strip()
    return combined[:max_chars]
