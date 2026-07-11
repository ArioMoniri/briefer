#!/usr/bin/env python3
"""Headless Google login for Briefer (no browser on the server needed).

Run:  ./manage.sh google-auth      (or:  .venv/bin/python authorize_google.py)

It prints a Google login link. Open it on ANY device (your phone/laptop),
sign in to the Google account that should own the sheets, and approve.
Google then redirects your browser to a http://localhost/... page that
won't load — that's fine. Copy the whole address-bar URL (or just the
`code=...` value) and paste it back here. A token.json is written and the
bot uses it (auto-refreshing) from then on.

Requires an OAuth client of type "Desktop app" (Google Cloud → Credentials
→ Create credentials → OAuth client ID → Desktop app), downloaded as the
file named by GOOGLE_OAUTH_CLIENT_FILE (default client_secret.json).
"""
from __future__ import annotations

import sys
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, "src")

from briefer.config import load_config  # noqa: E402

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def main() -> int:
    cfg = load_config()
    client_file = str(cfg.oauth_client_path)
    token_file = str(cfg.token_path)

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        print("google-auth-oauthlib is not installed. Run ./setup.sh first.")
        return 1

    try:
        flow = Flow.from_client_secrets_file(
            client_file, scopes=SCOPES, redirect_uri="http://localhost")
    except FileNotFoundError:
        print(f"OAuth client file not found: {client_file}\n"
              "Create a 'Desktop app' OAuth client in Google Cloud and save the\n"
              "downloaded JSON there (or set GOOGLE_OAUTH_CLIENT_FILE).")
        return 1

    auth_url, _ = flow.authorization_url(
        prompt="consent", access_type="offline", include_granted_scopes="true")

    print("\n1) Open this link on any device and sign in to your Google account:\n")
    print("   " + auth_url + "\n")
    print("2) Approve access. Your browser will try to open a http://localhost")
    print("   page that fails to load — that is expected.")
    print("3) Copy the FULL address-bar URL (or just the code=... value) and")
    print("   paste it below.\n")

    pasted = input("Paste the redirected URL or code here: ").strip()
    code = pasted
    if pasted.startswith("http"):
        qs = parse_qs(urlparse(pasted).query)
        code = (qs.get("code") or [""])[0]
    if not code:
        print("No authorization code found in what you pasted.")
        return 1

    flow.fetch_token(code=code)
    creds = flow.credentials
    import os
    # Write the token (contains a refresh token) with owner-only perms.
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    print(f"\n✅ Saved {token_file}. Set GOOGLE_AUTH_MODE=oauth in .env and "
          "restart:  ./manage.sh restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
