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

from liqguard.i18n import EN, t

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
    snoozed_until: Optional[float] = None
    meta: dict = field(default_factory=dict)

    @property
    def next_due(self) -> float:
        due = self.last_sent_at + self.interval_seconds
        return max(due, self.snoozed_until) if self.snoozed_until is not None else due

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
            "snoozed_until": self.snoozed_until,
            **self.meta,
        }


def urgency(reminder: int, max_reminders: int) -> str:
    if reminder >= max_reminders:
        return "final"
    return "urgent" if reminder >= 2 else "reminder"


def reminder_text(ack: PendingAck, reminder: int, elapsed_minutes: float, demo: bool = False, lang: str = EN) -> str:
    """Reminder `reminder` of `ack.max_reminders`; louder each time."""
    level = urgency(reminder, ack.max_reminders)
    dist = t(lang, "alert.reminder.distance", dist=f"{ack.distance_pct:.1f}") if ack.distance_pct is not None else ""
    lines = [
        t(lang, f"alert.reminder.head_{level}", n=reminder, max=ack.max_reminders) + (t(lang, "alert.demo_tag") if demo else ""),
        t(lang, "alert.reminder.sent", symbol=ack.symbol, severity=t(lang, f"common.severity_upper.{ack.severity}"),
          minutes=f"{elapsed_minutes:.0f}", dist=dist),
    ]
    if level == "final":
        lines.append(t(lang, "alert.reminder.final"))
    else:
        lines.append(t(lang, "alert.reminder.tap", next=f"{ack.interval_seconds / 60:g}"))
    return "\n".join(lines)


def ack_action(ack: PendingAck, lang: str = EN) -> tuple[str, str]:
    return (t(lang, "kb.alert_awake"), f"{ACK_PREFIX}{ack.id}")


def rescue_action_for(ack: PendingAck, lang: str = EN) -> tuple[str, str]:
    return (t(lang, "alert.rescue_button"), f"rescue:{ack.venue}:{ack.symbol}")


MATERIAL_DISTANCE_FACTOR = 0.5


@dataclass(frozen=True)
class Silenced:
    severity: str
    distance_pct: Optional[float]

    def lifted_by(self, severity: str, distance_pct: Optional[float]) -> bool:
        """A material change: a worse tier than the one acknowledged AND at most half the distance left."""
        if severity not in ESCALATING or ESCALATING.index(severity) <= ESCALATING.index(self.severity):
            return False
        if self.distance_pct is None or distance_pct is None:
            return True
        return distance_pct <= self.distance_pct * MATERIAL_DISTANCE_FACTOR


class EscalationTracker:
    """
    Pending acknowledgements per (owner, venue, symbol). Acknowledging (or
    snoozing past) a position silences it: no new reminders open for it until
    conditions materially change (`Silenced.lifted_by`) or it recovers
    (`resolve`).
    """

    def __init__(self, id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(9)):
        self._pending: dict[tuple[Optional[int], str, str], PendingAck] = {}
        self._superseded: dict[str, tuple[Optional[int], str, str]] = {}
        self._silenced: dict[tuple[Optional[int], str, str], Silenced] = {}
        self._ids = id_factory

    def is_silenced(self, user_id: Optional[int], venue: str, symbol: str, severity: Optional[str] = None,
                    distance_pct: Optional[float] = None) -> bool:
        s = self._silenced.get((user_id, venue, symbol))
        if s is None:
            return False
        return severity is None or not s.lifted_by(severity, distance_pct)

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
        silenced = self._silenced.get(key)
        if silenced is not None:
            if not silenced.lifted_by(severity, distance_pct):
                return None
            del self._silenced[key]
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
        self._silenced[key] = Silenced(ack.severity, ack.distance_pct)
        return ack

    def snooze(self, user_id: Optional[int], ack_id: str, until: float) -> Optional[PendingAck]:
        """Hold the owner's reminders for this alert until `until` (still pending; "I'm awake" still works)."""
        key = self._superseded.get(ack_id)
        if key is None:
            key = next((k for k, a in self._pending.items() if a.id == ack_id), None)
        ack = self._pending.get(key) if key is not None else None
        if ack is None or ack.user_id != user_id:
            return None
        ack.snoozed_until = until
        return ack

    def _forget(self, key: tuple) -> None:
        for old in [i for i, k in self._superseded.items() if k == key]:
            del self._superseded[old]

    def update(self, user_id: Optional[int], venue: str, symbol: str, distance_pct: float) -> None:
        ack = self._pending.get((user_id, venue, symbol))
        if ack is not None:
            ack.distance_pct = distance_pct

    def resolve(self, user_id: Optional[int], venue: str, symbol: str) -> Optional[PendingAck]:
        """The position recovered (or closed): stop reminding, and let a later relapse alert afresh."""
        self._forget((user_id, venue, symbol))
        self._silenced.pop((user_id, venue, symbol), None)
        return self._pending.pop((user_id, venue, symbol), None)

    def pending_for(self, user_id: Optional[int]) -> list[PendingAck]:
        """Unacknowledged alerts, including ones whose reminders hit the cap (still acknowledgeable)."""
        return [a for a in self._pending.values() if a.user_id == user_id]

    def active_for(self, user_id: Optional[int]) -> list[PendingAck]:
        return [a for a in self.pending_for(user_id) if not a.exhausted]

    def clear(self) -> None:
        self._pending.clear()
        self._superseded.clear()
        self._silenced.clear()


class EscalationService:
    """Wall-clock driver for live alerts: checks every few seconds and resends due reminders through the notifier."""

    def __init__(self, notifier, tracker: Optional[EscalationTracker] = None, check_seconds: float = 5.0,
                 clock: Callable[[], float] = time.time):
        self.notifier = notifier
        self.languages: dict[int, str] = {}
        self.tracker = tracker or EscalationTracker()
        self.check_seconds = check_seconds
        self.clock = clock
        self._task: Optional[asyncio.Task] = None

    def open(self, user_id: int, venue: str, symbol: str, severity: str, text: str, threshold,
             distance_pct: Optional[float] = None, lang: str = EN) -> Optional[PendingAck]:
        if not threshold.escalation_enabled:
            return None
        self.languages[user_id] = lang
        return self.tracker.open(
            user_id, venue, symbol, severity, text, self.clock(),
            interval_seconds=threshold.escalation_interval_minutes * 60,
            max_reminders=threshold.escalation_max_reminders, distance_pct=distance_pct,
        )

    def acknowledge(self, user_id: int, ack_id: str) -> Optional[PendingAck]:
        return self.tracker.acknowledge(user_id, ack_id, self.clock())

    def snooze(self, user_id: int, ack_id: str, minutes: float = 30) -> Optional[PendingAck]:
        return self.tracker.snooze(user_id, ack_id, self.clock() + minutes * 60)

    async def tick(self) -> int:
        now = self.clock()
        sent = 0
        for ack in self.tracker.due(now):
            n = ack.reminders_sent + 1
            lang = self.languages.get(ack.user_id, EN)
            text = reminder_text(ack, n, (now - ack.opened_at) / 60, lang=lang)
            try:
                await self.notifier.notify(ack.user_id, text, actions=[ack_action(ack, lang), rescue_action_for(ack, lang)], lang=lang)
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
