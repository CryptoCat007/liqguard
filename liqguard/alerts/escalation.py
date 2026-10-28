"""
Wake-up escalation for critical and emergency alerts.

Every critical/emergency alert opens a pending acknowledgement for its owner.
Until the owner taps "I'm awake", the alert is resent every N minutes (the
user's /settings) up to a cap, each reminder more urgent than the last. A
pending acknowledgement is closed by the owner's ack, by the position
recovering above the critical tier, or by reaching the cap.

`EscalationTracker` is clock-agnostic state (the live service passes wall
time, the demo simulation its own clock). Every lookup is keyed by owner, and
an ack id only works for the user it was issued to.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ESCALATING = ("critical", "emergency")
ACK_PREFIX = "ack:"


@dataclass
class PendingAck:
    id: str
    user_id: Optional[int]
    venue: str
    symbol: str
    severity: str
    text: str
    opened_at: float
    last_sent_at: float
    interval_seconds: float
    max_reminders: int
    reminders_sent: int = 0
    distance_pct: Optional[float] = None
    acknowledged_at: Optional[float] = None
    meta: dict = field(default_factory=dict)

    @property
    def next_due(self) -> float:
        return self.last_sent_at + self.interval_seconds

    @property
    def exhausted(self) -> bool:
        return self.reminders_sent >= self.max_reminders

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "venue": self.venue,
            "symbol": self.symbol,
            "severity": self.severity,
            "reminders_sent": self.reminders_sent,
            "max_reminders": self.max_reminders,
            "interval_seconds": self.interval_seconds,
            "distance_pct": None if self.distance_pct is None else round(self.distance_pct, 2),
            "acknowledged": self.acknowledged_at is not None,
            **self.meta,
        }


def urgency(reminder: int, max_reminders: int) -> str:
    if reminder >= max_reminders:
        return "final"
    return "urgent" if reminder >= 2 else "reminder"


def reminder_text(ack: PendingAck, reminder: int, elapsed_minutes: float, demo: bool = False) -> str:
    """Reminder `reminder` of `ack.max_reminders`; louder each time."""
    level = urgency(reminder, ack.max_reminders)
    head = {
        "reminder": "⏰ **REMINDER**",
        "urgent": "🚨🚨 **URGENT — STILL UNACKNOWLEDGED**",
        "final": "🆘🆘🆘 **FINAL WAKE-UP CALL**",
    }[level]
    dist = f" · now **{ack.distance_pct:.1f}%** from liquidation" if ack.distance_pct is not None else ""
    lines = [
        f"{head} {reminder}/{ack.max_reminders}{' · demo' if demo else ''}",
        f"**{ack.symbol}** {ack.severity.upper()} alert sent {elapsed_minutes:.0f} min ago{dist}.",
    ]
    if level == "final":
        lines.append("This is the last reminder. Tap **I'm awake** or act on the position now.")
    else:
        lines.append(f"Tap **I'm awake** to stop reminders (next in {ack.interval_seconds / 60:g} min).")
    return "\n".join(lines)


def ack_action(ack: PendingAck) -> tuple[str, str]:
    return ("✅ I'm awake", f"{ACK_PREFIX}{ack.id}")


class EscalationTracker:
    def __init__(self, id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(9)):
        self._pending: dict[tuple[Optional[int], str, str], PendingAck] = {}
        self._superseded: dict[str, tuple[Optional[int], str, str]] = {}
        self._ids = id_factory

    def open(
        self,
        user_id: Optional[int],
        venue: str,
        symbol: str,
        severity: str,
        text: str,
        now: float,
        interval_seconds: float,
        max_reminders: int,
        distance_pct: Optional[float] = None,
        meta: Optional[dict] = None,
    ) -> Optional[PendingAck]:
        """Start (or escalate) reminders for one position. A worse severity restarts the count."""
        if severity not in ESCALATING or max_reminders <= 0 or interval_seconds <= 0:
            return None
        key = (user_id, venue, symbol)
        current = self._pending.get(key)
        if current is not None and current.acknowledged_at is None and ESCALATING.index(severity) <= ESCALATING.index(current.severity):
            current.distance_pct = distance_pct
            return current
        ack = PendingAck(
            id=self._ids(), user_id=user_id, venue=venue, symbol=symbol, severity=severity, text=text,
            opened_at=now, last_sent_at=now, interval_seconds=interval_seconds, max_reminders=max_reminders,
            distance_pct=distance_pct, meta=dict(meta or {}),
        )
        if current is not None and current.acknowledged_at is None:
            self._superseded[current.id] = key
        self._pending[key] = ack
        return ack

    def due(self, now: float) -> list[PendingAck]:
        return [
            a for a in self._pending.values()
            if a.acknowledged_at is None and not a.exhausted and now >= a.next_due
        ]

    def mark_sent(self, ack: PendingAck, now: float) -> int:
        ack.reminders_sent += 1
        ack.last_sent_at = now
        return ack.reminders_sent

    def acknowledge(self, user_id: Optional[int], ack_id: str, now: float) -> Optional[PendingAck]:
        """Close the owner's pending ack with this id, or the one that superseded it when the
        position worsened (an older alert's button still works). Another user's id does nothing."""
        key = self._superseded.get(ack_id)
        if key is None:
            key = next((k for k, a in self._pending.items() if a.id == ack_id), None)
        ack = self._pending.get(key) if key is not None else None
        if ack is None or ack.user_id != user_id:
            return None
        ack.acknowledged_at = now
        del self._pending[key]
        self._forget(key)
        return ack

    def _forget(self, key: tuple) -> None:
        for old in [i for i, k in self._superseded.items() if k == key]:
            del self._superseded[old]

    def update(self, user_id: Optional[int], venue: str, symbol: str, distance_pct: float) -> None:
        ack = self._pending.get((user_id, venue, symbol))
        if ack is not None:
            ack.distance_pct = distance_pct

    def resolve(self, user_id: Optional[int], venue: str, symbol: str) -> Optional[PendingAck]:
        """The position recovered (or closed): stop reminding."""
        self._forget((user_id, venue, symbol))
        return self._pending.pop((user_id, venue, symbol), None)

    def pending_for(self, user_id: Optional[int]) -> list[PendingAck]:
        """Unacknowledged alerts, including ones whose reminders hit the cap (still acknowledgeable)."""
        return [a for a in self._pending.values() if a.user_id == user_id]

    def active_for(self, user_id: Optional[int]) -> list[PendingAck]:
        return [a for a in self.pending_for(user_id) if not a.exhausted]

    def clear(self) -> None:
        self._pending.clear()
        self._superseded.clear()


class EscalationService:
    """Wall-clock driver for live alerts: checks every few seconds and resends due reminders through the notifier."""

    def __init__(self, notifier, tracker: Optional[EscalationTracker] = None, check_seconds: float = 5.0,
                 clock: Callable[[], float] = time.time):
        self.notifier = notifier
        self.tracker = tracker or EscalationTracker()
        self.check_seconds = check_seconds
        self.clock = clock
        self._task: Optional[asyncio.Task] = None

    def open(self, user_id: int, venue: str, symbol: str, severity: str, text: str, threshold,
             distance_pct: Optional[float] = None) -> Optional[PendingAck]:
        if not threshold.escalation_enabled:
            return None
        return self.tracker.open(
            user_id, venue, symbol, severity, text, self.clock(),
            interval_seconds=threshold.escalation_interval_minutes * 60,
            max_reminders=threshold.escalation_max_reminders, distance_pct=distance_pct,
        )

    def acknowledge(self, user_id: int, ack_id: str) -> Optional[PendingAck]:
        return self.tracker.acknowledge(user_id, ack_id, self.clock())

    async def tick(self) -> int:
        now = self.clock()
        sent = 0
        for ack in self.tracker.due(now):
            n = ack.reminders_sent + 1
            text = reminder_text(ack, n, (now - ack.opened_at) / 60)
            try:
                await self.notifier.notify(ack.user_id, text, actions=[ack_action(ack), ("🛟 Rescue options", f"rescue:{ack.venue}:{ack.symbol}")])
            except Exception as e:
                logger.error("Escalation reminder for user %s failed: %s", ack.user_id, type(e).__name__)
                continue
            self.tracker.mark_sent(ack, now)
            sent += 1
        return sent

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Escalation loop error")
            await asyncio.sleep(self.check_seconds)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
