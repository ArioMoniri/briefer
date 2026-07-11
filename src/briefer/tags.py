"""Consistent colored status tags, shared by the pipeline (on write) and the
sheet sync (live refresh) so both sheets use the SAME scheme.

Color scheme (emoji tags render everywhere, incl. the mobile app):
  ✅ Done        — checkbox ticked
  🔴 Passed      — deadline/event date is in the past
  🟠 Due soon    — within 3 days
  🟡 Coming up   — within 14 days
  🟢 Upcoming    — further out  /  🟢 New for undated articles
  ⚪ No date     — event with no known date
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def status_tag(kind: str, checked: bool, deadline_dt: Optional[datetime],
               event_dt: Optional[datetime],
               now: Optional[datetime] = None) -> str:
    if checked:
        return "✅ Done"
    now = now or datetime.now(timezone.utc)
    now = _aware(now)
    dt = _aware(deadline_dt) or _aware(event_dt)
    if dt is None:
        return "🟢 New" if kind == "article" else "⚪ No date"
    days = (dt - now).total_seconds() / 86400
    if days < 0:
        return "🔴 Passed"
    if days <= 3:
        return "🟠 Due soon"
    if days <= 14:
        return "🟡 Coming up"
    return "🟢 Upcoming"
