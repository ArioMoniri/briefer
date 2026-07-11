"""Google Sheets output: two cumulative spreadsheets (Articles, Events).

Rows are only ever appended, so both sheets are a growing ledger. Headers
are created on first use. If sheet IDs are not configured, the sheets are
created and their IDs are logged for the operator to paste into .env.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import gspread
from google.oauth2.service_account import Credentials as SACredentials

log = logging.getLogger("briefer.sheets")

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    # drive.file (not full drive): only files this app creates/opens, so a
    # stolen token can't read the owner's whole Drive.
    "https://www.googleapis.com/auth/drive.file",
]


def build_gspread_client(auth_mode: str, service_account_file: str,
                         token_file: str):
    """Return an authorized gspread client for either auth mode.

    - service_account: uses the service-account JSON key.
    - oauth: uses a previously-authorized user token (token.json) that the
      `google-auth` layer auto-refreshes; created by authorize_google.py.
    """
    if auth_mode == "oauth":
        from google.oauth2.credentials import Credentials as UserCredentials
        from google.auth.transport.requests import Request

        creds = UserCredentials.from_authorized_user_file(token_file, _SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
        return gspread.authorize(creds), creds
    creds = SACredentials.from_service_account_file(
        service_account_file, scopes=_SCOPES)
    return gspread.authorize(creds), creds


class DriveUploader:
    """Uploads image bytes to a 'Briefer Images' Drive folder (drive.file
    scope) and returns a public view URL usable in an =IMAGE() cell.

    Best-effort: any failure returns None so a row still gets written.
    """

    _FOLDER = "Briefer Images"

    def __init__(self, creds) -> None:
        from google.auth.transport.requests import AuthorizedSession

        self._session = AuthorizedSession(creds)
        self._folder_id: str | None = None

    def _ensure_folder(self) -> str | None:
        if self._folder_id:
            return self._folder_id
        try:
            q = (f"mimeType='application/vnd.google-apps.folder' and "
                 f"name='{self._FOLDER}' and trashed=false")
            r = self._session.get(
                "https://www.googleapis.com/drive/v3/files"
                f"?q={quote(q)}&fields=files(id)&pageSize=1", timeout=20)
            files = r.json().get("files", []) if r.ok else []
            if files:
                self._folder_id = files[0]["id"]
            else:
                r = self._session.post(
                    "https://www.googleapis.com/drive/v3/files?fields=id",
                    json={"name": self._FOLDER,
                          "mimeType": "application/vnd.google-apps.folder"},
                    timeout=20)
                self._folder_id = r.json().get("id") if r.ok else None
        except Exception as exc:  # noqa: BLE001
            log.warning("Drive folder ensure failed: %s", exc)
            self._folder_id = None
        return self._folder_id

    def upload_image(self, data: bytes, mime: str, name: str) -> str | None:
        try:
            folder = self._ensure_folder()
            r = self._session.post(
                "https://www.googleapis.com/upload/drive/v3/files"
                "?uploadType=media&fields=id",
                headers={"Content-Type": mime or "image/jpeg"},
                data=data, timeout=60)
            if not r.ok:
                log.warning("Drive upload failed: %s", r.text[:200])
                return None
            file_id = r.json().get("id")
            if not file_id:
                return None
            meta = {"name": name}
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=id"
            if folder:
                url += f"&addParents={folder}"
            self._session.patch(url, json=meta, timeout=20)
            # Make it viewable so =IMAGE() can render it.
            self._session.post(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                json={"role": "reader", "type": "anyone"}, timeout=20)
            return f"https://drive.google.com/uc?export=view&id={file_id}"
        except Exception as exc:  # noqa: BLE001
            log.warning("Drive image upload failed: %s", exc)
            return None


def _col_letter(n: int) -> str:
    """1-indexed column number → spreadsheet letter (1→A, 27→AA)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _appended_row_number(resp: Any) -> int | None:
    try:
        rng = resp["updates"]["updatedRange"]  # e.g. "Articles!A5:P5"
        cell = rng.split("!", 1)[1].split(":", 1)[0]  # "A5"
        digits = "".join(ch for ch in cell if ch.isdigit())
        return int(digits) if digits else None
    except Exception:  # noqa: BLE001
        return None

# Trailing (non-data) columns, in order. The bot writes Image/ID/Done/Checked
# At/Time; NOTES and MY TAGS are yours to fill and are never overwritten (a
# cumulative re-submission only rewrites the data columns).
_TRAILING = ["Image", "ID", "Done", "Checked At", "Time→Check (h)",
             "Notes", "My Tags", "Remind At", "Status"]

_ARTICLE_DATA = [
    "Captured At", "Title", "Type", "Summary", "Catch Points",
    "Vivax Relevance", "Vivax Use Cases", "Entities", "Tags", "Links",
    "Source", "Verified", "Verification Notes", "Confidence", "Submitted By",
]
_EVENT_DATA = [
    "Captured At", "Title", "Event Type", "Summary", "Organizer", "Location",
    "Event Date", "Application Deadline", "Deadline (raw)", "Eligibility",
    "Required Materials", "Application Steps", "Application URL", "Cost",
    "Catch Points", "Vivax Relevance", "Should Apply", "Verified",
    "Deadline Confidence", "Verification Notes", "Source", "Submitted By",
]
ARTICLE_HEADERS = _ARTICLE_DATA + _TRAILING
EVENT_HEADERS = _EVENT_DATA + _TRAILING


def _control_cols(headers: list[str]) -> dict[str, int]:
    """1-based column numbers of the trailing columns, looked up by name."""
    def col(name: str) -> int:
        return headers.index(name) + 1
    return {
        "image": col("Image"), "id": col("ID"), "done": col("Done"),
        "checked_at": col("Checked At"), "time": col("Time→Check (h)"),
        "notes": col("Notes"), "tags": col("My Tags"),
        "remind_at": col("Remind At"), "status": col("Status"),
    }


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(f"• {str(v)}" for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class SheetsClient:
    def __init__(self, auth_mode: str, service_account_file: str,
                 token_file: str, articles_id: str, events_id: str) -> None:
        self._gc, self._creds = build_gspread_client(
            auth_mode, service_account_file, token_file)
        try:
            self._drive: DriveUploader | None = DriveUploader(self._creds)
        except Exception as exc:  # noqa: BLE001
            log.warning("Drive uploader unavailable (images won't be saved): %s", exc)
            self._drive = None
        self._articles = self._open_or_create(articles_id, "Briefer — Articles",
                                              ARTICLE_HEADERS)
        self._events = self._open_or_create(events_id, "Briefer — Events",
                                            EVENT_HEADERS)

    def _save_image(self, worksheet: gspread.Worksheet, row: int | None,
                    col: int, images: list[tuple[bytes, str]] | None) -> None:
        """Upload the first image and drop an =IMAGE() formula in its cell.

        The row's data is written RAW (untrusted). This is a SEPARATE,
        app-controlled formula write, so no untrusted content becomes a
        formula."""
        if not images or not row or not self._drive:
            return
        data, mime = images[0]
        url = self._drive.upload_image(data, mime, f"briefer_{row}.jpg")
        if not url:
            return
        cell = f"{_col_letter(col)}{row}"
        try:
            worksheet.update([[f'=IMAGE("{url}")']], cell,
                             value_input_option="USER_ENTERED")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set image cell: %s", exc)

    @property
    def articles_id(self) -> str:
        return self._articles.spreadsheet.id

    @property
    def events_id(self) -> str:
        return self._events.spreadsheet.id

    def _open_or_create(self, sheet_id: str, title: str,
                        headers: list[str]) -> gspread.Worksheet:
        if sheet_id:
            ss = self._gc.open_by_key(sheet_id)
        else:
            ss = self._gc.create(title)
            log.warning(
                "Created new spreadsheet '%s' with id=%s — add this id to .env "
                "and share it with the service account.", title, ss.id
            )
        ws = ss.sheet1
        existing = ws.row_values(1)
        if existing != headers:
            if not existing or not any(existing):
                ws.update([headers], "A1")
            elif headers[:len(existing)] == existing:
                # Older sheet with fewer columns — extend the header row in
                # place (we only ever append columns, never reorder).
                ws.update([headers], "A1")
                log.info("Extended '%s' header row with new columns.", title)
            else:
                log.info("Sheet '%s' has custom headers; appending by column order.",
                         title)
        ws.freeze(rows=1)
        return ws

    # --- data-row builders (base columns only, no control columns) ---
    @staticmethod
    def _article_data(a: dict[str, Any], source: str, user: str) -> list[str]:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        issues = a.get("_verification_issues") or []
        return [
            now, _fmt(a.get("title")), "Article", _fmt(a.get("summary")),
            _fmt(a.get("catch_points")), _fmt(a.get("vivax_relevance")),
            _fmt(a.get("vivax_use_cases")), _fmt(a.get("entities")),
            _fmt(a.get("tags")), _fmt(a.get("links")), _fmt(source),
            "✅" if a.get("_verified") else "⚠️",
            _fmt([f"{i.get('field')}: {i.get('problem')}" for i in issues]),
            _fmt(a.get("confidence")), _fmt(user),
        ]

    @staticmethod
    def _event_data(e: dict[str, Any], source: str, user: str) -> list[str]:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        issues = e.get("_verification_issues") or []
        return [
            now, _fmt(e.get("title")), _fmt(e.get("event_type")),
            _fmt(e.get("summary")), _fmt(e.get("organizer")),
            _fmt(e.get("location")), _fmt(e.get("event_date")),
            _fmt(e.get("application_deadline")), _fmt(e.get("deadline_raw")),
            _fmt(e.get("eligibility")), _fmt(e.get("required_materials")),
            _fmt(e.get("application_steps")), _fmt(e.get("application_url")),
            _fmt(e.get("cost")), _fmt(e.get("catch_points")),
            _fmt(e.get("vivax_relevance")), _fmt(e.get("should_apply")),
            "✅" if e.get("_verified") else "⚠️",
            _fmt(e.get("_deadline_confidence")),
            _fmt([f"{i.get('field')}: {i.get('problem')}" for i in issues]),
            _fmt(source), _fmt(user),
        ]

    def worksheet(self, sheet: str) -> gspread.Worksheet:
        return self._events if sheet == "event" else self._articles

    def _append(self, sheet: str, data: list[str], entry_id: str,
                images: list[tuple[bytes, str]] | None,
                status: str = "") -> int | None:
        ws = self.worksheet(sheet)
        headers = EVENT_HEADERS if sheet == "event" else ARTICLE_HEADERS
        cols = _control_cols(headers)
        # Trailing: Image, ID, Done, Checked At, Time, Notes, My Tags, Remind
        # At, Status. Done left blank; _make_checkbox sets a real boolean.
        row = data + ["", entry_id, "", "", "", "", "", "", status]
        # RAW: untrusted content is never parsed as a formula.
        resp = ws.append_row(row, value_input_option="RAW")
        rownum = _appended_row_number(resp)
        self._save_image(ws, rownum, cols["image"], images)
        if rownum:
            self._make_checkbox(ws, rownum, cols["done"])
        return rownum

    def append_article(self, a, source, user, images=None, entry_id="",
                       status="") -> int | None:
        return self._append("article", self._article_data(a, source, user),
                            entry_id, images, status)

    def append_event(self, e, source, user, images=None, entry_id="",
                     status="") -> int | None:
        return self._append("event", self._event_data(e, source, user),
                            entry_id, images, status)

    def write_status(self, sheet: str, rownum: int, tag: str) -> None:
        ws = self.worksheet(sheet)
        headers = EVENT_HEADERS if sheet == "event" else ARTICLE_HEADERS
        col = _col_letter(_control_cols(headers)["status"])
        try:
            ws.update([[tag]], f"{col}{rownum}", value_input_option="RAW")
        except Exception as exc:  # noqa: BLE001
            log.warning("write_status failed: %s", exc)

    def _make_checkbox(self, ws: gspread.Worksheet, row: int, col: int) -> None:
        grid = {"sheetId": ws.id,
                "startRowIndex": row - 1, "endRowIndex": row,
                "startColumnIndex": col - 1, "endColumnIndex": col}
        try:
            ws.spreadsheet.batch_update({"requests": [
                # 1) write a real boolean FALSE (not the text "FALSE", which
                #    would violate the checkbox rule → "Invalid" error).
                {"repeatCell": {
                    "range": grid,
                    "cell": {"userEnteredValue": {"boolValue": False}},
                    "fields": "userEnteredValue"}},
                # 2) render it as a checkbox. strict=False so a stray value
                #    (e.g. an old text 'FALSE') never triggers a blocking
                #    "Invalid" popup — it just shows the checkbox.
                {"setDataValidation": {
                    "range": grid,
                    "rule": {"condition": {"type": "BOOLEAN"},
                             "showCustomUi": True, "strict": False}}},
            ]})
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set checkbox: %s", exc)

    # --- cumulative update -------------------------------------------
    def find_row_by_id(self, sheet: str, entry_id: str) -> int | None:
        ws = self.worksheet(sheet)
        headers = EVENT_HEADERS if sheet == "event" else ARTICLE_HEADERS
        col = _control_cols(headers)["id"]
        try:
            ids = ws.col_values(col)
        except Exception as exc:  # noqa: BLE001
            log.warning("find_row_by_id read failed: %s", exc)
            return None
        for i, val in enumerate(ids[1:], start=2):  # skip header
            if val == entry_id:
                return i
        return None

    def update_data_row(self, sheet: str, rownum: int, merged: dict[str, Any],
                        source: str, user: str,
                        images: list[tuple[bytes, str]] | None = None) -> None:
        """Rewrite only the DATA columns of an existing row (cumulative merge).
        Control columns (ID/Done/Checked At/Time) are left untouched."""
        ws = self.worksheet(sheet)
        if sheet == "event":
            data = self._event_data(merged, source, user)
        else:
            data = self._article_data(merged, source, user)
        last = _col_letter(len(data))
        try:
            ws.update([data], f"A{rownum}:{last}{rownum}", value_input_option="RAW")
        except Exception as exc:  # noqa: BLE001
            log.warning("update_data_row failed: %s", exc)
        headers = EVENT_HEADERS if sheet == "event" else ARTICLE_HEADERS
        if images:
            self._save_image(ws, rownum, _control_cols(headers)["image"], images)

    def write_stats(self, sheet: str, stats: dict[str, Any]) -> None:
        """Maintain a 'Stats' tab (per spreadsheet) with counts + averages."""
        ss = self.worksheet(sheet).spreadsheet
        try:
            try:
                st = ss.worksheet("Stats")
            except gspread.WorksheetNotFound:
                st = ss.add_worksheet(title="Stats", rows=20, cols=3)
            when = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            rows = [
                ["Metric", "Value"],
                ["Total entries", stats.get("total", 0)],
                ["Checked (done)", stats.get("checked", 0)],
                ["Removed from sheet", stats.get("removed", 0)],
                ["Avg time to check (hours)", stats.get("avg_check_hours", 0)],
                ["Updated at", when],
            ]
            st.update(rows, "A1", value_input_option="RAW")
        except Exception as exc:  # noqa: BLE001
            log.warning("write_stats failed: %s", exc)

    def write_check_cells(self, sheet: str, rownum: int,
                          checked_at_iso: str, hours: Any) -> None:
        ws = self.worksheet(sheet)
        headers = EVENT_HEADERS if sheet == "event" else ARTICLE_HEADERS
        cols = _control_cols(headers)
        rng = f"{_col_letter(cols['checked_at'])}{rownum}:{_col_letter(cols['time'])}{rownum}"
        try:
            ws.update([[checked_at_iso, hours]], rng, value_input_option="RAW")
        except Exception as exc:  # noqa: BLE001
            log.warning("write_check_cells failed: %s", exc)
