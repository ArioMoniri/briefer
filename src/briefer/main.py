"""Entrypoint: wire everything together and run the bot (long-polling)."""
from __future__ import annotations

import logging
import sys

from telegram.ext import Application, CommandHandler

from .config import load_config, ConfigError
from .logging_conf import setup_logging
from .enrich import Enricher
from .llm import LLM
from .pipeline import Pipeline
from .storage import Store
from .telegram_bot import BrieferBot, build_application


def _run_bootstrap(cfg, log) -> None:
    """Minimal bot so a new operator can discover their chat id safely."""
    log.warning("BOOTSTRAP MODE — allowlist bypassed. Use /whoami, then set "
                "ALLOWED_CHAT_IDS and BRIEFER_BOOTSTRAP=0 and restart.")
    app = Application.builder().token(cfg.telegram_token).build()

    async def whoami(update, ctx):
        c = update.effective_chat
        await update.message.reply_text(
            f"chat id: {c.id}\nAdd this to ALLOWED_CHAT_IDS in .env, set "
            f"BRIEFER_BOOTSTRAP=0, then restart the service."
        )

    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", whoami))
    app.run_polling(allowed_updates=["message"])


def main() -> int:
    cfg = load_config()
    log = setup_logging(cfg.log_level, cfg.data_dir)
    log.info("Briefer starting (bootstrap=%s)", cfg.bootstrap)

    try:
        cfg.validate()
    except ConfigError as exc:
        log.error("Configuration error:%s", exc)
        if not cfg.bootstrap:
            return 2

    if cfg.bootstrap:
        _run_bootstrap(cfg, log)
        return 0

    # Lazy import so a missing google dep in bootstrap doesn't block discovery.
    from .sheets import SheetsClient

    store = Store(cfg.db_path)
    llm = LLM(cfg.anthropic_api_key, cfg.model, cfg.verify_model)
    enricher = Enricher(cfg.max_download_bytes)
    try:
        sheets = SheetsClient(
            str(cfg.service_account_path), cfg.articles_sheet_id, cfg.events_sheet_id
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Could not initialise Google Sheets: %s", exc)
        return 3

    pipeline = Pipeline(cfg, llm, enricher, sheets, store)
    bot = BrieferBot(cfg, pipeline, store)
    app = build_application(cfg, bot)

    log.info("Briefer is up. Articles=%s Events=%s",
             sheets.articles_id, sheets.events_id)
    app.run_polling(allowed_updates=["message", "callback_query"],
                    drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
