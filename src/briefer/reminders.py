"""Parse custom reminder requests.

Three entry points use this:
  • a Telegram reply / message containing "remind me <when>"
  • an inline directive in the submission text
  • a "Remind At" cell the user fills in the sheet

`extract_directive` pulls the "<when>" phrase out of free text; `parse_when`
turns a phrase (or an ISO/date string) into a timezone-aware datetime.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

log = logging.getLogger("briefer.reminders")

# "remind me in 3 days", "remind: 2026-08-01 18:00", "reminder tomorrow 9am",
# "/remind next friday", Turkish "hatırlat yarın".
_DIRECTIVE_RE = re.compile(
    r"(?:^|\b)(?:/?remind(?:\s+me)?|reminder|hat[ıi]rlat)\b[:\s]+(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def extract_directive(text: str) -> tuple[Optional[str], str]:
    """Return (when_phrase, note_without_directive). when_phrase is None if
    there's no reminder directive."""
    if not text:
        return None, text or ""
    m = _DIRECTIVE_RE.search(text)
    if not m:
        return None, text
    when = m.group(1).strip()
    note = text[: m.start()].strip()
    return when, note


# A description to file into the Notes column: a line starting with
# note:/notes:/desc:/description: — everything after it is the note.
_NOTE_RE = re.compile(
    r"(?:^|\n)\s*(?:note|notes|desc|description)\b\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def extract_note(text: str) -> tuple[Optional[str], str]:
    """Return (note, text_without_note). Pulls a `note: …` description out of a
    submission so it goes to the Notes column instead of being analysed."""
    if not text:
        return None, text or ""
    m = _NOTE_RE.search(text)
    if not m:
        return None, text
    return m.group(1).strip(), text[: m.start()].strip()


# "pass this to John", "assign it to John", "give to John", "for John", "→ John".
_ASSIGN_RE = re.compile(
    r"(?:pass|assign|give|send|forward|hand)\b(?:\s+(?:this|it|that))?"
    r"(?:\s+over)?\s+to\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_ASSIGN_RE2 = re.compile(r"(?:^|\b)(?:assign(?:ed)?|for|→|->)\s*[:]?\s*(.+)$",
                         re.IGNORECASE | re.DOTALL)


def extract_assignee_name(text: str) -> Optional[str]:
    """Pull a name out of 'pass this to John' / 'assign to John' / 'for John'.
    Returns the raw name string (not yet resolved to a person)."""
    t = (text or "").strip()
    if not t:
        return None
    m = _ASSIGN_RE.search(t) or _ASSIGN_RE2.search(t)
    if not m:
        return None
    name = m.group(1).strip().strip(".!,;:").strip()
    return name or None


def parse_when(text: str, tz: str) -> Optional[datetime]:
    """Parse a natural-language or ISO time into a tz-aware future datetime."""
    if not text:
        return None
    text = text.strip()
    # Fast path: plain ISO dates/datetimes.
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            from zoneinfo import ZoneInfo
            return datetime.strptime(text[:16], fmt).replace(tzinfo=ZoneInfo(tz))
        except (ValueError, Exception):  # noqa: BLE001
            pass
    try:
        import dateparser
    except Exception:  # noqa: BLE001
        return None
    try:
        dt = dateparser.parse(
            text,
            settings={"PREFER_DATES_FROM": "future",
                      "RETURN_AS_TIMEZONE_AWARE": True,
                      "TIMEZONE": tz, "TO_TIMEZONE": tz},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("dateparser failed on %r: %s", text, exc)
        return None
    return dt
