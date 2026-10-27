"""Per-user audit trail for protective actions."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional, Protocol

from .models import AuditLogEntry

logger = logging.getLogger(__name__)

AuditListener = Callable[[AuditLogEntry], None]


class AuditSink(Protocol):
    """Durable storage for audit entries (e.g. the `audit_log` table, scoped by user id)."""

    def write(self, entry: AuditLogEntry) -> None: ...


class AuditLogger:
    """
    Records every protective-action step.

    Entries are kept per user in memory and written to an optional durable
    sink. Listeners subscribe to one user id and only ever receive that
    user's entries.
    """

    def __init__(self, sink: Optional[AuditSink] = None, max_entries_per_user: int = 500):
        self.sink = sink
        self._max = max_entries_per_user
        self._entries: dict[int, list[AuditLogEntry]] = {}
        self._listeners: dict[int, dict[int, AuditListener]] = {}
        self._next_listener_id = 0

    def subscribe(self, user_id: int, listener: AuditListener) -> Callable[[], None]:
        self._next_listener_id += 1
        token = self._next_listener_id
        self._listeners.setdefault(user_id, {})[token] = listener
        return lambda: self._listeners.get(user_id, {}).pop(token, None)

    def log(self, entry: AuditLogEntry) -> None:
        entries = self._entries.setdefault(entry.user_id, [])
        entries.append(entry)
        if len(entries) > self._max:
            del entries[: len(entries) - self._max]

        if self.sink is not None:
            try:
                self.sink.write(entry)
            except Exception as e:
                logger.error("Failed to persist audit entry %s: %s", entry.action_id, type(e).__name__)

        for listener in list(self._listeners.get(entry.user_id, {}).values()):
            try:
                listener(entry)
            except Exception as e:
                logger.error("Audit listener failed: %s", e)

        logger.info("AUDIT user=%s %s %s %s", entry.user_id, entry.action_type.value, entry.status.value, entry.symbol)

    def get_user_entries(self, user_id: int, since: Optional[datetime] = None) -> list[AuditLogEntry]:
        entries = list(self._entries.get(user_id, []))
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        return entries
