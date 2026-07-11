"""Generate RFC 5545 .ics calendar files for events.

The file is sent to Telegram as a *document*; opening it on iOS/Android
offers "Add to Calendar". Each event embeds VALARM reminders so the phone
notifies you even without Briefer running: on the day (09:00 local) and
2 hours + 1 hour before the start.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo


def _esc(text: str) -> str:
    """Escape a value per RFC 5545 (backslash, comma, semicolon, newline)."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold long lines to <=75 octets with CRLF + space continuation."""
    out = []
    while len(line.encode("utf-8")) > 73:
        # find a cut point <=73 bytes
        cut = 73
        while len(line[:cut].encode("utf-8")) > 73:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    out.append(line)
    return "\r\n".join(out)


def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def build_event_ics(
    *,
    title: str,
    start: datetime,
    tz_name: str,
    all_day: bool = False,
    end: datetime | None = None,
    description: str = "",
    location: str = "",
    url: str = "",
    day_of_alarm: bool = True,
) -> bytes:
    """Return .ics bytes for a single VEVENT with reminders.

    `start`/`end` may be tz-aware or naive; naive values are interpreted in
    `tz_name`. For all-day events the date part is used.
    """
    tz = ZoneInfo(tz_name)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end is None:
        end = (start + timedelta(days=1)) if all_day else (start + timedelta(hours=1))
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)

    now = datetime.now(ZoneInfo("UTC"))
    uid_basis = f"{title}|{start.isoformat()}|{url}"
    uid = hashlib.sha256(uid_basis.encode()).hexdigest()[:24] + "@briefer"

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Briefer//Event//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_utc_stamp(now)}",
    ]
    if all_day:
        lines.append("DTSTART;VALUE=DATE:" + start.strftime("%Y%m%d"))
        lines.append("DTEND;VALUE=DATE:" + end.strftime("%Y%m%d"))
    else:
        lines.append("DTSTART:" + _utc_stamp(start))
        lines.append("DTEND:" + _utc_stamp(end))

    lines.append(_fold("SUMMARY:" + _esc(title)))
    if description:
        lines.append(_fold("DESCRIPTION:" + _esc(description)))
    if location:
        lines.append(_fold("LOCATION:" + _esc(location)))
    if url:
        lines.append(_fold("URL:" + _esc(url)))
    lines.append("STATUS:CONFIRMED")
    lines.append("TRANSP:OPAQUE")

    # --- reminders (VALARM) ---
    def alarm(trigger_prop: str, desc: str) -> list[str]:
        # trigger_prop is the full property, e.g. "TRIGGER:-PT2H" or
        # "TRIGGER;VALUE=DATE-TIME:20260801T060000Z".
        return [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            trigger_prop,
            _fold("DESCRIPTION:" + _esc(desc)),
            "END:VALARM",
        ]

    lines += alarm("TRIGGER:-PT2H", f"In 2 hours: {title}")
    lines += alarm("TRIGGER:-PT1H", f"In 1 hour: {title}")
    if day_of_alarm:
        # Absolute alarm at 09:00 local on the event's day.
        day_of = datetime.combine(start.astimezone(tz).date(), dtime(9, 0), tz)
        lines += alarm(
            "TRIGGER;VALUE=DATE-TIME:" + _utc_stamp(day_of), f"Today: {title}"
        )

    lines += ["END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")
