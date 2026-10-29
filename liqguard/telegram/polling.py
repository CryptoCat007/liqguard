"""Long-polling settings tuned so a stalled connection is dropped and retried quickly.

aiogram's defaults (60 s session timeout + 10 s getUpdates long poll) mean a silently
dead TCP connection holds updates for ~70 s before the retry. A short request timeout
and a fast, capped backoff keep the worst-case delay after a network blip to ~25 s.
"""

from __future__ import annotations

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.utils.backoff import BackoffConfig

REQUEST_TIMEOUT = 15
POLLING_TIMEOUT = 10
BACKOFF = BackoffConfig(min_delay=0.5, max_delay=3.0, factor=1.5, jitter=0.1)


def make_session() -> AiohttpSession:
    return AiohttpSession(timeout=REQUEST_TIMEOUT)


def polling_kwargs() -> dict:
    return {"polling_timeout": POLLING_TIMEOUT, "backoff_config": BACKOFF}


def worst_case_stall_seconds() -> float:
    """Longest wait before updates resume after the connection silently dies."""
    return REQUEST_TIMEOUT + POLLING_TIMEOUT + BACKOFF.min_delay * (1 + BACKOFF.jitter)
