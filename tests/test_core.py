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
    finally:
        s.close()
        os.remove(p)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
