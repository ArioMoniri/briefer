# Changelog

All notable changes to Briefer are noted here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses simple
date-stamped entries rather than semantic version tags.

## [Unreleased]

### Added
- 📝 **`note:` descriptions** — a `note: …` line in a message/caption is filed
  into the row's **Notes** column (not analysed) and accumulates across re-sends.
- 🏷 **Tags dropdown** — the **My Tags** column offers a dropdown of every tag
  already in use (pick or type a new one), refreshed as tags change.
- 👥 **Row assignments** — a **People directory** (`/people`, `/name <id>
  <name>`, `/unname <id>`) maps names to chat ids. Type a name in a row's new
  **Assignee** column (dropdown of your people; free text like "pass it to
  John" also resolves) and that person gets a Telegram ping with **👀 Seen** /
  **✅ Mark checked** buttons. New **Assignee Done** checkbox (theirs) and
  **Seen** column (acknowledgement time) round-trip between Telegram and the
  sheet. (Telegram has no true read-receipts, so *Seen* = they tapped 👀.)
- 📅 **Real calendar view** — `/calendar` now draws a month grid built from the
  actual article deadlines and event dates (one marker each), with ◀ / Today /
  ▶ navigation, plus a **🌐 "Open full calendar" button** that sends a
  self-contained interactive HTML calendar (Month / Week / Day / Year / List).
- 🔎 **Build indicator** — the running git commit is shown in the startup log
  and in `/status`, so you can confirm exactly which code is live.
- 🖼 **Docs** — hand-drawn architecture + pipeline diagrams (`docs/diagrams/`)
  and a "Make it your own" guide (`docs/CUSTOMIZE.md`).
- 🧰 Repo housekeeping: `LICENSE`, `CONTRIBUTING.md`, issue / PR templates.

### Changed
- 🔗 Every "Saved / Updated" reply now links to the **exact spreadsheet and
  row** the bot wrote to, so a stale duplicate sheet can't be mistaken for
  empty.
- 🧮 Re-sending the same link no longer bloats the row — byte-identical
  re-sends short-circuit before any model call, and the cumulative merge
  de-duplicates re-worded bullets and never overwrites a good value (like a
  title) with a placeholder such as "Untitled".
- ⬆️ `manage.sh update` now force-syncs tracked files to `origin/main`, so a
  local edit on the server can no longer silently block an update.

## [2026-07] — Initial build

### Added
- Two-agent Telegram intake bot (Articles + Events) with summary, catch points,
  company-relevance angle, and event deadline / eligibility / how-to-apply.
- Independent **verification pass** to flag hallucinations.
- Two cumulative **Google Sheets** with colored Status tags, a Done checkbox,
  Notes / Tags / Remind-At columns, image embedding via Drive, and a Stats tab.
- **Deadline + custom reminders** and `.ics` calendar files for events.
- Ingestion of links, PDF / Word / PowerPoint / Excel, images (vision),
  audio / video (yt-dlp + Whisper), tweets/X threads, and GitHub repos.
- Durable **SQLite queue** with a single worker that resumes after a restart.
- Password + allow-list **auth**, SSRF filtering, nested-link safety gate,
  rate limiting, and a hardened systemd + cron **self-healing** deployment.
- One-command installer (`setup.sh`) with a setup wizard and `manage.sh`.
