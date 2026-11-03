"""
Telegram and the dashboard ask Muse through one `MuseService`: the same
client, cache and inputs, so one book costs one Muse request whichever
channel looks first, and both channels show the same words. The dashboard
shows rule-based text at once and receives Muse's over its WebSocket.
"""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from liqguard.core import AppContext
from liqguard.core.wicks import WickService
from liqguard.i18n import ZH
from liqguard.models import User
from liqguard.storage import EncryptedStorage
from liqguard.telegram.app import wire_bot
from liqguard.telegram.handlers import BotHandlers
from liqguard.telegram.ui import TelegramUI
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE

from .conftest import ALICE, BOB, until
from .test_muse_async import SmartMuse, both, live_llm
from .zh_scan import english_in

SUMMARY = "Summarize portfolio"


class ZhMuse(SmartMuse):
    """Muse answering in Traditional Chinese, as it does for zh-Hant users."""

    def answer(self, system: str, user: str):
        if "rescue options" in system:
            plan = json.loads(user)
            return both({"headline": "Muse 比較了各救援方案。", "recommended_option": plan["recommended"],
                         "trade_offs": [] if plan["recommended"] == "none" else [{"option": plan["recommended"], "summary": "Muse 偏好這個方案。", "pros": [], "cons": []}],
                         "caveat": "Muse 提醒。"})
        if "stop-loss" in system:
            return both({"headline": "Muse 檢查了停損。", "explanation": "Muse 說明停損位置。", "suggested_fix": "none"})
        if SUMMARY in system or "總結投資組合" in system:
            return {"headline": f"Muse：{json.loads(user)['riskiest_symbol']} 風險最高。"}
        if "Explain risk" in system or "解釋風險" in system:
            return {"summary": "Muse 摘要。", "explanation": "Muse 解釋風險。"}
        return None


async def live_ctx(settings, muse: SmartMuse) -> AppContext:
    storage = EncryptedStorage(db_path=settings.db_path, encryption_key=EncryptedStorage.generate_key())
    return await AppContext.create(
        settings, storage=storage, llm=live_llm(muse),
        simulation_options={"tick_interval": 0, "escalation_seconds_per_minute": 0.05}, wicks=WickService(sources=[]),
    )


async def demo_user(ctx: AppContext, user_id: int = ALICE, language: str = "en") -> User:
    """A critical demo book, pinned: the demo market moves on every read, and Muse caches per snapshot."""
    await ctx.storage.save_user(User(telegram_id=user_id, is_demo_mode=True, language=language,
                                     threshold=ctx.settings.default_threshold()))
    ctx.positions.demo_adapter(user_id, "critical")
    user = await ctx.storage.get_user(user_id)
    book = await ctx.positions.snapshot_for(user)
    read = ctx.positions.snapshot_for

    async def pinned(who: User):
        return book if who.telegram_id == user_id else await read(who)

    ctx.positions.snapshot_for = pinned
    return user


def web_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


def signed_in(client, ctx: AppContext, user_id: int):
    client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(user_id, 0))
    return client


async def collect(sim) -> list[dict]:
    events: list[dict] = []

    async def on(event):
        events.append(event)

    sim.subscribe("test", on)
    return events


def summaries(muse: SmartMuse) -> int:
    return sum(SUMMARY in p for p in muse.prompts)


class TestOneService:
    async def test_every_channel_holds_the_same_muse_service(self, ctx):
        app = create_app(ctx)
        assert app.state.muse is ctx.muse
        assert BotHandlers(ctx).muse is ctx.muse
        assert TelegramUI(ctx).services.muse is ctx.muse
        assert wire_bot(ctx, MagicMock()).monitor.explainer is ctx.muse
        assert ctx.muse.llm is ctx.llm
        assert ctx.simulations.public.llm is ctx.muse.llm
        assert ctx.simulations.get_or_create(ALICE).llm is ctx.muse.llm

    async def test_risk_and_dashboard_call_the_same_service_and_share_one_answer(self, settings, monkeypatch):
        gate = asyncio.Event()
        muse = SmartMuse(gate)
        ctx = await live_ctx(settings, muse)
        try:
            user = await demo_user(ctx)
            asked = []
            portfolio = ctx.muse.portfolio

            def spy(*args, **kwargs):
                asked.append(args[0].telegram_id)
                return portfolio(*args, **kwargs)

            monkeypatch.setattr(ctx.muse, "portfolio", spy)
            handlers = BotHandlers(ctx)
            reply = await handlers._risk_reply(user)
            async with signed_in(web_client(create_app(ctx)), ctx, ALICE) as web:
                first = (await web.get("/api/me/account")).json()["muse"]
                assert first["summary"]["mock"] is True and first["summary_pending"] is True
                assert first["summary"]["source"] == "rules"
                assert reply.text.startswith(f"💡 {first['summary']['headline']}\n")

                gate.set()
                live = await reply.pending
                again = (await web.get("/api/me/account")).json()["muse"]
            assert asked == [ALICE] * 3
            assert summaries(muse) == 1
            assert again["summary"]["mock"] is False and again["summary_pending"] is False
            assert again["summary"]["headline"] == live["headline"]
            assert reply.compose(live).startswith(f"💡 {again['summary']['headline']} · ✦ Muse\n")
        finally:
            await ctx.close()

    async def test_dashboard_first_then_telegram_reuses_the_answer(self, settings):
        muse = SmartMuse()
        ctx = await live_ctx(settings, muse)
        try:
            user = await demo_user(ctx)
            async with signed_in(web_client(create_app(ctx)), ctx, ALICE) as web:
                await web.get("/api/me/account")
                assert await until(lambda: summaries(muse) == 1)
                await asyncio.sleep(0.05)
                reply = await BotHandlers(ctx)._risk_reply(user)
            assert reply.pending is None and reply.text.startswith("💡 Muse:") and reply.text.split("\n")[0].endswith("· ✦ Muse")
            assert summaries(muse) == 1
        finally:
            await ctx.close()


class TestWebSocketFollowUps:
    @pytest.fixture
    async def live(self, settings):
        gate = asyncio.Event()
        muse = SmartMuse(gate)
        ctx = await live_ctx(settings, muse)
        await demo_user(ctx)
        await demo_user(ctx, BOB)
        app = create_app(ctx)
        sim = ctx.simulations.get_or_create(ALICE)
        events = await collect(sim)
        async with signed_in(web_client(app), ctx, ALICE) as web:
            yield SimpleNamespace(ctx=ctx, gate=gate, muse=muse, web=web, events=events, app=app)
        await ctx.close()

    async def test_account_summary_and_suggestion_follow_over_the_socket(self, live):
        body = (await live.web.get("/api/me/account")).json()["muse"]
        assert body["mode"] == "pending"
        assert body["summary"]["mock"] is True
        suggestion = body["suggestion"]
        assert suggestion["mock"] is True and body["suggestion_pending"] is True
        assert suggestion["severity"] in ("warning", "critical", "emergency")

        live.gate.set()
        assert await until(lambda: len([e for e in live.events if e["type"] == "account_muse"]) == 2)
        pushed = {k: v for e in live.events if e["type"] == "account_muse" for k, v in e["data"].items()}
        assert pushed["view"] == body["view"]
        assert pushed["summary"]["headline"].startswith("Muse:") and pushed["summary"]["mock"] is False
        assert pushed["suggestion"]["explanation"] == "Muse explanation." and pushed["suggestion"]["mock"] is False
        assert pushed["suggestion"]["symbol"] == suggestion["symbol"]
        assert await until(lambda: {"type": "muse", "data": {"mode": "live"}} in live.events)

    async def test_live_rescue_text_follows_over_the_socket(self, live):
        now = (await live.web.get("/api/me/rescue/live?symbol=BTCUSDT&venue=demo")).json()
        assert now["muse_pending"] is True and now["explanation"]["mock"] is True
        live.gate.set()
        assert await until(lambda: any(e["type"] == "rescue_muse" for e in live.events))
        pushed = next(e["data"] for e in live.events if e["type"] == "rescue_muse")
        assert pushed["scope"] == "live" and pushed["symbol"] == "BTCUSDT" and pushed["venue"] == "demo"
        assert pushed["explanation"]["headline"] == "Muse compares the rescue options."
        assert sum("rescue options" in p for p in live.muse.prompts) == 1

    async def test_quick_reports_carry_muse_line_and_follow_over_the_socket(self, live):
        report = (await live.web.get("/api/me/position-tool/live?symbol=BTCUSDT&venue=demo&tool=rescue")).json()
        assert report["muse_pending"] is True
        assert report["muse"]["mock"] is True and report["muse"]["source"] == "rules"
        assert report["lines"][0].startswith("💡 ") and "✦ Muse" not in report["lines"][0]
        wick = (await live.web.get("/api/me/position-tool/live?symbol=BTCUSDT&venue=demo&tool=wick")).json()
        assert wick["muse_pending"] is False and not any("✦ Muse" in line for line in wick["lines"])

        live.gate.set()
        assert await until(lambda: any(e["type"] == "report_muse" for e in live.events))
        pushed = next(e["data"] for e in live.events if e["type"] == "report_muse")
        assert (pushed["scope"], pushed["tool"], pushed["symbol"], pushed["venue"]) == ("live", "rescue", "BTCUSDT", "demo")
        assert pushed["report"]["lines"][0] == "💡 Muse compares the rescue options. · ✦ Muse"
        assert sum("Muse compares" in line for line in pushed["report"]["lines"]) == 1
        assert pushed["report"]["title"] == report["title"]

        cached = (await live.web.get("/api/me/position-tool/live?symbol=BTCUSDT&venue=demo&tool=rescue")).json()
        assert cached["muse_pending"] is False and cached["lines"] == pushed["report"]["lines"]

    async def test_telegram_quick_report_shows_the_same_muse_line(self, live):
        carol = 3003
        await live.ctx.storage.save_user(User(telegram_id=carol, is_demo_mode=True, threshold=live.ctx.settings.default_threshold()))
        live.gate.set()
        sim = live.ctx.simulations.get_or_create(carol)
        await sim.start(source="test")
        await sim.wait()
        ui = TelegramUI(live.ctx)
        screen = await ui.router.run(carol, "pr", ("sim", "BTCUSDT"))
        if screen.later is not None:
            assert await screen.later() is not None
            screen = await ui.router.run(carol, "pr", ("sim", "BTCUSDT"))
        async with signed_in(web_client(live.app), live.ctx, carol) as mine:
            web_report = (await mine.get("/api/me/position-tool?symbol=BTCUSDT&tool=rescue")).json()
        assert web_report["muse_pending"] is False
        assert screen.text == web_report["title"] + "\n\n" + "\n".join(web_report["lines"]).strip()
        assert "💡 Muse compares the rescue options. · ✦ Muse" in screen.text

    async def test_telegram_quick_report_tap_is_edited_when_muse_answers(self, live):
        ui = TelegramUI(live.ctx)
        cb = AsyncMock()
        cb.from_user = MagicMock(id=ALICE, language_code=None)
        cb.message = AsyncMock()
        cb.message.chat = MagicMock(type="private", id=ALICE)
        cb.data = ui.codec.encode(ALICE, "pr", "demo", "BTCUSDT")
        await asyncio.wait_for(ui.router.handle(cb), 5)
        first = cb.message.edit_text.await_args
        assert "💡 " in first.args[0] and "✦ Muse" not in first.args[0]
        live.gate.set()
        assert await until(lambda: cb.message.edit_text.await_count == 2)
        edited = cb.message.edit_text.await_args
        assert "💡 Muse compares the rescue options. · ✦ Muse" in edited.args[0] and "✦ Muse (mock)" not in edited.args[0]
        assert edited.kwargs["reply_markup"] is first.kwargs["reply_markup"]

    async def test_other_users_never_see_the_follow_up(self, live):
        bob_events = await collect(live.ctx.simulations.get_or_create(BOB))
        await live.web.get("/api/me/account")
        live.gate.set()
        assert await until(lambda: any(e["type"] == "account_muse" for e in live.events))
        await asyncio.sleep(0.05)
        assert not [e for e in bob_events if e["type"].endswith("_muse")]

    async def test_demo_rescue_and_report_follow_over_the_public_socket(self, live):
        public = await collect(live.ctx.simulations.public)
        now = (await live.web.get("/api/demo/rescue?symbol=BTCUSDT")).json()
        report = (await live.web.get("/api/demo/position-tool?symbol=BTCUSDT&tool=rescue")).json()
        assert now["muse_pending"] is True and report["muse_pending"] is True
        live.gate.set()
        assert await until(lambda: {e["type"] for e in public} >= {"rescue_muse", "report_muse"})
        rescue = next(e["data"] for e in public if e["type"] == "rescue_muse")
        assert rescue["scope"] == "sim" and rescue["explanation"]["mock"] is False
        assert not [e for e in live.events if e["type"].endswith("_muse")]


def _receive_until(ws, kinds: set[str], timeout: float = 15) -> dict[str, dict]:
    seen: dict[str, dict] = {}

    def run():
        while not kinds <= seen.keys():
            event = ws.receive_json()
            seen.setdefault(event["type"], event)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    return seen


async def test_zh_follow_ups_reach_the_browser_in_chinese(settings):
    muse = ZhMuse(delay=0.3)
    ctx = await live_ctx(settings, muse)
    try:
        await demo_user(ctx, language=ZH)
        with TestClient(create_app(ctx), base_url="https://testserver") as c:
            c.cookies.set(SESSION_COOKIE, ctx.sessions.issue(ALICE, 0))
            with c.websocket_connect("/ws/me") as ws:
                assert ws.receive_json()["type"] == "snapshot"
                account = c.get("/api/me/account").json()
                rescue = c.get("/api/me/rescue/live?symbol=BTCUSDT&venue=demo").json()
                report = c.get("/api/me/position-tool/live?symbol=BTCUSDT&venue=demo&tool=stop").json()
                expected = {"account_muse", "rescue_muse"} | ({"report_muse"} if report["muse_pending"] else set())
                seen = _receive_until(ws, expected)
            assert expected <= seen.keys(), seen.keys()
            assert account["muse"]["summary"]["headline"] and rescue["explanation"]
            assert report["lines"][0].startswith("💡 ") and "模擬" not in report["lines"][0]
            leftovers = [hit for part in (account, rescue, report, *seen.values()) for hit in english_in(part)]
            assert not leftovers, leftovers[:10]
            assert any(e["data"].get("summary", {}).get("headline", "").startswith("Muse：") for e in seen.values()
                       if e["type"] == "account_muse") or account["muse"]["summary"]["mock"] is False
    finally:
        await ctx.close()
