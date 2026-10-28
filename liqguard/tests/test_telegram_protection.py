"""Telegram surface for wick stress, stop-loss checks, ADL lights, /settings and the "I'm awake" button."""

import time

import pytest

from liqguard.models import User
from liqguard.telegram.views import protection_lines, protection_summary

from .conftest import fake_callback, fake_message

ALICE, MALLORY = 1001, 2002


@pytest.fixture
def handlers(ctx):
    from liqguard.telegram import BotHandlers

    return BotHandlers(ctx)


def _sent(message) -> str:
    return message.answer.call_args.args[0]


class TestViews:
    def test_protection_lines_cover_wicks_tiers_stop_and_adl(self):
        info = {
            "symbol": "BTCUSDT",
            "wicks": {"source": "demo", "one_max_wick_liquidates": False, "exposures": [
                {"interval": "5m", "max_pct": 3.2, "max_wicks_away": 2.5, "typical_wicks_away": 9.0},
            ]},
            "thresholds": {"adapted": True, "effective": {"warning": 25.3, "critical": 11.5, "emergency": 5.8},
                           "reasons": ["weekend ×1.15"]},
            "stop": {"status": "beyond_liquidation", "stop_price": 45000.0, "gap_pct": -1.0, "needs_warning": True,
                     "required_gap_pct": 0.5, "fixes": [{"kind": "move_stop", "stop_price": 47137.0}]},
            "adl": {"supported": True, "lights": 4, "warn": True},
        }
        text = "\n".join(protection_lines(info))
        assert "2.5× max" in text and "(demo)" in text
        assert "25.3/11.5/5.8%" in text and "weekend" in text
        assert "beyond liq" in text and "$47,137.00" in text
        assert "●●●●○ 4/5" in text and "ADL queue" in text

    def test_hyperliquid_adl_and_unknown_orders(self):
        text = "\n".join(protection_lines({"stop": None, "adl": {"supported": False}}))
        assert "orders not available" in text
        assert "n/a" in text and "Hyperliquid" in text

    def test_summary_lists_risky_symbols(self):
        infos = [
            {"symbol": "ETH", "stop": {"status": "no_stop"}, "adl": {}, "wicks": None},
            {"symbol": "TSLA", "stop": {"status": "ok"}, "adl": {"warn": True}, "wicks": {"one_max_wick_liquidates": True}},
        ]
        summary = protection_summary(infos)
        assert "no stop: ETH" in summary and "ADL 4–5/5: TSLA" in summary and "one max wick" in summary
        assert protection_summary([{"symbol": "X", "stop": {"status": "ok"}, "adl": {}}]) is None


class TestCommands:
    async def test_positions_show_stop_and_adl_for_demo_book(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        message = fake_message(ALICE, "/positions")
        await handlers.cmd_positions(message)
        text = _sent(message)
        assert "Stop:" in text and "ADL:" in text and "Wicks away" in text

    async def test_risk_lists_protection_checks(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        message = fake_message(ALICE, "/risk")
        await handlers.cmd_risk(message)
        assert "Protection checks" in _sent(message)

    async def test_settings_text_args_persist_and_validate(self, ctx, handlers):
        message = fake_message(ALICE, "/settings")
        await handlers.cmd_settings(message, command=type("C", (), {"args": "interval 3"})())
        user = await ctx.storage.get_user(ALICE)
        assert user.threshold.escalation_interval_minutes == 3
        assert "every **3 min**" in _sent(message) and "Notifications" in _sent(message)

        bad = fake_message(ALICE, "/settings")
        await handlers.cmd_settings(bad, command=type("C", (), {"args": "interval 0.1"})())
        assert "out-of-range" in _sent(bad)
        assert (await ctx.storage.get_user(ALICE)).threshold.escalation_interval_minutes == 3

    async def test_settings_buttons_toggle_only_the_callers_settings(self, ctx, handlers):
        for uid in (ALICE, MALLORY):
            await handlers.cmd_settings(fake_message(uid, "/settings"))
        await handlers.callback_settings(fake_callback(ALICE, "settings:escalation:toggle"))
        await handlers.callback_settings(fake_callback(ALICE, "settings:max:10"))
        alice, mallory = await ctx.storage.get_user(ALICE), await ctx.storage.get_user(MALLORY)
        assert alice.threshold.escalation_enabled is False and alice.threshold.escalation_max_reminders == 10
        assert mallory.threshold.escalation_enabled is True and mallory.threshold.escalation_max_reminders == 5

    def test_apply_setting_rejects_garbage(self, handlers):
        from liqguard.models import UserThreshold

        t = UserThreshold()
        assert handlers.apply_setting(t, "max", "50") is None
        assert handlers.apply_setting(t, "max", "abc") is None
        assert handlers.apply_setting(t, "stops", "maybe") is None
        assert handlers.apply_setting(t, "stops", "off").stop_loss_checks is False


class TestAck:
    async def test_live_ack_is_owner_scoped(self, ctx, handlers):
        ack = ctx.escalations.open(ALICE, "binance", "BTCUSDT", "critical", "🚨", time.time(), 120, 5)
        intruder = fake_callback(MALLORY, f"ack:{ack.id}")
        await handlers.callback_ack(intruder)
        assert ack.acknowledged_at is None
        intruder.answer.assert_called_with("Already acknowledged or expired.")

        owner = fake_callback(ALICE, f"ack:{ack.id}")
        await handlers.callback_ack(owner)
        assert ack.acknowledged_at is not None
        owner.message.edit_reply_markup.assert_called_once()
        assert "BTCUSDT" in owner.message.answer.call_args.args[0]

    async def test_sim_ack_stops_simulated_reminders(self, ctx, handlers):
        await ctx.storage.save_user(User(telegram_id=ALICE, is_demo_mode=True))
        sim = handlers.simulation_for(await ctx.storage.get_user(ALICE))
        await sim.start()
        await sim.wait()
        pending = [a for a in sim.alerts if a.get("ack_id")]
        assert pending
        ack_id = pending[-1]["ack_id"]
        await handlers.callback_ack(fake_callback(MALLORY, f"ack:{ack_id}"))
        owner = fake_callback(ALICE, f"ack:{ack_id}")
        await handlers.callback_ack(owner)
        assert "reminders" in owner.message.answer.call_args.args[0]
        sim.reset()
