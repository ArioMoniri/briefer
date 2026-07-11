# Google Sheets setup

Briefer supports **two** ways to reach Google Sheets. Pick one in the wizard
(or via `GOOGLE_AUTH_MODE` in `.env`).

## Option 1 — Log in with your Google account (OAuth) — recommended

Sheets are created in **your own Drive** and you don't have to share anything.
The server has no browser, so login uses a **link**:

1. Google Cloud Console → **APIs & Services → Enable APIs** → enable **Google
   Sheets API** and **Google Drive API**.
2. **Credentials → Create credentials → OAuth client ID → Application type:
   Desktop app**. Download the JSON and save it on the server as
   `client_secret.json` (or set `GOOGLE_OAUTH_CLIENT_FILE`).
   - If your Google project is in "Testing" mode, add your Google address as a
     **Test user** on the OAuth consent screen.
3. Run the headless login:
   ```bash
   ./manage.sh google-auth
   ```
   It prints a **link**. Open it on your phone/laptop, sign in, approve. Google
   redirects to a `http://localhost/...` page that won't load — copy that
   whole address-bar URL (or just the `code=...`) and paste it back. This
   writes `token.json`; the bot auto-refreshes it forever.
4. Set `GOOGLE_AUTH_MODE=oauth` in `.env` (the wizard does this) and
   `./manage.sh restart`.

To log in with a **different** Google account later, just re-run
`./manage.sh google-auth`.

## Option 2 — Service account (a robot Google identity)

## 1. Create a service account
1. Go to <https://console.cloud.google.com/> → create/select a project.
2. **APIs & Services → Enable APIs** → enable **Google Sheets API** and
   **Google Drive API**.
3. **APIs & Services → Credentials → Create credentials → Service account**.
4. Open the service account → **Keys → Add key → JSON**. Download it.
5. Save the file into the repo as `service_account.json` (or set
   `GOOGLE_SERVICE_ACCOUNT_FILE` to its path). Keep it secret — it's
   git-ignored.

The JSON contains a `client_email` like
`briefer@your-project.iam.gserviceaccount.com`. You'll share your sheets
with that address.

## 2. Create the two spreadsheets

**Option A — let Briefer create them.** Leave `ARTICLES_SHEET_ID` and
`EVENTS_SHEET_ID` blank. On first run the bot creates both sheets and logs
their IDs. Copy the IDs into `.env` and share the sheets (below).

**Option B — create them yourself.** Make two blank Google Sheets. The ID is
the long string in the URL:
`https://docs.google.com/spreadsheets/d/`**`THIS_IS_THE_ID`**`/edit`.
Put them in `.env` as `ARTICLES_SHEET_ID` and `EVENTS_SHEET_ID`.

## 3. Share the sheets with the service account
For **each** sheet: **Share → add the `client_email` → Editor → Send**.
Without this the bot gets a `PermissionError` when it tries to append.

## 4. Verify
Start the bot and send it something. Run `/sheets` in Telegram to get the
direct links, and confirm a new row appears.

### Headers
Briefer writes headers automatically on first use:
- **Articles:** Captured At · Title · Type · Summary · Catch Points · Vivax
  Relevance · Vivax Use Cases · Entities · Tags · Links · Source · Verified ·
  Verification Notes · Confidence · Submitted By
- **Events:** Captured At · Title · Event Type · Summary · Organizer ·
  Location · Event Date · Application Deadline · Deadline (raw) · Eligibility ·
  Required Materials · Application Steps · Application URL · Cost · Catch
  Points · Vivax Relevance · Should Apply · Verified · Deadline Confidence ·
  Verification Notes · Source · Submitted By

Rows are only ever appended, so each sheet is a growing cumulative ledger.
