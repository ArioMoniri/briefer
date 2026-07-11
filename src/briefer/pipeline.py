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

        source = _source_label(content)
        images = _collect_images(content)

        # Semantic dedup: the same event/article sent differently (image vs
        # link) should merge, not duplicate. Match by normalized title+date.
        dedup_key = _dedup_key(kind, fresh)
        if not existing:
            existing = self.store.entry_by_key(dedup_key)

        if existing:
            # Cumulatively merge new info into the SAME row instead of skipping.
            merged, changed = _merge_analysis(existing["analysis"], fresh)
            row = self.sheets.find_row_by_id(existing["sheet"], existing["id"])
            if row and changed:
                self.sheets.update_data_row(existing["sheet"], row, merged,
                                            source, submitted_by, images)
            self.store.update_entry_analysis(existing["id"], merged,
                                             str(merged.get("title", "")))
            dl = _parse_deadline(merged.get("application_deadline"))
            ev, all_day = _parse_event_date(merged.get("event_date"))
            return Result(kind=existing["sheet"], analysis=merged, source=source,
                          deadline_dt=dl, event_dt=ev, event_all_day=all_day,
                          sheet_row=row, entry_id=existing["id"],
                          updated=True, changed=changed)

        entry_id = uuid.uuid4().hex[:12]
        if kind == "event":
            row = self.sheets.append_event(fresh, source, submitted_by, images, entry_id)
        else:
            row = self.sheets.append_article(fresh, source, submitted_by, images, entry_id)
        self.store.add_entry(entry_id, chat_id, kind, fp,
                             str(fresh.get("title", "")), fresh, dedup_key)

        deadline_dt = _parse_deadline(fresh.get("application_deadline"))
        event_dt, all_day = _parse_event_date(fresh.get("event_date"))
        return Result(kind=kind, analysis=fresh, source=source,
                      deadline_dt=deadline_dt, event_dt=event_dt,
                      event_all_day=all_day, sheet_row=row, entry_id=entry_id)


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
