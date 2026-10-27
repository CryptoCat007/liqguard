"""Signed, expiring session tokens (HMAC-SHA256). Stateless, but revocable via the user's session epoch."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional

VERSION = "v1"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class SessionClaims:
    user_id: int
    epoch: int
    issued_at: float
    expires_at: float
    username: Optional[str] = None


class SessionSigner:
    """
    Token format: `v1.<base64url JSON claims>.<base64url HMAC>`.

    The cookie holding it is HttpOnly + SameSite=Lax; the server additionally
    checks `epoch` against the user's current session epoch, so
    `/dashboard revoke` invalidates every outstanding session.
    """

    def __init__(self, secret: bytes, ttl_seconds: int = 12 * 3600, clock: Callable[[], float] = time.time):
        if len(secret) < 32:
            raise ValueError("Session secret must be at least 32 bytes")
        self._secret = secret
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    def __repr__(self) -> str:
        return f"SessionSigner(ttl={self.ttl_seconds})"

    def _mac(self, payload: str) -> str:
        return _b64e(hmac.new(self._secret, f"{VERSION}.{payload}".encode(), hashlib.sha256).digest())

    def issue(self, user_id: int, epoch: int, username: Optional[str] = None) -> str:
        now = self._clock()
        claims = {
            "uid": int(user_id),
            "ep": int(epoch),
            "iat": int(now),
            "exp": int(now + self.ttl_seconds),
            "un": username,
            "n": secrets.token_urlsafe(8),
        }
        payload = _b64e(json.dumps(claims, separators=(",", ":")).encode())
        return f"{VERSION}.{payload}.{self._mac(payload)}"

    def verify(self, token: Optional[str]) -> Optional[SessionClaims]:
        if not token or token.count(".") != 2:
            return None
        version, payload, mac = token.split(".")
        if version != VERSION or not hmac.compare_digest(mac, self._mac(payload)):
            return None
        try:
            claims = json.loads(_b64d(payload))
            result = SessionClaims(
                user_id=int(claims["uid"]),
                epoch=int(claims["ep"]),
                issued_at=float(claims["iat"]),
                expires_at=float(claims["exp"]),
                username=claims.get("un"),
            )
        except (ValueError, KeyError, TypeError):
            return None
        if result.expires_at <= self._clock():
            return None
        return result
