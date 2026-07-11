"""Telegram front-end: auth, menus, ingestion, notifications, reminders."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from telegram import (Update, InputFile, InlineKeyboardButton,
                      InlineKeyboardMarkup)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, filters,
)

from . import menus
from .calendar_ics import build_event_ics
from .config import Config
from .enrich import (Attachment, make_image_attachment, make_pdf_attachment,
                     make_text_attachment, make_media_attachment,
                     make_office_attachment)
from .pipeline import Pipeline, Result
from .security import RateLimiter, verify_password, hash_password
from .storage import Store

log = logging.getLogger("briefer.bot")

AUTH_TTL = 30 * 24 * 3600          # a chat stays logged in for 30 days
MAX_TEXT = 20000                   # clamp incoming text


class BrieferBot:
    def __init__(self, cfg: Config, pipeline: Pipeline, store: Store) -> None:
        self.cfg = cfg
        self.pipeline = pipeline
        self.store = store
        self.rate = RateLimiter(cfg.rate_limit_per_minute)
        salt, h = hash_password(cfg.login_password)
        self._pw_salt, self._pw_hash = salt, h
        self._wake: asyncio.Event | None = None
        self._worker_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------
    def _allowed(self, chat_id: int) -> bool:
        if self.cfg.bootstrap:
            return True
        return (chat_id in self.cfg.allowed_chat_ids
                or self.store.is_allowed(chat_id))

    def _is_admin(self, chat_id: int) -> bool:
        return chat_id in self.cfg.admins

    def _localize(self, dt: datetime) -> datetime:
        """Attach the configured timezone to a naive datetime so the ICS file,
        the Google Calendar link and reminder scheduling all agree."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ZoneInfo(self.cfg.timezone))
        return dt

    def _authed(self, chat_id: int) -> bool:
        return self.store.is_authed(chat_id, AUTH_TTL)

    async def _gate(self, update: Update) -> bool:
        """Returns True if the message may proceed. Silent for non-allowed."""
        chat = update.effective_chat
        if not chat:
            return False
        cid = chat.id
        if not self._allowed(cid):
            # Silently ignore strangers; only log.
            log.warning("Blocked non-allowlisted chat id=%s", cid)
            return False
        if not self.rate.allow(cid):
            await self._reply(update, "⏳ Slow down — rate limit reached. Try again shortly.")
            return False
        return True

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        await self._reply(update, menus.WELCOME, keyboard=menus.main_menu())

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        await self._reply(update, menus.HELP, keyboard=menus.main_menu())

    async def cmd_menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        await self._reply(update, "Quick actions:", keyboard=menus.main_menu())

    async def cmd_whoami(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        # Always reveal the chat id so a new user can ask an admin to /allow
        # them. Only reveal access-control STATE to already-allowlisted chats
        # (so we don't confirm the bot's allowlist to strangers).
        if self.cfg.bootstrap or self._allowed(chat.id):
            await self._reply(
                update,
                f"Your chat id: `{chat.id}`\n"
                f"Your user id: `{user.id if user else '?'}`\n"
                f"Allow-listed: {'yes' if self._allowed(chat.id) else 'no'}\n"
                f"Logged in: {'yes' if self._authed(chat.id) else 'no'}",
            )
        else:
            await self._reply(
                update,
                f"Your chat id: `{chat.id}`\n"
                "Ask an admin to run `/allow "
                f"{chat.id}`, then `/login <password>`.",
            )

    async def cmd_login(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not self._allowed(chat.id):
            log.warning("Login attempt from non-allowlisted id=%s", chat.id)
            return
        # Delete the message so the password isn't left in history.
        supplied = " ".join(ctx.args) if ctx.args else ""
        try:
            await update.message.delete()
        except Exception:  # noqa: BLE001
            pass
        # The user's message was just deleted, so send fresh messages
        # (a reply-to would target a now-missing message and can 400).
        if not supplied:
            await ctx.bot.send_message(
                chat.id, "Usage: /login <password> (your message is auto-deleted).")
            return
        if verify_password(supplied, self._pw_salt, self._pw_hash):
            self.store.set_authed(chat.id)
            await ctx.bot.send_message(
                chat.id, "✅ Logged in. Send me anything to analyse.",
                reply_markup=menus.main_menu())
        else:
            await asyncio.sleep(1.0)  # slow brute force
            await ctx.bot.send_message(chat.id, "❌ Wrong password.")

    async def cmd_logout(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        self.store.clear_auth(chat.id)
        await self._reply(update, "🔒 Logged out.")

    # --- admin: manage the allow-list at runtime ---------------------
    async def _require_admin(self, update: Update) -> bool:
        chat = update.effective_chat
        if not self._allowed(chat.id):
            return False
        if not self._is_admin(chat.id):
            await self._reply(update, "⛔ Admins only.")
            return False
        if not self._authed(chat.id):
            await self._reply(update, "🔒 Please `/login <password>` first.")
            return False
        return True

    async def cmd_allow(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        target = _parse_id(ctx.args)
        if target is None:
            await self._reply(update, "Usage: `/allow <chat_id>` (get it via /whoami).")
            return
        self.store.add_allowed(target, update.effective_chat.id,
                               note=" ".join(ctx.args[1:]) if len(ctx.args) > 1 else "")
        await self._reply(update, f"✅ Chat `{target}` is now allowed. They must "
                                  "still `/login` with the password.")

    async def cmd_deny(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        target = _parse_id(ctx.args)
        if target is None:
            await self._reply(update, "Usage: `/deny <chat_id>`.")
            return
        if target in self.cfg.allowed_chat_ids:
            await self._reply(update, "That id is pinned in .env; remove it there "
                                      "and restart to revoke.")
            return
        self.store.remove_allowed(target)
        self.store.clear_auth(target)
        await self._reply(update, f"🚫 Chat `{target}` removed and logged out.")

    async def cmd_allowlist(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        env_ids = sorted(self.cfg.allowed_chat_ids)
        dyn = self.store.list_allowed()
        lines = ["👥 *Allow-list*", "", "*Pinned (.env):*"]
        lines += [f"• `{i}`" for i in env_ids] or ["• (none)"]
        lines += ["", "*Added at runtime:*"]
        lines += [f"• `{d['chat_id']}` {('— ' + d['note']) if d['note'] else ''}"
                  for d in dyn] or ["• (none)"]
        await self._reply(update, "\n".join(lines))

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return
        up = _uptime(ctx)
        a = self.pipeline.sheets.articles_id
        e = self.pipeline.sheets.events_id
        media = "on" if self.cfg.enable_transcription else "off"
        queued = self.store.pending_count()
        done = self.store.get_meta("processed_total", "0")
        last_at = self.store.get_meta("last_processed_at", "")
        when = (datetime.fromtimestamp(int(last_at)).strftime("%Y-%m-%d %H:%M")
                if last_at.isdigit() else "never")
        art_row = self.store.get_meta("article_last_row", "?")
        evt_row = self.store.get_meta("event_last_row", "?")
        await self._reply(
            update,
            "💚 <b>Status</b>\n"
            f"Uptime: {up}\n"
            f"Queue: {queued} waiting · processed: {done} · last: {when}\n"
            f"Last row — Articles: {art_row}, Events: {evt_row}\n"
            f"Model: <code>{html.escape(self.cfg.model)}</code> / verify "
            f"<code>{html.escape(self.cfg.verify_model)}</code>\n"
            f"Transcription: {media} · keyframes: {self.cfg.video_keyframes}\n"
            f"Reminder lead times: {self.cfg.deadline_reminder_hours} h\n"
            f"📄 Articles: https://docs.google.com/spreadsheets/d/{html.escape(a)}\n"
            f"📅 Events: https://docs.google.com/spreadsheets/d/{html.escape(e)}",
            preview=True, parse_mode=ParseMode.HTML,
        )

    async def cmd_logs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Admin: show recent log lines. `/logs` = last 30, `/logs errors`
        = recent errors/warnings, `/logs 60` = last 60 lines."""
        if not await self._require_admin(update):
            return
        logfile = self.cfg.data_dir / "logs" / "briefer.log"
        if not logfile.exists():
            await self._reply(update, "No log file yet.")
            return
        arg = (ctx.args[0].lower() if ctx.args else "")
        try:
            lines = logfile.read_text("utf-8", "replace").splitlines()
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Could not read log: {exc}")
            return
        if arg in ("errors", "error", "err"):
            picked = [ln for ln in lines
                      if any(k in ln for k in ("ERROR", "WARNING", "CRITICAL",
                                               "Traceback", "Exception"))][-30:]
            header = "🪵 <b>Recent errors/warnings</b>"
        else:
            n = 30
            if arg.isdigit():
                n = max(1, min(200, int(arg)))
            picked = lines[-n:]
            header = f"🪵 <b>Last {len(picked)} log lines</b>"
        last_err = ctx.application.bot_data.get("last_error")
        body = "\n".join(picked) or "(empty)"
        # Telegram messages cap ~4096 chars; keep the tail.
        body = body[-3500:]
        msg = header
        if last_err:
            msg += f"\nLast analysis error: <code>{html.escape(str(last_err)[:200])}</code>"
        msg += f"\n<pre>{html.escape(body)}</pre>"
        await self._reply(update, msg, parse_mode=ParseMode.HTML)

    async def cmd_sheets(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return
        a = self.pipeline.sheets.articles_id
        e = self.pipeline.sheets.events_id
        # HTML, not Markdown: sheet IDs can contain '_' which breaks Markdown.
        await self._reply(
            update,
            "🗂 <b>Your sheets</b>\n"
            f"📄 Articles: https://docs.google.com/spreadsheets/d/{html.escape(a)}\n"
            f"📅 Events: https://docs.google.com/spreadsheets/d/{html.escape(e)}",
            preview=True, parse_mode=ParseMode.HTML,
        )

    async def cmd_deadlines(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return
        now = time.time()
        # Scope to THIS chat only — never reveal other chats' deadlines.
        rem = self.store.upcoming_reminders(
            update.effective_chat.id, now, 3650 * 86400)
        if not rem:
            await self._reply(update, "No upcoming deadlines are being tracked.")
            return
        # Titles are html.escaped, so send as HTML (not Markdown) to avoid
        # broken rendering / Telegram rejecting the message.
        lines = ["⏰ <b>Upcoming deadline reminders</b>"]
        for r in rem[:20]:
            when = datetime.fromtimestamp(r["fire_at"]).strftime("%Y-%m-%d %H:%M")
            lines.append(f"• {html.escape(r['title'])} — poke at {when}")
        await self._reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ctx.user_data.pop("force_kind", None)
        await self._reply(update, "Cancelled. Send new content any time.")

    async def cmd_force(self, kind: str, update: Update,
                        ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return
        text = " ".join(ctx.args) if ctx.args else ""
        if text:
            await self._enqueue(update, text, [], kind)
        else:
            ctx.user_data["force_kind"] = kind
            await self._reply(update, f"OK — send the {kind} content now.")

    # ------------------------------------------------------------------
    # Callback buttons
    # ------------------------------------------------------------------
    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        chat_id = q.message.chat.id
        if not self._allowed(chat_id):
            return
        data = q.data or ""
        if data.startswith("mode:"):
            kind = data.split(":", 1)[1]
            ctx.user_data["force_kind"] = kind
            await q.message.reply_text(f"Next item will be filed as a *{kind}*. Send it now.",
                                       parse_mode=ParseMode.MARKDOWN)
        elif data == "act:help":
            await q.message.reply_text(menus.HELP, parse_mode=ParseMode.MARKDOWN)
        elif data == "act:sheets":
            await self.cmd_sheets(update, ctx)
        elif data == "act:deadlines":
            await self.cmd_deadlines(update, ctx)
        elif data == "act:status":
            await self.cmd_status(update, ctx)

    # ------------------------------------------------------------------
    # Message ingestion
    # ------------------------------------------------------------------
    async def on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        # _gate already applied the allow-list + rate limit (one token). Do an
        # auth-only check here so a message doesn't burn two rate-limit tokens.
        if not await self._gate(update):
            return
        if not self._authed(update.effective_chat.id):
            await self._reply(update, "🔒 Please `/login <password>` first.")
            return
        msg = update.message
        text = (msg.text or msg.caption or "")[:MAX_TEXT]

        try:
            descriptors = await self._collect_descriptors(msg)
        except _TooLarge as exc:
            await self._reply(update, f"⚠️ {exc}")
            return
        except Exception:  # noqa: BLE001
            log.exception("attachment descriptor error")
            await self._reply(update, "⚠️ I couldn't read that attachment.")
            return

        if not text and not descriptors:
            await self._reply(update, "Send text, a link, a file, an image or an event.")
            return

        force_kind = ctx.user_data.pop("force_kind", None)
        await self._enqueue(update, text, descriptors, force_kind)

    async def _collect_descriptors(self, msg) -> list[dict[str, Any]]:
        """Describe attachments by Telegram file_id (not bytes), so a queued
        job survives a restart — the worker re-downloads from Telegram."""
        out: list[dict[str, Any]] = []
        limit = self.cfg.max_download_bytes

        if msg.photo:
            p = msg.photo[-1]  # largest
            if (p.file_size or 0) > limit:
                raise _TooLarge("That image is too large.")
            out.append({"t": "image", "file_id": p.file_id,
                        "mime": "image/jpeg", "name": "photo.jpg"})

        media = msg.video or msg.video_note or msg.animation or msg.audio or msg.voice
        if media is not None:
            if (getattr(media, "file_size", 0) or 0) > limit:
                raise _TooLarge("That media is too large to download (send a link).")
            mtype = getattr(media, "mime_type", "") or "application/octet-stream"
            name = getattr(media, "file_name", "") or ("media." + (
                mtype.split("/")[-1] if "/" in mtype else "bin"))
            out.append({"t": "media", "file_id": media.file_id,
                        "mime": mtype, "name": name})

        doc = msg.document
        if doc:
            if (doc.file_size or 0) > limit:
                raise _TooLarge("That file is too large.")
            mime = doc.mime_type or ""
            name = doc.file_name or "file"
            low = name.lower()
            if mime == "application/pdf" or low.endswith(".pdf"):
                out.append({"t": "pdf", "file_id": doc.file_id, "mime": mime, "name": name})
            elif mime.startswith("image/"):
                out.append({"t": "image", "file_id": doc.file_id, "mime": mime, "name": name})
            elif low.endswith((".docx", ".pptx", ".xlsx", ".xlsm")):
                out.append({"t": "office", "file_id": doc.file_id,
                            "mime": mime, "name": name})
            elif mime.startswith("text/") or low.endswith(
                (".txt", ".md", ".csv", ".json", ".log")
            ):
                out.append({"t": "text", "file_id": doc.file_id,
                            "mime": mime or "text/plain", "name": name})
            else:
                out.append({"t": "filenote", "name": name, "size": doc.file_size or 0})
        return out

    async def _materialize(self, bot, descriptors: list[dict[str, Any]]
                           ) -> list[Attachment]:
        """Turn stored file_id descriptors back into Attachment objects by
        re-downloading from Telegram at processing time."""
        out: list[Attachment] = []
        for d in descriptors:
            t = d.get("t")
            if t == "filenote":
                out.append(Attachment(
                    kind="file", media_type="", filename=d.get("name", "file"),
                    text=f"(binary file '{d.get('name')}', "
                         f"{d.get('size', 0)} bytes, not parsed)"))
                continue
            try:
                f = await bot.get_file(d["file_id"])
                data = bytes(await f.download_as_bytearray())
            except Exception as exc:  # noqa: BLE001
                log.warning("could not re-download %s: %s", d.get("name"), exc)
                out.append(Attachment(kind="file", media_type="",
                                      filename=d.get("name", "file"),
                                      text="(attachment could not be downloaded)"))
                continue
            if t == "image":
                out.append(make_image_attachment(data, d.get("mime", "image/jpeg"),
                                                 d.get("name", "image")))
            elif t == "pdf":
                out.append(make_pdf_attachment(data, d.get("name", "file.pdf")))
            elif t == "text":
                out.append(make_text_attachment(data, d.get("name", "file"),
                                                d.get("mime", "text/plain")))
            elif t == "office":
                out.append(make_office_attachment(
                    data, d.get("name", "file"), d.get("mime", "")))
            elif t == "media":
                out.append(make_media_attachment(
                    data, d.get("mime", "application/octet-stream"),
                    d.get("name", "media")))
        return out

    # ------------------------------------------------------------------
    # Durable queue + single worker (process one item at a time)
    # ------------------------------------------------------------------
    async def _enqueue(self, update: Update, text: str,
                       descriptors: list[dict[str, Any]],
                       force_kind: str | None) -> None:
        chat = update.effective_chat
        submitter = _submitter(update.effective_user)
        ahead = self.store.pending_count()
        if ahead == 0:
            note_txt = "🧠 Working on it…"
        else:
            note_txt = (f"📥 Queued — {ahead} item(s) ahead of you. "
                        "I'll analyse this and reply here.")
        note = await update.message.reply_text(note_txt)
        self.store.enqueue_job(chat.id, submitter, text, descriptors,
                               force_kind, note.message_id)
        self._wake_worker()

    def _wake_worker(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _worker_loop(self, app: "Application") -> None:
        assert self._wake is not None
        while True:
            try:
                await self._wake.wait()
                self._wake.clear()
                while True:
                    job = self.store.claim_next_job()
                    if not job:
                        break
                    await self._process_job(app, job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("worker loop error")
                await asyncio.sleep(2)

    async def _process_job(self, app: "Application", job: dict[str, Any]) -> None:
        bot = app.bot
        chat_id = job["chat_id"]
        note_id = job["note_message_id"]

        async def edit(text: str, **kw) -> None:
            if note_id:
                try:
                    await bot.edit_message_text(text, chat_id=chat_id,
                                                message_id=note_id, **kw)
                    return
                except Exception:  # noqa: BLE001
                    pass
            try:
                await bot.send_message(chat_id, text, **kw)
            except Exception:  # noqa: BLE001
                pass

        await edit("🧠 Analysing & double-checking…")
        try:
            attachments = await self._materialize(bot, job["attachments"])
            result: Result = await asyncio.to_thread(
                self.pipeline.process, job["text"], attachments,
                job["submitter"], job["force_kind"], chat_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline failure (job %s)", job["id"])
            reason = f"{type(exc).__name__}: {exc}"
            app.bot_data["last_error"] = reason
            self.store.finish_job(job["id"], "failed", reason)
            await edit("⚠️ Analysis failed.\n<code>" + html.escape(reason[:350])
                       + "</code>\nSee <b>/logs</b> for detail.",
                       parse_mode=ParseMode.HTML)
            return

        if result.updated and not result.changed:
            self.store.finish_job(job["id"], "done")
            await edit("♻️ Re-checked — no new info to add; the existing row is "
                       "already up to date.")
            return

        prefix = "🔄 <b>Updated existing entry with new info</b>\n\n" if result.updated else ""
        await edit(prefix + _format_catch(result), parse_mode=ParseMode.HTML,
                   disable_web_page_preview=True)
        # Checkpoint so /status shows progress and resets know where we are.
        self.store.set_meta("last_processed_at", int(time.time()))
        self.store.incr_meta("processed_total", 1)
        if getattr(result, "sheet_row", None):
            self.store.set_meta(f"{result.kind}_last_row", result.sheet_row)
        self.store.finish_job(job["id"], "done")

        # Schedule reminders only for brand-new events (updates keep theirs).
        if result.kind == "event" and not result.updated:
            if result.deadline_dt:
                self._schedule_deadline(chat_id, result)
            await self._send_calendar(bot, chat_id, result)

    # ------------------------------------------------------------------
    # Calendar (.ics) export
    # ------------------------------------------------------------------
    async def _send_calendar(self, bot, chat_id: int, result: Result) -> None:
        a = result.analysis
        # Prefer the event date; fall back to the application deadline so you
        # still get a calendar entry + alarms for the last day to apply.
        if result.event_dt:
            start, all_day, label = result.event_dt, result.event_all_day, ""
        elif result.deadline_dt:
            start, all_day = result.deadline_dt, True
            label = "Application deadline: "
        else:
            return
        # Localize a naive datetime once so the .ics and the Google Calendar
        # button describe the same instant.
        start = self._localize(start)

        title = label + str(a.get("title", "Event"))
        desc_parts = [str(a.get("summary", ""))]
        if a.get("application_deadline"):
            desc_parts.append(f"Deadline: {a['application_deadline']}")
        if a.get("required_materials"):
            desc_parts.append("Required: " + ", ".join(map(str, a["required_materials"])))
        if a.get("application_url"):
            desc_parts.append(f"Apply: {a['application_url']}")
        description = "\n".join(p for p in desc_parts if p)

        try:
            ics = build_event_ics(
                title=title, start=start, tz_name=self.cfg.timezone,
                all_day=all_day, description=description,
                location=str(a.get("location", "") or ""),
                url=str(a.get("application_url", "") or a.get("event_url", "") or ""),
            )
        except Exception:  # noqa: BLE001
            log.exception("ics build failed")
            return

        fname = _slug(title)[:40] + ".ics"
        gcal = _gcal_link(title, start, all_day, description,
                          str(a.get("location", "") or ""))
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("➕ Google Calendar", url=gcal)]]) if gcal else None
        try:
            await bot.send_document(
                chat_id,
                document=InputFile(io.BytesIO(ics), filename=fname),
                caption="📅 Tap the file → *Add to Calendar* (Apple/Android). "
                        "Reminders included: day-of, 2h and 1h before.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to send ics")

    # ------------------------------------------------------------------
    # Deadline reminders
    # ------------------------------------------------------------------
    def _schedule_deadline(self, chat_id: int, result: Result) -> None:
        dt = result.deadline_dt
        if dt is None:
            return
        # A naive deadline is in the configured local timezone (matches the
        # calendar exporter), not UTC.
        dt = self._localize(dt)
        title = result.analysis.get("title", "event")
        now = datetime.now(timezone.utc)
        scheduled = 0
        for hours in sorted(self.cfg.deadline_reminder_hours, reverse=True):
            fire = dt - timedelta(hours=hours)
            if fire <= now:
                continue
            self.store.add_reminder(
                chat_id, fire.timestamp(),
                f"{title} — {hours}h to deadline",
                {"title": title,
                 "deadline": dt.isoformat(),
                 "application_url": result.analysis.get("application_url"),
                 "required": result.analysis.get("required_materials", [])},
                entry_id=result.entry_id,
            )
            scheduled += 1
        if scheduled:
            log.info("Scheduled %d reminders for '%s'", scheduled, title)

    async def reminder_tick(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """JobQueue callback: fire any due reminders."""
        for r in self.store.due_reminders(time.time()):
            payload = r["payload"]
            url = payload.get("application_url")
            required = payload.get("required") or []
            body = (
                f"⏰ <b>Deadline approaching</b>\n"
                f"<b>{html.escape(payload.get('title', 'event'))}</b>\n"
                f"Deadline: {html.escape(str(payload.get('deadline', '')))}\n"
            )
            if required:
                body += "Bring: " + html.escape(", ".join(map(str, required))[:300]) + "\n"
            if url:
                body += f"Apply: {html.escape(str(url))}"
            try:
                await ctx.bot.send_message(r["chat_id"], body, parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                # Leave fired=0 so a transient failure retries next tick,
                # instead of silently dropping the reminder.
                log.exception("failed to send reminder %s; will retry", r["id"])
                continue
            self.store.mark_reminder_fired(r["id"])

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    async def _require_auth(self, update: Update) -> bool:
        chat = update.effective_chat
        if not self._allowed(chat.id):
            return False
        if not self.rate.allow(chat.id):
            await self._reply(update, "⏳ Rate limit reached.")
            return False
        if not self._authed(chat.id):
            await self._reply(update, "🔒 Please `/login <password>` first.")
            return False
        return True

    async def _reply(self, update: Update, text: str, *, keyboard=None,
                     preview: bool = False,
                     parse_mode: str = ParseMode.MARKDOWN) -> None:
        target = update.effective_message
        if target is None and update.callback_query:
            target = update.callback_query.message
        if target is None:
            return
        await target.reply_text(
            text, parse_mode=parse_mode, reply_markup=keyboard,
            disable_web_page_preview=not preview,
        )


class _TooLarge(Exception):
    pass


def _parse_id(args: list[str] | None) -> int | None:
    if not args:
        return None
    try:
        return int(args[0])
    except (ValueError, TypeError):
        return None


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in " -_") else "_" for c in text]
    return ("".join(keep).strip().replace(" ", "_") or "event")


def _gcal_link(title: str, start: datetime, all_day: bool, details: str,
               location: str) -> str:
    """Build a Google Calendar 'add event' template URL (fallback to ICS).

    `start` must already be tz-aware (localized by the caller) so this matches
    the .ics exporter.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if all_day:
        s = start.strftime("%Y%m%d")
        e = (start + timedelta(days=1)).strftime("%Y%m%d")
        dates = f"{s}/{e}"
    else:
        su = start.astimezone(timezone.utc)
        eu = (start + timedelta(hours=1)).astimezone(timezone.utc)
        dates = f"{su.strftime('%Y%m%dT%H%M%SZ')}/{eu.strftime('%Y%m%dT%H%M%SZ')}"
    params = {
        "action": "TEMPLATE", "text": title, "dates": dates,
        "details": details[:900], "location": location,
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _submitter(user) -> str:
    if not user:
        return "unknown"
    if user.username:
        return "@" + user.username
    return (user.full_name or str(user.id))


def _uptime(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    started = ctx.application.bot_data.get("started_at")
    if not started:
        return "?"
    secs = int(time.time() - started)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _li(items: Any, limit: int = 5) -> str:
    if not items:
        return ""
    if isinstance(items, str):
        items = [items]
    out = "\n".join(f"• {html.escape(str(i))}" for i in items[:limit])
    return out


def _format_catch(result: Result) -> str:
    a = result.analysis
    verified = a.get("_verified")
    badge = "✅ verified" if verified else "⚠️ needs review"
    title = html.escape(str(a.get("title", "Untitled")))
    parts = [f"🎯 <b>{title}</b>  <i>({result.kind}, {badge})</i>"]

    if a.get("summary"):
        parts.append(html.escape(str(a["summary"])))

    catch = _li(a.get("catch_points"))
    if catch:
        parts.append("<b>Catch points</b>\n" + catch)

    if a.get("vivax_relevance"):
        parts.append("<b>Vivax angle</b>\n" + html.escape(str(a["vivax_relevance"])))

    if result.kind == "event":
        dl = a.get("application_deadline") or a.get("deadline_raw")
        if dl:
            conf = a.get("_deadline_confidence", "?")
            parts.append(f"<b>⏳ Deadline:</b> {html.escape(str(dl))} "
                         f"<i>(confidence: {conf})</i>")
        if a.get("required_materials"):
            parts.append("<b>Required</b>\n" + _li(a["required_materials"]))
        if a.get("application_url"):
            parts.append(f"<b>Apply:</b> {html.escape(str(a['application_url']))}")
        if a.get("should_apply"):
            parts.append(f"<b>Verdict:</b> {html.escape(str(a['should_apply']))}")

    issues = a.get("_verification_issues") or []
    if issues:
        shown = "; ".join(
            f"{i.get('field')}: {i.get('problem')}" for i in issues[:3]
        )
        parts.append(f"<i>⚠️ Unverified: {html.escape(shown)}</i>")

    parts.append("<i>Saved to your Google Sheet.</i>")
    return "\n\n".join(parts)


BOT_COMMANDS = [
    ("start", "Welcome & menu"),
    ("help", "Full guide"),
    ("menu", "Quick action buttons"),
    ("article", "File the next item as an article"),
    ("event", "File the next item as an event"),
    ("sheets", "Links to your two Google Sheets"),
    ("deadlines", "Upcoming event deadlines"),
    ("status", "Bot health & sheet links"),
    ("logs", "Recent logs (admin)"),
    ("allow", "Allow a chat id (admin)"),
    ("deny", "Remove a chat id (admin)"),
    ("allowlist", "Show allowed chats (admin)"),
    ("login", "Authenticate this chat"),
    ("logout", "End this chat's session"),
    ("whoami", "Show your chat id"),
    ("cancel", "Cancel the current action"),
]


def build_application(cfg: Config, bot: BrieferBot) -> Application:
    async def _post_init(app: Application) -> None:
        from telegram import BotCommand

        # Registers the command list so typing "/" pops up the menu.
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
        except Exception:  # noqa: BLE001
            log.warning("set_my_commands failed", exc_info=True)
        # Start the single background worker and resume any leftover jobs.
        bot._wake = asyncio.Event()
        n = bot.store.requeue_processing()
        if n:
            log.info("Requeued %d interrupted job(s) after restart", n)
        bot._worker_task = asyncio.create_task(bot._worker_loop(app))
        bot._wake.set()  # drain anything already queued

    app = (Application.builder().token(cfg.telegram_token)
           .post_init(_post_init).build())
    app.bot_data["started_at"] = time.time()

    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("menu", bot.cmd_menu))
    app.add_handler(CommandHandler("whoami", bot.cmd_whoami))
    app.add_handler(CommandHandler("login", bot.cmd_login))
    app.add_handler(CommandHandler("logout", bot.cmd_logout))
    app.add_handler(CommandHandler("allow", bot.cmd_allow))
    app.add_handler(CommandHandler("deny", bot.cmd_deny))
    app.add_handler(CommandHandler("allowlist", bot.cmd_allowlist))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("logs", bot.cmd_logs))
    app.add_handler(CommandHandler("sheets", bot.cmd_sheets))
    app.add_handler(CommandHandler("deadlines", bot.cmd_deadlines))
    app.add_handler(CommandHandler("cancel", bot.cmd_cancel))
    app.add_handler(CommandHandler(
        "article", lambda u, c: bot.cmd_force("article", u, c)))
    app.add_handler(CommandHandler(
        "event", lambda u, c: bot.cmd_force("event", u, c)))
    app.add_handler(CallbackQueryHandler(bot.on_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VIDEO
         | filters.VIDEO_NOTE | filters.ANIMATION | filters.AUDIO | filters.VOICE)
        & ~filters.COMMAND,
        bot.on_message))

    if app.job_queue:
        app.job_queue.run_repeating(bot.reminder_tick, interval=60, first=10)
        # Sync the sheets (checkboxes, deletions, time-to-check stats).
        from .sheet_sync import SheetSync
        sync = SheetSync(bot.pipeline.sheets, bot.store)
        app.job_queue.run_repeating(sync.tick, interval=60, first=25)
    return app
