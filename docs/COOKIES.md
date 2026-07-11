# Accessing logged-in content (LinkedIn, Instagram, private X…)

Some platforms show **nothing** to a logged-out visitor — LinkedIn posts,
private Instagram, members-only pages. There is **no public API and no
installer that bypasses login**; the only reliable way to read that content
is to fetch it **as a logged-in user**, i.e. with your own browser cookies.

There are two ways to give Briefer your login. **Option A (persistent browser
login) is recommended** — you log in once through a real browser and the
session (cookies *and* localStorage) is kept in a profile the bot reuses, so it
stays logged in. Option B is a `cookies.txt` file.

---

## Option A — Persistent browser login (recommended)

Log in once; every future render is authenticated and it survives restarts.

### On a headless server (no screen) — via VNC
```bash
./manage.sh enable-browser     # once, if you haven't (installs Chromium)
./manage.sh browser-login      # starts a virtual screen + VNC and a browser
```
It prints instructions:
1. On **your computer**: `ssh -L 5900:localhost:5900 <you>@<server>`
2. Open a **VNC viewer** → connect to `localhost:5900`
3. In the browser window, **log in** to LinkedIn / Instagram / X.
4. Back in the SSH terminal, **press Enter** — the session is saved to
   `browser_profile/` + `storage_state.json`.
5. `./manage.sh restart`

(Any VNC viewer works: TigerVNC, RealVNC, macOS Screen Sharing → `vnc://localhost:5900`.)

### On your laptop instead (then copy up)
```bash
PYTHONPATH=src ./.venv/bin/python login_browser.py   # opens a real browser
# log in, press Enter, then copy the results to the server:
scp -r browser_profile storage_state.json <you>@<server>:/data/briefer/
```

The profile/state are **secrets** (they're your logged-in session) and are
git-ignored. Delete `browser_profile/` + `storage_state.json` to log out.

---

## Option B — a cookies.txt file

Briefer also accepts a single `cookies.txt` shared by all three fetchers:
the headless browser (Playwright), `yt-dlp` (videos) and `gallery-dl` (image
posts). Provide it once and LinkedIn/Instagram/etc. become readable.

> ⚠️ **`cookies.txt` is a secret.** It contains your live session cookies —
> anyone with the file can act as you on those sites. Briefer git-ignores it
> and keeps it on the server only. Use a throwaway/secondary account if you
> can, and delete the file to revoke.

## 1. Export cookies from your browser

Install a reputable "Export cookies" extension, e.g.:
- **Get cookies.txt LOCALLY** (Chrome/Edge) — runs locally, no upload.
- **cookies.txt** (Firefox).

Log in to the site(s) you care about (LinkedIn, Instagram…), then use the
extension to **export in "Netscape" format**. You can export "All cookies" or
per-site; a combined file works for all of them.

## 2. Put it on the server

```bash
scp cookies.txt user@server:/data/briefer/cookies.txt
chmod 600 /data/briefer/cookies.txt
```

`COOKIES_FILE=cookies.txt` is already in `.env`. The path is relative to the
project dir (or use an absolute path). Then:

```bash
./manage.sh restart
```

On startup you'll see `Using cookies file for authenticated fetches: …` in the
logs. Now send a LinkedIn/Instagram link and Briefer renders it logged-in.

## 3. Refreshing

Session cookies expire (LinkedIn's `li_at` lasts weeks–months). If content
starts coming back empty again, re-export and re-copy the file.

## Notes & limits

- Cookies are scoped by domain by the browser, so they're only sent to the
  sites they belong to.
- Even with cookies, a site may rate-limit or challenge automated access;
  Briefer degrades gracefully (falls back to og:meta snippet or a note).
- If you don't want to use cookies, screenshots still work for any platform —
  send a screenshot and Briefer reads it with vision (and saves it to the
  sheet's Image column).
