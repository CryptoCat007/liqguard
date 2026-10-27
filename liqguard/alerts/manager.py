"""Per-user alert cooldown and deduplication."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from liqguard.models import Alert, AlertSeverity

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {
    AlertSeverity.INFO: 0,
    AlertSeverity.WARNING: 1,
    AlertSeverity.CRITICAL: 2,
    AlertSeverity.EMERGENCY: 3,
}

PositionKey = tuple[int, str, str]


class AlertManager:
    """
    Alert state keyed by (user_id, venue, symbol).

    Two users holding the same symbol never suppress each other's alerts, and
    history queries only ever return the requesting user's alerts.
    Within one user's position, a repeat alert is suppressed during the
    cooldown unless severity increased.
    """

    def __init__(self, default_cooldown_seconds: int = 300):
        self.default_cooldown = timedelta(seconds=default_cooldown_seconds)
        self._history: dict[PositionKey, list[Alert]] = {}
        self._last_time: dict[PositionKey, datetime] = {}
        self._last_severity: dict[PositionKey, AlertSeverity] = {}

    @staticmethod
    def _key(user_id: int, venue: str, symbol: str) -> PositionKey:
        return (int(user_id), str(venue), symbol)

    def should_send_alert(self, user_id: int, alert: Alert, cooldown_seconds: Optional[int] = None) -> bool:
        key = self._key(user_id, alert.venue, alert.symbol)
        cooldown = timedelta(seconds=cooldown_seconds) if cooldown_seconds else self.default_cooldown
        last_time = self._last_time.get(key)
        if last_time and datetime.now(timezone.utc) - last_time < cooldown:
            last = self._last_severity.get(key)
            if last and SEVERITY_ORDER[alert.severity] <= SEVERITY_ORDER[last]:
                logger.debug("Alert suppressed for user %s %s: cooldown", user_id, alert.symbol)
                return False
        return True

    def record_alert(self, user_id: int, alert: Alert) -> None:
        key = self._key(user_id, alert.venue, alert.symbol)
        self._last_time[key] = datetime.now(timezone.utc)
        self._last_severity[key] = alert.severity
        history = self._history.setdefault(key, [])
        history.append(alert)
        if len(history) > 100:
            self._history[key] = history[-50:]

    def process_alert(self, user_id: int, alert: Alert, cooldown_seconds: Optional[int] = None) -> Optional[Alert]:
        """Return the alert if it should be delivered to `user_id` (and record it), else None."""
        if self.should_send_alert(user_id, alert, cooldown_seconds):
            self.record_alert(user_id, alert)
            return alert
        return None

    def get_alert_history(self, user_id: int, venue: str, symbol: str, limit: int = 10) -> list[Alert]:
        history = self._history.get(self._key(user_id, venue, symbol), [])
        return list(reversed(history[-limit:]))

    def clear_position_state(self, user_id: int, venue: str, symbol: str) -> None:
        key = self._key(user_id, venue, symbol)
        self._last_time.pop(key, None)
        self._last_severity.pop(key, None)

    def clear_user_state(self, user_id: int) -> None:
        for store in (self._history, self._last_time, self._last_severity):
            for key in [k for k in store if k[0] == user_id]:
                del store[key]

    def get_stats(self, user_id: int) -> dict:
        mine = [h for k, h in self._history.items() if k[0] == user_id]
        counts = {s.value: 0 for s in AlertSeverity}
        for history in mine:
            for alert in history:
                counts[alert.severity.value] += 1
        return {
            "total_alerts_sent": sum(len(h) for h in mine),
            "positions_tracked": len(mine),
            "severity_breakdown": counts,
        }
