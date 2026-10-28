"""Funding: live feeds (mocked HTTP), settlement math, liquidation drift, pre-funding warnings, the refresh service."""

import asyncio
import functools
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from liqguard.core import MarketDataService
from liqguard.exchanges import BinanceAdapter, HyperliquidAdapter
from liqguard.exchanges.funding import BinanceFundingFeed, HyperliquidFundingFeed
from liqguard.models import (
    CROSS,
    ISOLATED,
    FundingInfo,
    MaintenanceBracket,
    MarginAccount,
    Position,
    Side,
    UserThreshold,
    Venue,
)
from liqguard.risk import (
    PROJECTION_LABEL,
    RiskContext,
    funding_payment,
    next_settlement,
    pre_funding_warning,
    project_funding,
    settlements_within,
    true_liquidation,
)

NOW = datetime(2026, 10, 27, 7, 40, tzinfo=timezone.utc)
FLAT_04 = (MaintenanceBracket(0, float("inf"), 0.004, 0),)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.AsyncClient created by the feeds through `handler`."""
    calls: list[httpx.Request] = []

    def install(handler):
        def recording(request):
            calls.append(request)
            return handler(request)

        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", functools.partial(real, transport=httpx.MockTransport(recording)))
        return calls

    return install


# --------------------------------------------------------------------------- feeds


class TestBinanceFeed:
    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json=[
                {"symbol": "BTCUSDT", "markPrice": "64000.5", "lastFundingRate": "0.00010000",
                 "nextFundingTime": ms(datetime(2026, 10, 27, 8, tzinfo=timezone.utc))},
                {"symbol": "TIAUSDT", "markPrice": "5.12", "lastFundingRate": "-0.00042000",
                 "nextFundingTime": ms(datetime(2026, 10, 27, 8, tzinfo=timezone.utc))},
                {"symbol": "DELISTEDUSDT", "markPrice": "1", "lastFundingRate": "", "nextFundingTime": 0},
            ])
        if request.url.path == "/fapi/v1/fundingInfo":
            return httpx.Response(200, json=[{"symbol": "TIAUSDT", "fundingIntervalHours": 4, "adjustedFundingRateCap": "0.02"}])
        return httpx.Response(404)

    async def test_parses_rate_next_time_and_per_symbol_interval(self, mock_http):
        calls = mock_http(self.handler)
        rows = await BinanceFundingFeed().fetch()
        assert set(rows) == {"BTCUSDT", "TIAUSDT"}
        btc, tia = rows["BTCUSDT"], rows["TIAUSDT"]
        assert btc.rate == pytest.approx(0.0001) and btc.interval_hours == 8 and btc.mark_price == pytest.approx(64000.5)
        assert tia.rate == pytest.approx(-0.00042) and tia.interval_hours == 4
        assert btc.next_funding_time == datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
        assert btc.source == "live" and btc.venue == Venue.BINANCE
        assert {c.url.path for c in calls} == {"/fapi/v1/premiumIndex", "/fapi/v1/fundingInfo"}
        assert all(c.method == "GET" and "X-MBX-APIKEY" not in c.headers for c in calls)

    async def test_missing_funding_info_defaults_to_8h(self, mock_http):
        def handler(request):
            if request.url.path == "/fapi/v1/fundingInfo":
                return httpx.Response(500)
            return self.handler(request)

        mock_http(handler)
        rows = await BinanceFundingFeed().fetch()
        assert rows["TIAUSDT"].interval_hours == 8

    async def test_premium_index_failure_raises(self, mock_http):
        mock_http(lambda r: httpx.Response(451, json={"msg": "restricted location"}))
        with pytest.raises(httpx.HTTPStatusError):
            await BinanceFundingFeed().fetch()


class TestHyperliquidFeed:
    async def test_hourly_funding_and_max_leverage(self, mock_http):
        def handler(request):
            assert json.loads(request.content) == {"type": "metaAndAssetCtxs"}
            return httpx.Response(200, json=[
                {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}, {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]},
                [{"funding": "0.0000125", "markPx": "64010.0", "openInterest": "1"},
                 {"funding": "-0.00003", "markPx": "3301.5", "openInterest": "1"}],
            ])

        mock_http(handler)
        feed = HyperliquidFundingFeed()
        before = datetime.now(timezone.utc)
        rows = await feed.fetch()
        eth = rows["ETH"]
        assert eth.rate == pytest.approx(-0.00003) and eth.interval_hours == 1 and eth.mark_price == pytest.approx(3301.5)
        assert eth.next_funding_time.minute == 0 and eth.next_funding_time.second == 0
        assert timedelta(0) < eth.next_funding_time - before <= timedelta(hours=1)
        assert feed.max_leverage == {"BTC": 40, "ETH": 25}


# --------------------------------------------------------------------------- settlement math


def info(rate=0.0001, interval=8.0, next_at=datetime(2026, 10, 27, 8, tzinfo=timezone.utc), symbol="BTCUSDT", venue=Venue.BINANCE):
    return FundingInfo(venue, symbol, rate, interval, next_at)


def position(side=Side.LONG, size=1.0, entry=50000.0, mark=50000.0, symbol="BTCUSDT", mode=ISOLATED, wallet=None, venue=Venue.BINANCE):
    sign = 1 if side == Side.LONG else -1
    return Position(venue, symbol, side, size, entry, mark, 0.0, 5000, 10, sign * size * (mark - entry),
                    margin_mode=mode, isolated_wallet=wallet)


class TestSettlements:
    def test_next_settlement_rolls_stale_time_forward(self):
        assert next_settlement(info(next_at=NOW - timedelta(hours=9)), NOW) == NOW - timedelta(hours=1) + timedelta(hours=8)

    @pytest.mark.parametrize("interval,hours,expected", [(8, 8, 1), (8, 24, 3), (4, 24, 6), (1, 8, 8), (4, 0.25, 0)])
    def test_settlements_within(self, interval, hours, expected):
        assert settlements_within(info(interval=interval), NOW, hours) == expected

    def test_payment_sign(self):
        assert funding_payment(position(Side.LONG, mark=60000), 0.0001) == pytest.approx(6.0)
        assert funding_payment(position(Side.SHORT, mark=60000), 0.0001) == pytest.approx(-6.0)
        assert funding_payment(position(Side.SHORT, mark=60000), -0.0001) == pytest.approx(6.0)


class TestProjection:
    def test_paying_long_drifts_toward_liquidation(self):
        p = position(mark=48000, wallet=5000)
        proj = project_funding(p, None, lambda _: info(rate=0.001), NOW)
        d8, d24 = proj.drifts
        assert (d8.settlements, d24.settlements) == (1, 3)
        assert d24.pool_payment_usd == pytest.approx(3 * 48)
        assert proj.current.distance_pct > d8.distance_pct > d24.distance_pct
        # wallet shrinks by the payments: LP = (WB - paid - Q*E) / (Q*MMR - Q)
        assert d24.liquidation_price == pytest.approx((5000 - 144 - 50000) / (0.004 - 1))
        assert proj.to_dict()["label"] == PROJECTION_LABEL == "at current rate"

    def test_receiving_short_drifts_away(self):
        p = position(Side.SHORT, mark=52000, wallet=5000)
        proj = project_funding(p, None, lambda _: info(rate=0.0005), NOW)
        assert proj.next_payment_usd < 0
        assert proj.drifts[-1].distance_pct > proj.current.distance_pct

    def test_cross_pool_includes_other_positions_on_their_own_schedule(self):
        btc = position(size=0.8, entry=64200, mark=60000, mode=CROSS)
        sol = position(size=220, entry=148, mark=140, symbol="SOLUSDT", mode=CROSS)
        acct = MarginAccount(Venue.BINANCE, 16000, [btc, sol], {"BTCUSDT": FLAT_04, "SOLUSDT": FLAT_04})
        table = {"BTCUSDT": info(rate=0.0003), "SOLUSDT": info(rate=0.0002, interval=4, symbol="SOLUSDT")}
        proj = project_funding(btc, acct, lambda p: table[p.symbol], NOW)
        d24 = proj.drifts[1]
        assert d24.position_payment_usd == pytest.approx(3 * 0.8 * 60000 * 0.0003)
        assert d24.pool_payment_usd == pytest.approx(3 * 0.8 * 60000 * 0.0003 + 6 * 220 * 140 * 0.0002)
        assert d24.liquidation_price == pytest.approx(true_liquidation(btc, acct, wallet_delta=-d24.pool_payment_usd).price)

    def test_no_funding_no_projection(self):
        assert project_funding(position(), None, lambda _: None, NOW) is None


class TestPreFundingWarning:
    threshold = UserThreshold()

    def near_liq_long(self):
        # wallet 5000 on 1 BTC @ 50k → liq ≈ 45181; mark 47000 → ~3.9% away (emergency tier)
        return position(mark=47000, wallet=5000)

    def test_warns_before_large_payment(self):
        nxt = NOW + timedelta(minutes=20)
        w = pre_funding_warning(self.near_liq_long(), None, lambda _: info(rate=0.003, next_at=nxt), NOW, self.threshold)
        assert w is not None
        assert w.minutes_to_settlement == pytest.approx(20)
        assert w.payment_usd == pytest.approx(47000 * 0.003)
        assert w.distance_after_pct < w.distance_before_pct
        assert w.buffer_share_pct >= 5

    def test_quiet_outside_lead_time(self):
        nxt = NOW + timedelta(minutes=45)
        assert pre_funding_warning(self.near_liq_long(), None, lambda _: info(rate=0.003, next_at=nxt), NOW, self.threshold) is None

    def test_quiet_when_receiving(self):
        nxt = NOW + timedelta(minutes=10)
        assert pre_funding_warning(self.near_liq_long(), None, lambda _: info(rate=-0.003, next_at=nxt), NOW, self.threshold) is None

    def test_quiet_when_far_from_liquidation(self):
        far = position(mark=60000, wallet=5000)
        nxt = NOW + timedelta(minutes=10)
        assert pre_funding_warning(far, None, lambda _: info(rate=0.003, next_at=nxt), NOW, self.threshold) is None

    def test_quiet_when_payment_is_small(self):
        nxt = NOW + timedelta(minutes=10)
        assert pre_funding_warning(self.near_liq_long(), None, lambda _: info(rate=0.00001, next_at=nxt), NOW, self.threshold) is None


# --------------------------------------------------------------------------- refresh service


class FakeFeed:
    def __init__(self, venue, rows=None, error=None):
        self.venue, self.rows, self.error, self.calls = venue, rows or {}, error, 0

    async def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return dict(self.rows)


class TestMarketDataService:
    async def test_refresh_populates_lookup_by_base_symbol(self):
        hl = FakeFeed(Venue.HYPERLIQUID, {"ETH": info(rate=-0.00003, interval=1, symbol="ETH", venue=Venue.HYPERLIQUID)})
        bn = FakeFeed(Venue.BINANCE, {"BTCUSDT": info()})
        service = MarketDataService([bn, hl], refresh_seconds=60)
        await service.refresh()
        eth = position(Side.SHORT, symbol="ETH-PERP", venue=Venue.HYPERLIQUID)
        assert service.funding_for(eth).rate == pytest.approx(-0.00003)
        assert service.funding_for(position()).interval_hours == 8
        assert not service.is_stale("binance") and service.status()["binance"]["symbols"] == 1

    async def test_failed_refresh_keeps_last_good_data(self):
        bn = FakeFeed(Venue.BINANCE, {"BTCUSDT": info(rate=0.0002)})
        service = MarketDataService([bn], refresh_seconds=60)
        await service.refresh()
        bn.error = httpx.HTTPStatusError("451", request=httpx.Request("GET", "https://x"), response=httpx.Response(451))
        await service.refresh()
        assert service.funding_for(position()).rate == pytest.approx(0.0002)
        assert service.status()["binance"]["error"] == "HTTP 451"

    async def test_new_data_replaces_old_symbols(self):
        bn = FakeFeed(Venue.BINANCE, {"BTCUSDT": info(), "OLDUSDT": info(symbol="OLDUSDT")})
        service = MarketDataService([bn])
        await service.refresh()
        bn.rows = {"BTCUSDT": info(rate=0.0004)}
        await service.refresh()
        assert set(service.funding) == {("binance", "BTCUSDT")}

    async def test_ensure_fresh_is_throttled(self):
        bn = FakeFeed(Venue.BINANCE, error=RuntimeError("down"))
        service = MarketDataService([bn])
        await service.ensure_fresh()
        await service.ensure_fresh()
        assert bn.calls == 1

    async def test_background_loop_starts_and_stops(self):
        bn = FakeFeed(Venue.BINANCE, {"BTCUSDT": info()})
        service = MarketDataService([bn], refresh_seconds=3600)
        service.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await service.stop()
        assert bn.calls == 1

    async def test_snapshot_uses_live_funding_for_demo_book(self, ctx):
        from liqguard.models import User

        ctx.market._funding[("binance", "BTCUSDT")] = info(rate=0.0003)
        user = User(telegram_id=4242, is_demo_mode=True)
        snapshot = await ctx.positions.snapshot_for(user)
        btc = next(p for p in snapshot.positions if p.symbol == "BTCUSDT")
        assert snapshot.context.funding_for(btc).rate == pytest.approx(0.0003)


# --------------------------------------------------------------------------- adapter accounts


class TestAdapterAccounts:
    async def test_binance_account_reads_cross_wallet_and_brackets(self):
        def handler(request):
            path = request.url.path
            assert request.method == "GET"
            if path == "/fapi/v2/positionRisk":
                return httpx.Response(200, json=[
                    {"symbol": "BTCUSDT", "positionAmt": "0.8", "entryPrice": "64200", "markPrice": "60000",
                     "liquidationPrice": "47500", "leverage": "5", "unRealizedProfit": "-3360", "marginType": "cross",
                     "isolatedMargin": "0", "isolatedWallet": "0"},
                    {"symbol": "ETHUSDT", "positionAmt": "-2", "entryPrice": "3000", "markPrice": "3100",
                     "liquidationPrice": "3300", "leverage": "10", "unRealizedProfit": "-200", "marginType": "isolated",
                     "isolatedMargin": "400", "isolatedWallet": "600", "maintMargin": "31"},
                ])
            if path == "/fapi/v2/account":
                return httpx.Response(200, json={"totalCrossWalletBalance": "1", "assets": [
                    {"asset": "USDT", "crossWalletBalance": "16000"}, {"asset": "BNB", "crossWalletBalance": "3"}]})
            if path == "/fapi/v1/leverageBracket":
                return httpx.Response(200, json=[
                    {"symbol": "BTCUSDT", "brackets": [
                        {"bracket": 2, "notionalFloor": 50000, "notionalCap": 250000, "maintMarginRatio": 0.005, "cum": 50},
                        {"bracket": 1, "notionalFloor": 0, "notionalCap": 50000, "maintMarginRatio": 0.004, "cum": 0}]},
                    {"symbol": "DOGEUSDT", "brackets": []},
                ])
            return httpx.Response(404)

        adapter = BinanceAdapter("K" * 64, "S" * 64)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=adapter._client.headers)
        account = await adapter.get_account()
        await adapter.close()
        assert account.wallet_balance == pytest.approx(16000)
        btc, eth = account.positions
        assert btc.margin_mode == CROSS and btc.isolated_wallet is None
        assert eth.margin_mode == ISOLATED and eth.isolated_wallet == pytest.approx(600)
        assert [b.mmr for b in account.schedules["BTCUSDT"]] == [0.004, 0.005]
        assert "DOGEUSDT" not in account.schedules

    async def test_hyperliquid_account_value_minus_cross_upnl(self):
        wallet = "0x" + "ab" * 20

        def handler(request):
            body = json.loads(request.content)
            if body["type"] == "clearinghouseState":
                return httpx.Response(200, json={
                    "crossMarginSummary": {"accountValue": "10800"},
                    "assetPositions": [
                        {"position": {"coin": "ETH", "szi": "-12", "entryPx": "3150", "liquidationPx": "3990",
                                      "marginUsed": "9450", "unrealizedPnl": "-500", "leverage": {"type": "cross", "value": 4}}},
                        {"position": {"coin": "SOL", "szi": "10", "entryPx": "150", "liquidationPx": "120",
                                      "marginUsed": "290", "unrealizedPnl": "-10", "leverage": {"type": "isolated", "value": 5}}},
                    ],
                })
            if body["type"] == "allMids":
                return httpx.Response(200, json={"ETH": "3191.67", "SOL": "149"})
            if body["type"] == "meta":
                return httpx.Response(200, json={"universe": [{"name": "ETH", "maxLeverage": 25}, {"name": "SOL", "maxLeverage": 20}]})
            return httpx.Response(404)

        adapter = HyperliquidAdapter(wallet)
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        account = await adapter.get_account()
        await adapter.close()
        eth, sol = account.positions
        assert account.wallet_balance == pytest.approx(10800 + 500)
        assert eth.margin_mode == CROSS and sol.margin_mode == ISOLATED
        assert sol.isolated_wallet == pytest.approx(300)
        assert account.schedules["ETH"][0].mmr == pytest.approx(0.02)
        ctx = RiskContext(accounts=[account])
        assert ctx.account_for(eth) is account and ctx.computable(eth)
