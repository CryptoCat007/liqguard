"""One-time dashboard login links sent by the bot (/dashboard)."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Callable, Optional, Protocol


class LoginTokenStore(Protocol):
    async def create(self, telegram_id: int, token_hash: str, created_at: float, expires_at: float) -> None: ...
    async def consume(self, token_hash: str, now: float) -> Optional[int]: ...
    async def status(self, token_hash: str, now: float) -> Optional[str]: ...


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class LoginLinkService:
    """
    Issues 256-bit random tokens bound to one Telegram user. Only the SHA-256
    hash is stored; a token works once and expires after `ttl_seconds`.
    """

    def __init__(self, store: LoginTokenStore, ttl_seconds: int = 300, clock: Callable[[], float] = time.time):
        self.store = store
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    async def create(self, telegram_id: int) -> str:
        raw = secrets.token_urlsafe(32)
        now = self._clock()
        await self.store.create(telegram_id, hash_token(raw), now, now + self.ttl_seconds)
        return raw

    async def redeem(self, raw: Optional[str]) -> Optional[int]:
        if not raw or len(raw) > 128:
            return None
        return await self.store.consume(hash_token(raw), self._clock())

    async def failure(self, raw: Optional[str]) -> str:
        """Why `redeem` refused `raw`: "link_used", "link_expired" or "link" (unknown or malformed)."""
        if not raw or len(raw) > 128:
            return "link"
        state = await self.store.status(hash_token(raw), self._clock())
        return {"used": "link_used", "expired": "link_expired"}.get(state or "", "link")
