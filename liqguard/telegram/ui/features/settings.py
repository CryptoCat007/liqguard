"""🔔 通知設定: alert tiers, wake-up escalation interval/count and quiet hours (same rules as the dashboard)."""

from __future__ import annotations

from liqguard.core.user_settings import PRESETS, apply_setting, settings_dict

from .. import keyboards as kb
from ..screen import Request, Screen

HINTS = {
    "warning": "距強平小於這個 % 時發出 ⚠️ 警告",
    "critical": "距強平小於這個 % 時發出 🚨 嚴重警報（會啟動喚醒提醒）",
    "emergency": "距強平小於這個 % 時發出 🆘 緊急警報",
    "interval": "嚴重/緊急警報在你按「✅ 我醒了」前，每隔幾分鐘再提醒一次",
    "max": "最多重複提醒幾次",
    "quiet": "勿擾時段（台北時間）內只發送嚴重與緊急警報，其他通知延後",
}


def _text(current: dict, note: str = "") -> str:
    onoff = {True: "開", False: "關"}
    return (
        "🔔 通知設定\n\n"
        f"警示門檻：⚠️ {current['warning']:g}% · 🚨 {current['critical']:g}% · 🆘 {current['emergency']:g}%\n"
        f"喚醒提醒：{onoff[current['escalation']]}，每 {current['interval']:g} 分鐘，最多 {current['max']} 次\n"
        f"勿擾時段：{current['quiet_label']}（UTC{current['quiet_utc_offset_hours']:+g}，嚴重/緊急警報仍會送達）\n"
        f"自適應門檻：{onoff[current['adaptive']]}（週末、清淡時段、插針大的幣自動放寬）\n"
        f"止損檢查：{onoff[current['stops']]}\n"
        + (f"\n{note}\n" if note else "")
        + "\n點按鈕修改。"
    )


async def show(req: Request) -> Screen:
    user = await req.services.user(req.user_id)
    current = settings_dict(user.threshold)
    return Screen(_text(current), kb.settings(req.sign, current))


async def pick(req: Request) -> Screen:
    key = req.args[0]
    user = await req.services.user(req.user_id)
    current = settings_dict(user.threshold)
    if key not in PRESETS:
        updated = apply_setting(user.threshold, key, "toggle")
        if updated is not None:
            await req.services.save_threshold(user, updated)
            current = settings_dict(updated)
        return Screen(_text(current), kb.settings(req.sign, current), toast="已更新")
    value = current[key] if key != "quiet" else (current["quiet"] or "off")
    return Screen(f"🔔 {kb.SETTING_LABELS[key]}\n\n{HINTS[key]}\n\n請選擇：", kb.setting_options(req.sign, key, PRESETS[key], value))


async def apply(req: Request) -> Screen:
    key, value = req.args
    user = await req.services.user(req.user_id)
    updated = apply_setting(user.threshold, key, value)
    if updated is None:
        current = settings_dict(user.threshold)
        note = "❌ 無法套用：門檻必須維持 緊急 < 嚴重 < 警告，且數值在允許範圍內。"
        return Screen(_text(current, note), kb.settings(req.sign, current), toast="無法套用這個設定", alert=True)
    await req.services.save_threshold(user, updated)
    current = settings_dict(updated)
    return Screen(_text(current, "✅ 已儲存"), kb.settings(req.sign, current), toast="已儲存")


HANDLERS = {"se": show, "sp": pick, "ss": apply}
