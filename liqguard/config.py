"""Runtime settings from environment variables (see `.env.example`)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


def _bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    telegram_token: Optional[str] = field(default=None, repr=False)
    meta_api_key: Optional[str] = field(default=None, repr=False)
    session_secret: Optional[str] = field(default=None, repr=False)

    key_file: Optional[Path] = None
    db_path: str = "liqguard.db"
    poll_interval: int = 30

    dashboard_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    public_url: Optional[str] = None
    bot_username: Optional[str] = None
    session_ttl: int = 12 * 3600
    login_link_ttl: int = 300

    protect_enabled: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        key_file = env.get("LIQGUARD_KEY_FILE")
        return cls(
            telegram_token=env.get("TELEGRAM_BOT_TOKEN") or None,
            meta_api_key=env.get("META_API_KEY") or None,
            session_secret=env.get("LIQGUARD_SESSION_SECRET") or None,
            key_file=Path(key_file).expanduser() if key_file else None,
            db_path=env.get("LIQGUARD_DB_PATH", "liqguard.db"),
            poll_interval=_int(env.get("LIQGUARD_POLL_INTERVAL"), 30),
            dashboard_enabled=_bool(env.get("LIQGUARD_DASHBOARD")),
            dashboard_host=env.get("LIQGUARD_DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=_int(env.get("LIQGUARD_DASHBOARD_PORT"), 8765),
            public_url=(env.get("LIQGUARD_PUBLIC_URL") or "").rstrip("/") or None,
            bot_username=(env.get("LIQGUARD_BOT_USERNAME") or "").lstrip("@") or None,
            session_ttl=_int(env.get("LIQGUARD_SESSION_TTL"), 12 * 3600),
            login_link_ttl=_int(env.get("LIQGUARD_LOGIN_LINK_TTL"), 300),
            protect_enabled=_bool(env.get("LIQGUARD_PROTECT_ENABLED")),
        )

    @property
    def dashboard_url(self) -> str:
        return self.public_url or f"http://{self.dashboard_host}:{self.dashboard_port}"

    @property
    def secure_cookies(self) -> bool:
        return self.dashboard_url.startswith("https://")
