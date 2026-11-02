"""
Shared English-word detector for the zh-Hant dashboard tests.

Allowed in Chinese text: tickers and units (all-caps tokens such as BTCUSDT,
USDT, APR, ADL, UTC; single letters such as the h / m / d / x unit suffixes)
and proper names (exchanges, the Muse assistant, the product and Telegram), plus
Telegram slash commands such as /demo and @handles such as the bot username.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

NAMES = {"Binance", "Hyperliquid", "Muse", "LiqGuard", "Telegram"}
WORD = re.compile(r"[A-Za-z][A-Za-z']*")
COMMAND = re.compile(r"/[a-z_]+|@\w+")
IDENTIFIER = re.compile(r"^[a-z0-9_]+$|^\d{4}-\d\d-\d\dT")
# Machine values the UI maps to labels itself (never shown as-is).
IDENTIFIER_FIELDS = {"exit_kind", "lang", "ack_id", "id"}


def english_words(text: str) -> list[str]:
    return [w for w in WORD.findall(COMMAND.sub(" ", text)) if len(w) > 1 and not w.isupper() and w not in NAMES]


def english_in(payload: Any, path: str = "$") -> Iterable[tuple[str, str]]:
    """(path, text) for every user-facing string in `payload` that still contains English words."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k not in IDENTIFIER_FIELDS:
                yield from english_in(v, f"{path}.{k}")
    elif isinstance(payload, list):
        for v in payload:
            yield from english_in(v, f"{path}[]")
    elif isinstance(payload, str) and not IDENTIFIER.match(payload) and english_words(payload):
        yield path, payload
