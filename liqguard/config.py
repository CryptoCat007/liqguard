"""Runtime settings from environment variables (see `.env.example`)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from liqguard.models import UserThreshold


def _bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _float(value: Optional[str], default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _hours(value: Optional[str]) -> tuple[int, int]:
    try:
        start, end = (int(x) % 24 for x in (value or "21-1").split("-", 1))
        return start, end
    except ValueError:
        return 21, 1


def _path(value: Optional[str]) -> Optional[Path]:
    return Path(value).expanduser() if value else None


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

    data_dir: Optional[Path] = None
    key_file: Optional[Path] = None
    db_path: str = "liqguard.db"
    audit_log_path: Optional[Path] = None
    poll_interval: int = 30

    dashboard_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    public_url: Optional[str] = None
    bot_username: Optional[str] = None
    session_ttl: int = 12 * 3600
    login_link_ttl: int = 300

    protect_enabled: bool = False

    taker_fee_binance: float = 0.0005
    taker_fee_hyperliquid: float = 0.00045
    funding_refresh: int = 60
    prefunding_lead_minutes: int = 30

    wick_days: int = 30
    wick_refresh: int = 3600
    low_liquidity_hours: tuple[int, int] = (21, 1)
    stop_buffer_pct: float = 0.5
    escalation_interval_minutes: float = 2.0
    escalation_max_reminders: int = 5
    risk_per_trade_pct: float = 2.0
    max_margin_pct: float = 25.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        data_dir = _path(env.get("LIQGUARD_DATA_DIR"))
        key_file = _path(env.get("LIQGUARD_KEY_FILE")) or (data_dir / "encryption.key" if data_dir else None)
        db_path = env.get("LIQGUARD_DB_PATH") or str(data_dir / "liqguard.db" if data_dir else "liqguard.db")
        audit_log = _path(env.get("LIQGUARD_AUDIT_LOG")) or (data_dir / "audit.log" if data_dir else None)
        return cls(
            telegram_token=env.get("TELEGRAM_BOT_TOKEN") or None,
            meta_api_key=env.get("META_API_KEY") or None,
            session_secret=env.get("LIQGUARD_SESSION_SECRET") or None,
            data_dir=data_dir,
            key_file=key_file,
            db_path=str(Path(db_path).expanduser()),
            audit_log_path=audit_log,
            poll_interval=_int(env.get("LIQGUARD_POLL_INTERVAL"), 30),
            dashboard_enabled=_bool(env.get("LIQGUARD_DASHBOARD")),
            dashboard_host=env.get("LIQGUARD_DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=_int(env.get("LIQGUARD_DASHBOARD_PORT") or env.get("PORT"), 8765),
            public_url=(env.get("LIQGUARD_PUBLIC_URL") or "").rstrip("/") or None,
            bot_username=(env.get("LIQGUARD_BOT_USERNAME") or "").lstrip("@") or None,
            session_ttl=_int(env.get("LIQGUARD_SESSION_TTL"), 12 * 3600),
            login_link_ttl=_int(env.get("LIQGUARD_LOGIN_LINK_TTL"), 300),
            protect_enabled=_bool(env.get("LIQGUARD_PROTECT_ENABLED")),
            taker_fee_binance=_float(env.get("LIQGUARD_TAKER_FEE_BINANCE"), 0.0005),
            taker_fee_hyperliquid=_float(env.get("LIQGUARD_TAKER_FEE_HYPERLIQUID"), 0.00045),
            funding_refresh=max(10, _int(env.get("LIQGUARD_FUNDING_REFRESH"), 60)),
            prefunding_lead_minutes=_int(env.get("LIQGUARD_PREFUNDING_LEAD_MINUTES"), 30),
            wick_days=min(30, max(1, _int(env.get("LIQGUARD_WICK_DAYS"), 30))),
            wick_refresh=max(300, _int(env.get("LIQGUARD_WICK_REFRESH"), 3600)),
            low_liquidity_hours=_hours(env.get("LIQGUARD_LOW_LIQUIDITY_HOURS_UTC")),
            stop_buffer_pct=max(0.0, _float(env.get("LIQGUARD_STOP_BUFFER_PCT"), 0.5)),
            escalation_interval_minutes=max(0.5, _float(env.get("LIQGUARD_ESCALATION_INTERVAL_MINUTES"), 2.0)),
            escalation_max_reminders=max(1, _int(env.get("LIQGUARD_ESCALATION_MAX_REMINDERS"), 5)),
            risk_per_trade_pct=min(max(_float(env.get("LIQGUARD_RISK_PER_TRADE_PCT"), 2.0), 0.1), 20.0),
            max_margin_pct=min(max(_float(env.get("LIQGUARD_MAX_MARGIN_PCT"), 25.0), 1.0), 100.0),
        )

    def default_threshold(self) -> UserThreshold:
        """Thresholds for a new user (escalation and sizing defaults come from the environment)."""
        return UserThreshold(
            escalation_interval_minutes=self.escalation_interval_minutes,
            escalation_max_reminders=self.escalation_max_reminders,
            risk_per_trade_pct=self.risk_per_trade_pct,
            max_margin_pct=self.max_margin_pct,
        )

    def ensure_data_dirs(self) -> None:
        """Create the data directory and the parents of every data file (DB, master key, audit log)."""
        dirs = [self.data_dir] if self.data_dir else []
        dirs += [Path(p).parent for p in (self.db_path, self.key_file, self.audit_log_path) if p]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    @property
    def taker_fees(self) -> dict[str, float]:
        return {"binance": self.taker_fee_binance, "hyperliquid": self.taker_fee_hyperliquid, "demo": self.taker_fee_binance}

    @property
    def dashboard_url(self) -> str:
        if self.public_url:
            return self.public_url
        host = "127.0.0.1" if self.dashboard_host in ("", "0.0.0.0", "::") else self.dashboard_host
        return f"http://{host}:{self.dashboard_port}"

    @property
    def dashboard_login_available(self) -> bool:
        """Login links only make sense if a dashboard is served here or reachable at LIQGUARD_PUBLIC_URL."""
        return bool(self.public_url) or self.dashboard_enabled

    @property
    def secure_cookies(self) -> bool:
        return self.dashboard_url.startswith("https://")
