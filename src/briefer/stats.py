"""Richer, more descriptive statistics for a sheet, used by /stats and the
Stats tab. Computes a status breakdown, done-rate, overdue, recent activity
and time-to-check figures from the stored entries."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


def rich_stats(store, sheet: str) -> dict[str, Any]:
    from .pipeline import _parse_deadline, _parse_event_date
    from .tags import status_tag

    entries = store.active_entries(sheet)
    now_dt = datetime.now(timezone.utc)
    now = time.time()
    by = {"passed": 0, "due_soon": 0, "coming": 0, "upcoming": 0,
          "done": 0, "no_date": 0}
    durations: list[float] = []
    overdue = added_7d = upcoming_7d = 0

    for e in entries:
        a = e.get("analysis") or {}
        checked = e.get("checked_at") is not None
        dl = _parse_deadline(a.get("application_deadline"))
        ev, _ = _parse_event_date(a.get("event_date"))
        tag = status_tag(e.get("sheet", sheet), checked, dl, ev, now_dt)
        if "Done" in tag:
            by["done"] += 1
        elif "Passed" in tag:
            by["passed"] += 1
            overdue += 1  # passed and not done
        elif "Due soon" in tag:
            by["due_soon"] += 1
        elif "Coming" in tag:
            by["coming"] += 1
        elif "No date" in tag:
            by["no_date"] += 1
        else:
            by["upcoming"] += 1
        if checked:
            durations.append((e["checked_at"] - e["created_at"]) / 3600)
        if now - (e.get("created_at") or now) <= 7 * 86400:
            added_7d += 1
        d = dl or ev
        if d is not None:
            ts = d.timestamp() if d.tzinfo else d.replace(tzinfo=timezone.utc).timestamp()
            if now <= ts <= now + 7 * 86400:
                upcoming_7d += 1

    total = len(entries)
    done = by["done"]
    avg = round(sum(durations) / len(durations), 1) if durations else 0
    med = 0.0
    if durations:
        s = sorted(durations)
        med = round(s[len(s) // 2], 1)
    removed = int(store.get_meta(f"{sheet}_removed_total", "0") or 0)
    return {
        "total": total, "done": done,
        "done_pct": round(100 * done / total) if total else 0,
        "pending": total - done, "overdue": overdue,
        "removed": removed, "added_7d": added_7d, "upcoming_7d": upcoming_7d,
        "avg_check_hours": avg, "median_check_hours": med, "by_status": by,
    }


def stats_rows(sheet_name: str, s: dict[str, Any]) -> list[list[Any]]:
    """Key/value rows for the Stats tab."""
    b = s["by_status"]
    when = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return [
        ["Metric", sheet_name],
        ["Total (active)", s["total"]],
        ["Done", f"{s['done']} ({s['done_pct']}%)"],
        ["Pending", s["pending"]],
        ["Overdue (passed, not done)", s["overdue"]],
        ["Upcoming (next 7 days)", s["upcoming_7d"]],
        ["Added (last 7 days)", s["added_7d"]],
        ["Removed (all-time)", s["removed"]],
        ["Avg time to check (h)", s["avg_check_hours"]],
        ["Median time to check (h)", s["median_check_hours"]],
        ["🔴 Passed", b["passed"]],
        ["🟠 Due soon", b["due_soon"]],
        ["🟡 Coming up", b["coming"]],
        ["🟢 Upcoming / New", b["upcoming"]],
        ["⚪ No date", b["no_date"]],
        ["Updated at", when],
    ]
