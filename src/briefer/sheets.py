"""Google Sheets output: two cumulative spreadsheets (Articles, Events).

Rows are only ever appended, so both sheets are a growing ledger. Headers
are created on first use. If sheet IDs are not configured, the sheets are
created and their IDs are logged for the operator to paste into .env.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger("briefer.sheets")

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

ARTICLE_HEADERS = [
    "Captured At", "Title", "Type", "Summary", "Catch Points",
    "Vivax Relevance", "Vivax Use Cases", "Entities", "Tags", "Links",
    "Source", "Verified", "Verification Notes", "Confidence", "Submitted By",
]

EVENT_HEADERS = [
    "Captured At", "Title", "Event Type", "Summary", "Organizer", "Location",
    "Event Date", "Application Deadline", "Deadline (raw)", "Eligibility",
    "Required Materials", "Application Steps", "Application URL", "Cost",
    "Catch Points", "Vivax Relevance", "Should Apply", "Verified",
    "Deadline Confidence", "Verification Notes", "Source", "Submitted By",
]


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(f"• {str(v)}" for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class SheetsClient:
    def __init__(self, service_account_file: str, articles_id: str,
                 events_id: str) -> None:
        creds = Credentials.from_service_account_file(
            service_account_file, scopes=_SCOPES
        )
        self._gc = gspread.authorize(creds)
        self._articles = self._open_or_create(articles_id, "Briefer — Articles",
                                              ARTICLE_HEADERS)
        self._events = self._open_or_create(events_id, "Briefer — Events",
                                            EVENT_HEADERS)

    @property
    def articles_id(self) -> str:
        return self._articles.spreadsheet.id

    @property
    def events_id(self) -> str:
        return self._events.spreadsheet.id

    def _open_or_create(self, sheet_id: str, title: str,
                        headers: list[str]) -> gspread.Worksheet:
        if sheet_id:
            ss = self._gc.open_by_key(sheet_id)
        else:
            ss = self._gc.create(title)
            log.warning(
                "Created new spreadsheet '%s' with id=%s — add this id to .env "
                "and share it with the service account.", title, ss.id
            )
        ws = ss.sheet1
        existing = ws.row_values(1)
        if existing != headers:
            if not existing:
                ws.update([headers], "A1")
            elif not any(existing):
                ws.update([headers], "A1")
            # If headers exist but differ, leave them; append still works by
            # position. We log so the operator can reconcile.
            if existing and existing != headers:
                log.info("Sheet '%s' already has headers; appending by column order.",
                         title)
        ws.freeze(rows=1)
        return ws

    def append_article(self, a: dict[str, Any], source: str, user: str) -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        issues = a.get("_verification_issues") or []
        row = [
            now,
            _fmt(a.get("title")),
            "Article",
            _fmt(a.get("summary")),
            _fmt(a.get("catch_points")),
            _fmt(a.get("vivax_relevance")),
            _fmt(a.get("vivax_use_cases")),
            _fmt(a.get("entities")),
            _fmt(a.get("tags")),
            _fmt(a.get("links")),
            _fmt(source),
            "✅" if a.get("_verified") else "⚠️",
            _fmt([f"{i.get('field')}: {i.get('problem')}" for i in issues]),
            _fmt(a.get("confidence")),
            _fmt(user),
        ]
        self._articles.append_row(row, value_input_option="USER_ENTERED")

    def append_event(self, e: dict[str, Any], source: str, user: str) -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        issues = e.get("_verification_issues") or []
        row = [
            now,
            _fmt(e.get("title")),
            _fmt(e.get("event_type")),
            _fmt(e.get("summary")),
            _fmt(e.get("organizer")),
            _fmt(e.get("location")),
            _fmt(e.get("event_date")),
            _fmt(e.get("application_deadline")),
            _fmt(e.get("deadline_raw")),
            _fmt(e.get("eligibility")),
            _fmt(e.get("required_materials")),
            _fmt(e.get("application_steps")),
            _fmt(e.get("application_url")),
            _fmt(e.get("cost")),
            _fmt(e.get("catch_points")),
            _fmt(e.get("vivax_relevance")),
            _fmt(e.get("should_apply")),
            "✅" if e.get("_verified") else "⚠️",
            _fmt(e.get("_deadline_confidence")),
            _fmt([f"{i.get('field')}: {i.get('problem')}" for i in issues]),
            _fmt(source),
            _fmt(user),
        ]
        self._events.append_row(row, value_input_option="USER_ENTERED")
