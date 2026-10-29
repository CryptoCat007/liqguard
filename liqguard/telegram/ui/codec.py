"""
Versioned, signed `callback_data` for the inline-keyboard UI.

Wire format (ASCII, ≤ 64 bytes, Telegram's limit):

    1.<action>.<arg>….<issued_at base36>.<tag>

`tag` is a truncated HMAC-SHA256 over the requesting user's id and the rest of
the payload, so a button only works for the user it was rendered for: a
forwarded message, a group chat or a hand-crafted payload from another user
fails verification. Arguments are restricted to `[A-Za-z0-9_-]` so they can
never contain the separator.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Optional

VERSION = "1"
SEP = "."
MAX_BYTES = 64
TAG_CHARS = 8

ACTION_RE = re.compile(r"^[a-z][a-z0-9]{0,7}$")
ARG_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
TS_RE = re.compile(r"^[0-9a-z]{1,8}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_-]{%d}$" % TAG_CHARS)


class CallbackError(Exception):
    """`reason` is one of: format, version, size, tag, expired, action, args."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Decoded:
    action: str
    args: tuple[str, ...]
    issued_at: int


def _b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def _tag(secret: bytes, user_id: int, body: str) -> str:
    mac = hmac.new(secret, f"{user_id}:{body}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode()[:TAG_CHARS]


def is_ui_payload(data: Optional[str]) -> bool:
    return bool(data) and data.startswith(VERSION + SEP)


class CallbackCodec:
    def __init__(self, secret: bytes):
        if not secret:
            raise ValueError("callback codec needs a secret")
        self.secret = secret

    def encode(self, user_id: int, action: str, *args: str, now: Optional[float] = None) -> str:
        if not ACTION_RE.match(action):
            raise ValueError(f"bad action {action!r}")
        for a in args:
            if not ARG_RE.match(str(a)):
                raise ValueError(f"bad callback argument {a!r}")
        issued = int(time.time() if now is None else now)
        body = SEP.join([VERSION, action, *map(str, args), _b36(issued)])
        data = f"{body}{SEP}{_tag(self.secret, user_id, body)}"
        if len(data.encode()) > MAX_BYTES:
            raise ValueError(f"callback data over {MAX_BYTES} bytes: {data!r}")
        return data

    def decode(self, user_id: int, data: Optional[str], max_age: Optional[float] = None,
               now: Optional[float] = None) -> Decoded:
        if not isinstance(data, str) or not data:
            raise CallbackError("format")
        if len(data.encode("utf-8", "replace")) > MAX_BYTES:
            raise CallbackError("size")
        parts = data.split(SEP)
        if len(parts) < 4:
            raise CallbackError("format")
        if parts[0] != VERSION:
            raise CallbackError("version")
        action, args, ts, tag = parts[1], tuple(parts[2:-2]), parts[-2], parts[-1]
        if not ACTION_RE.match(action) or not TS_RE.match(ts) or not TAG_RE.match(tag) \
                or not all(ARG_RE.match(a) for a in args):
            raise CallbackError("format")
        body = data[: -(len(tag) + 1)]
        if not hmac.compare_digest(_tag(self.secret, user_id, body), tag):
            raise CallbackError("tag")
        issued = int(ts, 36)
        current = time.time() if now is None else now
        if issued > current + 300:
            raise CallbackError("tag")
        if max_age is not None and current - issued > max_age:
            raise CallbackError("expired")
        return Decoded(action, args, issued)


def derive_secret(*material: Optional[str]) -> bytes:
    """Stable across restarts when configured; otherwise a per-process secret (old buttons then read as invalid)."""
    seed = next((m for m in material if m), None)
    if seed is None:
        import secrets
        return secrets.token_bytes(32)
    return hmac.new(b"liqguard-callback-v1", seed.encode(), hashlib.sha256).digest()
