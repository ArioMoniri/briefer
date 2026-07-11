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
    admin_chat_ids: set[int]
    login_password: str
    bootstrap: bool

    # Anthropic
    anthropic_api_key: str
    model: str
    verify_model: str

    # Google Sheets
    google_auth_mode: str          # "service_account" | "oauth"
    service_account_file: str
    oauth_client_file: str
    token_file: str
    articles_sheet_id: str
    events_sheet_id: str

    # Company context
    company_name: str
    company_url: str
    company_focus: str

    # Media (tweets / video)
    twitter_bearer_token: str
    twitter_consumer_key: str
    twitter_consumer_secret: str
    enable_transcription: bool
    whisper_model: str
    transcription_max_seconds: int
    media_max_bytes: int
    video_keyframes: int
    enable_gallery_dl: bool
    enable_browser_fallback: bool
    cookies_file: str
    browser_profile_dir: str
    browser_storage_state: str

    # Nested-link safety
    follow_nested_links: bool
    max_nested_links: int
    enable_link_guard: bool
    link_guard_model: str
    google_safe_browsing_key: str

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
        if not self.bootstrap:
            if self.google_auth_mode == "oauth":
                if not self.token_path.exists():
                    errors.append(
                        f"OAuth token file not found: {self.token_path}. Run "
                        "`./manage.sh google-auth` to log in with your Google "
                        "account (it prints a link)."
                    )
            else:
                if not self.service_account_path.exists():
                    errors.append(
                        f"Google service-account file not found: "
                        f"{self.service_account_path}. Sheets will fail without it."
                    )
        if errors:
            raise ConfigError("\n  - " + "\n  - ".join(errors))

    @property
    def admins(self) -> set[int]:
        # If no explicit admins, the operator ids in ALLOWED_CHAT_IDS are admins.
        return self.admin_chat_ids or self.allowed_chat_ids

    def _resolve(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.project_root / p)

    @property
    def service_account_path(self) -> Path:
        return self._resolve(self.service_account_file)

    @property
    def oauth_client_path(self) -> Path:
        return self._resolve(self.oauth_client_file)

    @property
    def token_path(self) -> Path:
        return self._resolve(self.token_file)

    @property
    def cookies_path(self) -> str:
        """Absolute path to cookies.txt if it exists, else empty string."""
        if not self.cookies_file:
            return ""
        p = self._resolve(self.cookies_file)
        return str(p) if p.exists() else ""

    @property
    def browser_profile_path(self) -> str:
        """Absolute path to the persistent browser profile dir if it exists."""
        if not self.browser_profile_dir:
            return ""
        p = self._resolve(self.browser_profile_dir)
        return str(p) if p.is_dir() else ""

    @property
    def browser_storage_state_path(self) -> str:
        if not self.browser_storage_state:
            return ""
        p = self._resolve(self.browser_storage_state)
        return str(p) if p.exists() else ""


def load_config() -> Config:
    cfg = Config(
        project_root=_PROJECT_ROOT,
        telegram_token=_get("TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=set(_get_int_list("ALLOWED_CHAT_IDS")),
        admin_chat_ids=set(_get_int_list("ADMIN_CHAT_IDS")),
        login_password=_get("LOGIN_PASSWORD"),
        bootstrap=_get_bool("BRIEFER_BOOTSTRAP", False),
        anthropic_api_key=_get("ANTHROPIC_API_KEY"),
        model=_get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        verify_model=_get("ANTHROPIC_VERIFY_MODEL", "claude-opus-4-8"),
        google_auth_mode=_get("GOOGLE_AUTH_MODE", "service_account").lower(),
        service_account_file=_get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"),
        oauth_client_file=_get("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json"),
        token_file=_get("GOOGLE_TOKEN_FILE", "token.json"),
        articles_sheet_id=_get("ARTICLES_SHEET_ID"),
        events_sheet_id=_get("EVENTS_SHEET_ID"),
        twitter_bearer_token=_get("TWITTER_BEARER_TOKEN"),
        twitter_consumer_key=_get("TWITTER_CONSUMER_KEY"),
        twitter_consumer_secret=_get("TWITTER_CONSUMER_SECRET"),
        enable_transcription=_get_bool("ENABLE_TRANSCRIPTION", True),
        whisper_model=_get("WHISPER_MODEL", "base"),
        transcription_max_seconds=_get_int("TRANSCRIPTION_MAX_SECONDS", 1800),
        media_max_bytes=_get_int("MEDIA_MAX_BYTES", 50_000_000),
        video_keyframes=_get_int("VIDEO_KEYFRAMES", 4),
        enable_gallery_dl=_get_bool("ENABLE_GALLERY_DL", True),
        # On by default but a no-op until Playwright + a browser are installed
        # (./manage.sh enable-browser).
        enable_browser_fallback=_get_bool("ENABLE_BROWSER_FALLBACK", True),
        # Netscape cookies.txt shared by the browser / yt-dlp / gallery-dl so
        # logged-in content (LinkedIn, Instagram, private X…) is accessible.
        cookies_file=_get("COOKIES_FILE", "cookies.txt"),
        # Persistent Chromium profile — log in once (./manage.sh browser-login)
        # and every render stays logged in. Empty string disables.
        browser_profile_dir=_get("BROWSER_PROFILE_DIR", "browser_profile"),
        browser_storage_state=_get("BROWSER_STORAGE_STATE", "storage_state.json"),
        # Follow links found INSIDE a post to their article — after a safety
        # gate (SSRF + heuristics + optional Safe Browsing + a cheap guard LLM).
        follow_nested_links=_get_bool("FOLLOW_NESTED_LINKS", True),
        max_nested_links=_get_int("MAX_NESTED_LINKS", 3),
        enable_link_guard=_get_bool("ENABLE_LINK_GUARD", True),
        link_guard_model=_get("LINK_GUARD_MODEL", "claude-haiku-4-5-20251001"),
        google_safe_browsing_key=_get("GOOGLE_SAFE_BROWSING_KEY", ""),
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
