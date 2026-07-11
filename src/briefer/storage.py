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
"""


class Store:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
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

    # --- reminders ---------------------------------------------------
    def add_reminder(self, chat_id: int, fire_at: float, title: str,
                     payload: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders(chat_id, fire_at, title, payload) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, fire_at, title, json.dumps(payload)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

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

    def mark_reminder_fired(self, reminder_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
