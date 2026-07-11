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

    from .media import TweetExtractor, VideoTranscriber

    store = Store(cfg.db_path)
    llm = LLM(cfg.anthropic_api_key, cfg.model, cfg.verify_model)
    tweets = TweetExtractor(cfg.twitter_bearer_token, cfg.twitter_consumer_key,
                            cfg.twitter_consumer_secret)
    cookies = cfg.cookies_path
    if cookies:
        log.info("Using cookies file for authenticated fetches: %s", cookies)
    transcriber = VideoTranscriber(
        cfg.enable_transcription, cfg.whisper_model,
        cfg.transcription_max_seconds, cfg.media_max_bytes,
        keyframes=cfg.video_keyframes, cookies_file=cookies)
    enricher = Enricher(cfg.max_download_bytes, tweet_extractor=tweets,
                        transcriber=transcriber,
                        enable_gallery_dl=cfg.enable_gallery_dl,
                        enable_browser=cfg.enable_browser_fallback,
                        cookies_file=cookies,
                        browser_profile_dir=cfg.browser_profile_path,
                        browser_storage_state=cfg.browser_storage_state_path,
                        llm=llm,
                        follow_nested_links=cfg.follow_nested_links,
                        max_nested_links=cfg.max_nested_links,
                        enable_link_guard=cfg.enable_link_guard,
                        link_guard_model=cfg.link_guard_model,
                        safe_browsing_key=cfg.google_safe_browsing_key)
    if cfg.browser_profile_path:
        log.info("Using persistent browser profile: %s", cfg.browser_profile_path)
    # Reuse previously auto-created sheets so restarts don't spawn new empty
    # ones (and lose your data). Env IDs always win; otherwise fall back to the
    # ids we saved last time.
    import json
    ids_path = cfg.data_dir / "sheet_ids.json"
    saved: dict[str, str] = {}
    if ids_path.exists():
        try:
            saved = json.loads(ids_path.read_text())
        except Exception:  # noqa: BLE001
            saved = {}
    articles_id = cfg.articles_sheet_id or saved.get("articles", "")
    events_id = cfg.events_sheet_id or saved.get("events", "")
    try:
        sheets = SheetsClient(
            cfg.google_auth_mode, str(cfg.service_account_path),
            str(cfg.token_path), articles_id, events_id
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Could not initialise Google Sheets: %s", exc)
        return 3
    try:
        ids_path.write_text(json.dumps(
            {"articles": sheets.articles_id, "events": sheets.events_id}))
    except Exception:  # noqa: BLE001
        pass

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
