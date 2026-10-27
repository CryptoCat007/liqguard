"""
Telegram Login Widget verification
(https://core.telegram.org/widgets/login#checking-authorization).

secret_key = SHA256(bot_token)
hash       = HMAC_SHA256(secret_key, "\\n".join(sorted("k=v" for every field except hash)))
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Mapping, Optional

ALLOWED_FIELDS = {"id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash"}


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: int
    username: Optional[str]
    auth_date: int


def telegram_login_hash(data: Mapping[str, str], bot_token: str) -> str:
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data) if k != "hash")
    secret = hashlib.sha256(bot_token.encode()).digest()
    return hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()


def verify_telegram_login(
    data: Mapping[str, str],
    bot_token: str,
    max_age_seconds: int = 600,
    now: Optional[float] = None,
) -> Optional[TelegramIdentity]:
    """Return the identity if `data` was signed by Telegram for this bot and is fresh, else None."""
    if not bot_token or "hash" not in data or "id" not in data or "auth_date" not in data:
        return None
    fields = {k: str(v) for k, v in data.items() if k in ALLOWED_FIELDS}
    if not hmac.compare_digest(telegram_login_hash(fields, bot_token), fields["hash"]):
        return None
    try:
        auth_date = int(fields["auth_date"])
        user_id = int(fields["id"])
    except ValueError:
        return None
    current = time.time() if now is None else now
    if auth_date > current + 60 or current - auth_date > max_age_seconds:
        return None
    return TelegramIdentity(user_id=user_id, username=fields.get("username"), auth_date=auth_date)
