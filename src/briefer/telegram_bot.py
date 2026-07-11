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
                     make_office_attachment, URL_RE)
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

    async def cmd_id(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        # No allow-list, no login: anyone can discover their chat id here.
        chat = update.effective_chat
        await update.effective_message.reply_text(
            f"Your chat id: `{chat.id}`\nGive this to an admin to be added "
            "(`/allow <id>`), then `/login <password>`.",
            parse_mode=ParseMode.MARKDOWN)

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
        from .version import build_version
        await self._reply(
            update,
            "💚 <b>Status</b>\n"
            f"Build: <code>{html.escape(build_version())}</code>\n"
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

    async def cmd_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return

        from .stats import rich_stats

        def block(name: str, sheet: str) -> str:
            s = rich_stats(self.store, sheet)
            b = s["by_status"]
            return (
                f"<b>{name}</b> — {s['total']} active\n"
                f"  ✅ Done {s['done']} ({s['done_pct']}%) · ⏳ Pending {s['pending']}\n"
                f"  🔴 Overdue {s['overdue']} · 📅 Next 7d {s['upcoming_7d']} · "
                f"🆕 Added 7d {s['added_7d']}\n"
                f"  Status: 🔴{b['passed']} 🟠{b['due_soon']} 🟡{b['coming']} "
                f"🟢{b['upcoming']} ⚪{b['no_date']}\n"
                f"  Time to check: avg {s['avg_check_hours']}h · "
                f"median {s['median_check_hours']}h\n"
                f"  🗑 Removed all-time: {s['removed']}")

        await self._reply(
            update,
            "📊 <b>Statistics</b>\n\n" + block("Articles", "article")
            + "\n\n" + block("Events", "event"),
            parse_mode=ParseMode.HTML)

    async def cmd_cookies(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_auth(update):
            return
        from .cookies import cookie_status, format_status
        rows = cookie_status(self.cfg, self.cfg.cookie_warn_days)
        msg = format_status(rows)
        if any(r.get("status") in ("expired", "expiring") for r in rows):
            msg += ("\n\n⚠️ Renew with <code>./manage.sh browser-login</code> "
                    "(or refresh cookies.txt).")
        await self._reply(update, msg, parse_mode=ParseMode.HTML)

    async def cookie_check(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Daily job: warn admins if any login cookie is expired/expiring."""
        from .cookies import cookie_status, problems
        rows = cookie_status(self.cfg, self.cfg.cookie_warn_days)
        bad = problems(rows)
        if not bad:
            return
        lines = ["⚠️ <b>Login cookies need attention</b>"]
        for r in bad:
            d = r.get("days_left")
            state = "EXPIRED" if (d is not None and d < 0) else f"{d}d left"
            lines.append(f"🔴 {r['platform']} — {state}")
        lines.append("\nRenew: <code>./manage.sh browser-login</code>")
        await self._notify_admins(ctx.application, "\n".join(lines))

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

    async def cmd_calendar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A real month-grid calendar of every dated item (article deadlines,
        event dates & deadlines, custom reminders), with ◀/▶ navigation."""
        if not await self._require_auth(update):
            return
        from . import calendar_view
        today = datetime.now()
        items = calendar_view.collect_items(self.store, update.effective_chat.id)
        text, kb = calendar_view.render(items, today.year, today.month, today)
        await self._reply(update, text, parse_mode=ParseMode.HTML, keyboard=kb)

    async def _show_calendar_month(self, q, chat_id: int, year: int,
                                   month: int) -> None:
        from . import calendar_view
        today = datetime.now()
        items = calendar_view.collect_items(self.store, chat_id)
        text, kb = calendar_view.render(items, year, month, today)
        try:
            await q.message.edit_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=kb)
        except Exception:  # noqa: BLE001 — e.g. "message not modified"
            pass

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
        elif data == "cal:today":
            now = datetime.now()
            await self._show_calendar_month(q, chat_id, now.year, now.month)
        elif data == "cal:html":
            await self._send_calendar_html(q, chat_id)
        elif data.startswith("cal:"):
            try:
                y, m = data.split(":", 1)[1].split("-")
                await self._show_calendar_month(q, chat_id, int(y), int(m))
            except (ValueError, IndexError):
                pass

    async def _send_calendar_html(self, q, chat_id: int) -> None:
        """Generate the interactive HTML calendar and send it as a file."""
        import io
        from . import calendar_view
        items = calendar_view.collect_items(self.store, chat_id)
        if not items:
            await q.message.reply_text(
                "No dated items yet — add an event or an article with a "
                "deadline and it'll appear here.")
            return
        html_doc = calendar_view.build_html(items)
        buf = io.BytesIO(html_doc.encode("utf-8"))
        buf.name = "briefer-calendar.html"
        await q.message.reply_document(
            document=buf, filename="briefer-calendar.html",
            caption="🌐 Open this in a browser — Month / Week / Day / Year / "
                    "List views, with navigation.")

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

        # Standalone reminder: a reply or plain message that is ONLY a
        # "remind me <when>" directive (no link/file to analyse).
        from .reminders import extract_directive, parse_when
        when_phrase, note = extract_directive(text)
        has_content = bool(descriptors) or bool(URL_RE.search(text or ""))
        if when_phrase and not has_content:
            when = parse_when(when_phrase, self.cfg.timezone)
            if not when:
                await self._reply(update, "I couldn't understand that time. Try "
                                  "`remind me in 3 days` or `remind me 2026-08-01 18:00`.")
                return
            reply_text = ""
            if msg.reply_to_message is not None:
                rm = msg.reply_to_message
                reply_text = (rm.text or rm.caption or "")[:500]
            note_text = note or reply_text or "(reminder)"
            title = (reply_text or note or "Reminder")[:80]
            self._add_custom_reminder(update.effective_chat.id, when, title,
                                      note_text, None)
            await self._reply(update, "⏰ Reminder set for "
                              + when.strftime("%Y-%m-%d %H:%M") + ".")
            return

        force_kind = ctx.user_data.pop("force_kind", None)
        # One message may contain several links/items — analyse each separately.
        for sub_text, sub_desc in self._split_submissions(text, descriptors):
            await self._enqueue(update, sub_text, sub_desc, force_kind)

    def _split_submissions(self, text: str, descriptors: list[dict[str, Any]]
                           ) -> list[tuple[str, list[dict[str, Any]]]]:
        from .reminders import extract_directive
        urls = list(dict.fromkeys(URL_RE.findall(text or "")))
        if len(urls) <= 1:
            return [(text, descriptors)]
        # Preserve any reminder directive on every split item.
        when_phrase, _ = extract_directive(text)
        directive = f" remind me {when_phrase}" if when_phrase else ""
        subs: list[tuple[str, list[dict[str, Any]]]] = [
            (u + directive, []) for u in urls]
        for d in descriptors:  # attachments become their own items
            subs.append((directive.strip(), [d]))
        return subs

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
            app.bot_data["last_error"] = f"{type(exc).__name__}: {exc}"
            self.store.finish_job(job["id"], "failed", str(exc))
            friendly, infra = _error_message(exc)
            await edit("⚠️ Analysis failed.\n" + html.escape(friendly)
                       + "\nSee <b>/logs</b> for detail.",
                       parse_mode=ParseMode.HTML)
            if infra:  # API credits / rate / auth → ping the operators too
                await self._notify_admins(
                    app, "⚠️ <b>Service issue</b>\n" + html.escape(friendly),
                    exclude=chat_id)
            return

        if result.updated and not result.changed:
            self.store.finish_job(job["id"], "done")
            await edit("♻️ Re-checked — no new info to add; the existing row is "
                       "already up to date.")
            return

        prefix = "🔄 <b>Updated existing entry with new info</b>\n\n" if result.updated else ""
        try:
            sheet_url = self.pipeline.sheets.row_url(result.kind, result.sheet_row)
        except Exception:  # noqa: BLE001
            sheet_url = None
        await edit(prefix + _format_catch(result, sheet_url),
                   parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        # Checkpoint so /status shows progress and resets know where we are.
        self.store.set_meta("last_processed_at", int(time.time()))
        self.store.incr_meta("processed_total", 1)
        if getattr(result, "sheet_row", None):
            self.store.set_meta(f"{result.kind}_last_row", result.sheet_row)
        self.store.finish_job(job["id"], "done")

        if result.kind == "event":
            # On a merge, cancel the old reminders first so an updated deadline
            # doesn't leave stale/duplicate ones, then (re)schedule fresh.
            if result.updated and result.entry_id:
                self.store.cancel_entry_reminders(result.entry_id)
            self._schedule_event_reminders(chat_id, result)
            if not result.updated:
                await self._send_calendar(bot, chat_id, result)

        # Inline "remind me <when>" directive in the submission text → a custom
        # reminder for this entry (works for articles too).
        from .reminders import extract_directive, parse_when
        when_phrase, _ = extract_directive(job["text"])
        if when_phrase:
            when = parse_when(when_phrase, self.cfg.timezone)
            if when:
                self._add_custom_reminder(
                    chat_id, when, str(result.analysis.get("title", "item")),
                    "Your reminder for this item.", result.entry_id)
                try:
                    await bot.send_message(
                        chat_id, "⏰ Reminder set for "
                        + html.escape(when.strftime("%Y-%m-%d %H:%M")) + ".")
                except Exception:  # noqa: BLE001
                    pass

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
    def _schedule_event_reminders(self, chat_id: int, result: Result) -> None:
        """Schedule reminder sets for BOTH the application deadline and the
        event date itself (each with the configured lead times)."""
        title = result.analysis.get("title", "event")
        a = result.analysis
        if result.deadline_dt:
            self._schedule_lead_reminders(
                chat_id, self._localize(result.deadline_dt), result.entry_id,
                kind="deadline", title=title,
                extra={"deadline": self._localize(result.deadline_dt).isoformat(),
                       "application_url": a.get("application_url"),
                       "required": a.get("required_materials", [])})
        if result.event_dt:
            self._schedule_lead_reminders(
                chat_id, self._localize(result.event_dt), result.entry_id,
                kind="event_date", title=title,
                extra={"when": self._localize(result.event_dt).isoformat(),
                       "url": a.get("application_url") or a.get("event_url")})

    def _schedule_lead_reminders(self, chat_id: int, when: datetime,
                                 entry_id: str | None, *, kind: str, title: str,
                                 extra: dict) -> None:
        now = datetime.now(timezone.utc)
        scheduled = 0
        for hours in sorted(self.cfg.deadline_reminder_hours, reverse=True):
            fire = when - timedelta(hours=hours)
            if fire <= now:
                continue
            self.store.add_reminder(
                chat_id, fire.timestamp(), f"{title} — {hours}h",
                {"kind": kind, "title": title, **extra}, entry_id=entry_id)
            scheduled += 1
        # Also fire exactly at the moment (in case all leads are in the past).
        if scheduled == 0 and when > now:
            self.store.add_reminder(
                chat_id, when.timestamp(), title,
                {"kind": kind, "title": title, **extra}, entry_id=entry_id)
            scheduled = 1
        if scheduled:
            log.info("Scheduled %d %s reminders for '%s'", scheduled, kind, title)

    def _add_custom_reminder(self, chat_id: int, when: datetime, title: str,
                             note: str, entry_id: str | None,
                             url: str = "") -> None:
        when = self._localize(when)
        self.store.add_reminder(
            chat_id, when.timestamp(), title,
            {"kind": "custom", "title": title, "note": note,
             "url": url, "when": when.strftime("%Y-%m-%d %H:%M")},
            entry_id=entry_id)
        log.info("Custom reminder for '%s' at %s", title, when.isoformat())

    async def reminder_tick(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """JobQueue callback: fire any due reminders."""
        for r in self.store.due_reminders(time.time()):
            payload = r["payload"]
            kind = payload.get("kind", "deadline")
            title = html.escape(str(payload.get("title", "item")))
            url = payload.get("application_url") or payload.get("url")
            required = payload.get("required") or []
            if kind == "custom":
                body = f"⏰ <b>Reminder</b>\n<b>{title}</b>\n"
                if payload.get("note"):
                    body += html.escape(str(payload["note"])[:600]) + "\n"
                if payload.get("when"):
                    body += f"<i>(set for {html.escape(str(payload['when']))})</i>\n"
            elif kind == "event_date":
                body = (f"📅 <b>Event coming up</b>\n<b>{title}</b>\n"
                        f"When: {html.escape(str(payload.get('when', '')))}\n")
            else:  # deadline
                body = (f"⏰ <b>Application deadline approaching</b>\n<b>{title}</b>\n"
                        f"Deadline: {html.escape(str(payload.get('deadline', '')))}\n")
            if required:
                body += "Bring: " + html.escape(", ".join(map(str, required))[:300]) + "\n"
            if url:
                body += f"Link: {html.escape(str(url))}"
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
    # Error reporting
    # ------------------------------------------------------------------
    async def _notify_admins(self, app: "Application", text: str,
                             exclude: int | None = None) -> None:
        for cid in self.cfg.admins:
            if cid == exclude:
                continue
            try:
                await app.bot.send_message(cid, text, parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                pass

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Global handler: any uncaught error in a handler is logged and, for
        service issues (API credits/rate/auth), reported to the admins."""
        exc = getattr(ctx, "error", None)
        log.error("handler error", exc_info=exc)
        friendly, infra = _error_message(exc)
        ctx.application.bot_data["last_error"] = (
            f"{type(exc).__name__}: {exc}" if exc else "unknown")
        chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
        # Tell the user something broke (if we know the chat).
        if chat_id is not None:
            try:
                await ctx.bot.send_message(
                    chat_id, "⚠️ " + html.escape(friendly),
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                pass
        if infra:
            await self._notify_admins(
                ctx.application, "⚠️ <b>Service issue</b>\n" + html.escape(friendly),
                exclude=chat_id)

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


def _error_message(exc: BaseException | None) -> tuple[str, bool]:
    """Return (friendly message, is_infrastructure). Infra errors (API credits,
    rate limits, auth) are worth pinging the admins about."""
    if exc is None:
        return "unknown error", False
    name = type(exc).__name__
    s = str(exc)
    low = s.lower()
    if any(k in low for k in ("credit balance", "insufficient", "billing",
                              "quota", "payment")):
        return ("💳 Anthropic API: out of credits / billing problem. Add "
                "credit at console.anthropic.com — analysis resumes once it's "
                "topped up.", True)
    if name == "RateLimitError" or "rate limit" in low or "429" in s:
        return ("⏳ Anthropic API rate limit reached. It retries automatically; "
                "if it persists, slow down or raise your plan limit.", True)
    if name == "AuthenticationError" or "invalid x-api-key" in low or (
            "authentication" in low) or "401" in s:
        return ("🔑 Anthropic API key was rejected. Check ANTHROPIC_API_KEY in "
                ".env, then ./manage.sh restart.", True)
    if "overloaded" in low or "529" in s:
        return ("🌩️ Anthropic API is overloaded right now. Retrying shortly.",
                True)
    return f"{name}: {s[:300]}", False


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


def _format_catch(result: Result, sheet_url: str | None = None) -> str:
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

    web = a.get("_web_sources") or []
    if web:
        parts.append("<i>🔎 Enriched & verified from " + str(len(web))
                     + " web source(s).</i>")

    if sheet_url:
        row = f" (row {result.sheet_row})" if getattr(result, "sheet_row", None) else ""
        parts.append(f'<i>Saved to <a href="{html.escape(sheet_url)}">your '
                     f"Google Sheet</a>{row}.</i>")
    else:
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
    ("calendar", "Calendar view of all reminders"),
    ("status", "Bot health & sheet links"),
    ("stats", "Totals, checked, removed, avg time-to-check"),
    ("cookies", "Login/cookie freshness (LinkedIn, etc.)"),
    ("logs", "Recent logs (admin)"),
    ("allow", "Allow a chat id (admin)"),
    ("deny", "Remove a chat id (admin)"),
    ("allowlist", "Show allowed chats (admin)"),
    ("login", "Authenticate this chat"),
    ("logout", "End this chat's session"),
    ("id", "Show your chat id (no login needed)"),
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
    app.add_handler(CommandHandler("id", bot.cmd_id))
    app.add_handler(CommandHandler("login", bot.cmd_login))
    app.add_handler(CommandHandler("logout", bot.cmd_logout))
    app.add_handler(CommandHandler("allow", bot.cmd_allow))
    app.add_handler(CommandHandler("deny", bot.cmd_deny))
    app.add_handler(CommandHandler("allowlist", bot.cmd_allowlist))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("stats", bot.cmd_stats))
    app.add_handler(CommandHandler("cookies", bot.cmd_cookies))
    app.add_handler(CommandHandler("cookie", bot.cmd_cookies))
    app.add_handler(CommandHandler("logs", bot.cmd_logs))
    app.add_handler(CommandHandler("sheets", bot.cmd_sheets))
    app.add_handler(CommandHandler("deadlines", bot.cmd_deadlines))
    app.add_handler(CommandHandler("calendar", bot.cmd_calendar))
    app.add_handler(CommandHandler("reminders", bot.cmd_calendar))
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

    app.add_error_handler(bot.on_error)

    if app.job_queue:
        app.job_queue.run_repeating(bot.reminder_tick, interval=60, first=10)
        # Warn admins once a day if a login cookie is expiring/expired.
        app.job_queue.run_repeating(bot.cookie_check, interval=86400, first=120)
        # Sync the sheets (checkboxes, deletions, time-to-check stats).
        from .sheet_sync import SheetSync
        sync = SheetSync(bot.pipeline.sheets, bot.store, cfg.timezone)
        app.job_queue.run_repeating(sync.tick, interval=60, first=25)
    return app
