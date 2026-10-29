"""
Every button the inline UI can render: short action code → owning feature,
argument shapes and how long a rendered button stays valid. The router only
dispatches actions listed here, with arguments that match their spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MINUTE, HOUR, DAY = 60, 3600, 86400

SOURCE = re.compile(r"^(binance|hyperliquid|demo|sim)$")
SYMBOL = re.compile(r"^[A-Za-z0-9]{1,20}$")
ACK_ID = re.compile(r"^[A-Za-z0-9_-]{6,24}$")
SETTING_KEY = re.compile(r"^(warning|critical|emergency|interval|max|quiet|escalation|adaptive|stops)$")
SETTING_VALUE = re.compile(r"^(toggle|off|on|\d{1,2}(-\d{1,2})?)$")
VENUE = re.compile(r"^(binance|hyperliquid)$")
NETWORK = re.compile(r"^(mainnet|testnet)$")
SCENARIO = re.compile(r"^(normal|volatile|critical|safe|mixed|cross_longs)$")
LANGUAGE = re.compile(r"^(en|zh)$")


@dataclass(frozen=True)
class ActionSpec:
    feature: str
    args: tuple[re.Pattern, ...] = ()
    ttl: float = 30 * DAY
    description: str = ""

    def valid_args(self, args: tuple[str, ...]) -> bool:
        return len(args) == len(self.args) and all(p.match(a) for p, a in zip(self.args, args))


POSITION = (SOURCE, SYMBOL)

ACTIONS: dict[str, ActionSpec] = {
    "m": ActionSpec("menu", description="🏠 Main menu"),
    "h": ActionSpec("menu", description="❓ Help"),
    "l": ActionSpec("language", description="🌐 Language picker"),
    "ls": ActionSpec("language", (LANGUAGE,), ttl=DAY, description="Set language"),
    "pl": ActionSpec("positions", ttl=7 * DAY, description="📊 My positions"),
    "pd": ActionSpec("positions", POSITION, ttl=DAY, description="Position detail"),
    "pr": ActionSpec("positions", POSITION, ttl=DAY, description="Rescue sim"),
    "pw": ActionSpec("positions", POSITION, ttl=DAY, description="Wick stress"),
    "ps": ActionSpec("positions", POSITION, ttl=DAY, description="Stop check"),
    "pf": ActionSpec("positions", POSITION, ttl=DAY, description="Funding impact"),
    "tr": ActionSpec("triage", ttl=7 * DAY, description="🚨 What first"),
    "st": ActionSpec("stress", ttl=7 * DAY, description="🧪 Stress test"),
    "ff": ActionSpec("funding", ttl=7 * DAY, description="💸 Funding & fees"),
    "se": ActionSpec("settings", description="🔔 Alerts"),
    "sp": ActionSpec("settings", (SETTING_KEY,), ttl=DAY, description="Pick a value"),
    "ss": ActionSpec("settings", (SETTING_KEY, SETTING_VALUE), ttl=DAY, description="Apply a value"),
    "b": ActionSpec("binding", description="🔑 Bind account"),
    "bb": ActionSpec("binding", ttl=DAY, description="Bind Binance"),
    "bh": ActionSpec("binding", ttl=DAY, description="Bind Hyperliquid"),
    "bl": ActionSpec("binding", ttl=DAY, description="Show bound"),
    "bu": ActionSpec("binding", ttl=DAY, description="Unbind"),
    "bx": ActionSpec("binding", (VENUE, NETWORK), ttl=HOUR, description="Confirm unbind"),
    "by": ActionSpec("binding", (VENUE, NETWORK), ttl=2 * MINUTE, description="Do unbind"),
    "bc": ActionSpec("binding", ttl=DAY, description="Cancel binding"),
    "db": ActionSpec("dashboard", description="🖥️ Dashboard"),
    "dm": ActionSpec("demo", description="🎬 Demo"),
    "ds": ActionSpec("demo", (SCENARIO,), ttl=DAY, description="Pick a demo scenario"),
    "dx": ActionSpec("demo", ttl=DAY, description="Stop simulation"),
    "do": ActionSpec("demo", ttl=DAY, description="Leave demo"),
    "aa": ActionSpec("alerts", (ACK_ID,), ttl=DAY, description="✅ I'm awake"),
    "az": ActionSpec("alerts", (ACK_ID,), ttl=DAY, description="😴 Snooze 30 min"),
}
