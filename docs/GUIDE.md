# Briefer — Complete Guide 📖

Everything Briefer can do, how to talk to it, and how to run it. If you just
want to *use* the bot, read parts 1–6. If you *run* the server, read 7–10.

- [1. What Briefer is](#1-what-briefer-is)
- [2. Getting in (login & access)](#2-getting-in-login--access)
- [3. What you can send](#3-what-you-can-send-input-formats)
- [4. Inline directives (note / remind / many-at-once)](#4-inline-directives)
- [5. The two sheets & every column](#5-the-two-sheets--every-column)
- [6. Reminders & the calendar](#6-reminders--the-calendar)
- [7. Assigning rows to people](#7-assigning-rows-to-people)
- [8. Full command reference](#8-full-command-reference)
- [9. Running the server (manage.sh)](#9-running-the-server-managesh)
- [10. Configuration (.env)](#10-configuration-env)
- [11. Optional features](#11-optional-features)
- [12. Security model](#12-security-model)
- [13. Troubleshooting & FAQ](#13-troubleshooting--faq)

---

## 1. What Briefer is

Forward or send Briefer **anything** — an article, a post, a link, a PDF, an
image, a video, a tweet, a GitHub repo, or an event/Luma page. It:

1. **Summarises** it and pulls the **catch points**.
2. Explains **where your company could use it** (set once via `COMPANY_FOCUS`).
3. For **events**: extracts the **deadline, eligibility, materials & how to apply**.
4. **Double-checks its own work** for hallucinations (dates, numbers, links).
5. Appends it to one of **two cumulative Google Sheets** (Articles / Events).
6. **Pokes you on Telegram** with the catch, and again **before deadlines**.

Everything is queued and processed **one item at a time**, survives restarts,
and never runs your content through a shell.

---

## 2. Getting in (login & access)

Two gates protect the bot:

1. **Allow-list** — only chat ids in `ALLOWED_CHAT_IDS` (or added at runtime
   with `/allow`) are served. Everyone else is silently ignored.
2. **Password** — an allow-listed chat must `/login <password>` once per session.

**First time:**
1. Message the bot `/whoami` → it replies with your chat id.
2. Add that id to `ALLOWED_CHAT_IDS` (or the admin runs `/allow <id>`).
3. `/login <password>` (the shared password set at install).

`/login` messages are auto-deleted; only a PBKDF2 hash is kept in memory.
`/logout` ends the session.

---

## 3. What you can send (input formats)

| You send… | Briefer does… |
|---|---|
| **A link** | fetches & reads the page (SSRF-filtered) |
| **Plain text** | analyses it directly |
| **PDF** | extracts the text |
| **Word `.docx` / PowerPoint `.pptx` / Excel `.xlsx`** | extracts text/tables |
| **`.txt` / `.md` / `.csv` / `.json`** | reads it |
| **Image / screenshot** | reads it with vision (great for IG/LinkedIn text) |
| **Video / voice note** | transcribes it (Whisper) + reads a few keyframes |
| **YouTube / Vimeo / TikTok / IG / FB / X video link** | downloads & transcribes |
| **Tweet / X status link** | the post + the tweet it replies to + any quoted/RT original + media |
| **GitHub repo link** | reads the README + metadata |
| **Luma / event page** | pulls dates, criteria, how to apply |

Tips:
- For **Instagram / LinkedIn text** (no public API), send a **screenshot** or
  **paste the text** — works everywhere, no login needed.
- For **logged-in** content, add a `cookies.txt` (see [11](#11-optional-features)).
- Auto-detection routes each item to Articles or Events. Override with
  `/article <text>` or `/event <text>`, or the buttons in `/menu`.

---

## 4. Inline directives

Add these to the message (or a file/photo **caption**) you send:

### 📝 `note:` — file a description into the Notes column
```
https://example.com/paper
note: follow up with the CFO before the board meeting
```
The note goes to that row's **Notes** column (not analysed) and **accumulates**
across re-sends. Aliases: `notes:`, `desc:`, `description:`.

### ⏰ `remind me …` — set a reminder
```
https://lu.ma/some-event
remind me in 3 days
```
Also accepts dates: `remind me 2026-08-01 18:00`. You can also **reply** to any
message with `remind me <when>`, or type a date in the sheet's **Remind At**
column. Events already auto-remind before the deadline and the event date.

### 📎 Send many at once
Paste **several links** or a big block — Briefer splits them into separate
items, queues them, and replies under each. Nothing is dropped; the queue
resumes after a restart.

---

## 5. The two sheets & every column

Auto-created (or set `ARTICLES_SHEET_ID` / `EVENTS_SHEET_ID`). `/sheets` links
them; `/status` shows their ids.

**Articles** columns: Captured At · Title · Type · Summary · Catch Points ·
[Company] Relevance · [Company] Use Cases · Entities · Tags · Links · Source ·
Verified · Verification Notes · Confidence · Submitted By.

**Events** columns: Captured At · Title · Event Type · Summary · Organizer ·
Location · Event Date · Application Deadline · Deadline (raw) · Eligibility ·
Required Materials · Application Steps · Application URL · Cost · Catch Points ·
[Company] Relevance · Should Apply · Verified · Deadline Confidence ·
Verification Notes · Source · Submitted By.

**Control columns (both sheets), which YOU interact with:**

| Column | What it does |
|---|---|
| **Image** | first attached image, embedded via `=IMAGE()` |
| **ID** | the row's stable id (don't edit) |
| **Done** ☑️ | tick when handled → reminders stop, check-time recorded; un-tick resets |
| **Checked At / Time→Check (h)** | when you ticked Done, and how long it took |
| **Notes** | your description (from `note:`), or type your own; preserved on re-send |
| **My Tags** | your tags — a **dropdown** of tags in use, or type a new one |
| **Remind At** | type a date → a reminder is scheduled; change/clear syncs in ~60s |
| **Status** | colored live tag: 🔴 Passed · 🟠 Due soon · 🟡 Coming up · 🟢 Upcoming/New · ✅ Done · ⚪ No date |
| **Assignee** | pick a person (dropdown) → they're pinged (see [7](#7-assigning-rows-to-people)) |
| **Assignee Done** ☑️ | the assignee's checkbox |
| **Seen** | when the assignee acknowledged the ping |

**Behaviour to know:**
- **Delete a row** → Briefer notices, archives a copy to a **Deleted** tab
  (recoverable), cancels its reminders, and never reminds again.
- **Re-send the same item** → the **analysed data columns refresh** cumulatively
  (nothing lost, re-worded duplicates merged), while **Notes / My Tags /
  Assignee** are preserved.
- **New rows fill from the top** and skip any row you filled in by hand.
- A **Stats** tab per sheet shows totals, done %, overdue, and avg time-to-check
  (`/stats` shows the same in chat).

---

## 6. Reminders & the calendar

**Ways to set a reminder:**
- Reply to a message with `remind me in 2 days`.
- Add `remind me <when>` to a submission.
- Type a date in the sheet's **Remind At** column (edits/clears sync live).
- Events auto-remind at `DEADLINE_REMINDER_HOURS` (default **72, 24, 3** hours)
  before both the **deadline** and the **event date**.

**`/calendar`** — a month grid of all your **deadlines** ⏰ and **event dates**
📅 plus your own **reminders** 📌 (the auto 72/24/3h pokes are hidden), with
◀ Today ▶ navigation and a **🌐 Open full calendar** button that sends a
self-contained **HTML** file with Month / Week / Day / Year / List views.
(Open it in a real browser — macOS Quick Look blocks its JavaScript.)

**`.ics` files** — every event also arrives as a `.ics` document: open on
iPhone/Android and tap *Add to Calendar* (with day-of + 2h/1h alarms and a
Google Calendar button).

---

## 7. Assigning rows to people

Hand a row to a teammate and Briefer pings them.

1. **Map people once** (admin): `/name <chat_id> <name>` — e.g.
   `/name 123456789 John`. They must have **/start**-ed the bot first.
   `/people` lists everyone; `/unname <chat_id>` removes.
2. **Assign a row**: in the **Assignee** column pick the name from the dropdown
   (or just type it — even *"pass it to John"* resolves to John).
3. **They get pinged** on Telegram with **👀 Seen** and **✅ Mark checked**
   buttons and a link to the row.
4. **Tracking**: the **Seen** column records when they tapped 👀; **Assignee
   Done** is their checkbox (tickable from the sheet or the ✅ button). An
   unknown name is flagged in the Seen column so you know to map them.

> Telegram bots can't get true read-receipts, so **"Seen" means they tapped 👀**
> — the closest reliable signal.

---

## 8. Full command reference

**Everyone (after login):**

| Command | Does |
|---|---|
| `/start` | welcome + menu |
| `/help` | the in-chat guide |
| `/menu` | quick action buttons |
| `/article <text>` | force-file as an article |
| `/event <text>` | force-file as an event |
| `/sheets` | links to both Google Sheets |
| `/deadlines` | upcoming deadline reminders |
| `/calendar` (`/reminders`) | month calendar + HTML export |
| `/stats` | totals, done %, overdue, time-to-check |
| `/cookies` (`/cookie`) | login freshness + expiry warnings |
| `/status` | bot health, build, queue, sheet links |
| `/people` | list assignable people |
| `/name <id> <name>` | map/rename a person |
| `/unname <id>` | remove a person |
| `/login <pw>` | authenticate this chat |
| `/logout` | end this chat's session |
| `/id` · `/whoami` | show your chat id (no login needed) |
| `/cancel` | cancel the current action |

**Admins (`ADMIN_CHAT_IDS`):**

| Command | Does |
|---|---|
| `/allow <chat_id>` | let another chat use the bot (no restart) |
| `/deny <chat_id>` | revoke a runtime-added chat |
| `/allowlist` | show who can access the bot |
| `/logs` | recent logs / errors |

Type `/` in Telegram to see the list pop up.

---

## 9. Running the server (manage.sh)

From the install directory (e.g. `/data/briefer` or `/opt/briefer`):

```bash
./manage.sh <command>
```

| Command | Does |
|---|---|
| `start` / `stop` / `restart` | run control (systemd → tmux → nohup, auto-picked) |
| `status` | is it running? |
| `refresh` | restart with current code & `.env` |
| `reset` | wipe runtime state (sessions, dedup, reminders); keeps `.env` |
| `update` | force-sync to `origin/main`, reinstall deps, re-run config, restart |
| `logs` | live logs (follow) |
| `errors` | recent errors/warnings |
| `attach` | attach to the tmux session |
| `foreground` (`fg`) | run in the foreground |
| `google-auth` (`gauth`) | headless Google OAuth (prints a link) |
| `reconfigure` (`configure`) | fill in any missing/new `.env` settings |
| `enable-browser` | install the Playwright/Chromium fallback |
| `browser-login` | log in to LinkedIn/IG via a VNC-shared headless browser |

**Updating to the latest code:** `./manage.sh update`, then check `/status`
shows the new build hash.

**First install:** `sudo ./setup.sh` runs an interactive wizard (token, keys,
allow-list, password, company focus, Google auth, optional sheet ids) and sets
up a hardened auto-restarting service + cron watchdog.

---

## 10. Configuration (.env)

Set at install or edit `.env` then `./manage.sh reconfigure && ./manage.sh restart`.
Full "make it yours" walkthrough: [`CUSTOMIZE.md`](CUSTOMIZE.md).

**Core**
- `TELEGRAM_BOT_TOKEN` · `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL` (main analyst) · `ANTHROPIC_VERIFY_MODEL` (fact-checker)
- `COMPANY_NAME` · `COMPANY_URL` · `COMPANY_FOCUS` — tailors the relevance analysis
- `TIMEZONE` · `DATA_DIR` · `LOG_LEVEL`

**Access**
- `ALLOWED_CHAT_IDS` · `ADMIN_CHAT_IDS` · `LOGIN_PASSWORD` · `BRIEFER_BOOTSTRAP`

**Google Sheets**
- `GOOGLE_AUTH_MODE` (`oauth` | `service_account`)
- `GOOGLE_OAUTH_CLIENT_FILE` · `GOOGLE_TOKEN_FILE` · `GOOGLE_SERVICE_ACCOUNT_FILE`
- `ARTICLES_SHEET_ID` · `EVENTS_SHEET_ID` (blank → auto-create + remember)

**Reminders** — `DEADLINE_REMINDER_HOURS` (e.g. `72,24,3`)

**Media** — `ENABLE_TRANSCRIPTION` · `WHISPER_MODEL` (`tiny|base|small|medium`) ·
`TRANSCRIPTION_MAX_SECONDS` · `MEDIA_MAX_BYTES` · `VIDEO_KEYFRAMES` · `ENABLE_GALLERY_DL`

**Browser fallback** — `ENABLE_BROWSER_FALLBACK` · `BROWSER_PROFILE_DIR` ·
`BROWSER_STORAGE_STATE` · `COOKIES_FILE`

**Nested-link safety** — `FOLLOW_NESTED_LINKS` · `MAX_NESTED_LINKS` ·
`ENABLE_LINK_GUARD` · `LINK_GUARD_MODEL` · `GOOGLE_SAFE_BROWSING_KEY`

**Web search** — `ENABLE_WEB_SEARCH` · `WEB_SEARCH_PROVIDER` (`ddg|brave|serpapi`) ·
`WEB_SEARCH_API_KEY` · `WEB_SEARCH_MAX_RESULTS` · `WEB_SEARCH_ONLY_IF_APPLY_MISSING`

**X/Twitter (optional)** — `TWITTER_BEARER_TOKEN`, or `TWITTER_CONSUMER_KEY` +
`TWITTER_CONSUMER_SECRET` (a no-auth fallback reads tweets without any keys).

**Limits** — `MAX_DOWNLOAD_BYTES` · `RATE_LIMIT_PER_MINUTE`

---

## 11. Optional features

**Transcription (videos & audio)** — on by default; needs no system packages
(ffmpeg + Whisper are pip-installed in the venv). Set `ENABLE_TRANSCRIPTION=0`
on tiny servers.

**Browser fallback** — for JS-only pages (LinkedIn, SPAs):
```bash
./manage.sh enable-browser
```
Activates automatically only when a plain fetch returns too little text.

**Logged-in content (LinkedIn / Instagram / private X)** — two options:
1. Export a `cookies.txt` from your logged-in browser, point `COOKIES_FILE` at
   it (used by the browser, `yt-dlp`, and `gallery-dl`).
2. `./manage.sh browser-login` — log in through a VNC-shared headless browser;
   the session is saved to a persistent profile the bot reuses.

`/cookies` reports freshness and warns before anything expires.

**Web search enrichment** — `ENABLE_WEB_SEARCH=1`. The default `ddg` provider
needs **no API key**; `brave`/`serpapi` do. Results are verified to be the same
item before anything is added.

**Nested-link safety** — links *inside* a post are safety-checked (heuristics +
a guard model, and Google Safe Browsing if `GOOGLE_SAFE_BROWSING_KEY` is set)
before Briefer ever opens them.

---

## 12. Security model

- **Access control** — allow-list + shared-password login; `/login` auto-deleted.
- **No command execution** — your content never touches a shell; no `eval`/`exec`.
- **Prompt-injection guard** — forwarded content is treated as *data*, not
  instructions; injection attempts are flagged.
- **SSRF protection** — every URL is resolved and rejected if it points at a
  loopback/private/link-local/cloud-metadata address, before and after
  redirects. Only `http(s)`, size-capped downloads, ports 80/443.
- **Rate limiting** per chat; input length/size caps.
- **Least privilege** — dedicated non-login user under a hardened systemd unit.
- **Secrets** live in a `600` `.env`, are git-ignored, and are redacted from logs.

Details: [`SECURITY.md`](SECURITY.md).

---

## 13. Troubleshooting & FAQ

**"Updated existing entry" but my sheet is empty.** You're likely looking at a
different (duplicate) sheet. Every reply links the exact spreadsheet + row —
click it, or use `/sheets`. Then delete stray duplicate sheets from Drive.

**New rows appear far down (row 900+).** Historical junk rows from before
dedup. Delete the empty rows once; new content fills from the top and skips
your manual rows.

**The calendar HTML is blank.** You opened it in **Quick Look**, which disables
JavaScript. It still shows a static list there; for the interactive views,
right-click → **Open With → Safari/Chrome**.

**A README diagram looks old.** GitHub caches images; hard-refresh
(Cmd/Ctrl+Shift+R).

**An assignee didn't get pinged.** They must have **/start**-ed the bot, and
their name must be mapped (`/people` → `/name`). An unmapped name is flagged in
the row's **Seen** column.

**Is my code actually deployed?** `/status` shows the running **build** hash.
Run `./manage.sh update` and confirm it changed.

**Something failed.** `/logs` (admin) in chat, or `./manage.sh errors` on the
server. Anthropic credit/rate/auth problems are classified and surfaced.

---

*Set it up for your own company in minutes → [`CUSTOMIZE.md`](CUSTOMIZE.md).*
