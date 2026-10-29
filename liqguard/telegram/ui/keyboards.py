"""
Pure keyboard builders: plain data in, `InlineKeyboardMarkup` out. No storage,
network or feature imports; the only dependency is the per-user `Signer` that
turns (action, args) into signed callback data.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .screen import Signer

Button = InlineKeyboardButton
Rows = list[list[InlineKeyboardButton]]

MAX_POSITIONS = 8
SCENARIOS = (
    ("normal", "😊 一般行情"),
    ("volatile", "📈 劇烈波動"),
    ("critical", "🚨 瀕臨強平"),
    ("safe", "✅ 安全組合"),
    ("mixed", "🎬 強平模擬"),
    ("cross_longs", "🎬 5 個多單同錢包"),
)
SETTING_LABELS = {
    "warning": "⚠️ 警告",
    "critical": "🚨 嚴重",
    "emergency": "🆘 緊急",
    "interval": "⏰ 提醒間隔",
    "max": "🔁 最多提醒",
    "quiet": "🌙 勿擾時段",
}
TOGGLE_LABELS = {"escalation": "喚醒提醒", "adaptive": "自適應門檻", "stops": "止損檢查"}
TOOL_LABELS = {"pr": "🛟 救倉試算", "pw": "🕯 插針壓測", "ps": "🛑 止損檢查", "pf": "💸 資金費影響"}


def _kb(rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def back_row(sign: Signer) -> list[Button]:
    return [Button(text="⬅️ 返回主選單", callback_data=sign("m"))]


def main_menu(sign: Signer) -> InlineKeyboardMarkup:
    return _kb([
        [Button(text="📊 我的倉位", callback_data=sign("pl")), Button(text="🚨 先處理哪個", callback_data=sign("tr"))],
        [Button(text="🧪 壓力測試", callback_data=sign("st")), Button(text="💸 資金費率/手續費", callback_data=sign("ff"))],
        [Button(text="🔔 通知設定", callback_data=sign("se")), Button(text="🔑 綁定帳號", callback_data=sign("b"))],
        [Button(text="🖥️ 網頁看板", callback_data=sign("db")), Button(text="🎬 Demo", callback_data=sign("dm"))],
        [Button(text="❓ 幫助", callback_data=sign("h"))],
    ])


def tool_row(sign: Signer, source: str, symbol: str, skip: Optional[str] = None) -> list[Button]:
    return [Button(text=label, callback_data=sign(code, source, symbol)) for code, label in TOOL_LABELS.items() if code != skip]


def positions_list(sign: Signer, positions: Sequence[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """`positions`: (source, symbol, header label) riskiest first; each gets a header and its four tools."""
    rows: Rows = []
    for source, symbol, label in positions[:MAX_POSITIONS]:
        rows.append([Button(text=label, callback_data=sign("pd", source, symbol))])
        tools = tool_row(sign, source, symbol)
        rows += [tools[:2], tools[2:]]
    rows.append([Button(text="🔄 重新整理", callback_data=sign("pl"))])
    rows.append(back_row(sign))
    return _kb(rows)


def position_tools(sign: Signer, source: str, symbol: str, current: Optional[str] = None) -> InlineKeyboardMarkup:
    tools = tool_row(sign, source, symbol, skip=current)
    return _kb([
        tools[:2], tools[2:],
        [Button(text="📊 返回倉位列表", callback_data=sign("pl"))],
        back_row(sign),
    ])


def triage(sign: Signer, focus: Sequence[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """`focus`: (source, symbol, label) in handling order."""
    rows: Rows = [[Button(text=f"🚑 {label}", callback_data=sign("pr", source, symbol))] for source, symbol, label in focus[:3]]
    rows.append([Button(text="🔄 重新整理", callback_data=sign("tr")), Button(text="📊 我的倉位", callback_data=sign("pl"))])
    rows.append(back_row(sign))
    return _kb(rows)


def stress(sign: Signer, wick_targets: Sequence[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: Rows = [[Button(text=f"🕯 {symbol} 插針壓測", callback_data=sign("pw", source, symbol))
                   for source, symbol in wick_targets[:3]]]
    rows.append([Button(text="🔄 重新整理", callback_data=sign("st"))])
    rows.append(back_row(sign))
    return _kb(rows)


def refresh_back(sign: Signer, action: str) -> InlineKeyboardMarkup:
    return _kb([[Button(text="🔄 重新整理", callback_data=sign(action))], back_row(sign)])


def no_account(sign: Signer) -> InlineKeyboardMarkup:
    return _kb([
        [Button(text="🔑 綁定帳號", callback_data=sign("b")), Button(text="🎬 Demo", callback_data=sign("dm"))],
        back_row(sign),
    ])


def back_only(sign: Signer) -> InlineKeyboardMarkup:
    return _kb([back_row(sign)])


def settings(sign: Signer, current: dict) -> InlineKeyboardMarkup:
    def toggle(key: str) -> Button:
        on = bool(current.get(key))
        return Button(text=f"{'✅' if on else '⬜'} {TOGGLE_LABELS[key]}", callback_data=sign("ss", key, "toggle"))

    return _kb([
        [Button(text=f"{SETTING_LABELS[k]} {current[k]:g}%", callback_data=sign("sp", k)) for k in ("warning", "critical", "emergency")],
        [Button(text=f"{SETTING_LABELS['interval']} {current['interval']:g} 分", callback_data=sign("sp", "interval")),
         Button(text=f"{SETTING_LABELS['max']} {current['max']} 次", callback_data=sign("sp", "max"))],
        [Button(text=f"{SETTING_LABELS['quiet']} {current['quiet_label']}", callback_data=sign("sp", "quiet"))],
        [toggle("escalation"), toggle("adaptive")],
        [toggle("stops")],
        back_row(sign),
    ])


def setting_options(sign: Signer, key: str, options: Iterable, current) -> InlineKeyboardMarkup:
    def label(v) -> str:
        if key == "quiet":
            text = "關閉" if v == "off" else f"{int(str(v).split('-')[0]):02d}:00–{int(str(v).split('-')[1]):02d}:00"
        else:
            text = f"{v:g}{'%' if key in ('warning', 'critical', 'emergency') else ' 分' if key == 'interval' else ' 次'}"
        return ("● " if str(v) == str(current) else "") + text

    buttons = [Button(text=label(v), callback_data=sign("ss", key, str(v))) for v in options]
    rows: Rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([Button(text="⬅️ 返回通知設定", callback_data=sign("se"))])
    return _kb(rows)


def binding_menu(sign: Signer) -> InlineKeyboardMarkup:
    return _kb([
        [Button(text="🟡 綁定 Binance", callback_data=sign("bb")), Button(text="🟢 綁定 Hyperliquid", callback_data=sign("bh"))],
        [Button(text="📋 查看已綁定", callback_data=sign("bl")), Button(text="🗑 解除綁定", callback_data=sign("bu"))],
        back_row(sign),
    ])


def binding_waiting(sign: Signer) -> InlineKeyboardMarkup:
    return _kb([[Button(text="❌ 取消綁定", callback_data=sign("bc"))]])


def binding_back(sign: Signer) -> InlineKeyboardMarkup:
    return _kb([[Button(text="⬅️ 返回綁定帳號", callback_data=sign("b"))], back_row(sign)])


def unbind_list(sign: Signer, bindings: Sequence[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """`bindings`: (venue, network, masked label)."""
    rows: Rows = [[Button(text=f"🗑 {label}", callback_data=sign("bx", venue, network))] for venue, network, label in bindings]
    rows.append([Button(text="⬅️ 返回綁定帳號", callback_data=sign("b"))])
    return _kb(rows)


def unbind_confirm(sign: Signer, venue: str, network: str) -> InlineKeyboardMarkup:
    return _kb([[Button(text="✅ 確認解除", callback_data=sign("by", venue, network)),
                 Button(text="❌ 取消", callback_data=sign("bu"))]])


def dashboard(sign: Signer, url: Optional[str]) -> InlineKeyboardMarkup:
    rows: Rows = []
    if url and url.startswith("https://"):
        rows.append([Button(text="🖥️ 開啟我的看板", url=url)])
    rows.append(back_row(sign))
    return _kb(rows)


def demo_menu(sign: Signer, running: bool, demo_mode: bool) -> InlineKeyboardMarkup:
    rows: Rows = []
    if running:
        rows.append([Button(text="📊 我的倉位", callback_data=sign("pl")), Button(text="🚨 先處理哪個", callback_data=sign("tr"))])
        rows.append([Button(text="⏹ 停止模擬", callback_data=sign("dx"))])
    else:
        buttons = [Button(text=label, callback_data=sign("ds", key)) for key, label in SCENARIOS]
        rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if demo_mode:
        rows.append([Button(text="❌ 關閉 Demo 模式", callback_data=sign("do"))])
    rows.append(back_row(sign))
    return _kb(rows)


def demo_started(sign: Signer) -> InlineKeyboardMarkup:
    return _kb([
        [Button(text="📊 我的倉位", callback_data=sign("pl")), Button(text="🚨 先處理哪個", callback_data=sign("tr"))],
        [Button(text="🧪 壓力測試", callback_data=sign("st")), Button(text="💸 資金費率/手續費", callback_data=sign("ff"))],
        [Button(text="🎬 Demo 選單", callback_data=sign("dm"))],
        back_row(sign),
    ])


def alert_rows(sign: Signer, source: Optional[str], symbol: Optional[str], ack_id: Optional[str]) -> Rows:
    rows: Rows = []
    if source and symbol:
        rows.append([Button(text="🚑 先處理這個", callback_data=sign("pr", source, symbol))])
    if ack_id:
        rows.append([Button(text="✅ 我醒了", callback_data=sign("aa", ack_id)),
                     Button(text="😴 暫停提醒 30 分鐘", callback_data=sign("az", ack_id))])
    return rows


def alert_keyboard(sign: Signer, source: Optional[str], symbol: Optional[str], ack_id: Optional[str]) -> Optional[InlineKeyboardMarkup]:
    rows = alert_rows(sign, source, symbol, ack_id)
    return _kb(rows) if rows else None


def iter_buttons(markup: Optional[InlineKeyboardMarkup]) -> Iterator[InlineKeyboardButton]:
    for row in (markup.inline_keyboard if markup else []):
        yield from row
