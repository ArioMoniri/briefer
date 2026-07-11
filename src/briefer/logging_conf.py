"""Logging setup: rotating file + stderr, secrets redacted."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Patterns that must never reach the logs.
_REDACT = [
    re.compile(r"(bot)?\d{6,}:[A-Za-z0-9_\-]{30,}"),  # telegram tokens
    re.compile(r"sk-[A-Za-z0-9\-_]{20,}"),            # api keys
]


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for pat in _REDACT:
            if pat.search(msg):
                record.msg = pat.sub("***REDACTED***", msg)
                record.args = ()
        return True


def setup_logging(level: str, data_dir: Path) -> logging.Logger:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redact = _RedactFilter()

    fh = RotatingFileHandler(
        log_dir / "briefer.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.addFilter(redact)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.addFilter(redact)

    root.addHandler(fh)
    root.addHandler(sh)

    # httpx is noisy at INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger("briefer")
