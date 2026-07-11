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


def test_media_regexes_and_tweet_parse():
    from briefer import media as m
    assert m.TWEET_RE.match("https://x.com/u/status/1790000000000000000")
    assert m.TWEET_RE.match("https://twitter.com/u/status/123")
    assert m.VIDEO_HOST_RE.match("https://youtu.be/abcdefghijk")
    assert m.VIDEO_HOST_RE.match("https://x.com/u/status/9")
    # token is deterministic and strips 0s/dots
    tok = m._syndication_token("1790000000000000000")
    assert tok and "0" not in tok and "." not in tok
    j = {
        "text": "main", "user": {"screen_name": "alice"},
        "in_reply_to_screen_name": "bob",
        "parent": {"text": "p", "user": {"screen_name": "bob"}},
        "quoted_tweet": {"text": "q", "user": {"screen_name": "carol"}},
        "retweeted_status": {"text": "r", "user": {"screen_name": "dave"}},
        "mediaDetails": [
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/x.jpg"},
            {"type": "video", "video_info": {"variants": [
                {"type": "video/mp4", "src": "https://v/hi.mp4", "bitrate": 800000}]}},
        ],
    }
    td = m._parse_syndication(j, "https://x.com/alice/status/1")
    assert td.is_reply and td.reply_to.author == "bob"
    assert td.quoted.author == "carol" and td.retweet_of.author == "dave"
    assert td.photo_urls == ["https://pbs.twimg.com/x.jpg"]
    assert td.video_urls == ["https://v/hi.mp4"]
    r = td.render()
    assert "@alice" in r and "Reposted" in r and "Quoted" in r and "reply" in r.lower()


def test_video_handler_adds_caption_and_keyframes():
    from briefer.enrich import Enricher, EnrichedContent

    class FakeTranscriber:
        def transcribe_url(self, u):
            return {"title": "Clip", "uploader": "@c", "description": "cap #ai",
                    "transcript": "spoken", "keyframes": [b"\xff\xd8\xffa", b"\xff\xd8\xffb"],
                    "note": "whisper"}

    e = Enricher(15_000_000, transcriber=FakeTranscriber(), enable_gallery_dl=False)
    c = EnrichedContent()
    assert e._handle_video("https://www.tiktok.com/@x/video/1", c) is True
    block = c.link_texts["https://www.tiktok.com/@x/video/1"]
    assert "cap #ai" in block and "spoken" in block
    assert sum(1 for a in c.attachments if a.kind == "image") == 2


def test_enrich_survives_bad_url():
    from briefer.enrich import Enricher
    e = Enricher(15_000_000)
    e._handle_url = lambda url, content: (_ for _ in ()).throw(RuntimeError("boom"))
    c = e.enrich("visit https://example.com/x", [])
    # No exception propagated; the failure is recorded as a note.
    assert any("boom" in n for n in c.notes)


def test_bot_commands_cover_key_commands():
    from briefer.telegram_bot import BOT_COMMANDS
    names = {c for c, _ in BOT_COMMANDS}
    for required in ("start", "help", "login", "sheets", "status", "logs"):
        assert required in names


def test_job_queue_flow():
    import tempfile
    from briefer.storage import Store
    s = Store(Path(tempfile.mktemp(suffix=".db")))
    try:
        j1 = s.enqueue_job(1, "me", "a", [{"t": "image", "file_id": "x"}], None, 10)
        j2 = s.enqueue_job(1, "me", "b", [], "event", 11)
        assert s.pending_count() == 2
        job = s.claim_next_job()
        assert job["id"] == j1 and job["attachments"][0]["file_id"] == "x"
        # crash + resume
        assert s.requeue_processing() == 1
        again = s.claim_next_job()
        assert again["id"] == j1
        s.finish_job(j1, "done")
        assert s.claim_next_job()["id"] == j2
        s.finish_job(j2, "done")
        assert s.claim_next_job() is None and s.pending_count() == 0
        s.set_meta("article_last_row", 7)
        assert s.get_meta("article_last_row") == "7"
        assert s.incr_meta("processed_total") == 1
    finally:
        s.close()


def test_sheet_column_and_row_helpers():
    from briefer.sheets import _col_letter, _appended_row_number
    assert _col_letter(1) == "A" and _col_letter(16) == "P" and _col_letter(27) == "AA"
    assert _appended_row_number({"updates": {"updatedRange": "Events!A12:W12"}}) == 12
    assert _appended_row_number({"bad": 1}) is None


def test_analysis_merge_is_cumulative():
    from briefer.pipeline import _merge_analysis
    prev = {"catch_points": ["a", "b"], "tags": ["x"], "summary": "old"}
    new = {"catch_points": ["b", "c"], "tags": ["x", "y"], "summary": "new"}
    merged, changed = _merge_analysis(prev, new)
    assert merged["catch_points"] == ["a", "b", "c"]
    assert merged["tags"] == ["x", "y"]
    assert merged["summary"] == "new" and changed is True
    _, unchanged = _merge_analysis(prev, prev)
    assert unchanged is False


def test_office_docx_extraction():
    import io
    from docx import Document
    from briefer.enrich import office_to_text
    d = Document()
    d.add_paragraph("Deadline Aug 1")
    buf = io.BytesIO()
    d.save(buf)
    assert "Deadline Aug 1" in office_to_text(buf.getvalue(), "x.docx")


def test_entries_and_checkbox_sync():
    import tempfile
    import time as _t
    from briefer.storage import Store
    from briefer.sheet_sync import SheetSync
    from briefer.sheets import EVENT_HEADERS, _control_cols

    s = Store(Path(tempfile.mktemp(suffix=".db")))
    try:
        s.add_entry("e1", 5, "event", "fp1", "H", {"title": "H"})
        s.add_reminder(5, _t.time() + 9999, "r", {}, entry_id="e1")
        assert s.entry_by_fingerprint("fp1")["id"] == "e1"

        c = _control_cols(EVENT_HEADERS)
        row = [""] * len(EVENT_HEADERS)
        row[c["id"] - 1] = "e1"
        row[c["done"] - 1] = "TRUE"

        class FakeWS:
            def __init__(self, rows): self._rows = rows
            def get_all_values(self): return self._rows

        class FakeSheets:
            def __init__(self, rows): self._ws = FakeWS(rows); self.check = []
            def worksheet(self, sheet): return self._ws
            def write_check_cells(self, sheet, r, iso, h): self.check.append((r, h))
            def write_stats(self, sheet, st): pass
            def write_status(self, sheet, r, tag): pass
            def archive_entry(self, sheet, entry): pass

        # Checked → checked_at set + reminders cancelled.
        fake = FakeSheets([EVENT_HEADERS, row])
        SheetSync(fake, s)._sync_sheet("event")
        assert s.entry_by_fingerprint("fp1")["checked_at"] is not None
        assert len(s.due_reminders(_t.time() + 99999)) == 0
        assert fake.check  # a check cell was written

        # Deleted row → entry removed + counted.
        SheetSync(FakeSheets([EVENT_HEADERS]), s)._sync_sheet("event")
        assert s.active_entries("event") == []
        assert s.get_meta("event_removed_total") == "1"
    finally:
        s.close()


def test_netscape_cookie_parser():
    import tempfile
    import os
    from briefer.browser import _load_netscape_cookies
    p = tempfile.mktemp()
    with open(p, "w") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        fh.write(".linkedin.com\tTRUE\t/\tTRUE\t9999999999\tli_at\tSECRET\n")
        fh.write("#HttpOnly_.x.com\tTRUE\t/\tTRUE\t0\tauth\tabc\n")
    try:
        cookies = _load_netscape_cookies(p)
    finally:
        os.remove(p)
    names = {c["name"]: c for c in cookies}
    assert names["li_at"]["domain"] == ".linkedin.com"
    assert names["li_at"]["expires"] == 9999999999
    assert "auth" in names  # #HttpOnly_ line is parsed


def test_og_meta_extraction():
    from briefer.enrich import Enricher
    e = Enricher(1000)
    html = ('<html><head><title>T</title>'
            '<meta property="og:description" content="The real post text here">'
            '</head><body>Sign in</body></html>')
    assert "The real post text here" in e._extract_html_text(html)


def test_link_safety_gate():
    from briefer.link_safety import heuristic_flags, is_probably_article, assess_link
    # hard fails
    assert heuristic_flags("http://user:pass@evil.com/")[0] is False
    # soft flags but not hard-fail
    ok, flags = heuristic_flags("https://bit.ly/x")
    assert ok and "URL shortener" in flags
    assert heuristic_flags("https://nature.com/articles/s1")[1] == []
    # article pre-filter
    assert is_probably_article("https://site.com/a/deep-post", set())
    assert not is_probably_article("https://site.com/logo.png", set())
    assert not is_probably_article("https://x.com/", {"x.com"})

    class SafeLLM:
        def json(self, s, u, *, model=None, max_tokens=2000, **k):
            return {"safe": True, "relevant": True, "category": "news"}

    class BadLLM:
        def json(self, s, u, *, model=None, max_tokens=2000, **k):
            return {"safe": False, "reason": "phishing"}

    # A public, resolvable host passes SSRF; the guard verdict then decides.
    v = assess_link("https://example.com/article", "ctx", llm=SafeLLM(),
                    guard_model="g")
    assert v.safe and v.fetch
    v2 = assess_link("https://example.com/verify", "ctx", llm=BadLLM(),
                     guard_model="g")
    assert not v2.safe and not v2.fetch


def test_reminder_directive_and_parse():
    from briefer.reminders import extract_directive, parse_when
    assert extract_directive("remind me in 3 days") == ("in 3 days", "")
    w, note = extract_directive("great paper https://x.com remind me 2026-08-01 18:00")
    assert w == "2026-08-01 18:00" and "great paper" in note
    assert extract_directive("just an article")[0] is None
    dt = parse_when("2026-08-01 18:00", "Europe/Istanbul")
    assert dt is not None and dt.tzinfo is not None
    assert parse_when("in 2 days", "UTC") is not None


def test_multi_link_split():
    from briefer.telegram_bot import BrieferBot
    subs = BrieferBot._split_submissions(None, "a https://x.com/1 b https://y.com/2", [])
    assert len(subs) == 2
    assert subs[0][0].startswith("https://x.com/1")
    # single link → single item
    one = BrieferBot._split_submissions(None, "just https://x.com/1", [])
    assert len(one) == 1


def test_error_message_classifier():
    from briefer.telegram_bot import _error_message

    class Credit(Exception):
        pass
    msg, infra = _error_message(Credit("Your credit balance is too low"))
    assert infra and "credit" in msg.lower()
    msg2, infra2 = _error_message(ValueError("boom"))
    assert not infra2 and "boom" in msg2


def test_status_tags_consistent():
    from datetime import datetime, timezone
    from briefer.tags import status_tag
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    assert status_tag("event", True, None, None, now) == "✅ Done"
    assert "🔴" in status_tag("event", False, datetime(2026, 7, 1, tzinfo=timezone.utc), None, now)
    assert "🟠" in status_tag("event", False, datetime(2026, 7, 13, tzinfo=timezone.utc), None, now)
    assert "🟢" in status_tag("article", False, None, None, now)
    # naive datetime doesn't crash
    assert status_tag("event", False, datetime(2026, 11, 4), None, now)


def test_semantic_dedup_key():
    from briefer.pipeline import _dedup_key
    k1 = _dedup_key("event", {"title": "SIMYA Industrial AI Demo Day",
                              "event_date": "2026-11-04"})
    k2 = _dedup_key("event", {"title": "simya  industrial ai DEMO day!",
                              "application_deadline": "2026-11-04T09:00"})
    assert k1 == k2  # same event, different submission → same key
    ka = _dedup_key("article", {"title": "RisQ", "entities": ["Daniel Rueckert"]})
    assert ka.startswith("article|risq")


def test_apply_link_extraction():
    from briefer.enrich import Enricher
    e = Enricher(1000)
    html = ('<a href="/apply-now/">Hemen Başvur</a>'
            '<a href="https://lu.ma/x">Register</a>'
            '<a href="/about">About</a>')
    links = e._apply_links(html, "https://simya.vc/industrial-ai-day/")
    assert "https://simya.vc/apply-now/" in links
    assert "https://lu.ma/x" in links
    assert not any("/about" in u for u in links)


def test_guess_media_type():
    from briefer.media import guess_media_type
    assert guess_media_type(b"\xff\xd8\xff\xe0xx") == "image/jpeg"
    assert guess_media_type(b"\x89PNG\r\n\x1a\nxx") == "image/png"
    assert guess_media_type(b"RIFF0000WEBPxx") == "image/webp"


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
