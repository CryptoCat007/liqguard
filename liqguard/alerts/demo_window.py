"""
When the position monitor may alert on a user's demo book.

The static demo books (Normal / Volatile / Near liquidation / Safe) redraw their marks on every
read, so a monitor watching them forever re-raises alerts every poll. Instead, picking a static
scenario opens a short, finite window; the first ✅ / 😴 / Dismiss / ⏹ on any demo alert closes
it for good. Nothing is persisted: after a restart a demo book stays quiet until the user picks a
scenario again. The market simulation sends its own alerts and is never monitored.
"""

from __future__ import annotations

import time
from typing import Callable

DEMO_ALERT_WINDOW_SECONDS = 600.0


class DemoAlertWindow:
    def __init__(self, seconds: float = DEMO_ALERT_WINDOW_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self.clock = clock
        self._until: dict[int, float] = {}

    def open(self, user_id: int) -> None:
        self._until[user_id] = self.clock() + self.seconds

    def close(self, user_id: int) -> bool:
        """Mute this user's demo book now; True if it was open."""
        until = self._until.pop(user_id, None)
        return until is not None and until > self.clock()

    def allows(self, user_id: int) -> bool:
        until = self._until.get(user_id)
        if until is None:
            return False
        if until <= self.clock():
            del self._until[user_id]
            return False
        return True
