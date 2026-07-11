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
            notify = await asyncio.to_thread(self._sync_all)
        except Exception:  # noqa: BLE001
            log.exception("sheet sync failed")
            return
        for n in notify:  # deliver assignment pings on the event loop
            try:
                await self._send_assignment(context, n)
            except Exception:  # noqa: BLE001
                log.exception("assignment notify failed for %s", n.get("entry_id"))

    async def _send_assignment(self, context, n: dict) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.constants import ParseMode
        eid = n["entry_id"]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("👀 Seen", callback_data=f"asgn:seen:{eid}"),
            InlineKeyboardButton("✅ Mark checked", callback_data=f"asgn:done:{eid}"),
        ]])
        link = f'\n<a href="{n["url"]}">Open the row</a>' if n.get("url") else ""
        await context.bot.send_message(
            chat_id=n["chat_id"],
            text=(f"📌 <b>You've been asked to check something</b>\n\n"
                  f"<b>{n['title']}</b>\n<i>{n['sheet_name']}</i>{link}\n\n"
                  f"Tap <b>👀 Seen</b> to acknowledge, and <b>✅ Mark checked</b> "
                  f"when you're done (or tick <i>Assignee Done</i> in the sheet)."),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=kb)
        self.store.mark_assignment_notified(eid)
        log.info("Assignment ping sent to %s for %s", n["chat_id"], eid)

    def _sync_all(self) -> list[dict]:
        notify: list[dict] = []
        for sheet in ("article", "event"):
            try:
                self._sync_sheet(sheet, notify)
                from .stats import rich_stats, stats_rows
                name = "Events" if sheet == "event" else "Articles"
                self.sheets.write_stats(
                    sheet, stats_rows(name, rich_stats(self.store, sheet)))
            except Exception:  # noqa: BLE001
                log.exception("sync failed for %s sheet", sheet)
        return notify

    def _resolve_assignee(self, raw: str) -> dict | None:
        """Turn an Assignee cell ('John' or 'pass it to John') into a person."""
        raw = (raw or "").strip()
        if not raw:
            return None
        p = self.store.person_by_name(raw)
        if p:
            return p
        low = raw.lower()
        words = set(low.replace(",", " ").split())
        for person in self.store.list_people():
            nm = person["name"].strip().lower()
            if nm and (nm in words or nm in low):
                return person
        return None

    def _handle_assignee(self, sheet: str, entry: dict, rnum: int,
                         assignee_raw: str, adone: bool, seen_cur: str,
                         notify: list[dict]) -> None:
        eid = entry["id"]
        cur = self.store.get_assignment(eid)
        assignee_raw = (assignee_raw or "").strip()
        if not assignee_raw:
            if cur:  # cleared → drop the assignment
                self.store.clear_assignment(eid)
                self.sheets.write_assignee_cells(sheet, rnum, seen="")
            return
        # (Re)assign when the name changed.
        if not cur or (cur.get("name") or "").strip().lower() != assignee_raw.lower():
            person = self._resolve_assignee(assignee_raw)
            self.store.set_assignment(
                eid, sheet, assignee_raw, person["chat_id"] if person else None)
            if person:
                notify.append({
                    "entry_id": eid, "chat_id": person["chat_id"],
                    "title": entry.get("title") or "an item",
                    "sheet_name": "Events" if sheet == "event" else "Articles",
                    "url": self.sheets.row_url(sheet, rnum)})
            else:
                # Unknown name → flag it in Seen so you know to map them.
                self.sheets.write_assignee_cells(
                    sheet, rnum, seen=f"⚠️ '{assignee_raw}' not mapped — /people")
            return
        # Same assignee: reflect the sheet's Assignee-Done checkbox.
        if adone and not cur.get("done_at"):
            self.store.mark_assignment_done(eid, True)
            when = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
            self.sheets.write_assignee_cells(sheet, rnum, seen=f"✅ done {when}")
        elif not adone and cur.get("done_at"):
            self.store.mark_assignment_done(eid, False)

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
        # The Remind At cell changed (or is new) → drop the old sheet reminder
        # and schedule the new time, so edits take effect in real time without
        # leaving a stale/duplicate poke behind.
        self.store.cancel_sheet_reminders(entry["id"])
        self.store.add_reminder(
            entry["chat_id"], ts, entry.get("title", "item"),
            {"kind": "custom", "source": "sheet",
             "title": entry.get("title", "item"),
             "note": "Reminder set from the sheet.",
             "when": when.strftime("%Y-%m-%d %H:%M")},
            entry_id=entry["id"])
        self.store.set_entry_sheet_remind(entry["id"], ts)
        log.info("Sheet reminder for %s (re)scheduled at %s",
                 entry["id"], when.isoformat())

    def _sync_sheet(self, sheet: str, notify: list[dict] | None = None) -> None:
        if notify is None:
            notify = []
        ws = self.sheets.worksheet(sheet)
        headers = EVENT_HEADERS if sheet == "event" else ARTICLE_HEADERS
        cols = _control_cols(headers)
        id_i, done_i, rem_i = cols["id"] - 1, cols["done"] - 1, cols["remind_at"] - 1
        stat_i = cols["status"] - 1
        asg_i, adone_i, seen_i = (cols["assignee"] - 1,
                                  cols["assignee_done"] - 1, cols["seen"] - 1)
        try:
            values = ws.get_all_values()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s sheet: %s", sheet, exc)
            return

        present: dict[str, tuple] = {}
        for rnum, row in enumerate(values[1:], start=2):
            if len(row) <= id_i:
                continue
            eid = row[id_i].strip()
            if not eid:
                continue
            def cell(i):
                return row[i].strip() if len(row) > i else ""
            done = cell(done_i).upper() in _TRUE
            adone = cell(adone_i).upper() in _TRUE
            present[eid] = (rnum, done, cell(rem_i), cell(stat_i),
                            cell(asg_i), adone, cell(seen_i))

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
            rnum, done, remind_raw, cur_status, assignee_raw, adone, seen_cur = \
                present[eid]
            # Assignment: name tag → ping that person, track seen/done.
            try:
                self._handle_assignee(sheet, entry, rnum, assignee_raw, adone,
                                      seen_cur, notify)
            except Exception:  # noqa: BLE001
                log.exception("assignee handling failed for %s", eid)
            # Live colored status tag (time urgency), consistent across sheets.
            new_status = self._status_for(entry, done)
            if new_status and new_status != cur_status:
                self.sheets.write_status(sheet, rnum, new_status)
            # Sheet-driven "Remind At": user typed a date → schedule a reminder.
            if remind_raw:
                self._maybe_sheet_reminder(entry, remind_raw)
            elif entry.get("sheet_remind_at"):
                # Cell cleared → cancel the pending sheet reminder in real time.
                self.store.cancel_sheet_reminders(eid)
                self.store.set_entry_sheet_remind(eid, None)
                log.info("Sheet Remind At cleared for %s — reminder cancelled", eid)
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
