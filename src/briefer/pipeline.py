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
                submitted_by: str, force_kind: str | None = None) -> Result:
        content = self.enricher.enrich(text, attachments)
        fp = self._fingerprint(content)
        if self.store.seen(fp):
            log.info("Duplicate submission (fp=%s)", fp[:12])
            return Result(kind="article", analysis={}, source=_source_label(content),
                          duplicate=True)

        kind = force_kind or analysis.classify(self.llm, content)
        log.info("Classified as %s", kind)

        if kind == "event":
            raw = analysis.analyze_event(self.llm, self.cfg, content)
        else:
            raw = analysis.analyze_article(self.llm, self.cfg, content)

        verification = analysis.verify(self.llm, content, raw, kind)
        merged = analysis.apply_corrections(raw, verification)

        source = _source_label(content)
        if kind == "event":
            self.sheets.append_event(merged, source, submitted_by)
        else:
            self.sheets.append_article(merged, source, submitted_by)

        self.store.mark_seen(fp, kind)

        deadline_dt = _parse_deadline(merged.get("application_deadline"))
        return Result(kind=kind, analysis=merged, source=source,
                      deadline_dt=deadline_dt)


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
