# Accessing logged-in content (LinkedIn, Instagram, private X…)

Some platforms show **nothing** to a logged-out visitor — LinkedIn posts,
private Instagram, members-only pages. There is **no public API and no
installer that bypasses login**; the only reliable way to read that content
is to fetch it **as a logged-in user**, i.e. with your own browser cookies.

Briefer supports a single `cookies.txt` that is shared by all three fetchers:
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
