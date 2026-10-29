"""
Validated edits to a user's alert settings: the one place Telegram (/settings,
the 🔔 Alerts menu) and the web dashboard settings panel change thresholds,
wake-up escalation and quiet hours.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Optional

from liqguard.i18n import EN, t
from liqguard.models import UserThreshold

TOGGLES = {"escalation": "escalation_enabled", "adaptive": "adaptive_thresholds", "stops": "stop_loss_checks"}
TIERS = {"warning": "warning_distance_pct", "critical": "critical_distance_pct", "emergency": "emergency_distance_pct"}
KEYS = ("interval", "max", "risk", "maxmargin", "safety", "quiet", *TOGGLES, *TIERS)

PRESETS = {
    "warning": (15, 20, 25, 30),
    "critical": (8, 10, 12, 15),
    "emergency": (3, 5, 7),
    "interval": (1, 2, 5, 10),
    "max": (3, 5, 10),
    "quiet": ("off", "23-7", "0-8", "22-6"),
}

_QUIET = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def _number(value: str) -> float:
    return float(str(value).strip().rstrip("%"))


def apply_setting(threshold: UserThreshold, key: str, value: str) -> Optional[UserThreshold]:
    """A new `UserThreshold` with one setting changed, or None if the key or value is invalid."""
    key, value = str(key).strip().lower(), str(value).strip().lower()
    try:
        if key == "interval":
            minutes = _number(value)
            return dataclasses.replace(threshold, escalation_interval_minutes=minutes) if 0.5 <= minutes <= 60 else None
        if key == "max":
            n = int(value)
            return dataclasses.replace(threshold, escalation_max_reminders=n) if 1 <= n <= 20 else None
        if key == "risk":
            pct = _number(value)
            return dataclasses.replace(threshold, risk_per_trade_pct=pct) if 0.1 <= pct <= 20 else None
        if key == "maxmargin":
            pct = _number(value)
            return dataclasses.replace(threshold, max_margin_pct=pct) if 1 <= pct <= 100 else None
        if key == "safety":
            pct = _number(value)
            return dataclasses.replace(threshold, safety_target_pct=pct) if 1 <= pct <= 90 else None
        if key in TIERS:
            pct = _number(value)
            if not 0.5 <= pct <= 90:
                return None
            updated = dataclasses.replace(threshold, **{TIERS[key]: pct})
            ok = updated.emergency_distance_pct < updated.critical_distance_pct < updated.warning_distance_pct
            return updated if ok else None
    except ValueError:
        return None
    if key == "quiet":
        if value in ("off", "none"):
            return dataclasses.replace(threshold, quiet_hours_start=None, quiet_hours_end=None)
        m = _QUIET.match(value)
        if not m:
            return None
        start, end = int(m.group(1)), int(m.group(2))
        if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
            return None
        return dataclasses.replace(threshold, quiet_hours_start=start, quiet_hours_end=end)
    if key in TOGGLES:
        current = getattr(threshold, TOGGLES[key])
        if value == "toggle":
            new = not current
        elif value in ("on", "off"):
            new = value == "on"
        else:
            return None
        return dataclasses.replace(threshold, **{TOGGLES[key]: new})
    return None


def quiet_window(value: str, lang: str = EN) -> str:
    """`"23-7"` → `23:00–07:00`; `"off"` → Off / 關閉."""
    if str(value) in ("off", "none", ""):
        return t(lang, "settings.quiet_off")
    start, end = (int(x) for x in str(value).split("-", 1))
    return f"{start:02d}:00–{end:02d}:00"


def quiet_label(threshold: UserThreshold, lang: str = EN) -> str:
    if threshold.quiet_hours_start is None or threshold.quiet_hours_end is None:
        return t(lang, "settings.quiet_off")
    return quiet_window(f"{threshold.quiet_hours_start}-{threshold.quiet_hours_end}", lang)


def settings_dict(threshold: UserThreshold, lang: str = EN) -> dict:
    """What the dashboard settings panel shows (no secrets here, only numbers and toggles)."""
    return {
        "warning": threshold.warning_distance_pct,
        "critical": threshold.critical_distance_pct,
        "emergency": threshold.emergency_distance_pct,
        "safety": threshold.safety_target_pct,
        "interval": threshold.escalation_interval_minutes,
        "max": threshold.escalation_max_reminders,
        "escalation": threshold.escalation_enabled,
        "adaptive": threshold.adaptive_thresholds,
        "stops": threshold.stop_loss_checks,
        "risk": threshold.risk_per_trade_pct,
        "maxmargin": threshold.max_margin_pct,
        "quiet": None if threshold.quiet_hours_start is None else f"{threshold.quiet_hours_start}-{threshold.quiet_hours_end}",
        "quiet_label": quiet_label(threshold, lang),
        "quiet_utc_offset_hours": threshold.quiet_utc_offset_hours,
        "presets": {k: list(v) for k, v in PRESETS.items()},
    }
