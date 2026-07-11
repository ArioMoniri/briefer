"""Central configuration, loaded once from the environment / .env file.

Everything the rest of the app needs is validated here so that a
misconfiguration fails loudly at startup instead of mid-request.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level above this package's parent).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _get_int_list(name: str) -> list[int]:
    raw = _get(name)
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass
class Config:
    project_root: Path

    # Telegram
    telegram_token: str
    allowed_chat_ids: set[int]
    login_password: str
    bootstrap: bool

    # Anthropic
    anthropic_api_key: str
    model: str
    verify_model: str

    # Google Sheets
    service_account_file: str
    articles_sheet_id: str
    events_sheet_id: str

    # Company context
    company_name: str
    company_url: str
    company_focus: str

    # Behaviour
    max_download_bytes: int
    rate_limit_per_minute: int
    deadline_reminder_hours: list[int]
    timezone: str
    data_dir: Path
    log_level: str

    downloads_dir: Path = field(init=False)
    db_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = (self.project_root / self.data_dir).resolve() if not Path(
            self.data_dir
        ).is_absolute() else Path(self.data_dir)
        self.downloads_dir = self.data_dir / "downloads"
        self.db_path = self.data_dir / "briefer.db"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    # --- validation --------------------------------------------------
    def validate(self) -> None:
        errors: list[str] = []
        if not self.telegram_token:
            errors.append("TELEGRAM_BOT_TOKEN is required.")
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is required.")
        if not self.login_password:
            errors.append("LOGIN_PASSWORD is required.")
        if not self.allowed_chat_ids and not self.bootstrap:
            errors.append(
                "ALLOWED_CHAT_IDS is empty. Set BRIEFER_BOOTSTRAP=1 to run "
                "in discovery mode and use /whoami to find your id."
            )
        sa = self.service_account_path
        if not sa.exists() and not self.bootstrap:
            errors.append(
                f"Google service-account file not found: {sa}. "
                "Sheets features will fail without it."
            )
        if errors:
            raise ConfigError("\n  - " + "\n  - ".join(errors))

    @property
    def service_account_path(self) -> Path:
        p = Path(self.service_account_file)
        return p if p.is_absolute() else (self.project_root / p)


def load_config() -> Config:
    cfg = Config(
        project_root=_PROJECT_ROOT,
        telegram_token=_get("TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=set(_get_int_list("ALLOWED_CHAT_IDS")),
        login_password=_get("LOGIN_PASSWORD"),
        bootstrap=_get_bool("BRIEFER_BOOTSTRAP", False),
        anthropic_api_key=_get("ANTHROPIC_API_KEY"),
        model=_get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        verify_model=_get("ANTHROPIC_VERIFY_MODEL", "claude-opus-4-8"),
        service_account_file=_get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"),
        articles_sheet_id=_get("ARTICLES_SHEET_ID"),
        events_sheet_id=_get("EVENTS_SHEET_ID"),
        company_name=_get("COMPANY_NAME", "Vivax"),
        company_url=_get("COMPANY_URL", "https://getvivax.com"),
        company_focus=_get(
            "COMPANY_FOCUS",
            "Medical AI, medical education, clinical simulation and OR intelligence.",
        ),
        max_download_bytes=_get_int("MAX_DOWNLOAD_BYTES", 15_000_000),
        rate_limit_per_minute=_get_int("RATE_LIMIT_PER_MINUTE", 20),
        deadline_reminder_hours=_get_int_list("DEADLINE_REMINDER_HOURS") or [72, 24, 3],
        timezone=_get("TIMEZONE", "UTC"),
        data_dir=Path(_get("DATA_DIR", "data")),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
    )
    return cfg
