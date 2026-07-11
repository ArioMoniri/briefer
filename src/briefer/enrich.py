"""Turn a raw Telegram message into normalised, analysable text.

Handles: plain text, URLs (SSRF-safe fetch + readable-text extraction),
GitHub repos (README + metadata), PDFs, and images (kept as base64 for
the vision model). All network egress goes through the SSRF guard.
"""
from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .security import is_safe_url, clamp

log = logging.getLogger("briefer.enrich")

URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_GITHUB_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s#?]+)", re.IGNORECASE)
_LUMA_RE = re.compile(r"https?://(?:lu\.ma|luma\.com)/[^\s]+", re.IGNORECASE)

_HEADERS = {"User-Agent": "BrieferBot/1.0 (+content-summariser)"}


@dataclass
class Attachment:
    kind: str            # "image" | "pdf" | "file"
    media_type: str
    data_b64: str = ""   # for images (vision)
    text: str = ""       # extracted text (pdf/file)
    filename: str = ""


@dataclass
class EnrichedContent:
    raw_text: str = ""
    urls: list[str] = field(default_factory=list)
    link_texts: dict[str, str] = field(default_factory=dict)  # url -> extracted text
    github_repos: list[dict[str, Any]] = field(default_factory=list)
    luma_urls: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_source_block(self, per_source_limit: int = 6000) -> str:
        """Flatten everything into a single text block for the model."""
        parts: list[str] = []
        if self.raw_text:
            parts.append("=== MESSAGE TEXT ===\n" + self.raw_text)
        for url, txt in self.link_texts.items():
            parts.append(f"=== LINK: {url} ===\n" + clamp(txt, per_source_limit))
        for repo in self.github_repos:
            parts.append(
                f"=== GITHUB REPO: {repo.get('full_name')} ===\n"
                f"Description: {repo.get('description', '')}\n"
                f"Stars: {repo.get('stars', '?')}  Language: {repo.get('language', '?')}\n"
                f"README (excerpt):\n" + clamp(repo.get("readme", ""), per_source_limit)
            )
        for att in self.attachments:
            if att.text:
                parts.append(
                    f"=== FILE: {att.filename} ({att.media_type}) ===\n"
                    + clamp(att.text, per_source_limit)
                )
            elif att.kind == "image":
                parts.append(f"=== IMAGE: {att.filename or 'photo'} (analysed via vision) ===")
        for note in self.notes:
            parts.append(f"[note] {note}")
        return "\n\n".join(parts) if parts else "(no extractable content)"


class Enricher:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes

    def _fetch(self, url: str) -> httpx.Response | None:
        ok, reason = is_safe_url(url)
        if not ok:
            log.warning("Refusing to fetch %s: %s", url, reason)
            return None
        try:
            with httpx.Client(timeout=15, follow_redirects=True,
                              headers=_HEADERS, max_redirects=4) as client:
                resp = client.get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Fetch failed for %s: %s", url, exc)
            return None
        # Re-validate the final URL after redirects (defends against
        # redirect-to-internal SSRF).
        final = str(resp.url)
        ok, reason = is_safe_url(final)
        if not ok:
            log.warning("Refusing redirected URL %s: %s", final, reason)
            return None
        if len(resp.content) > self.max_bytes:
            log.warning("Body too large for %s (%d bytes)", url, len(resp.content))
            return None
        return resp

    def _extract_html_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "svg"]):
            tag.decompose()
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        text = " ".join(soup.get_text(" ").split())
        return (f"{title}\n\n{text}").strip()

    def _github(self, owner: str, repo: str) -> dict[str, Any] | None:
        repo = repo.removesuffix(".git")
        api = f"https://api.github.com/repos/{owner}/{repo}"
        meta_resp = self._fetch(api)
        if not meta_resp or meta_resp.status_code != 200:
            return None
        meta = meta_resp.json()
        readme_resp = self._fetch(f"{api}/readme")
        readme = ""
        if readme_resp and readme_resp.status_code == 200:
            content = readme_resp.json().get("content", "")
            try:
                readme = base64.b64decode(content).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                readme = ""
        return {
            "full_name": meta.get("full_name"),
            "description": meta.get("description") or "",
            "stars": meta.get("stargazers_count"),
            "language": meta.get("language"),
            "topics": meta.get("topics", []),
            "html_url": meta.get("html_url"),
            "readme": readme,
        }

    def enrich(self, text: str, attachments: list[Attachment]) -> EnrichedContent:
        content = EnrichedContent(raw_text=text or "", attachments=attachments)
        urls = list(dict.fromkeys(URL_RE.findall(text or "")))
        content.urls = urls

        for url in urls:
            if _LUMA_RE.match(url):
                content.luma_urls.append(url)
            gh = _GITHUB_RE.match(url)
            if gh:
                repo = self._github(gh.group(1), gh.group(2))
                if repo:
                    content.github_repos.append(repo)
                    continue  # repo readme is richer than the html page
            resp = self._fetch(url)
            if not resp:
                content.notes.append(f"Could not fetch {url} (blocked or unreachable).")
                continue
            ctype = resp.headers.get("content-type", "")
            if "application/pdf" in ctype:
                content.link_texts[url] = _pdf_to_text(resp.content)
            elif "text/html" in ctype or "text/plain" in ctype or not ctype:
                content.link_texts[url] = self._extract_html_text(resp.text)
            else:
                content.notes.append(f"{url}: unsupported content-type {ctype}.")
        return content


def _pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:40]:
            pages.append(page.extract_text() or "")
        return "\n".join(pages)
    except Exception as exc:  # noqa: BLE001
        log.warning("PDF parse failed: %s", exc)
        return "(could not extract PDF text)"


def make_image_attachment(data: bytes, media_type: str, filename: str = "") -> Attachment:
    return Attachment(
        kind="image",
        media_type=media_type,
        data_b64=base64.b64encode(data).decode(),
        filename=filename,
    )


def make_pdf_attachment(data: bytes, filename: str) -> Attachment:
    return Attachment(
        kind="pdf",
        media_type="application/pdf",
        text=_pdf_to_text(data),
        filename=filename,
    )


def make_text_attachment(data: bytes, filename: str, media_type: str) -> Attachment:
    return Attachment(
        kind="file",
        media_type=media_type,
        text=data.decode("utf-8", "replace"),
        filename=filename,
    )
