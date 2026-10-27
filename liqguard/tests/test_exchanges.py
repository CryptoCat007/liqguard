"""Exchange key policy: mainnet keys must be read-only; testnet may trade but never withdraw."""

import httpx
import pytest

from liqguard.exchanges import (
    BinanceAdapter,
    ReadOnlyViolation,
    adapter_for_binding,
    evaluate_binance_restrictions,
)
from liqguard.models import MAINNET, TESTNET, ExchangeBinding

KEY, SECRET = "K" * 64, "S" * 64
READ_ONLY = {"enableReading": True, "ipRestrict": True}


def with_transport(adapter: BinanceAdapter, handler) -> BinanceAdapter:
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=adapter._client.headers)
    return adapter


class TestRestrictionPolicy:
    def test_read_only_mainnet_key_is_accepted(self):
        check = evaluate_binance_restrictions(READ_ONLY, MAINNET)
        assert check.accepted and check.verified
        assert "READ" in check.permissions and "IP_RESTRICTED" in check.permissions

    @pytest.mark.parametrize("flag", ["enableFutures", "enableSpotAndMarginTrading", "enableMargin", "enableVanillaOptions"])
    def test_trading_mainnet_key_is_rejected(self, flag):
        check = evaluate_binance_restrictions({**READ_ONLY, flag: True}, MAINNET)
        assert not check.accepted
        assert "read-only" in check.message

    @pytest.mark.parametrize("network", [MAINNET, TESTNET])
    @pytest.mark.parametrize("flag", ["enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer"])
    def test_withdraw_or_transfer_is_always_rejected(self, flag, network):
        assert not evaluate_binance_restrictions({**READ_ONLY, flag: True}, network).accepted

    def test_testnet_key_may_trade(self):
        assert evaluate_binance_restrictions({**READ_ONLY, "enableFutures": True}, TESTNET).accepted

    def test_key_without_reading_is_rejected(self):
        assert not evaluate_binance_restrictions({"enableReading": False}, MAINNET).accepted

    def test_unrestricted_ip_gets_a_tip(self):
        assert "restrict the key" in evaluate_binance_restrictions({"enableReading": True}, MAINNET).message


class TestBinanceAdapter:
    async def test_mainnet_adapter_refuses_writes(self):
        adapter = with_transport(BinanceAdapter(KEY, SECRET, MAINNET), lambda r: httpx.Response(200, json={}))
        with pytest.raises(ReadOnlyViolation):
            await adapter.add_isolated_margin("BTCUSDT", 10)
        with pytest.raises(ReadOnlyViolation):
            await adapter._request("DELETE", "/fapi/v1/order")
        await adapter.close()

    async def test_check_key_uses_key_restrictions(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append(request.url.path)
            return httpx.Response(200, json={**READ_ONLY, "enableFutures": True})

        adapter = with_transport(BinanceAdapter(KEY, SECRET, MAINNET), handler)
        check = await adapter.check_key()
        await adapter.close()
        assert seen == ["/sapi/v1/account/apiRestrictions"]
        assert not check.accepted

    async def test_unverifiable_mainnet_key_is_rejected(self):
        adapter = with_transport(BinanceAdapter(KEY, SECRET, MAINNET), lambda r: httpx.Response(500, json={}))
        check = await adapter.check_key()
        await adapter.close()
        assert not check.accepted

    async def test_invalid_key_message_does_not_echo_secrets(self):
        adapter = with_transport(
            BinanceAdapter(KEY, SECRET, MAINNET), lambda r: httpx.Response(401, json={"code": -2015, "msg": "bad"})
        )
        check = await adapter.check_key()
        await adapter.close()
        assert not check.accepted
        assert KEY not in check.message and SECRET not in check.message

    async def test_testnet_key_with_withdraw_is_rejected(self):
        adapter = with_transport(
            BinanceAdapter(KEY, SECRET, TESTNET), lambda r: httpx.Response(200, json={"canTrade": True, "canWithdraw": True})
        )
        check = await adapter.check_key()
        await adapter.close()
        assert not check.accepted

    async def test_testnet_trading_key_is_accepted(self):
        adapter = with_transport(
            BinanceAdapter(KEY, SECRET, TESTNET), lambda r: httpx.Response(200, json={"canTrade": True, "canWithdraw": False})
        )
        check = await adapter.check_key()
        await adapter.close()
        assert check.accepted and "TESTNET_TRADE" in check.permissions

    def test_repr_hides_credentials(self):
        adapter = BinanceAdapter(KEY, SECRET, MAINNET)
        assert KEY not in repr(adapter) and SECRET not in repr(adapter)

    def test_binding_repr_hides_credentials(self):
        binding = ExchangeBinding(venue="binance", binding_type="api_key", api_key=KEY, api_secret=SECRET)
        assert KEY not in repr(binding) and SECRET not in repr(binding)
        assert KEY not in str(binding.to_dict()) and SECRET not in str(binding.to_dict())

    def test_factory_respects_network(self):
        binding = ExchangeBinding(venue="binance", binding_type="api_key", api_key=KEY, api_secret=SECRET, network=TESTNET)
        assert adapter_for_binding(binding).network == TESTNET
