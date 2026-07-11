"""SQLite-backed state: authenticated sessions, dedup of processed items,
and scheduled deadline reminders (so they survive restarts).

Uses only parameterised queries — no string interpolation into SQL.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    chat_id     INTEGER PRIMARY KEY,
    authed_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed (
    fingerprint TEXT PRIMARY KEY,
    kind        TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    fire_at     REAL NOT NULL,
    title       TEXT,
    payload     TEXT,
    fired       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_fire ON reminders (fire_at, fired);
CREATE TABLE IF NOT EXISTS allowed_chats (
    chat_id     INTEGER PRIMARY KEY,
    added_by    INTEGER,
    added_at    REAL NOT NULL,
    note        TEXT
);
-- Durable work queue: submissions are enqueued here and processed one by
-- one by a single worker, so nothing is lost and nothing overcrowds. Rows
-- survive restarts, so the bot resumes exactly where it left off.
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    submitter       TEXT,
    text            TEXT,
    attachments     TEXT,            -- JSON list of file_id descriptors
    force_kind      TEXT,
    note_message_id INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|done|failed
    error           TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, id);
-- Simple key/value checkpoints (last sheet row written, last processed time…).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- One row per sheet entry, keyed by a stable UUID also written to the sheet's
-- ID column. Lets us survive row deletion/reordering, update cumulatively, and
-- track the checkbox / time-to-check independent of the moving row number.
CREATE TABLE IF NOT EXISTS entries (
    id           TEXT PRIMARY KEY,   -- uuid, mirrored in the sheet ID column
    chat_id      INTEGER,
    sheet        TEXT,               -- 'article' | 'event'
    fingerprint  TEXT,
    title        TEXT,
    analysis     TEXT,               -- merged analysis JSON (for cumulative merge)
    created_at   REAL NOT NULL,
    checked_at   REAL,               -- when Done first seen TRUE; NULL otherwise
    removed      INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_entries_fp ON entries (fingerprint);
CREATE INDEX IF NOT EXISTS idx_entries_sheet ON entries (sheet, removed);
-- Directory mapping a person's name to their Telegram chat id, so a name tag
-- typed in a row's Assignee column can be turned into a direct message.
CREATE TABLE IF NOT EXISTS people (
    chat_id    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- Maps a bot result message to the entry it reported, so replying to that
-- message ("pass this to John", "remind me tomorrow", "note: …") acts on that
-- row instead of being analysed as new content.
CREATE TABLE IF NOT EXISTS row_messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    entry_id   TEXT,
    sheet      TEXT,
    PRIMARY KEY (chat_id, message_id)
);
-- One assignment per entry: who a row is assigned to, and the state of their
-- notification (delivered, acknowledged/seen, checked-done).
CREATE TABLE IF NOT EXISTS assignments (
    entry_id    TEXT PRIMARY KEY,
    sheet       TEXT,
    name        TEXT,
    chat_id     INTEGER,
    notified_at REAL,
    seen_at     REAL,
    done_at     REAL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        # Migration: link reminders to an entry so we can cancel them when the
        # row is checked/deleted (older DBs won't have the column).
        try:
            self._conn.execute("ALTER TABLE reminders ADD COLUMN entry_id TEXT")
        except sqlite3.OperationalError:
            pass
        # Track the last sheet-driven "Remind At" we scheduled, so we don't
        # reschedule it every sync tick.
        try:
            self._conn.execute("ALTER TABLE entries ADD COLUMN sheet_remind_at REAL")
        except sqlite3.OperationalError:
            pass
        # Semantic dedup key (normalized title+date) so the same event/article
        # submitted differently (image vs link) merges instead of duplicating.
        try:
            self._conn.execute("ALTER TABLE entries ADD COLUMN dedup_key TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_key ON entries (dedup_key)")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    # --- sessions ----------------------------------------------------
    def set_authed(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions(chat_id, authed_at) VALUES (?, ?)",
                (chat_id, time.time()),
            )
            self._conn.commit()

    def is_authed(self, chat_id: int, ttl_seconds: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT authed_at FROM sessions WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        if not row:
            return False
        return (time.time() - row[0]) < ttl_seconds

    def clear_auth(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    # --- dedup -------------------------------------------------------
    def seen(self, fingerprint: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return row is not None

    def mark_seen(self, fingerprint: str, kind: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed(fingerprint, kind, created_at) "
                "VALUES (?, ?, ?)",
                (fingerprint, kind, time.time()),
            )
            self._conn.commit()

    # --- dynamic allow-list ------------------------------------------
    def add_allowed(self, chat_id: int, added_by: int, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO allowed_chats(chat_id, added_by, added_at, note) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, added_by, time.time(), note),
            )
            self._conn.commit()

    def remove_allowed(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM allowed_chats WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    def list_allowed(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id, added_by, added_at, note FROM allowed_chats "
                "ORDER BY added_at").fetchall()
        return [{"chat_id": r[0], "added_by": r[1], "added_at": r[2], "note": r[3]}
                for r in rows]

    def is_allowed(self, chat_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM allowed_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row is not None

    # --- job queue ---------------------------------------------------
    def enqueue_job(self, chat_id: int, submitter: str, text: str,
                    attachments: list[Any], force_kind: str | None,
                    note_message_id: int | None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs(chat_id, submitter, text, attachments, "
                "force_kind, note_message_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (chat_id, submitter, text, json.dumps(attachments),
                 force_kind, note_message_id, time.time(), time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def claim_next_job(self) -> dict[str, Any] | None:
        """Atomically take the oldest pending job and mark it processing."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, chat_id, submitter, text, attachments, force_kind, "
                "note_message_id FROM jobs WHERE status = 'pending' "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE jobs SET status='processing', updated_at=? WHERE id=?",
                (time.time(), row[0]),
            )
            self._conn.commit()
        return {
            "id": row[0], "chat_id": row[1], "submitter": row[2],
            "text": row[3] or "", "attachments": json.loads(row[4] or "[]"),
            "force_kind": row[5], "note_message_id": row[6],
        }

    def finish_job(self, job_id: int, status: str, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error[:500], time.time(), job_id),
            )
            self._conn.commit()

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
            ).fetchone()
        return int(row[0]) if row else 0

    def requeue_processing(self) -> int:
        """On startup, any job left 'processing' was interrupted — requeue it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='pending', updated_at=? "
                "WHERE status='processing'", (time.time(),))
            self._conn.commit()
            return cur.rowcount

    # --- key/value checkpoints ---------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def incr_meta(self, key: str, by: int = 1) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            cur = int(row[0]) if row and str(row[0]).lstrip("-").isdigit() else 0
            cur += by
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, str(cur)))
            self._conn.commit()
            return cur

    # --- entries (stable per-row records) ----------------------------
    def add_entry(self, entry_id: str, chat_id: int, sheet: str,
                  fingerprint: str, title: str, analysis: dict[str, Any],
                  dedup_key: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO entries(id, chat_id, sheet, fingerprint, "
                "title, analysis, created_at, checked_at, removed, updated_at, "
                "dedup_key) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)",
                (entry_id, chat_id, sheet, fingerprint, title,
                 json.dumps(analysis), time.time(), time.time(), dedup_key),
            )
            self._conn.commit()

    _SELECT = ("SELECT id, chat_id, sheet, fingerprint, title, analysis, "
               "created_at, checked_at, removed, sheet_remind_at, dedup_key "
               "FROM entries ")

    def _entry_row(self, r) -> dict[str, Any]:
        return {"id": r[0], "chat_id": r[1], "sheet": r[2], "fingerprint": r[3],
                "title": r[4], "analysis": json.loads(r[5] or "{}"),
                "created_at": r[6], "checked_at": r[7], "removed": r[8],
                "sheet_remind_at": r[9] if len(r) > 9 else None,
                "dedup_key": r[10] if len(r) > 10 else None}

    def entry_by_key(self, dedup_key: str) -> dict[str, Any] | None:
        if not dedup_key:
            return None
        with self._lock:
            r = self._conn.execute(
                self._SELECT + "WHERE dedup_key = ? AND removed = 0 "
                "ORDER BY created_at LIMIT 1", (dedup_key,)).fetchone()
        return self._entry_row(r) if r else None

    def set_entry_sheet_remind(self, entry_id: str, ts: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE entries SET sheet_remind_at = ? WHERE id = ?",
                (ts, entry_id))
            self._conn.commit()

    def entry_by_fingerprint(self, fp: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                self._SELECT + "WHERE fingerprint = ? AND removed = 0 "
                "ORDER BY created_at LIMIT 1", (fp,)).fetchone()
        return self._entry_row(r) if r else None

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                self._SELECT + "WHERE id = ?", (entry_id,)).fetchone()
        return self._entry_row(r) if r else None

    def active_entries(self, sheet: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                self._SELECT + "WHERE sheet = ? AND removed = 0",
                (sheet,)).fetchall()
        return [self._entry_row(r) for r in rows]

    def update_entry_analysis(self, entry_id: str, analysis: dict[str, Any],
                              title: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE entries SET analysis = ?, title = ?, updated_at = ? "
                "WHERE id = ?",
                (json.dumps(analysis), title, time.time(), entry_id))
            self._conn.commit()

    def set_entry_checked(self, entry_id: str, checked_at: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE entries SET checked_at = ?, updated_at = ? WHERE id = ?",
                (checked_at, time.time(), entry_id))
            self._conn.commit()

    def mark_entry_removed(self, entry_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE entries SET removed = 1, updated_at = ? WHERE id = ?",
                (time.time(), entry_id))
            self._conn.commit()

    def entry_stats(self, sheet: str) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM entries WHERE sheet=? AND removed=0",
                (sheet,)).fetchone()[0]
            removed = self._conn.execute(
                "SELECT COUNT(*) FROM entries WHERE sheet=? AND removed=1",
                (sheet,)).fetchone()[0]
            rows = self._conn.execute(
                "SELECT created_at, checked_at FROM entries "
                "WHERE sheet=? AND removed=0 AND checked_at IS NOT NULL",
                (sheet,)).fetchall()
        checked = len(rows)
        avg_h = (sum((c - cr) for cr, c in rows) / checked / 3600) if checked else 0.0
        return {"total": total, "removed": removed, "checked": checked,
                "avg_check_hours": round(avg_h, 2)}

    # --- reminders ---------------------------------------------------
    def add_reminder(self, chat_id: int, fire_at: float, title: str,
                     payload: dict[str, Any], entry_id: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders(chat_id, fire_at, title, payload, entry_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, fire_at, title, json.dumps(payload), entry_id),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def cancel_entry_reminders(self, entry_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reminders SET fired = 1 WHERE entry_id = ? AND fired = 0",
                (entry_id,))
            self._conn.commit()
            return cur.rowcount

    def cancel_sheet_reminders(self, entry_id: str) -> int:
        """Cancel only reminders that were set from the sheet's Remind At cell,
        so a changed/cleared cell can be re-synced without touching the
        auto deadline/event pokes."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reminders SET fired = 1 WHERE entry_id = ? AND fired = 0 "
                "AND json_extract(payload, '$.source') = 'sheet'", (entry_id,))
            self._conn.commit()
            return cur.rowcount

    def due_reminders(self, now: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, chat_id, fire_at, title, payload FROM reminders "
                "WHERE fired = 0 AND fire_at <= ?",
                (now,),
            ).fetchall()
        return [
            {
                "id": r[0], "chat_id": r[1], "fire_at": r[2],
                "title": r[3], "payload": json.loads(r[4] or "{}"),
            }
            for r in rows
        ]

    def upcoming_reminders(self, chat_id: int, now: float,
                           horizon: float) -> list[dict[str, Any]]:
        """Not-yet-fired reminders for ONE chat within [now, now+horizon]."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, chat_id, fire_at, title, payload FROM reminders "
                "WHERE chat_id = ? AND fired = 0 AND fire_at >= ? AND fire_at <= ? "
                "ORDER BY fire_at",
                (chat_id, now, now + horizon),
            ).fetchall()
        return [
            {"id": r[0], "chat_id": r[1], "fire_at": r[2],
             "title": r[3], "payload": json.loads(r[4] or "{}")}
            for r in rows
        ]

    def all_reminders(self, chat_id: int) -> list[dict[str, Any]]:
        """Every not-yet-fired reminder for one chat, any time (no window).
        Used by the calendar, which needs past and far-future dates alike."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, chat_id, fire_at, title, payload FROM reminders "
                "WHERE chat_id = ? AND fired = 0 ORDER BY fire_at",
                (chat_id,)).fetchall()
        return [
            {"id": r[0], "chat_id": r[1], "fire_at": r[2],
             "title": r[3], "payload": json.loads(r[4] or "{}")}
            for r in rows]

    # --- people directory --------------------------------------------
    def set_person(self, chat_id: int, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO people(chat_id, name, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, "
                "updated_at=excluded.updated_at",
                (chat_id, name.strip(), time.time()))
            self._conn.commit()

    def remove_person(self, chat_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM people WHERE chat_id = ?", (chat_id,))
            self._conn.commit()
            return cur.rowcount

    def list_people(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id, name FROM people ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [{"chat_id": r[0], "name": r[1]} for r in rows]

    def person_by_name(self, name: str) -> dict[str, Any] | None:
        """Resolve a typed name to a person (case-insensitive, exact match)."""
        key = (name or "").strip().lower()
        if not key:
            return None
        for p in self.list_people():
            if p["name"].strip().lower() == key:
                return p
        return None

    def person_name(self, chat_id: int) -> str | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT name FROM people WHERE chat_id = ?", (chat_id,)).fetchone()
        return r[0] if r else None

    # --- result message → entry (for replies) ------------------------
    def set_row_message(self, chat_id: int, message_id: int, entry_id: str,
                        sheet: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO row_messages(chat_id, message_id, "
                "entry_id, sheet) VALUES (?,?,?,?)",
                (chat_id, message_id, entry_id, sheet))
            self._conn.commit()

    def get_row_message(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT entry_id, sheet FROM row_messages WHERE chat_id=? AND "
                "message_id=?", (chat_id, message_id)).fetchone()
        return {"entry_id": r[0], "sheet": r[1]} if r else None

    # --- assignments -------------------------------------------------
    def get_assignment(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT entry_id, sheet, name, chat_id, notified_at, seen_at, "
                "done_at FROM assignments WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        if not r:
            return None
        return {"entry_id": r[0], "sheet": r[1], "name": r[2], "chat_id": r[3],
                "notified_at": r[4], "seen_at": r[5], "done_at": r[6]}

    def set_assignment(self, entry_id: str, sheet: str, name: str,
                       chat_id: int | None) -> None:
        """Create/replace the assignment for a row (resets notify/seen/done)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO assignments(entry_id, sheet, name, chat_id) "
                "VALUES (?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET "
                "sheet=excluded.sheet, name=excluded.name, chat_id=excluded.chat_id, "
                "notified_at=NULL, seen_at=NULL, done_at=NULL",
                (entry_id, sheet, name, chat_id))
            self._conn.commit()

    def mark_assignment_notified(self, entry_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE assignments SET notified_at=? WHERE entry_id=?",
                (time.time(), entry_id))
            self._conn.commit()

    def mark_assignment_seen(self, entry_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE assignments SET seen_at=COALESCE(seen_at,?) WHERE entry_id=?",
                (time.time(), entry_id))
            self._conn.commit()

    def mark_assignment_done(self, entry_id: str, done: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE assignments SET done_at=? WHERE entry_id=?",
                (time.time() if done else None, entry_id))
            self._conn.commit()

    def clear_assignment(self, entry_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM assignments WHERE entry_id = ?", (entry_id,))
            self._conn.commit()

    def mark_reminder_fired(self, reminder_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
