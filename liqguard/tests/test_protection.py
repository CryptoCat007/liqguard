"""Wick stress test, session-adaptive tiers, stop-loss sanity check, ADL lights and wake-up escalation."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from liqguard.ai import MetaLLMClient, mock_stop_explanation, validate_stop_explanation
from liqguard.alerts import (
    AlertManager,
    EscalationService,
    EscalationTracker,
    PositionMonitor,
    urgency,
)
from liqguard.core import BookSnapshot
from liqguard.core.wicks import WickService
from liqguard.exchanges import BinanceAdapter, HyperliquidAdapter
from liqguard.exchanges.klines import (
    BinanceKlineSource,
    HyperliquidKlineSource,
    RateLimiter,
    binance_kline_weight,
)
from liqguard.exchanges.orders import parse_binance_order, parse_hyperliquid_order
from liqguard.models import OpenOrder, Position, Side, UserThreshold, Venue, WickProfile, WickStat
from liqguard.models.orders import STOP_MARKET, TAKE_PROFIT, TRAILING_STOP
from liqguard.risk import (
    RiskContext,
    adaptive_threshold,
    adl_status,
    check_stop_loss,
    market_session,
)
from liqguard.risk.adl import lights_from_quantile
from liqguard.risk.sessions import is_stock_perp, parse_hours, us_equity_market_open
from liqguard.risk.wicks import aggregate, percentile, stress_summary, wick_stat, wicks_away

MIN = 60_000
SAT_2000 = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)
TUE_1500 = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
TUE_2300 = datetime(2026, 10, 27, 23, 0, tzinfo=timezone.utc)


def pos(side=Side.LONG, mark=100.0, liq=90.0, symbol="BTCUSDT", venue=Venue.BINANCE, pnl=-5.0, adl=None, size=1.0):
    return Position(
        venue=venue, symbol=symbol, side=side, size=size, entry_price=105.0, mark_price=mark,
        liquidation_price=liq, margin=10.0, leverage=10.0, unrealized_pnl=pnl, adl_quantile=adl,
        margin_mode="cross",
    )


def stop(trigger, side=Side.SHORT, symbol="BTCUSDT", venue=Venue.BINANCE, **kw):
    return OpenOrder(venue=venue, symbol=symbol, side=side, order_type=STOP_MARKET, trigger_price=trigger, close_position=True, **kw)


def profile(one_max=2.0, five_max=4.0, one_typ=0.2, five_typ=0.5, symbol="BTCUSDT"):
    return WickProfile(Venue.BINANCE, symbol, {
        "1m": WickStat("1m", 43200, 30.0, one_typ, one_max, one_typ, one_max),
        "5m": WickStat("5m", 8640, 30.0, five_typ, five_max, five_typ, five_max),
    }, source="demo")


# --------------------------------------------------------------------------- wick statistics


class TestWicks:
    def test_percentile_interpolates(self):
        assert percentile([0, 10], 50) == 5
        assert percentile([3.0], 95) == 3.0
        assert percentile([], 95) == 0.0

    def test_wick_stat_long_and_short_sides(self):
        candles = [(0, 100, 101, 99, 100), (MIN, 100, 103, 96, 101), (2 * MIN, 100, 100.5, 99.5, 100)]
        s = wick_stat(candles, "1m")
        assert s.candles == 3
        assert s.long_max_pct == pytest.approx(4.0)
        assert s.short_max_pct == pytest.approx(3.0)
        assert s.long_max_at == datetime.fromtimestamp(60, tz=timezone.utc)
        assert s.max_pct(True) == s.long_max_pct and s.max_pct(False) == s.short_max_pct
        assert s.window_days == pytest.approx(3 * MIN / 86_400_000)

    def test_aggregate_builds_aligned_5m_candles(self):
        ones = [(i * MIN, 100 + i, 101 + i, 99 - i, 100 + i) for i in range(7)]
        fives = aggregate(ones, 5)
        assert [c[0] for c in fives] == [0, 5 * MIN]
        t, o, h, low, c = fives[0]
        assert (o, h, low, c) == (100, 105, 95, 104)

    def test_wicks_away_and_stress_summary(self):
        assert wicks_away(10.0, 2.5) == 4.0
        assert wicks_away(10.0, 0.0) is None
        summary = stress_summary(pos(), 10.0, profile(one_max=2.0, five_max=12.0))
        assert summary["worst_interval"] == "5m"
        assert summary["max_wicks_away"] == pytest.approx(10 / 12, abs=0.01)
        assert summary["one_max_wick_liquidates"] is True
        assert stress_summary(pos(), 10.0, None) is None


# --------------------------------------------------------------------------- kline sources & cache


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class TestRateLimiter:
    async def test_waits_when_budget_is_spent(self):
        clock = FakeClock()
        limiter = RateLimiter(10, window=60, clock=clock, sleep=clock.sleep)
        for _ in range(5):
            await limiter.acquire(2)
        assert clock.t == 0 and limiter.used() == 10
        await limiter.acquire(2)
        assert clock.t >= 60 and limiter.waited >= 60

    def test_binance_weights(self):
        assert [binance_kline_weight(n) for n in (50, 100, 500, 1000, 1500)] == [1, 2, 5, 5, 10]


def kline_rows(start, n, step=MIN):
    return [[start + i * step, "100", "101", "99", "100", "1", 0, "0", 0, "0", "0", "0"] for i in range(n)]


@pytest.fixture
def patch_http(monkeypatch):
    """Route every httpx.AsyncClient created by the kline sources through a handler."""
    def install(handler):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr("liqguard.exchanges.klines.httpx.AsyncClient", factory)
    return install


class TestKlineSources:
    async def test_binance_paginates_and_spends_weight(self, patch_http):
        calls = []

        def handler(request):
            p = dict(request.url.params)
            calls.append(p)
            start, limit = int(p["startTime"]), int(p["limit"])
            end = int(p["endTime"])
            n = min(limit, (end - start) // MIN + 1)
            return httpx.Response(200, json=kline_rows(start, n))

        patch_http(handler)
        source = BinanceKlineSource(limiter=RateLimiter(1200))
        candles = await source.fetch("BTCUSDT", "1m", 0, 3000 * MIN)
        assert len(candles) == 3000
        assert [int(c["limit"]) for c in calls] == [1500, 1500]
        assert source.limiter.used() == 20
        assert candles[0] == (0, 100.0, 101.0, 99.0, 100.0)

    async def test_hyperliquid_candle_snapshot(self, patch_http):
        seen = []

        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            return httpx.Response(200, json=[{"t": 0, "o": "10", "h": "11", "l": "9", "c": "10"},
                                             {"t": 300_000, "o": "10", "h": "10.5", "l": "8", "c": "9"}])

        patch_http(handler)
        source = HyperliquidKlineSource(limiter=RateLimiter(600))
        candles = await source.fetch("ETH", "5m", 0, 600_000)
        assert seen[0]["type"] == "candleSnapshot" and seen[0]["req"]["coin"] == "ETH" and seen[0]["req"]["interval"] == "5m"
        assert candles[1] == (300_000, 10.0, 10.5, 8.0, 9.0)
        assert source.limiter.used() == 20


class FakeSource:
    venue = Venue.BINANCE
    native_intervals = ("1m",)

    def __init__(self):
        self.calls = []

    async def fetch(self, symbol, interval, start_ms, end_ms):
        self.calls.append((start_ms, end_ms))
        first = start_ms - start_ms % MIN
        return [(t, 100.0, 100.5, 99.0 if t % (7 * MIN) else 95.0, 100.0) for t in range(first, end_ms + 1, MIN)]


class FailingSource(FakeSource):
    async def fetch(self, symbol, interval, start_ms, end_ms):
        raise httpx.HTTPStatusError("geo", request=httpx.Request("GET", "http://x"), response=httpx.Response(451))


class TestWickService:
    async def test_full_then_incremental_refresh_with_5m_from_1m(self):
        clock = FakeClock()
        clock.t = 10 * 86_400.0
        source = FakeSource()
        svc = WickService(sources=[source], days=1, refresh_seconds=600, clock=clock)
        prof = await svc.refresh("binance", "BTCUSDT")
        assert set(prof.stats) == {"1m", "5m"}
        assert prof.stat("1m").long_max_pct == pytest.approx(5.0)
        full = svc.cached_candles("binance", "BTCUSDT", "1m")
        assert full == 1441

        clock.t += 300
        await svc.refresh("binance", "BTCUSDT")
        start, _ = source.calls[-1]
        assert start > source.calls[0][0]
        assert svc.cached_candles("binance", "BTCUSDT", "1m") == 1441

    async def test_errors_keep_last_profile_and_are_reported(self):
        svc = WickService(sources=[FailingSource()], clock=lambda: 1_000_000.0)
        assert await svc.refresh("binance", "BTCUSDT") is None
        assert svc.status()["binance"]["errors"] == {"BTCUSDT": "HTTP 451"}

    async def test_request_is_non_blocking_and_skips_unknown_venues(self):
        svc = WickService(sources=[FakeSource()], days=1, clock=lambda: 10 * 86_400.0)
        assert svc.request_keys([("binance", "BTCUSDT"), ("demo", "X")]) == {}
        await svc._tasks[("binance", "BTCUSDT")]
        assert ("binance", "BTCUSDT") in svc.request_keys([("binance", "BTCUSDT")])
        await svc.stop()


# --------------------------------------------------------------------------- sessions & adaptive tiers


class TestSessions:
    def test_weekend_and_thin_hours(self):
        s = market_session(SAT_2000, "BTCUSDT")
        assert s.weekend and not s.low_liquidity and s.factor == pytest.approx(1.15)
        thin = market_session(TUE_2300, "BTCUSDT")
        assert thin.low_liquidity and not thin.weekend and thin.factor == pytest.approx(1.10)
        assert market_session(TUE_1500, "BTCUSDT").factor == 1.0

    def test_stock_perps_widen_when_nyse_is_closed_and_cap_applies(self):
        assert is_stock_perp("TSLAUSDT") and is_stock_perp("xyz:NVDA") and not is_stock_perp("BTCUSDT")
        assert us_equity_market_open(TUE_1500)
        assert not us_equity_market_open(TUE_2300) and not us_equity_market_open(SAT_2000)
        weekend_stock = market_session(SAT_2000, "TSLAUSDT")
        assert weekend_stock.stock_market_closed and weekend_stock.factor == pytest.approx(1.5)
        assert market_session(TUE_1500, "TSLAUSDT").factor == 1.0

    def test_parse_hours(self):
        assert parse_hours("21-1") == (21, 1)
        assert parse_hours("22-26") == (22, 2)
        assert parse_hours("garbage") == (21, 1)


class TestAdaptive:
    def test_wick_floors_raise_tiers(self):
        a = adaptive_threshold(UserThreshold(), pos(), profile(one_max=6.0, five_max=12.0))
        tiers = a.tiers()
        assert tiers["emergency"] == pytest.approx(6.0)
        assert tiers["critical"] == pytest.approx(12.0)
        assert tiers["warning"] == pytest.approx(24.0)
        assert a.adapted and any("1m max wick" in r for r in a.reasons)

    def test_session_factor_and_caps(self):
        a = adaptive_threshold(UserThreshold(), pos(), None, market_session(SAT_2000, "BTCUSDT"))
        assert a.tiers() == pytest.approx({"warning": 23.0, "critical": 11.5, "emergency": 5.75})
        huge = adaptive_threshold(UserThreshold(), pos(), profile(one_max=40, five_max=50), market_session(SAT_2000, "TSLAUSDT"))
        assert huge.tiers() == {"warning": 60.0, "critical": 40.0, "emergency": 25.0}

    def test_switch_off_keeps_base(self):
        t = UserThreshold(adaptive_thresholds=False)
        a = adaptive_threshold(t, pos(), profile(one_max=9.0), market_session(SAT_2000, "BTCUSDT"))
        assert a.threshold is t and not a.adapted


# --------------------------------------------------------------------------- stop-loss sanity


class TestStopLoss:
    def test_no_stop_suggests_a_safe_stop(self):
        check = check_stop_loss(pos(), [], 90.0)
        assert check.status == "no_stop" and check.needs_warning
        move = check.fix("move_stop")
        assert 90.0 < move.stop_price < 100.0

    def test_stop_beyond_liquidation_is_critical_with_keep_stop_fixes(self):
        check = check_stop_loss(pos(), [stop(89.0)], 90.0)
        assert check.status == "beyond_liquidation" and check.severity == "critical"
        assert check.gap_pct < 0
        kinds = {f.kind for f in check.fixes}
        assert "move_stop" in kinds and "add_margin" in kinds
        add = check.fix("add_margin")
        assert add.new_liquidation_price < 89.0

    def test_too_close_uses_typical_5m_wick_as_required_gap(self):
        check = check_stop_loss(pos(), [stop(91.0)], 90.0, profile=profile(five_typ=1.5))
        assert check.required_gap_pct == pytest.approx(1.5)
        assert check.status == "too_close"
        assert check_stop_loss(pos(), [stop(95.0)], 90.0, profile=profile(five_typ=1.5)).status == "ok"

    def test_short_side_and_ignores_take_profit_and_other_symbols(self):
        short = pos(side=Side.SHORT, mark=100.0, liq=110.0)
        tp = OpenOrder(Venue.BINANCE, "BTCUSDT", Side.LONG, TAKE_PROFIT, trigger_price=90.0, close_position=True)
        other = stop(105.0, side=Side.LONG, symbol="ETHUSDT")
        assert check_stop_loss(short, [tp, other], 110.0).status == "no_stop"
        assert check_stop_loss(short, [stop(111.0, side=Side.LONG)], 110.0).status == "beyond_liquidation"
        assert check_stop_loss(short, [stop(105.0, side=Side.LONG)], 110.0).status == "ok"

    def test_partial_coverage(self):
        partial = OpenOrder(Venue.BINANCE, "BTCUSDT", Side.SHORT, STOP_MARKET, trigger_price=95.0, quantity=0.4, reduce_only=True)
        assert check_stop_loss(pos(), [partial], 90.0).status == "partial"

    def test_context_unknown_orders_vs_empty(self):
        p = pos()
        assert RiskContext().orders_for(p) is None
        assert RiskContext(orders={("binance", p.network): []}).orders_for(p) == []

    def test_muse_mock_fix_is_validated(self):
        check = check_stop_loss(pos(), [stop(89.0)], 90.0)
        expl = mock_stop_explanation(check)
        assert expl["suggested_fix"] in {f.kind for f in check.fixes}
        assert validate_stop_explanation(expl, check) is None
        assert validate_stop_explanation({**expl, "suggested_fix": "hedge"}, check)
        assert validate_stop_explanation({**expl, "explanation": "Add $123,456.00 now"}, check)
        zh = mock_stop_explanation(check, "zh-TW")
        assert validate_stop_explanation(zh, check) is None

    async def test_muse_client_explains_in_mock_mode(self):
        check = check_stop_loss(pos(), [], 90.0)
        out = await MetaLLMClient(use_mock=True).explain_stop_fix(check, "en")
        assert out["mock"] is True and out["suggested_fix"] == "move_stop"


# --------------------------------------------------------------------------- order parsers & adapters


class TestOrderParsers:
    def test_binance_regular_algo_and_trailing(self):
        reg = parse_binance_order({"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "stopPrice": "58000",
                                   "price": "0", "origQty": "0", "closePosition": True, "orderId": 7})
        assert reg.is_stop and reg.trigger_price == 58000 and reg.close_position and reg.side == Side.SHORT
        algo = parse_binance_order({"symbol": "BTCUSDT", "side": "SELL", "orderType": "STOP_MARKET", "triggerPrice": "57000",
                                    "quantity": "0.5", "reduceOnly": "true", "algoId": 9})
        assert algo.trigger_price == 57000 and algo.quantity == 0.5 and algo.reduce_only and algo.order_id == "9"
        trail = parse_binance_order({"symbol": "BTCUSDT", "side": "SELL", "type": "TRAILING_STOP_MARKET", "stopPrice": "1"})
        assert trail.order_type == TRAILING_STOP and trail.trigger_price is None
        assert parse_binance_order({"type": "LIMIT"}) is None

    def test_hyperliquid_trigger_and_position_tpsl(self):
        o = parse_hyperliquid_order({"coin": "ETH", "side": "B", "orderType": "Stop Market", "isTrigger": True,
                                     "triggerPx": "4100", "sz": "0", "isPositionTpsl": True, "oid": 1})
        assert o.side == Side.LONG and o.trigger_price == 4100 and o.close_position and o.is_stop
        assert o.base_symbol == "ETH"
        lim = parse_hyperliquid_order({"coin": "ETH", "side": "A", "orderType": "Limit", "limitPx": "3000", "sz": "1"})
        assert not lim.is_stop and lim.trigger_price is None


def binance_with(handler):
    adapter = BinanceAdapter("K" * 64, "S" * 64)
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=adapter._client.headers)
    return adapter


class TestAdapters:
    async def test_binance_open_orders_merge_algo_orders_read_only(self):
        methods = []

        def handler(request):
            methods.append((request.method, request.url.path))
            if request.url.path == "/fapi/v1/openOrders":
                return httpx.Response(200, json=[{"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "stopPrice": "58000", "closePosition": True}])
            return httpx.Response(200, json={"orders": [{"symbol": "ETHUSDT", "side": "BUY", "orderType": "STOP_MARKET", "triggerPrice": "4000", "quantity": "1"}]})

        adapter = binance_with(handler)
        orders = await adapter.get_open_orders()
        assert {o.symbol for o in orders} == {"BTCUSDT", "ETHUSDT"}
        assert all(m == "GET" for m, _ in methods)
        await adapter.close()

    async def test_binance_algo_endpoint_failure_keeps_regular_orders(self):
        def handler(request):
            if request.url.path == "/fapi/v1/openOrders":
                return httpx.Response(200, json=[{"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "stopPrice": "1"}])
            return httpx.Response(404, json={})

        adapter = binance_with(handler)
        assert len(await adapter.get_open_orders()) == 1
        await adapter.close()

    async def test_binance_adl_quantile_is_attached(self):
        def handler(request):
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "adlQuantile": {"LONG": 1, "SHORT": 2, "HEDGE": 0, "BOTH": 3}}])

        adapter = binance_with(handler)
        positions = await adapter._attach_adl([pos()])
        assert positions[0].adl_quantile == 3
        await adapter.close()

    async def test_hyperliquid_frontend_open_orders_by_address(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=[{"coin": "BTC", "side": "A", "orderType": "Stop Market", "isTrigger": True, "triggerPx": "58000", "sz": "0.1"}])

        adapter = HyperliquidAdapter("0x" + "a" * 40)
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        orders = await adapter.get_open_orders()
        assert seen == [{"type": "frontendOpenOrders", "user": "0x" + "a" * 40}]
        assert orders[0].trigger_price == 58000
        await adapter.client.aclose()


# --------------------------------------------------------------------------- ADL


class TestAdl:
    def test_lights_from_binance_quantile(self):
        assert [lights_from_quantile(q) for q in (0, 1, 2, 3, 4, None)] == [1, 2, 3, 4, 5, None]

    def test_warns_only_when_profitable_and_high(self):
        assert adl_status(pos(adl=3, pnl=50)).warn
        assert not adl_status(pos(adl=3, pnl=-5)).warn
        assert not adl_status(pos(adl=1, pnl=50)).warn
        assert adl_status(pos(adl=None)).lights is None

    def test_hyperliquid_is_unsupported(self):
        s = adl_status(pos(venue=Venue.HYPERLIQUID, symbol="ETH", adl=4, pnl=10))
        assert not s.supported and not s.warn and "Hyperliquid" in s.note


# --------------------------------------------------------------------------- escalation


class TestEscalationTracker:
    def test_reminders_are_spaced_capped_and_owner_scoped(self):
        tr = EscalationTracker()
        ack = tr.open(1, "binance", "BTCUSDT", "critical", "🚨", now=0, interval_seconds=120, max_reminders=3)
        assert tr.due(119) == [] and tr.due(120) == [ack]
        for n, t in enumerate((120, 240, 360), start=1):
            assert tr.mark_sent(ack, t) == n
        assert tr.due(10_000) == []
        assert tr.pending_for(1) == [ack] and tr.active_for(1) == []
        assert tr.acknowledge(2, ack.id, 400) is None
        assert tr.acknowledge(1, ack.id, 400) is ack and tr.pending_for(1) == []

    def test_worse_severity_restarts_and_old_id_still_acknowledges(self):
        tr = EscalationTracker()
        crit = tr.open(1, "binance", "BTCUSDT", "critical", "", 0, 120, 5)
        assert tr.open(1, "binance", "BTCUSDT", "critical", "", 10, 120, 5) is crit
        emerg = tr.open(1, "binance", "BTCUSDT", "emergency", "", 20, 120, 5)
        assert emerg is not crit and emerg.reminders_sent == 0
        assert tr.acknowledge(2, crit.id, 30) is None
        assert tr.acknowledge(1, crit.id, 30) is emerg

    def test_resolve_and_warning_never_escalates(self):
        tr = EscalationTracker()
        assert tr.open(1, "binance", "X", "warning", "", 0, 120, 5) is None
        tr.open(1, "binance", "X", "emergency", "", 0, 120, 5)
        tr.resolve(1, "binance", "X")
        assert tr.pending_for(1) == []

    def test_ack_silences_the_position_until_it_materially_worsens(self):
        tr = EscalationTracker()
        crit = tr.open(1, "binance", "BTCUSDT", "critical", "", 0, 120, 5, distance_pct=8.0)
        assert tr.acknowledge(1, crit.id, 10) is crit
        assert tr.is_silenced(1, "binance", "BTCUSDT")
        assert tr.open(1, "binance", "BTCUSDT", "critical", "", 20, 120, 5, distance_pct=6.0) is None
        assert tr.open(1, "binance", "BTCUSDT", "emergency", "", 30, 120, 5, distance_pct=4.5) is None
        assert tr.due(10_000) == [] and tr.pending_for(1) == []
        assert not tr.is_silenced(2, "binance", "BTCUSDT") and tr.open(2, "binance", "BTCUSDT", "critical", "", 0, 120, 5)
        worse = tr.open(1, "binance", "BTCUSDT", "emergency", "", 40, 120, 5, distance_pct=3.9)
        assert worse is not None and worse.id != crit.id and not tr.is_silenced(1, "binance", "BTCUSDT")

    def test_recovery_lifts_the_silence(self):
        tr = EscalationTracker()
        ack = tr.open(1, "binance", "X", "critical", "", 0, 120, 5, distance_pct=8.0)
        tr.acknowledge(1, ack.id, 1)
        tr.resolve(1, "binance", "X")
        assert tr.open(1, "binance", "X", "critical", "", 2, 120, 5, distance_pct=8.0) is not None

    def test_urgency_grows(self):
        assert [urgency(n, 5) for n in (1, 2, 4, 5)] == ["reminder", "urgent", "urgent", "final"]


class TestEscalationService:
    async def test_tick_resends_due_reminders_with_ack_button(self):
        clock = FakeClock()
        notifier = AsyncMock()
        svc = EscalationService(notifier, EscalationTracker(), clock=clock)
        ack = svc.open(1, "binance", "BTCUSDT", "emergency", "🆘 BTC", UserThreshold(escalation_interval_minutes=2), 3.0)
        assert await svc.tick() == 0
        clock.t = 121
        assert await svc.tick() == 1
        user_id, text = notifier.notify.call_args.args[:2]
        assert user_id == 1 and "1/5" in text
        actions = notifier.notify.call_args.kwargs.get("actions") or notifier.notify.call_args.args[2]
        assert any(cb == f"ack:{ack.id}" for _, cb in actions)
        assert svc.acknowledge(1, ack.id) is ack
        clock.t = 1000
        assert await svc.tick() == 0

    def test_disabled_in_settings(self):
        svc = EscalationService(AsyncMock(), EscalationTracker())
        assert svc.open(1, "binance", "X", "critical", "", UserThreshold(escalation_enabled=False)) is None


# --------------------------------------------------------------------------- monitor integration


class TestMonitor:
    async def _run(self, book, threshold=None, escalation=None):
        user = __import__("liqguard.models", fromlist=["User"]).User(telegram_id=1, threshold=threshold or UserThreshold())

        async def users():
            return [user]

        async def positions(_):
            return book

        notifier = AsyncMock()
        monitor = PositionMonitor(users, positions, notifier, AlertManager(), MetaLLMClient(use_mock=True), escalation=escalation)
        await monitor.check_all_users()
        return notifier, monitor

    async def test_stop_beyond_liq_and_adl_warnings(self):
        p_btc = pos(mark=100.0, liq=60.0, pnl=-5.0)
        p_tsla = pos(symbol="TSLAUSDT", mark=100.0, liq=50.0, pnl=40.0, adl=4)
        ctx = RiskContext(orders={("binance", p_btc.network): [stop(55.0)]})
        notifier, monitor = await self._run(BookSnapshot([p_btc, p_tsla], "live", ctx))
        texts = [c.args[1] for c in notifier.notify.call_args_list]
        assert any("BTCUSDT" in t and "55" in t for t in texts)
        assert any("ADL" in t and "TSLAUSDT" in t for t in texts)
        assert any("TSLAUSDT" in t and "stop" in t.lower() for t in texts)

        notifier.reset_mock()
        await monitor.check_all_users()
        assert notifier.notify.call_count == 0

    async def test_unknown_orders_do_not_warn(self):
        notifier, _ = await self._run(BookSnapshot([pos(mark=100.0, liq=60.0)], "live", RiskContext()))
        assert notifier.notify.call_count == 0

    async def test_stop_checks_can_be_disabled(self):
        p = pos(mark=100.0, liq=60.0)
        ctx = RiskContext(orders={("binance", p.network): []})
        notifier, _ = await self._run(BookSnapshot([p], "live", ctx), UserThreshold(stop_loss_checks=False))
        assert notifier.notify.call_count == 0

    async def test_critical_alert_opens_escalation_with_ack_first(self):
        tracker = EscalationTracker()
        svc = EscalationService(AsyncMock(), tracker)
        p = pos(mark=100.0, liq=93.0)
        ctx = RiskContext(orders={("binance", p.network): [stop(96.0)]})
        notifier, _ = await self._run(BookSnapshot([p], "live", ctx), escalation=svc)
        pending = tracker.pending_for(1)
        assert len(pending) == 1 and pending[0].severity in ("critical", "emergency")
        alert_call = next(c for c in notifier.notify.call_args_list if "ack:" in str(c))
        actions = alert_call.kwargs.get("actions") or alert_call.args[2]
        assert actions[0][1] == f"ack:{pending[0].id}"

    async def test_acknowledged_position_is_not_re_alerted_until_it_materially_worsens(self):
        tracker = EscalationTracker()
        svc = EscalationService(AsyncMock(), tracker)
        threshold = UserThreshold(adaptive_thresholds=False, stop_loss_checks=False)
        book = BookSnapshot([pos(mark=100.0, liq=92.0)], "live", RiskContext())
        notifier, monitor = await self._run(book, threshold, escalation=svc)
        (ack,) = tracker.pending_for(1)
        assert ack.severity == "critical" and svc.acknowledge(1, ack.id) is ack

        notifier.reset_mock()
        book.positions[:] = [pos(mark=100.0, liq=95.5)]
        await monitor.check_all_users()
        assert notifier.notify.call_count == 0 and tracker.pending_for(1) == []

        book.positions[:] = [pos(mark=100.0, liq=98.0)]
        await monitor.check_all_users()
        assert notifier.notify.call_count == 1
        (fresh,) = tracker.pending_for(1)
        assert fresh.severity == "emergency" and fresh.id != ack.id
