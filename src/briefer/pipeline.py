"""Orchestrates a single ingested message through the full flow:

  enrich → classify → analyse (Agent A or B) → verify → write sheet →
  build a human 'catch' notification (+ schedule deadline reminders).

Runs off the Telegram event loop (in a thread) because the network/LLM
calls are blocking.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from . import analysis
from .config import Config
from .enrich import Enricher, EnrichedContent, Attachment
from .llm import LLM
from .sheets import SheetsClient
from .storage import Store

log = logging.getLogger("briefer.pipeline")


@dataclass
class Result:
    kind: str                     # "article" | "event"
    analysis: dict[str, Any]
    source: str
    duplicate: bool = False
    deadline_dt: Optional[datetime] = None
    event_dt: Optional[datetime] = None
    event_all_day: bool = False
    sheet_row: Optional[int] = None
    entry_id: Optional[str] = None
    updated: bool = False          # re-submission that updated an existing row
    changed: bool = False          # …and it actually added new info


class Pipeline:
    def __init__(self, cfg: Config, llm: LLM, enricher: Enricher,
                 sheets: SheetsClient, store: Store) -> None:
        self.cfg = cfg
        self.llm = llm
        self.enricher = enricher
        self.sheets = sheets
        self.store = store

    def _fingerprint(self, content: EnrichedContent) -> str:
        basis = (content.raw_text or "") + "|" + "|".join(sorted(content.urls))
        for a in content.attachments:
            basis += "|" + (a.filename or a.media_type) + str(len(a.text or a.data_b64))
        return hashlib.sha256(basis.encode()).hexdigest()

    def process(self, text: str, attachments: list[Attachment],
                submitted_by: str, force_kind: str | None = None,
                chat_id: int = 0) -> Result:
        content = self.enricher.enrich(text, attachments)
        fp = self._fingerprint(content)
        existing = self.store.entry_by_fingerprint(fp)

        # Re-submission: keep the item on its original sheet.
        kind = force_kind or (existing["sheet"] if existing
                              else analysis.classify(self.llm, content))
        log.info("Classified as %s%s", kind, " (re-submission)" if existing else "")

        if kind == "event":
            raw = analysis.analyze_event(self.llm, self.cfg, content)
        else:
            raw = analysis.analyze_article(self.llm, self.cfg, content)
        verification = analysis.verify(self.llm, content, raw, kind)
        fresh = analysis.apply_corrections(raw, verification)

        # If the model didn't find an application URL but the page had an
        # apply/register button, use it (fixes the 'Hemen Başvur' case).
        if kind == "event" and not fresh.get("application_url") and content.apply_links:
            fresh["application_url"] = content.apply_links[0]

        # General web-search enrichment: find MORE details, verified to be the
        # same up-to-date item (not a similarly-named one).
        if self.cfg.enable_web_search and not existing:
            need_apply = kind == "event" and not fresh.get("application_url")
            if (not self.cfg.web_search_only_if_apply_missing) or need_apply:
                try:
                    fresh = _augment_with_search(self.cfg, self.llm,
                                                 self.enricher, fresh, kind)
                except Exception as exc:  # noqa: BLE001
                    log.warning("web-search enrichment failed: %s", exc)

        source = _source_label(content)
        images = _collect_images(content)

        # Semantic dedup: the same event/article sent differently (image vs
        # link) should merge, not duplicate. Match by normalized title+date.
        dedup_key = _dedup_key(kind, fresh)
        if not existing:
            existing = self.store.entry_by_key(dedup_key)

        if existing:
            row = self.sheets.find_row_by_id(existing["sheet"], existing["id"])
            if row is None:
                # The row was deleted from the sheet — don't silently update a
                # ghost. Keep the previously-analysed (rich) data by merging it
                # in, retire the old entry, and fall through to a fresh row.
                log.info("Entry %s row missing from sheet — creating a new one",
                         existing["id"])
                fresh, _ = _merge_analysis(existing["analysis"], fresh)
                dedup_key = _dedup_key(kind, fresh)
                self.store.mark_entry_removed(existing["id"])
                existing = None
            else:
                # Cumulatively merge new info into the SAME row.
                merged, changed = _merge_analysis(existing["analysis"], fresh)
                if changed:
                    self.sheets.update_data_row(existing["sheet"], row, merged,
                                                source, submitted_by, images)
                self.store.update_entry_analysis(existing["id"], merged,
                                                 str(merged.get("title", "")))
                dl = _parse_deadline(merged.get("application_deadline"))
                ev, all_day = _parse_event_date(merged.get("event_date"))
                return Result(kind=existing["sheet"], analysis=merged,
                              source=source, deadline_dt=dl, event_dt=ev,
                              event_all_day=all_day, sheet_row=row,
                              entry_id=existing["id"], updated=True,
                              changed=changed)

        entry_id = uuid.uuid4().hex[:12]
        deadline_dt = _parse_deadline(fresh.get("application_deadline"))
        event_dt, all_day = _parse_event_date(fresh.get("event_date"))
        from .tags import status_tag
        status = status_tag(kind, False, deadline_dt, event_dt)
        if kind == "event":
            row = self.sheets.append_event(fresh, source, submitted_by, images,
                                           entry_id, status)
        else:
            row = self.sheets.append_article(fresh, source, submitted_by, images,
                                             entry_id, status)
        self.store.add_entry(entry_id, chat_id, kind, fp,
                             str(fresh.get("title", "")), fresh, dedup_key)
        return Result(kind=kind, analysis=fresh, source=source,
                      deadline_dt=deadline_dt, event_dt=event_dt,
                      event_all_day=all_day, sheet_row=row, entry_id=entry_id)


def _augment_with_search(cfg, llm, enricher, fresh: dict[str, Any],
                         kind: str) -> dict[str, Any]:
    from . import web_search
    from .link_safety import assess_link

    query = analysis.web_search_query(kind, fresh)
    if not query:
        return fresh
    results = web_search.search(query, cfg.web_search_max_results,
                                cfg.web_search_provider, cfg.web_search_api_key)
    candidates: list[dict[str, Any]] = []
    for r in results:
        url = r.get("url", "")
        if not url:
            continue
        verdict = assess_link(
            url, r.get("snippet", ""), llm=llm,
            guard_model=cfg.link_guard_model,
            safe_browsing_key=cfg.google_safe_browsing_key,
            enable_guard=cfg.enable_link_guard)
        if not verdict.safe:
            continue
        candidates.append({"url": url, "title": r.get("title", ""),
                           "snippet": r.get("snippet", ""),
                           "text": enricher.fetch_text(url)})
        if len(candidates) >= 4:
            break
    if not candidates:
        return fresh
    verify = analysis.web_verify(llm, fresh, candidates, kind)
    matched = [m.get("url") for m in (verify.get("matched_sources") or [])
               if m.get("url")]
    if not matched:
        log.info("web search: no candidate matched the exact item")
        return fresh
    return _apply_web_details(fresh, verify, matched)


def _apply_web_details(fresh: dict[str, Any], verify: dict[str, Any],
                       matched: list[str]) -> dict[str, Any]:
    add = verify.get("additional") or {}
    for k in ("catch_points", "required_materials", "eligibility", "links"):
        old = fresh.get(k) or []
        old = [old] if isinstance(old, str) else list(old)
        new = add.get(k) or []
        new = [new] if isinstance(new, str) else list(new)
        seen, union = set(), []
        for item in old + new:
            key = str(item).strip().lower()
            if key and key not in seen:
                seen.add(key)
                union.append(item)
        if union:
            fresh[k] = union
    for k in ("application_url", "application_deadline", "event_date"):
        if not fresh.get(k) and add.get(k):
            fresh[k] = add[k]
    if add.get("summary_addendum"):
        fresh["summary"] = (str(fresh.get("summary", "")) + " "
                            + str(add["summary_addendum"])).strip()
    fresh["links"] = list(dict.fromkeys((fresh.get("links") or []) + matched))
    note = verify.get("up_to_date_note")
    fresh["_web_sources"] = matched
    if note:
        fresh["_web_note"] = note
    return fresh


def _norm(s: Any) -> str:
    import re
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)  # drop punctuation/accents-ish
    return " ".join(s.split())[:80]


def _dedup_key(kind: str, analysis: dict[str, Any]) -> str:
    """A stable semantic key so the same item merges across submissions.

    Events: normalized title + the event/deadline date (year-month-day).
    Articles: normalized title (+ first author if present).
    """
    title = _norm(analysis.get("title"))
    if not title:
        return ""
    if kind == "event":
        date = str(analysis.get("event_date") or analysis.get("application_deadline")
                   or "")[:10]
        return f"event|{title}|{date}"
    entities = analysis.get("entities") or []
    author = _norm(entities[0]) if entities else ""
    return f"article|{title}|{author}"


_LIST_FIELDS = {
    "catch_points", "vivax_use_cases", "entities", "tags", "links",
    "eligibility", "required_materials", "application_steps",
}


def _merge_analysis(prev: dict[str, Any], new: dict[str, Any]
                    ) -> tuple[dict[str, Any], bool]:
    """Cumulatively merge a fresh analysis into a previous one.

    List fields are unioned (nothing is lost); scalar fields take the newest
    non-empty value. Returns (merged, changed) where `changed` is True if any
    new information was actually added.
    """
    merged = dict(new)
    changed = False
    for k in _LIST_FIELDS:
        old = prev.get(k) or []
        neu = new.get(k) or []
        old = [old] if isinstance(old, str) else list(old)
        neu = [neu] if isinstance(neu, str) else list(neu)
        seen: set[str] = set()
        union: list[Any] = []
        for item in old + neu:
            key = str(item).strip().lower()
            if key and key not in seen:
                seen.add(key)
                union.append(item)
        if len(union) > len(old):
            changed = True
        merged[k] = union
    for k, v in new.items():
        if k in _LIST_FIELDS or k.startswith("_"):
            continue
        if v not in (None, "") and str(prev.get(k, "")) != str(v):
            changed = True
    return merged, changed


def _collect_images(content: EnrichedContent, limit: int = 3
                    ) -> list[tuple[bytes, str]]:
    """Pull decoded image bytes from image attachments (photos, screenshots,
    tweet images, video keyframes) so they can be saved into the sheet."""
    import base64
    out: list[tuple[bytes, str]] = []
    for att in content.attachments:
        if att.kind == "image" and att.data_b64:
            try:
                out.append((base64.b64decode(att.data_b64),
                            att.media_type or "image/jpeg"))
            except Exception:  # noqa: BLE001
                continue
        if len(out) >= limit:
            break
    return out


def _source_label(content: EnrichedContent) -> str:
    if content.urls:
        return content.urls[0]
    if content.attachments:
        return content.attachments[0].filename or content.attachments[0].kind
    snippet = (content.raw_text or "").strip().split("\n")[0]
    return (snippet[:80] + "…") if len(snippet) > 80 else (snippet or "text message")


def _parse_deadline(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M"),
    ):
        try:
            return parser(v)
        except (ValueError, TypeError):
            continue
    return None


def _parse_event_date(value: Any) -> tuple[Optional[datetime], bool]:
    """Parse the event date, returning (datetime, is_all_day).

    Handles single dates, datetimes, and ranges (takes the start). all_day is
    True when only a date (no time) is present.
    """
    if not value or not isinstance(value, str):
        return None, False
    # Take the start of a range like "2026-08-01 to 2026-08-03" / "..—..".
    first = value
    for sep in (" to ", " – ", " — ", " - ", "–", "—", "/"):
        if sep in value:
            first = value.split(sep, 1)[0]
            break
    first = first.strip().replace("Z", "+00:00")
    # Datetime first (has a time component).
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M"),
    ):
        try:
            dt = parser(first)
            if dt.hour or dt.minute or ("T" in first or ":" in first):
                return dt, False
            return dt, True
        except (ValueError, TypeError):
            continue
    # Date only → all-day.
    try:
        return datetime.strptime(first, "%Y-%m-%d"), True
    except (ValueError, TypeError):
        return None, False
