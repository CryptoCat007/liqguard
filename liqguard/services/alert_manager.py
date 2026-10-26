"""Alert manager with cooldown and deduplication."""

import logging
from datetime import datetime, timedelta
from typing import Optional

from liqguard.models import Alert, AlertSeverity

logger = logging.getLogger(__name__)


class AlertManager:
    """
    Manages alert state, cooldowns, and deduplication.

    Prevents alert spam by:
    - Enforcing cooldown periods between alerts
    - Only re-alerting if severity increases
    - Tracking alert history per position
    """

    def __init__(self, default_cooldown_seconds: int = 300):
        """
        Initialize alert manager.

        Args:
            default_cooldown_seconds: Default cooldown between alerts (5 minutes)
        """
        self.default_cooldown = timedelta(seconds=default_cooldown_seconds)

        self._alert_history: dict[str, list[Alert]] = {}

        self._last_alert_time: dict[str, datetime] = {}
        self._last_alert_severity: dict[str, AlertSeverity] = {}

    def _get_position_key(self, alert: Alert) -> str:
        """Generate a unique key for a position."""
        return f"{alert.venue}:{alert.symbol}"

    def should_send_alert(
        self,
        alert: Alert,
        cooldown_seconds: Optional[int] = None
    ) -> bool:
        """
        Determine if an alert should be sent.

        Args:
            alert: Alert to check
            cooldown_seconds: Override cooldown period

        Returns:
            True if alert should be sent, False otherwise
        """
        key = self._get_position_key(alert)
        cooldown = timedelta(seconds=cooldown_seconds) if cooldown_seconds else self.default_cooldown

        last_time = self._last_alert_time.get(key)
        if last_time:
            time_since_last = datetime.utcnow() - last_time

            if time_since_last < cooldown:
                last_severity = self._last_alert_severity.get(key)
                if last_severity:
                    severity_order = {
                        AlertSeverity.INFO: 0,
                        AlertSeverity.WARNING: 1,
                        AlertSeverity.CRITICAL: 2,
                        AlertSeverity.EMERGENCY: 3,
                    }

                    if severity_order[alert.severity] <= severity_order[last_severity]:
                        logger.debug(
                            f"Alert suppressed for {key}: in cooldown and severity not increased"
                        )
                        return False

        return True

    def record_alert(self, alert: Alert) -> None:
        """
        Record that an alert was sent.

        Args:
            alert: Alert that was sent
        """
        key = self._get_position_key(alert)

        self._last_alert_time[key] = datetime.utcnow()
        self._last_alert_severity[key] = alert.severity

        if key not in self._alert_history:
            self._alert_history[key] = []
        self._alert_history[key].append(alert)

        if len(self._alert_history[key]) > 100:
            self._alert_history[key] = self._alert_history[key][-50:]

    def get_alert_history(
        self,
        venue: str,
        symbol: str,
        limit: int = 10
    ) -> list[Alert]:
        """
        Get alert history for a position.

        Args:
            venue: Exchange venue
            symbol: Trading symbol
            limit: Maximum alerts to return

        Returns:
            List of recent alerts, newest first
        """
        key = f"{venue}:{symbol}"
        history = self._alert_history.get(key, [])
        return list(reversed(history[-limit:]))

    def clear_position_state(self, venue: str, symbol: str) -> None:
        """
        Clear alert state for a position (e.g., when position is closed).

        Args:
            venue: Exchange venue
            symbol: Trading symbol
        """
        key = f"{venue}:{symbol}"
        self._last_alert_time.pop(key, None)
        self._last_alert_severity.pop(key, None)

    def clear_all_state(self) -> None:
        """Clear all alert state."""
        self._alert_history.clear()
        self._last_alert_time.clear()
        self._last_alert_severity.clear()

    def process_alert(
        self,
        alert: Alert,
        cooldown_seconds: Optional[int] = None
    ) -> Optional[Alert]:
        """
        Process an alert: check if it should be sent and record it.

        Args:
            alert: Alert to process
            cooldown_seconds: Override cooldown period

        Returns:
            The alert if it should be sent, None otherwise
        """
        if self.should_send_alert(alert, cooldown_seconds):
            self.record_alert(alert)
            return alert
        return None

    def get_stats(self) -> dict:
        """Get alert manager statistics."""
        total_alerts = sum(len(h) for h in self._alert_history.values())
        positions_tracked = len(self._alert_history)

        severity_counts = {s.value: 0 for s in AlertSeverity}
        for history in self._alert_history.values():
            for alert in history:
                severity_counts[alert.severity.value] += 1

        return {
            "total_alerts_sent": total_alerts,
            "positions_tracked": positions_tracked,
            "severity_breakdown": severity_counts,
        }
