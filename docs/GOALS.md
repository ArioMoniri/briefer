# Goals → implementation traceability

Every requirement from the original brief, and where it lives.

| # | Goal | Status | Where |
|---|------|--------|-------|
| 1 | Telegram bot you forward things to | ✅ | `telegram_bot.py`, long-polling in `main.py` |
| 2 | **Agent A**: summarise articles/posts, put *catch ideas* into a sheet | ✅ | `analysis.analyze_article`, `sheets.append_article` |
| 3 | **Agent B**: same for events, **tracking deadlines** | ✅ | `analysis.analyze_event`, `sheets.append_event` |
| 4 | Summarise **everything**: links, files, images, repos | ✅ | `enrich.py` (HTML/PDF/text/image/GitHub) |
| 5 | Add a **catch point** to each item | ✅ | `catch_points` field, shown in the poke + sheet |
| 6 | Tie relevance to **getvivax.com** & company focus; suggest where we could use it | ✅ | `vivax_relevance`, `vivax_use_cases`; `COMPANY_*` env |
| 7 | For events / photos / Luma: find application details, deadlines, required info, criteria | ✅ | `analyze_event` (eligibility, required_materials, application_steps/url, cost), Luma detection in `enrich.py` |
| 8 | **Double-verify** to guard hallucinations | ✅ | `analysis.verify` (independent pass on a stronger model) + `apply_corrections` |
| 9 | **Two separate Google Sheets**, continuously **cumulative** | ✅ | `sheets.py` — append-only Articles + Events |
| 10 | You send from Telegram; bot **pokes you** with catches | ✅ | `_format_catch` reply after each item |
| 11 | Also poke before **deadlines** | ✅ | `_schedule_deadline` + `reminder_tick` (72/24/3h, configurable) |
| 12 | **Guide/help menu** on typing / commands | ✅ | `menus.py`, `/start` `/help` `/menu` + inline buttons |
| 13 | **Login password** for the bot | ✅ | `/login`, PBKDF2 hash, auto-deletes the message |
| 14 | **Allowed chat id** allow-list, not everyone | ✅ | `ALLOWED_CHAT_IDS`, silent drop otherwise |
| 15 | Server setup; **reset / stop / refresh** + **cron**; revive on reboot | ✅ | `manage.sh`, `deploy/briefer.service` (enabled), `deploy/healthcheck.sh` cron |
| 16 | Bot **downloads what it needs** itself | ✅ | `setup.sh` installs system + Python deps |
| 17 | **No injection / no server risk from the bot** | ✅ | no shell exec of input, SSRF guard, prompt-injection guard, systemd hardening — see `docs/SECURITY.md` |
| 18 | **A single `.sh`** to transfer + **self-healing** + **setup wizard** + writes needed `.env` | ✅ | `setup.sh` (wizard writes `.env`), self-healing via systemd+cron |
| 19 | Add whatever improves security / usability / features | ✅ | rate limiting, dedup, per-item confidence, verified badge, `/deadlines`, `/status`, `/sheets`, log redaction, reminders persisted across restarts |
| 20 | **Log in to Google** in the wizard, headless (no UI) via a link | ✅ | `authorize_google.py`, `./manage.sh google-auth`, `GOOGLE_AUTH_MODE=oauth`; also supports service account |
| 21 | Everything over **port 443** (odd ports like 993 may be blocked) | ✅ | outbound 443 only, no inbound; fetch restricted to 80/443; see README “Networking” |
| 22 | **Add other allowed chats later** | ✅ | `/allow` `/deny` `/allowlist` (admin), persisted in DB, no restart |
| 23 | **Cumulative append** of every new item | ✅ | append-only sheets (`sheets.py`) |
| 24 | Event **`.ics` calendar files** sent in Telegram, addable on Apple/Android | ✅ | `calendar_ics.py`, sent as a document + Google Calendar button |
| 25 | ICS **alarms**: day-of, 2h and 1h before the event | ✅ | 3 VALARMs per event (`build_event_ics`) |

## Runbook (server)
```bash
sudo ./setup.sh                 # install + wizard
./manage.sh status | logs
./manage.sh stop | refresh | reset | restart | update
```

## Not yet wired (nice future adds)
- Webhook mode (currently long-polling — simpler & needs no inbound port).
- Optional Notion/Airtable sinks alongside Sheets.
- Weekly digest of the week's catches.
