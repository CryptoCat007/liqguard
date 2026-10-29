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
    "m": ActionSpec("menu", description="🏠 主選單"),
    "h": ActionSpec("menu", description="❓ 幫助"),
    "pl": ActionSpec("positions", ttl=7 * DAY, description="📊 我的倉位"),
    "pd": ActionSpec("positions", POSITION, ttl=DAY, description="倉位詳情"),
    "pr": ActionSpec("positions", POSITION, ttl=DAY, description="救倉試算"),
    "pw": ActionSpec("positions", POSITION, ttl=DAY, description="插針壓測"),
    "ps": ActionSpec("positions", POSITION, ttl=DAY, description="止損檢查"),
    "pf": ActionSpec("positions", POSITION, ttl=DAY, description="資金費影響"),
    "tr": ActionSpec("triage", ttl=7 * DAY, description="🚨 先處理哪個"),
    "st": ActionSpec("stress", ttl=7 * DAY, description="🧪 壓力測試"),
    "ff": ActionSpec("funding", ttl=7 * DAY, description="💸 資金費率/手續費"),
    "se": ActionSpec("settings", description="🔔 通知設定"),
    "sp": ActionSpec("settings", (SETTING_KEY,), ttl=DAY, description="選擇設定值"),
    "ss": ActionSpec("settings", (SETTING_KEY, SETTING_VALUE), ttl=DAY, description="套用設定"),
    "b": ActionSpec("binding", description="🔑 綁定帳號"),
    "bb": ActionSpec("binding", ttl=DAY, description="綁定 Binance"),
    "bh": ActionSpec("binding", ttl=DAY, description="綁定 Hyperliquid"),
    "bl": ActionSpec("binding", ttl=DAY, description="查看已綁定"),
    "bu": ActionSpec("binding", ttl=DAY, description="解除綁定"),
    "bx": ActionSpec("binding", (VENUE, NETWORK), ttl=HOUR, description="確認解除綁定"),
    "by": ActionSpec("binding", (VENUE, NETWORK), ttl=2 * MINUTE, description="執行解除綁定"),
    "bc": ActionSpec("binding", ttl=DAY, description="取消綁定"),
    "db": ActionSpec("dashboard", description="🖥️ 網頁看板"),
    "dm": ActionSpec("demo", description="🎬 Demo"),
    "ds": ActionSpec("demo", (SCENARIO,), ttl=DAY, description="選擇 Demo 情境"),
    "dx": ActionSpec("demo", ttl=DAY, description="停止模擬"),
    "do": ActionSpec("demo", ttl=DAY, description="關閉 Demo"),
    "aa": ActionSpec("alerts", (ACK_ID,), ttl=DAY, description="✅ 我醒了"),
    "az": ActionSpec("alerts", (ACK_ID,), ttl=DAY, description="😴 暫停提醒 30 分鐘"),
}
