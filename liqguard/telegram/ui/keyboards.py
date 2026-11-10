"""
Pure keyboard builders: plain data in, `InlineKeyboardMarkup` out. No storage,
network or feature imports; the only dependencies are the reader's
`Translator` (every label is a locale key) and the per-user `Signer` that
turns (action, args) into signed callback data.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from liqguard.core.user_settings import quiet_window
from liqguard.i18n import LANGUAGE_NAMES, LANGUAGES, Translator

from .screen import Signer

Button = InlineKeyboardButton
Rows = list[list[InlineKeyboardButton]]

MAX_POSITIONS = 8
SCENARIOS = ("normal", "volatile", "critical", "safe", "mixed", "cross_longs")
TIERS = ("warning", "critical", "emergency")
TOGGLES = ("escalation", "adaptive", "stops")
TOOLS = {"pr": "rescue", "pw": "wick", "ps": "stop", "pf": "funding"}
LANGUAGE_CODES = {"en": "en", "zh-Hant": "zh"}


def _kb(rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def back_row(T: Translator, sign: Signer) -> list[Button]:
    return [Button(text=T("kb.back_menu"), callback_data=sign("m"))]


def main_menu(T: Translator, sign: Signer, demo_running: bool = False) -> InlineKeyboardMarkup:
    return _kb([
        [Button(text=T("kb.sim_stop"), callback_data=sign("dx"))] if demo_running else [],
        [Button(text=T("kb.risk_overview"), callback_data=sign("ro"))],
        [Button(text=T("kb.positions"), callback_data=sign("pl")), Button(text=T("kb.stop_check"), callback_data=sign("sc"))],
        [Button(text=T("kb.triage"), callback_data=sign("tr")), Button(text=T("kb.stress"), callback_data=sign("st"))],
        [Button(text=T("kb.funding"), callback_data=sign("ff")), Button(text=T("kb.ask"), callback_data=sign("qa"))],
        [Button(text=T("kb.settings"), callback_data=sign("se")), Button(text=T("kb.binding"), callback_data=sign("b"))],
        [Button(text=T("kb.account"), callback_data=sign("ac")), Button(text=T("kb.dashboard"), callback_data=sign("db"))],
        [Button(text=T("kb.demo"), callback_data=sign("dm")), Button(text=T("kb.language"), callback_data=sign("l"))],
        [Button(text=T("kb.help"), callback_data=sign("h"))],
    ])


def language_menu(T: Translator, sign: Signer) -> InlineKeyboardMarkup:
    rows: Rows = [[
        Button(text=("● " if lang == T.lang else "") + T(f"kb.lang_{LANGUAGE_CODES[lang]}", name=LANGUAGE_NAMES[lang]),
               callback_data=sign("ls", LANGUAGE_CODES[lang]))
        for lang in LANGUAGES
    ]]
    rows.append(back_row(T, sign))
    return _kb(rows)


def tool_row(T: Translator, sign: Signer, source: str, symbol: str, skip: Optional[str] = None) -> list[Button]:
    return [Button(text=T(f"tools.name.{tool}"), callback_data=sign(code, source, symbol))
            for code, tool in TOOLS.items() if code != skip]


def positions_list(T: Translator, sign: Signer, positions: Sequence[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """`positions`: (source, symbol, header label) riskiest first; each gets a header and its four tools."""
    rows: Rows = []
    for source, symbol, label in positions[:MAX_POSITIONS]:
        rows.append([Button(text=label, callback_data=sign("pd", source, symbol))])
        tools = tool_row(T, sign, source, symbol)
        rows += [tools[:2], tools[2:]]
    rows.append([Button(text=T("kb.refresh"), callback_data=sign("pl"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def position_tools(T: Translator, sign: Signer, source: str, symbol: str, current: Optional[str] = None) -> InlineKeyboardMarkup:
    tools = tool_row(T, sign, source, symbol, skip=current)
    return _kb([
        tools[:2], tools[2:],
        [Button(text=T("kb.back_positions"), callback_data=sign("pl"))],
        back_row(T, sign),
    ])


def triage(T: Translator, sign: Signer, focus: Sequence[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """`focus`: (source, symbol, label) in handling order."""
    rows: Rows = [[Button(text=f"🚑 {label}", callback_data=sign("pr", source, symbol))] for source, symbol, label in focus[:3]]
    rows.append([Button(text=T("kb.refresh"), callback_data=sign("tr")), Button(text=T("kb.positions"), callback_data=sign("pl"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def stress(T: Translator, sign: Signer, wick_targets: Sequence[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: Rows = [[Button(text=T("kb.wick_for", symbol=symbol), callback_data=sign("pw", source, symbol))
                   for source, symbol in wick_targets[:3]]]
    rows.append([Button(text=T("kb.refresh"), callback_data=sign("st"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def risk_overview(T: Translator, sign: Signer, first: Optional[tuple[str, str]]) -> InlineKeyboardMarkup:
    """`first`: (source, symbol) of the position triage puts first, if any is at risk."""
    return _kb([
        [Button(text=T("kb.rescue_first", symbol=first[1]), callback_data=sign("pr", *first))] if first else [],
        [Button(text=T("kb.positions"), callback_data=sign("pl")), Button(text=T("kb.stop_check"), callback_data=sign("sc"))],
        [Button(text=T("kb.triage"), callback_data=sign("tr")), Button(text=T("kb.stress"), callback_data=sign("st"))],
        [Button(text=T("kb.ask"), callback_data=sign("qa")), Button(text=T("kb.refresh"), callback_data=sign("ro"))],
        back_row(T, sign),
    ])


def stop_list(T: Translator, sign: Signer, positions: Sequence[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """`positions`: (source, symbol, label); each opens that position's full stop-loss report."""
    rows: Rows = [[Button(text=label, callback_data=sign("ps", source, symbol))] for source, symbol, label in positions[:MAX_POSITIONS]]
    rows.append([Button(text=T("kb.refresh"), callback_data=sign("sc")), Button(text=T("kb.risk_overview"), callback_data=sign("ro"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def account(T: Translator, sign: Signer, pending: Sequence[tuple[str, str]], kill_active: bool) -> InlineKeyboardMarkup:
    """`pending`: (action id, short id), each with confirm / cancel."""
    rows: Rows = [[Button(text=T("kb.confirm_action", short=short), callback_data=sign("ay", action_id)),
                   Button(text=T("kb.cancel"), callback_data=sign("an", action_id))] for action_id, short in pending]
    rows.append([Button(text=T("kb.killswitch_off" if kill_active else "kb.killswitch_on"),
                        callback_data=sign("ak", "off" if kill_active else "on"))])
    rows.append([Button(text=T("kb.settings"), callback_data=sign("se")), Button(text=T("kb.binding"), callback_data=sign("b"))])
    rows.append([Button(text=T("kb.refresh"), callback_data=sign("ac"))])
    rows.append([Button(text=T("pro.kb.upgrade"), callback_data=sign("pu"))])
    rows.append([Button(text=T("pro.kb.agent"), callback_data=sign("px", "auto"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def ask_menu(T: Translator, sign: Signer, questions: Sequence[str]) -> InlineKeyboardMarkup:
    rows: Rows = [[Button(text=T(f"ask.q.{q}"), callback_data=sign("qq", q))] for q in questions]
    rows.append(back_row(T, sign))
    return _kb(rows)


def ask_answer(T: Translator, sign: Signer) -> InlineKeyboardMarkup:
    return _kb([
        [Button(text=T("kb.ask_more"), callback_data=sign("qa")), Button(text=T("kb.risk_overview"), callback_data=sign("ro"))],
        back_row(T, sign),
    ])


def refresh_back(T: Translator, sign: Signer, action: str) -> InlineKeyboardMarkup:
    return _kb([[Button(text=T("kb.refresh"), callback_data=sign(action))], back_row(T, sign)])


def no_account(T: Translator, sign: Signer) -> InlineKeyboardMarkup:
    return _kb([
        [Button(text=T("kb.binding"), callback_data=sign("b")), Button(text=T("kb.demo"), callback_data=sign("dm"))],
        back_row(T, sign),
    ])


def back_only(T: Translator, sign: Signer) -> InlineKeyboardMarkup:
    return _kb([back_row(T, sign)])


def setting_value(T: Translator, key: str, value) -> str:
    if key == "quiet":
        return quiet_window(str(value), T.lang)
    if key in TIERS:
        return f"{value:g}%"
    return T("kb.minutes" if key == "interval" else "kb.times", n=value)


def settings(T: Translator, sign: Signer, current: dict) -> InlineKeyboardMarkup:
    def toggle(key: str) -> Button:
        on = bool(current.get(key))
        return Button(text=f"{'✅' if on else '⬜'} {T(f'settings.toggle.{key}')}", callback_data=sign("ss", key, "toggle"))

    def pick(key: str, value) -> Button:
        return Button(text=f"{T(f'settings.label.{key}')} {value}", callback_data=sign("sp", key))

    return _kb([
        [pick(k, setting_value(T, k, current[k])) for k in TIERS],
        [pick("interval", setting_value(T, "interval", current["interval"])), pick("max", setting_value(T, "max", current["max"]))],
        [pick("quiet", current["quiet_label"])],
        [toggle("escalation"), toggle("adaptive")],
        [toggle("stops")],
        back_row(T, sign),
    ])


def setting_options(T: Translator, sign: Signer, key: str, options: Iterable, current) -> InlineKeyboardMarkup:
    buttons = [Button(text=("● " if str(v) == str(current) else "") + setting_value(T, key, v), callback_data=sign("ss", key, str(v)))
               for v in options]
    rows: Rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([Button(text=T("kb.back_settings"), callback_data=sign("se"))])
    return _kb(rows)


def binding_menu(T: Translator, sign: Signer) -> InlineKeyboardMarkup:
    return _kb([
        [Button(text=T("kb.bind_binance"), callback_data=sign("bb")), Button(text=T("kb.bind_hyperliquid"), callback_data=sign("bh"))],
        [Button(text=T("kb.bound"), callback_data=sign("bl")), Button(text=T("kb.unbind"), callback_data=sign("bu"))],
        back_row(T, sign),
    ])


def binding_waiting(T: Translator, sign: Signer) -> InlineKeyboardMarkup:
    return _kb([[Button(text=T("kb.bind_cancel"), callback_data=sign("bc"))]])


def binding_back(T: Translator, sign: Signer) -> InlineKeyboardMarkup:
    return _kb([[Button(text=T("kb.back_binding"), callback_data=sign("b"))], back_row(T, sign)])


def unbind_list(T: Translator, sign: Signer, bindings: Sequence[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """`bindings`: (venue, network, masked label)."""
    rows: Rows = [[Button(text=f"🗑 {label}", callback_data=sign("bx", venue, network))] for venue, network, label in bindings]
    rows.append([Button(text=T("kb.back_binding"), callback_data=sign("b"))])
    return _kb(rows)


def unbind_confirm(T: Translator, sign: Signer, venue: str, network: str) -> InlineKeyboardMarkup:
    return _kb([[Button(text=T("kb.unbind_confirm"), callback_data=sign("by", venue, network)),
                 Button(text=T("kb.cancel"), callback_data=sign("bu"))]])


def dashboard(T: Translator, sign: Signer, url: Optional[str]) -> InlineKeyboardMarkup:
    rows: Rows = []
    if url and url.startswith("https://"):
        rows.append([Button(text=T("kb.open_dashboard"), url=url)])
    rows.append([Button(text=T("pro.kb.upgrade"), callback_data=sign("pu"))])
    rows.append([Button(text=T("pro.kb.agent"), callback_data=sign("px", "auto"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def pro_upgrade(T: Translator, sign: Signer, checkout_url: Optional[str] = None,
                manage: bool = False) -> InlineKeyboardMarkup:
    """Pro upgrade / billing keyboard. PayPal URL buttons charge only after the user taps them."""
    rows: Rows = []
    if checkout_url:
        rows.append([Button(text=T("pro.kb.upgrade"), url=checkout_url)])
    if manage:
        rows.append([Button(text=T("pro.cancel_cta"), callback_data=sign("pm"))])
    else:
        rows.append([Button(text=T("pro.kb.rescue_pass"), callback_data=sign("rp"))])
        rows.append([Button(text=T("pro.kb.manage"), callback_data=sign("pm"))])
    rows.append([Button(text=T("pro.kb.agent"), callback_data=sign("px", "auto"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def pro_manage(T: Translator, sign: Signer, checkout_url: Optional[str] = None) -> InlineKeyboardMarkup:
    rows: Rows = []
    if checkout_url:
        rows.append([Button(text=T("pro.kb.upgrade"), url=checkout_url)])
    rows.append([Button(text=T("pro.kb.upgrade"), callback_data=sign("pu"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def rescue_pass(T: Translator, sign: Signer, checkout_url: Optional[str] = None) -> InlineKeyboardMarkup:
    rows: Rows = []
    if checkout_url:
        rows.append([Button(text=T("pro.kb.rescue_pass"), url=checkout_url)])
    rows.append([Button(text=T("pro.kb.upgrade"), callback_data=sign("pu"))])
    rows.append([Button(text=T("pro.kb.agent"), callback_data=sign("px", "auto"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def agent_checkout(T: Translator, sign: Signer, checkout_url: Optional[str] = None,
                   product: Optional[str] = None) -> InlineKeyboardMarkup:
    """Approve-on-PayPal URL plus the rest of the billing keyboard. Never auto-charges."""
    rows: Rows = []
    if checkout_url:
        label = T("pro.kb.rescue_pass") if product == "rescue" else T("pro.kb.upgrade")
        rows.append([Button(text=label, url=checkout_url)])
    rows.append([Button(text=T("pro.kb.upgrade"), callback_data=sign("pu")),
                 Button(text=T("pro.kb.rescue_pass"), callback_data=sign("rp"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def rescue_with_upgrade(T: Translator, sign: Signer, source: str, symbol: str, show_upgrade: bool = False) -> InlineKeyboardMarkup:
    """Rescue sim keyboard with optional Pro upgrade hint."""
    rows: Rows = [tool_row(T, sign, source, symbol, skip="pr")]
    if show_upgrade:
        rows.append([Button(text=T("pro.kb.upgrade"), callback_data=sign("pu"))])
    rows.append([Button(text=T("kb.back_positions"), callback_data=sign("pl"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def demo_menu(T: Translator, sign: Signer, running: bool, demo_mode: bool) -> InlineKeyboardMarkup:
    rows: Rows = []
    if running:
        rows.append([Button(text=T("kb.risk_overview"), callback_data=sign("ro"))])
        rows.append([Button(text=T("kb.positions"), callback_data=sign("pl")), Button(text=T("kb.stop_check"), callback_data=sign("sc"))])
        rows.append([Button(text=T("kb.sim_stop"), callback_data=sign("dx"))])
    else:
        buttons = [Button(text=T(f"demo.scenario.{key}"), callback_data=sign("ds", key)) for key in SCENARIOS]
        rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if demo_mode:
        rows.append([Button(text=T("kb.demo_off"), callback_data=sign("do"))])
    rows.append(back_row(T, sign))
    return _kb(rows)


def demo_started(T: Translator, sign: Signer) -> InlineKeyboardMarkup:
    return _kb([
        [Button(text=T("kb.risk_overview"), callback_data=sign("ro"))],
        [Button(text=T("kb.positions"), callback_data=sign("pl")), Button(text=T("kb.stop_check"), callback_data=sign("sc"))],
        [Button(text=T("kb.triage"), callback_data=sign("tr")), Button(text=T("kb.stress"), callback_data=sign("st"))],
        [Button(text=T("kb.funding"), callback_data=sign("ff"))],
        [Button(text=T("kb.demo_menu"), callback_data=sign("dm"))],
        back_row(T, sign),
    ])


def alert_rows(T: Translator, sign: Signer, source: Optional[str], symbol: Optional[str], ack_id: Optional[str],
               demo: bool = False, agent: bool = False) -> Rows:
    rows: Rows = []
    if source and symbol:
        rows.append([Button(text=T("kb.alert_rescue"), callback_data=sign("pr", source, symbol))])
    if agent:
        rows.append([Button(text=T("pro.kb.agent"), callback_data=sign("px", "rescue"))])
    if ack_id:
        rows.append([Button(text=T("kb.alert_awake"), callback_data=sign("aa", ack_id)),
                     Button(text=T("kb.alert_snooze_demo" if demo else "kb.alert_snooze"), callback_data=sign("az", ack_id))])
    if demo:
        rows.append([Button(text=T("kb.sim_stop"), callback_data=sign("dx"))])
    return rows


def alert_keyboard(T: Translator, sign: Signer, source: Optional[str], symbol: Optional[str],
                   ack_id: Optional[str], demo: bool = False, agent: bool = False) -> Optional[InlineKeyboardMarkup]:
    rows = alert_rows(T, sign, source, symbol, ack_id, demo, agent=agent)
    return _kb(rows) if rows else None


def iter_buttons(markup: Optional[InlineKeyboardMarkup]) -> Iterator[InlineKeyboardButton]:
    for row in (markup.inline_keyboard if markup else []):
        yield from row
