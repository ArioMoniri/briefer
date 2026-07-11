"""Polls the two sheets and reconciles them with our stored entries:

- A row whose ID disappeared → the user deleted it → mark removed, cancel its
  reminders, count it (never remind again).
- Done checkbox TRUE (and not already checked) → record the check time, write
  it + the time-to-check into the row, and stop reminding.
- Done unchecked after being checked ("checked out") → treat as never checked
  and keep counting from the original start time.
- Maintains a Stats tab: totals, checked, removed, average time-to-check.

All Sheets I/O is blocking, so tick() offloads to a thread.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from .sheets import ARTICLE_HEADERS, EVENT_HEADERS, _control_cols
from .storage import Store

log = logging.getLogger("briefer.sheetsync")

_TRUE = {"TRUE", "✓", "YES", "1", "✔", "☑"}


class SheetSync:
    def __init__(self, sheets, store: Store, timezone: str = "UTC") -> None:
        self.sheets = sheets
        self.store = store
        self.timezone = timezone

    async def tick(self, context) -> None:  # JobQueue callback
        try:
            await asyncio.to_thread(self._sync_all)
        except Exception:  # noqa: BLE001
            log.exception("sheet sync failed")

    def _sync_all(self) -> None:
        for sheet in ("article", "event"):
            try:
                self._sync_sheet(sheet)
                self.sheets.write_stats(sheet, self.store.entry_stats(sheet))
            except Exception:  # noqa: BLE001
                log.exception("sync failed for %s sheet", sheet)

    def _status_for(self, entry: dict, done: bool) -> str:
        from .pipeline import _parse_deadline, _parse_event_date
        from .tags import status_tag
        a = entry.get("analysis") or {}
        deadline = _parse_deadline(a.get("application_deadline"))
        event_dt, _ = _parse_event_date(a.get("event_date"))
        checked = done or entry.get("checked_at") is not None
        return status_tag(entry.get("sheet", "article"), checked,
                          deadline, event_dt)

    def _maybe_sheet_reminder(self, entry: dict, remind_raw: str) -> None:
        from .reminders import parse_when
        when = parse_when(remind_raw, self.timezone)
        if not when:
            return
        ts = when.timestamp()
        prev = entry.get("sheet_remind_at")
        if prev and abs(ts - float(prev)) < 60:
            return  # already scheduled this exact time
        self.store.add_reminder(
            entry["chat_id"], ts, entry.get("title", "item"),
            {"kind": "custom", "title": entry.get("title", "item"),
             "note": "Reminder set from the sheet.",
             "when": when.strftime("%Y-%m-%d %H:%M")},
            entry_id=entry["id"])
        self.store.set_entry_sheet_remind(entry["id"], ts)
        log.info("Sheet reminder for %s at %s", entry["id"], when.isoformat())

    def _sync_sheet(self, sheet: str) -> None:
        ws = self.sheets.worksheet(sheet)
        headers = EVENT_HEADERS if sheet == "event" else ARTICLE_HEADERS
        cols = _control_cols(headers)
        id_i, done_i, rem_i = cols["id"] - 1, cols["done"] - 1, cols["remind_at"] - 1
        stat_i = cols["status"] - 1
        try:
            values = ws.get_all_values()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s sheet: %s", sheet, exc)
            return

        present: dict[str, tuple[int, bool, str, str]] = {}
        for rnum, row in enumerate(values[1:], start=2):
            if len(row) <= id_i:
                continue
            eid = row[id_i].strip()
            if not eid:
                continue
            done = len(row) > done_i and row[done_i].strip().upper() in _TRUE
            remind_raw = row[rem_i].strip() if len(row) > rem_i else ""
            cur_status = row[stat_i].strip() if len(row) > stat_i else ""
            present[eid] = (rnum, done, remind_raw, cur_status)

        for entry in self.store.active_entries(sheet):
            eid = entry["id"]
            if eid not in present:
                # Row deleted by the user → archive it (recoverable), stop
                # reminders, mark removed.
                try:
                    self.sheets.archive_entry(sheet, entry)
                except Exception:  # noqa: BLE001
                    pass
                self.store.mark_entry_removed(eid)
                self.store.cancel_entry_reminders(eid)
                self.store.incr_meta(f"{sheet}_removed_total", 1)
                log.info("Entry %s deleted from %s sheet — archived + reminders "
                         "cancelled", eid, sheet)
                continue
            rnum, done, remind_raw, cur_status = present[eid]
            # Live colored status tag (time urgency), consistent across sheets.
            new_status = self._status_for(entry, done)
            if new_status and new_status != cur_status:
                self.sheets.write_status(sheet, rnum, new_status)
            # Sheet-driven "Remind At": user typed a date → schedule a reminder.
            if remind_raw:
                self._maybe_sheet_reminder(entry, remind_raw)
            elif entry.get("sheet_remind_at"):
                self.store.set_entry_sheet_remind(eid, None)  # cleared
            if done and entry["checked_at"] is None:
                now = time.time()
                self.store.set_entry_checked(eid, now)
                self.store.cancel_entry_reminders(eid)
                hours = round((now - entry["created_at"]) / 3600, 2)
                iso = datetime.fromtimestamp(now).astimezone().isoformat(
                    timespec="seconds")
                self.sheets.write_check_cells(sheet, rnum, iso, hours)
                log.info("Entry %s checked in %.2fh", eid, hours)
            elif not done and entry["checked_at"] is not None:
                # Checked-out → forget the check, keep counting from the start.
                self.store.set_entry_checked(eid, None)
                self.sheets.write_check_cells(sheet, rnum, "", "")
                log.info("Entry %s un-checked — resetting time-to-check", eid)
