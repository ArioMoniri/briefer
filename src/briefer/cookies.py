"""Inspect the freshness of your logged-in sessions (LinkedIn/Instagram/X…).

Reads expiry from the Playwright storage_state.json and/or a Netscape
cookies.txt, and reports each platform's key session cookie so you can renew
it before it lapses. The persistent browser profile keeps live cookies in an
encrypted sqlite DB we don't parse; storage_state (saved at login) is the
readable proxy.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("briefer.cookies")

# The one cookie whose expiry tells you if a platform login is still good.
_IMPORTANT = {
    "linkedin.com": ["li_at"],
    "instagram.com": ["sessionid"],
    "x.com": ["auth_token"],
    "twitter.com": ["auth_token"],
    "facebook.com": ["c_user", "xs"],
}


def _gather(cfg) -> list[tuple[str, str, float]]:
    out: list[tuple[str, str, float]] = []
    ss = cfg.browser_storage_state_path
    if ss:
        try:
            data = json.loads(open(ss, encoding="utf-8").read())
            for c in data.get("cookies", []):
                out.append((str(c.get("domain", "")), str(c.get("name", "")),
                            float(c.get("expires", -1) or -1)))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read storage_state: %s", exc)
    ck = cfg.cookies_path
    if ck:
        try:
            from .browser import _load_netscape_cookies
            for c in _load_netscape_cookies(ck):
                out.append((c.get("domain", ""), c.get("name", ""),
                            float(c.get("expires", -1) or -1)))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read cookies.txt: %s", exc)
    return out


def cookie_status(cfg, warn_days: int = 3) -> list[dict[str, Any]]:
    """One entry per platform we have a session for, with days-left + status."""
    cookies = _gather(cfg)
    now = time.time()
    out: list[dict[str, Any]] = []
    for host, names in _IMPORTANT.items():
        best: tuple[str, float] | None = None
        for dom, name, exp in cookies:
            d = dom.lstrip(".").lower()
            if name in names and (d == host or d.endswith("." + host)
                                  or host.endswith(d)):
                if exp and exp > 0:
                    if best is None or exp < best[1]:
                        best = (name, exp)
                elif best is None:
                    best = (name, -1)
        if not best:
            continue
        name, exp = best
        if exp == -1:
            out.append({"platform": host, "cookie": name,
                        "status": "session", "days_left": None})
        else:
            days = (exp - now) / 86400
            status = ("expired" if days < 0
                      else "expiring" if days <= warn_days else "ok")
            out.append({"platform": host, "cookie": name, "status": status,
                        "days_left": round(days, 1), "expires_at": exp})
    return out


def format_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ("No logged-in sessions found. Add cookies.txt or run "
                "<code>./manage.sh browser-login</code>.")
    icon = {"ok": "🟢", "expiring": "🟠", "expired": "🔴", "session": "⚪"}
    lines = ["🍪 <b>Session / cookie status</b>"]
    for r in rows:
        d = r.get("days_left")
        when = ("session-only" if r["status"] == "session"
                else f"{d}d left" if d is not None and d >= 0
                else "EXPIRED")
        lines.append(f"{icon.get(r['status'], '⚪')} <b>{r['platform']}</b> "
                     f"({r['cookie']}) — {when}")
    return "\n".join(lines)


def problems(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("status") in ("expired", "expiring")]
