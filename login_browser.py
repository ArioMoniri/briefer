#!/usr/bin/env python
"""Interactive browser login → persistent profile (stays logged in).

Run it where a browser window can be SEEN:
  • on your laptop:   PYTHONPATH=src ./.venv/bin/python login_browser.py
  • on the server:    ./manage.sh browser-login   (starts a virtual display + VNC)

It opens the login pages; you sign in to whichever sites you want, then press
Enter back in the terminal. The session is saved into `browser_profile/` (and
`storage_state.json`), which the bot reuses for every render — so LinkedIn /
Instagram / private X pages load logged-in from then on.
"""
from __future__ import annotations

import os
import sys

PROFILE = os.environ.get("BROWSER_PROFILE_DIR", "browser_profile")
STATE = os.environ.get("BROWSER_STORAGE_STATE", "storage_state.json")
SITES = os.environ.get(
    "LOGIN_SITES",
    "https://www.linkedin.com/login,https://www.instagram.com/accounts/login/",
).split(",")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("Playwright is not installed. Run: ./manage.sh enable-browser")
        return 1

    os.makedirs(PROFILE, exist_ok=True)
    print(f"Opening a browser (profile: {PROFILE}) …")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        first = ctx.pages[0] if ctx.pages else ctx.new_page()
        for i, url in enumerate([s.strip() for s in SITES if s.strip()]):
            page = first if i == 0 else ctx.new_page()
            try:
                page.goto(url, timeout=45000)
            except Exception as exc:  # noqa: BLE001
                print(f"  (could not open {url}: {exc})")
        print("\n>>> Log in to each site in the browser window.")
        print(">>> When you're done, come back here and press Enter.")
        try:
            input()
        except EOFError:
            pass
        try:
            ctx.storage_state(path=STATE)
        except Exception:  # noqa: BLE001
            pass
        ctx.close()
    print(f"\n✅ Saved your login to '{PROFILE}' and '{STATE}'.")
    print("Restart the bot to use it:  ./manage.sh restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
