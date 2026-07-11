# Make Briefer your own 🎨

This repo ships configured for **Vivax** (medical AI), but nothing about
Briefer is Vivax-specific — the company identity is just a couple of
environment variables. Point it at **your** company, your topics, and your
own Google Sheets, and it becomes *your* intake analyst. Nothing here is
hard-coded; you don't touch the source.

Everything below lives in your `.env` (created by `./setup.sh`, or copy
`.env.example`). After changing it, run `./manage.sh reconfigure` then
`./manage.sh restart`.

---

## 1. Tell it who you are 🏢

Two variables drive the whole "why does this matter to us?" analysis:

```ini
COMPANY_NAME=Acme Robotics
COMPANY_FOCUS=Warehouse automation and autonomous mobile robots. We build
  fleet-coordination software and pick-and-place arms for e-commerce
  fulfilment centres.
```

- **`COMPANY_NAME`** — appears in the bot's replies and the "relevance" column.
- **`COMPANY_FOCUS`** — a short paragraph describing what you do. The model
  uses it to judge **why each item matters to you** and to suggest concrete
  **use-cases**. Be specific: products, customers, the problems you solve. The
  more precise this is, the sharper the "your angle" section becomes.

That's the whole rebrand. Send an article and the "Vivax angle" section
becomes the "Acme Robotics angle", reasoned against *your* focus.

> The two sheets are titled `Briefer — Articles` / `Briefer — Events` by
> default. Create your own two spreadsheets with any name you like and put
> their IDs in `ARTICLES_SHEET_ID` / `EVENTS_SHEET_ID`, or leave those blank
> and Briefer creates a fresh pair on first run and remembers them.

---

## 2. Pick your model & how careful it is 🧠

```ini
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-4-8               # the main analyst
ANTHROPIC_VERIFY_MODEL=claude-haiku-4-5-20251001   # the cheaper fact-checker
```

Use a smaller `ANTHROPIC_MODEL` to cut cost, or the same model for both if you want the
strongest possible verification. The verifier re-checks dates, numbers and
links against the source and flags anything it can't confirm.

---

## 3. Choose what it ingests 🎛️

Turn features on/off to match your server and your content:

```ini
ENABLE_TRANSCRIPTION=1     # videos & voice notes (yt-dlp + Whisper). 0 on tiny boxes
WHISPER_MODEL=base         # tiny|base|small|medium — bigger = better + slower
VIDEO_KEYFRAMES=4          # frames per video sent to the vision model (0 = off)
ENABLE_BROWSER_FALLBACK=1  # render JS-only pages (needs ./manage.sh enable-browser)
ENABLE_WEB_SEARCH=1        # enrich with verified web results
ENABLE_GALLERY_DL=1        # download image-only posts for the vision model
```

For logged-in sources (LinkedIn, Instagram, private X), add a `cookies.txt` —
see [`docs/COOKIES.md`](COOKIES.md).

---

## 4. Reminders & timezone ⏰

```ini
TIMEZONE=America/New_York
DEADLINE_REMINDER_HOURS=72,24,3   # when to poke before an event deadline
```

Users can also set ad-hoc reminders by replying `remind me in 3 days`, adding
`remind me <when>` to a message, or typing a date in the sheet's **Remind At**
column.

---

## 5. Who's allowed in 🔒

```ini
LOGIN_PASSWORD=                # blank → setup wizard generates a strong one
ALLOWED_CHAT_IDS=              # blank + BRIEFER_BOOTSTRAP=1 to discover yours
ADMIN_CHAT_IDS=
BRIEFER_BOOTSTRAP=1            # set to 0 once you've locked the allow-list
```

Start in bootstrap mode, message the bot `/whoami`, paste the id into
`ALLOWED_CHAT_IDS`, set `BRIEFER_BOOTSTRAP=0`, and restart. Add teammates
later with `/allow <chat_id>` from an admin chat — they still need the shared
`/login` password.

---

## 6. Make it a different kind of tracker 🔁

Because the "two agents" are just prompts + two sheets, the same machine works
for lots of intake jobs by only changing `COMPANY_FOCUS`. A few examples:

| You are… | Set `COMPANY_FOCUS` to… | You get… |
|---|---|---|
| A VC associate | your thesis & check size | deal/notes triage + demo-day deadlines |
| A grad student | your research area | paper summaries + CFP / conference deadlines |
| A hackathon team | your product idea | idea-relevant articles + hackathon deadlines |
| A community lead | your community's mission | event tracking + application deadlines |

Same bot, same sheets, same reminders — just described for **your** world.

---

### That's it

No code changes, no redeploy beyond `reconfigure` + `restart`. If you fork it
and build something on top, a link back is appreciated but not required — see
the [LICENSE](../LICENSE) (MIT).
