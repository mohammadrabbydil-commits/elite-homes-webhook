"""Central configuration for the Elite Homes USA automation POC.

All runtime settings come from environment variables, loaded from `config/.env`
(see `config/.env.example` for the template). Nothing secret is hard-coded here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# --- Paths -----------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

ENV_FILE = CONFIG_DIR / ".env"
POSTS_FILE = DATA_DIR / "posts.json"
TEMPLATES_FILE = CONFIG_DIR / "message_templates.json"

LOG_DIR.mkdir(exist_ok=True)

# Load config/.env if present; real environment variables always win.
load_dotenv(ENV_FILE, override=False)


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _get_int(key: str, default: int) -> int:
    raw = _get(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Resolved application settings."""

    # --- Facebook Graph API ---
    fb_app_id: str = field(default_factory=lambda: _get("FB_APP_ID"))
    fb_app_secret: str = field(default_factory=lambda: _get("FB_APP_SECRET"))
    fb_page_access_token: str = field(default_factory=lambda: _get("FB_PAGE_ACCESS_TOKEN"))
    fb_page_id: str = field(default_factory=lambda: _get("FB_PAGE_ID"))
    graph_api_version: str = field(default_factory=lambda: _get("FB_GRAPH_API_VERSION", "v21.0"))

    # --- Personal profile (outreach module only) ---
    fb_user_email: str = field(default_factory=lambda: _get("FB_USER_EMAIL"))
    fb_user_password: str = field(default_factory=lambda: _get("FB_USER_PASSWORD"))

    # --- Messenger auto-reply ---
    fb_verify_token: str = field(default_factory=lambda: _get("FB_VERIFY_TOKEN", "elite-homes-verify"))
    autoreply_enabled: bool = field(
        default_factory=lambda: _get("AUTOREPLY_ENABLED", "false").lower() == "true"
    )
    autoreply_min_delay_seconds: int = field(
        default_factory=lambda: _get_int("AUTOREPLY_MIN_DELAY_SECONDS", 45)
    )
    autoreply_max_delay_seconds: int = field(
        default_factory=lambda: _get_int("AUTOREPLY_MAX_DELAY_SECONDS", 90)
    )
    autoreply_max_per_conversation: int = field(
        default_factory=lambda: _get_int("AUTOREPLY_MAX_PER_CONVERSATION", 1)
    )
    business_hours_start: int = field(default_factory=lambda: _get_int("BUSINESS_HOURS_START", 8))
    business_hours_end: int = field(default_factory=lambda: _get_int("BUSINESS_HOURS_END", 20))

    # --- Dashboard ---
    dashboard_username: str = field(default_factory=lambda: _get("DASHBOARD_USERNAME", "admin"))
    dashboard_password: str = field(default_factory=lambda: _get("DASHBOARD_PASSWORD", "elite2024"))

    # --- Database ---
    database_url: str = field(
        default_factory=lambda: _get("DATABASE_URL", "sqlite:///./elite_homes.db")
    )

    # --- Outreach throttling (conservative defaults) ---
    outreach_daily_limit: int = field(default_factory=lambda: _get_int("OUTREACH_DAILY_LIMIT", 20))
    outreach_min_delay_seconds: int = field(
        default_factory=lambda: _get_int("OUTREACH_MIN_DELAY_SECONDS", 90)
    )
    outreach_max_delay_seconds: int = field(
        default_factory=lambda: _get_int("OUTREACH_MAX_DELAY_SECONDS", 300)
    )

    # --- Misc ---
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))
    timezone: str = field(default_factory=lambda: _get("TIMEZONE", "America/New_York"))

    @property
    def graph_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"

    def missing_graph_settings(self) -> list[str]:
        """Names of the Graph API settings that are still unset."""
        required = {
            "FB_APP_ID": self.fb_app_id,
            "FB_APP_SECRET": self.fb_app_secret,
            "FB_PAGE_ACCESS_TOKEN": self.fb_page_access_token,
            "FB_PAGE_ID": self.fb_page_id,
        }
        return [name for name, value in required.items() if not value]


settings = Settings()


def configure_logging(level: str | None = None) -> None:
    """Configure root logging to both stderr and `logs/elite_homes.log`."""
    resolved = (level or settings.log_level).upper()
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_DIR / "elite_homes.log", encoding="utf-8"),
        ],
    )
    # APScheduler and urllib3 are noisy at INFO.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
