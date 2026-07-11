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

ARTICLE_HEADERS = [
    "Captured At", "Title", "Type", "Summary", "Catch Points",
    "Vivax Relevance", "Vivax Use Cases", "Entities", "Tags", "Links",
    "Source", "Verified", "Verification Notes", "Confidence", "Submitted By",
    "Image",
]

EVENT_HEADERS = [
    "Captured At", "Title", "Event Type", "Summary", "Organizer", "Location",
    "Event Date", "Application Deadline", "Deadline (raw)", "Eligibility",
    "Required Materials", "Application Steps", "Application URL", "Cost",
    "Catch Points", "Vivax Relevance", "Should Apply", "Verified",
    "Deadline Confidence", "Verification Notes", "Source", "Submitted By",
    "Image",
]


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
            if not existing:
                ws.update([headers], "A1")
            elif not any(existing):
                ws.update([headers], "A1")
            # If headers exist but differ, leave them; append still works by
            # position. We log so the operator can reconcile.
            if existing and existing != headers:
                log.info("Sheet '%s' already has headers; appending by column order.",
                         title)
        ws.freeze(rows=1)
        return ws

    def append_article(self, a: dict[str, Any], source: str, user: str,
                       images: list[tuple[bytes, str]] | None = None) -> int | None:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        issues = a.get("_verification_issues") or []
        row = [
            now,
            _fmt(a.get("title")),
            "Article",
            _fmt(a.get("summary")),
            _fmt(a.get("catch_points")),
            _fmt(a.get("vivax_relevance")),
            _fmt(a.get("vivax_use_cases")),
            _fmt(a.get("entities")),
            _fmt(a.get("tags")),
            _fmt(a.get("links")),
            _fmt(source),
            "✅" if a.get("_verified") else "⚠️",
            _fmt([f"{i.get('field')}: {i.get('problem')}" for i in issues]),
            _fmt(a.get("confidence")),
            _fmt(user),
            "",  # Image (filled separately, as a controlled formula)
        ]
        # RAW (not USER_ENTERED): untrusted content must never be parsed as a
        # formula (=IMPORTXML/HYPERLINK exfiltration). RAW stores it verbatim.
        resp = self._articles.append_row(row, value_input_option="RAW")
        rownum = _appended_row_number(resp)
        self._save_image(self._articles, rownum, len(ARTICLE_HEADERS), images)
        return rownum

    def append_event(self, e: dict[str, Any], source: str, user: str,
                     images: list[tuple[bytes, str]] | None = None) -> int | None:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        issues = e.get("_verification_issues") or []
        row = [
            now,
            _fmt(e.get("title")),
            _fmt(e.get("event_type")),
            _fmt(e.get("summary")),
            _fmt(e.get("organizer")),
            _fmt(e.get("location")),
            _fmt(e.get("event_date")),
            _fmt(e.get("application_deadline")),
            _fmt(e.get("deadline_raw")),
            _fmt(e.get("eligibility")),
            _fmt(e.get("required_materials")),
            _fmt(e.get("application_steps")),
            _fmt(e.get("application_url")),
            _fmt(e.get("cost")),
            _fmt(e.get("catch_points")),
            _fmt(e.get("vivax_relevance")),
            _fmt(e.get("should_apply")),
            "✅" if e.get("_verified") else "⚠️",
            _fmt(e.get("_deadline_confidence")),
            _fmt([f"{i.get('field')}: {i.get('problem')}" for i in issues]),
            _fmt(source),
            _fmt(user),
            "",  # Image
        ]
        resp = self._events.append_row(row, value_input_option="RAW")
        rownum = _appended_row_number(resp)
        self._save_image(self._events, rownum, len(EVENT_HEADERS), images)
        return rownum
