"""Unit tests for the pure-logic parts (no network / no live API keys).

Run:  PYTHONPATH=src ./.venv/bin/python -m pytest -q
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from briefer.security import (  # noqa: E402
    is_safe_url, RateLimiter, hash_password, verify_password, clamp,
)
from briefer.storage import Store  # noqa: E402
from briefer.llm import _extract_json  # noqa: E402
from briefer.pipeline import _parse_deadline  # noqa: E402


def test_ssrf_blocks_private_and_metadata():
    assert is_safe_url("http://localhost/")[0] is False
    assert is_safe_url("http://169.254.169.254/latest/meta-data")[0] is False
    assert is_safe_url("http://10.0.0.5/")[0] is False
    assert is_safe_url("http://192.168.1.1/")[0] is False
    assert is_safe_url("file:///etc/passwd")[0] is False
    assert is_safe_url("https://example.com/")[0] is True


def test_ssrf_rejects_odd_ports_and_pins_public_ip():
    from briefer.security import safe_resolve
    assert is_safe_url("http://example.com:8080/")[0] is False
    assert is_safe_url("https://example.com:22/")[0] is False
    ok, _reason, ip = safe_resolve("https://example.com/")
    assert ok and ip  # returns a concrete pinned public IP


def test_password_roundtrip():
    salt, h = hash_password("hunter2")
    assert verify_password("hunter2", salt, h)
    assert not verify_password("wrong", salt, h)


def test_rate_limiter():
    rl = RateLimiter(2)
    assert rl.allow(1) and rl.allow(1)
    assert not rl.allow(1)
    assert rl.allow(2)  # different chat


def test_json_extraction():
    assert _extract_json('x {"a": 1} y') == {"a": 1}
    assert _extract_json('```json\n{"b": true}\n```') == {"b": True}
    assert _extract_json("no json here") == {}


def test_deadline_parsing():
    assert _parse_deadline("2026-09-15") is not None
    assert _parse_deadline("2026-09-15T23:59:00+00:00") is not None
    assert _parse_deadline("next friday") is None
    assert _parse_deadline(None) is None


def test_event_date_parsing():
    from briefer.pipeline import _parse_event_date
    dt, allday = _parse_event_date("2026-08-01")
    assert dt is not None and allday is True
    dt, allday = _parse_event_date("2026-08-01T14:30")
    assert dt is not None and allday is False
    dt, allday = _parse_event_date("2026-08-01 to 2026-08-03")
    assert dt is not None and dt.day == 1
    assert _parse_event_date("someday") == (None, False)


def test_ics_generation():
    from datetime import datetime
    from briefer.calendar_ics import build_event_ics
    ics = build_event_ics(title="A, b; c", start=datetime(2026, 8, 1, 14, 0),
                          tz_name="Europe/Istanbul").decode()
    assert ics.startswith("BEGIN:VCALENDAR")
    assert ics.count("BEGIN:VALARM") == 3       # day-of + 2h + 1h
    assert "TRIGGER:-PT2H" in ics and "TRIGGER:-PT1H" in ics
    assert "SUMMARY:A\\, b\\; c" in ics          # RFC 5545 escaping
    assert "DTSTART:20260801T110000Z" in ics     # 14:00 Istanbul -> 11:00 UTC


def test_ics_and_gcal_timezone_agree():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import re
    from briefer.calendar_ics import build_event_ics
    from briefer.telegram_bot import _gcal_link
    start = datetime(2026, 8, 1, 14, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    ics = build_event_ics(title="X", start=start, tz_name="Europe/Istanbul").decode()
    gl = _gcal_link("X", start, False, "d", "loc")
    ics_dt = re.search(r"DTSTART:(\d+T\d+Z)", ics).group(1)
    gl_dt = re.search(r"dates=(\d+T\d+Z)", gl).group(1)
    assert ics_dt == gl_dt == "20260801T110000Z"


def test_ics_escapes_bare_cr():
    from datetime import datetime
    from briefer.calendar_ics import build_event_ics
    ics = build_event_ics(title="A\rEND:VEVENT", start=datetime(2026, 8, 1),
                          tz_name="UTC", all_day=True).decode()
    # A bare CR must not create a new physical line inside SUMMARY.
    assert "\rEND:VEVENT" not in ics
    assert "SUMMARY:A\\nEND:VEVENT" in ics


def test_clamp():
    assert clamp("abc", 10) == "abc"
    assert "truncated" in clamp("a" * 100, 10)


def test_store_dedup_and_reminders():
    p = Path(tempfile.mktemp(suffix=".db"))
    s = Store(p)
    try:
        s.set_authed(7)
        assert s.is_authed(7, 1000)
        assert not s.is_authed(8, 1000)
        s.mark_seen("fp1", "event")
        assert s.seen("fp1") and not s.seen("fp2")
        rid = s.add_reminder(7, 0.0, "t", {"k": "v"})
        assert len(s.due_reminders(100)) == 1
        s.mark_reminder_fired(rid)
        assert len(s.due_reminders(100)) == 0
        # upcoming_reminders must be scoped to the requesting chat only
        s.add_reminder(7, 500.0, "chat7 event", {})
        s.add_reminder(8, 500.0, "chat8 event", {})
        mine = s.upcoming_reminders(7, 0.0, 1000.0)
        assert len(mine) == 1 and mine[0]["title"] == "chat7 event"
    finally:
        s.close()
        os.remove(p)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
