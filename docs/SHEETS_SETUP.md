# Google Sheets setup

Briefer writes to two spreadsheets via a **service account** (no OAuth
browser flow, works headless on a server).

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
