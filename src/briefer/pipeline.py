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

        # Exact re-send: byte-identical content we've already analysed. If the
        # row still exists there is, by definition, nothing new — so DON'T burn
        # tokens re-analysing, and above all don't let repeated re-sends pile
        # near-duplicate bullets into the same cell (each LLM run phrases them
        # slightly differently, which otherwise grows the row every time).
        exact = self.store.entry_by_fingerprint(fp)
        if exact:
            row = self.sheets.find_row_by_id(exact["sheet"], exact["id"])
            if row is not None:
                a = exact["analysis"]
                log.info("Exact re-send of %s — already up to date, skipping "
                         "re-analysis", exact["id"])
                return Result(
                    kind=exact["sheet"], analysis=a, source=_source_label(content),
                    deadline_dt=_parse_deadline(a.get("application_deadline")),
                    event_dt=_parse_event_date(a.get("event_date"))[0],
                    event_all_day=_parse_event_date(a.get("event_date"))[1],
                    sheet_row=row, entry_id=exact["id"],
                    updated=True, changed=False)
            # Row was deleted from the sheet — retire the old entry and re-create
            # a fresh one below (with a brand-new, un-bloated analysis).
            log.info("Entry %s row missing from sheet — re-creating", exact["id"])
            self.store.mark_entry_removed(exact["id"])

        # Re-submission (semantic, not byte-exact): keep the item on its sheet.
        existing = None
        kind = force_kind or analysis.classify(self.llm, content)
        log.info("Classified as %s", kind)

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


# Values that must never overwrite a previously-good scalar (a re-analysis
# that failed to extract a title/date shouldn't wipe the one we already have).
_PLACEHOLDERS = {"", "untitled", "unknown", "n/a", "na", "none", "no title",
                 "no date", "tbd", "-"}


def _merge_analysis(prev: dict[str, Any], new: dict[str, Any]
                    ) -> tuple[dict[str, Any], bool]:
    """Cumulatively merge a fresh analysis into a previous one.

    List fields are unioned with normalized dedup (so slightly re-worded but
    equivalent bullets don't accumulate). Scalar fields adopt the new value
    only when it's meaningful and different — a placeholder like "Untitled"
    never clobbers a good previous value. Returns (merged, changed).
    """
    merged = dict(prev)            # start from what we already had
    changed = False
    for k in _LIST_FIELDS:
        union, added = _union_list(prev.get(k), new.get(k))
        merged[k] = union
        if added:
            changed = True
    for k, v in new.items():
        if k in _LIST_FIELDS or k.startswith("_"):
            continue
        if v in (None, "") or str(v).strip().lower() in _PLACEHOLDERS:
            continue               # don't overwrite good data with a placeholder
        if str(prev.get(k, "")) != str(v):
            merged[k] = v
            changed = True
    for k, v in new.items():       # carry over fresh verification/private flags
        if k.startswith("_"):
            merged[k] = v
    return merged, changed


def _union_list(old: Any, new: Any) -> tuple[list[Any], bool]:
    """Union two lists, de-duplicating on normalized text (case/punctuation-
    insensitive) so re-phrased duplicates collapse. Returns (union, added)."""
    import re
    def norm(x: Any) -> str:  # full-length (unlike _norm, which caps at 80)
        return " ".join(re.sub(r"[^a-z0-9]+", " ", str(x).lower()).split())
    old = [old] if isinstance(old, str) else list(old or [])
    new = [new] if isinstance(new, str) else list(new or [])
    seen: set[str] = set()
    union: list[Any] = []
    added = False
    for item in old:
        key = norm(item)
        if key and key not in seen:
            seen.add(key)
            union.append(item)
    for item in new:
        key = norm(item)
        if key and key not in seen:
            seen.add(key)
            union.append(item)
            added = True
    return union, added


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
