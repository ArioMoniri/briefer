"""Telegram front-end: auth, menus, ingestion, notifications, reminders."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, filters,
)

from . import menus
from .config import Config
from .enrich import (Attachment, make_image_attachment, make_pdf_attachment,
                     make_text_attachment)
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

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------
    def _allowed(self, chat_id: int) -> bool:
        if self.cfg.bootstrap:
            return True
        return chat_id in self.cfg.allowed_chat_ids

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
        await self._reply(
            update,
            f"Your chat id: `{chat.id}`\n"
            f"Your user id: `{user.id if user else '?'}`\n"
            f"Allow-listed: {'yes' if self._allowed(chat.id) else 'no'}\n"
            f"Logged in: {'yes' if self._authed(chat.id) else 'no'}",
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

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return
        up = _uptime(ctx)
        await self._reply(
            update,
            "💚 *Status*\n"
            f"Uptime: {up}\n"
            f"Model: `{self.cfg.model}` / verify `{self.cfg.verify_model}`\n"
            f"Articles sheet: {'set' if self.cfg.articles_sheet_id else 'auto'}\n"
            f"Events sheet: {'set' if self.cfg.events_sheet_id else 'auto'}\n"
            f"Reminder lead times: {self.cfg.deadline_reminder_hours} h",
        )

    async def cmd_sheets(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return
        a = self.pipeline.sheets.articles_id
        e = self.pipeline.sheets.events_id
        await self._reply(
            update,
            "🗂 *Your sheets*\n"
            f"📄 Articles: https://docs.google.com/spreadsheets/d/{a}\n"
            f"📅 Events: https://docs.google.com/spreadsheets/d/{e}",
            preview=True,
        )

    async def cmd_deadlines(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return
        now = time.time()
        rem = [r for r in self.store.due_reminders(now + 3650 * 86400)
               if r["fire_at"] >= now]
        rem.sort(key=lambda r: r["fire_at"])
        if not rem:
            await self._reply(update, "No upcoming deadlines are being tracked.")
            return
        lines = ["⏰ *Upcoming deadline reminders*"]
        for r in rem[:20]:
            when = datetime.fromtimestamp(r["fire_at"]).strftime("%Y-%m-%d %H:%M")
            lines.append(f"• {html.escape(r['title'])} — poke at {when}")
        await self._reply(update, "\n".join(lines))

    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ctx.user_data.pop("force_kind", None)
        await self._reply(update, "Cancelled. Send new content any time.")

    async def cmd_force(self, kind: str, update: Update,
                        ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return
        text = " ".join(ctx.args) if ctx.args else ""
        if text:
            await self._ingest(update, ctx, text=text, attachments=[], force_kind=kind)
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
        if not await self._gate(update):
            return
        if not await self._require_auth(update):
            return
        msg = update.message
        text = (msg.text or msg.caption or "")[:MAX_TEXT]
        attachments: list[Attachment] = []

        try:
            attachments = await self._collect_attachments(msg)
        except _TooLarge as exc:
            await self._reply(update, f"⚠️ {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("attachment error")
            await self._reply(update, "⚠️ I couldn't read that attachment.")
            return

        if not text and not attachments:
            await self._reply(update, "Send text, a link, a file, an image or an event.")
            return

        force_kind = ctx.user_data.pop("force_kind", None)
        await self._ingest(update, ctx, text=text, attachments=attachments,
                           force_kind=force_kind)

    async def _collect_attachments(self, msg) -> list[Attachment]:
        out: list[Attachment] = []
        limit = self.cfg.max_download_bytes

        if msg.photo:
            photo = msg.photo[-1]  # largest
            if (photo.file_size or 0) > limit:
                raise _TooLarge("That image is too large.")
            f = await photo.get_file()
            data = bytes(await f.download_as_bytearray())
            out.append(make_image_attachment(data, "image/jpeg", "photo.jpg"))

        doc = msg.document
        if doc:
            if (doc.file_size or 0) > limit:
                raise _TooLarge("That file is too large.")
            f = await doc.get_file()
            data = bytes(await f.download_as_bytearray())
            mime = doc.mime_type or ""
            name = doc.file_name or "file"
            if mime == "application/pdf" or name.lower().endswith(".pdf"):
                out.append(make_pdf_attachment(data, name))
            elif mime.startswith("image/"):
                out.append(make_image_attachment(data, mime, name))
            elif mime.startswith("text/") or name.lower().endswith(
                (".txt", ".md", ".csv", ".json", ".log")
            ):
                out.append(make_text_attachment(data, name, mime or "text/plain"))
            else:
                # Unknown binary: don't execute or parse it, just note it.
                out.append(Attachment(kind="file", media_type=mime, filename=name,
                                      text=f"(binary file '{name}', {len(data)} bytes, not parsed)"))
        return out

    async def _ingest(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, *,
                      text: str, attachments: list[Attachment],
                      force_kind: str | None) -> None:
        chat = update.effective_chat
        user = update.effective_user
        submitter = _submitter(user)
        await ctx.bot.send_chat_action(chat.id, ChatAction.TYPING)
        note = await update.message.reply_text("🧠 Analysing & double-checking…")

        try:
            result: Result = await asyncio.to_thread(
                self.pipeline.process, text, attachments, submitter, force_kind
            )
        except Exception:  # noqa: BLE001
            log.exception("pipeline failure")
            await note.edit_text("⚠️ Something went wrong while analysing. It's logged.")
            return

        if result.duplicate:
            await note.edit_text("♻️ I've already processed this one — skipping the sheet.")
            return

        await note.edit_text(_format_catch(result), parse_mode=ParseMode.HTML,
                             disable_web_page_preview=True)

        if result.kind == "event" and result.deadline_dt:
            self._schedule_deadline(chat.id, result)

    # ------------------------------------------------------------------
    # Deadline reminders
    # ------------------------------------------------------------------
    def _schedule_deadline(self, chat_id: int, result: Result) -> None:
        dt = result.deadline_dt
        if dt is None:
            return
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
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
                log.exception("failed to send reminder %s", r["id"])
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
                     preview: bool = False) -> None:
        target = update.effective_message
        if target is None and update.callback_query:
            target = update.callback_query.message
        if target is None:
            return
        await target.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
            disable_web_page_preview=not preview,
        )


class _TooLarge(Exception):
    pass


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


def build_application(cfg: Config, bot: BrieferBot) -> Application:
    app = Application.builder().token(cfg.telegram_token).build()
    app.bot_data["started_at"] = time.time()

    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("menu", bot.cmd_menu))
    app.add_handler(CommandHandler("whoami", bot.cmd_whoami))
    app.add_handler(CommandHandler("login", bot.cmd_login))
    app.add_handler(CommandHandler("logout", bot.cmd_logout))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("sheets", bot.cmd_sheets))
    app.add_handler(CommandHandler("deadlines", bot.cmd_deadlines))
    app.add_handler(CommandHandler("cancel", bot.cmd_cancel))
    app.add_handler(CommandHandler(
        "article", lambda u, c: bot.cmd_force("article", u, c)))
    app.add_handler(CommandHandler(
        "event", lambda u, c: bot.cmd_force("event", u, c)))
    app.add_handler(CallbackQueryHandler(bot.on_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
        bot.on_message))

    if app.job_queue:
        app.job_queue.run_repeating(bot.reminder_tick, interval=60, first=10)
    return app
