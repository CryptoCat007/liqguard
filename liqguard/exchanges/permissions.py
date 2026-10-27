"""
Binance API-key permission policy (pure, no I/O).

Input is the JSON from `GET /sapi/v1/account/apiRestrictions`, which reports
what *this key* may do (unlike `/fapi/v2/account` `canTrade`, which describes
the account).

Policy:
- mainnet: the key must be read-only. Any withdraw, transfer or trading
  permission is rejected.
- testnet: trading is allowed (tier-1 protective actions run there) but
  withdraw/transfer permissions are still rejected.
"""

from __future__ import annotations

from liqguard.models import MAINNET, TESTNET

from .base import KeyCheck

WITHDRAW_FLAGS = {
    "enableWithdrawals": "WITHDRAW",
    "enableInternalTransfer": "INTERNAL_TRANSFER",
    "permitsUniversalTransfer": "UNIVERSAL_TRANSFER",
}

TRADE_FLAGS = {
    "enableFutures": "FUTURES_TRADE",
    "enableSpotAndMarginTrading": "SPOT_MARGIN_TRADE",
    "enableMargin": "MARGIN",
    "enableVanillaOptions": "OPTIONS_TRADE",
    "enablePortfolioMarginTrading": "PORTFOLIO_MARGIN_TRADE",
    "enableFixApiTrade": "FIX_API_TRADE",
}

REBIND_HELP = (
    "Create a new key with only 'Enable Reading' ticked "
    "(no trading, no withdrawals, no transfers), ideally IP-restricted."
)


def detected_permissions(restrictions: dict) -> list[str]:
    perms = []
    if restrictions.get("enableReading"):
        perms.append("READ")
    for flag, name in {**WITHDRAW_FLAGS, **TRADE_FLAGS}.items():
        if restrictions.get(flag):
            perms.append(name)
    if restrictions.get("ipRestrict"):
        perms.append("IP_RESTRICTED")
    return perms


def evaluate_binance_restrictions(restrictions: dict, network: str = MAINNET) -> KeyCheck:
    """Apply LiqGuard's key policy to an apiRestrictions response."""
    perms = detected_permissions(restrictions)

    withdraw = [name for flag, name in WITHDRAW_FLAGS.items() if restrictions.get(flag)]
    if withdraw:
        return KeyCheck(False, f"Rejected: key has {', '.join(withdraw)} permission. {REBIND_HELP}", perms, True)

    if not restrictions.get("enableReading"):
        return KeyCheck(False, "Rejected: key cannot read account data ('Enable Reading' is off).", perms, True)

    trade = [name for flag, name in TRADE_FLAGS.items() if restrictions.get(flag)]
    if trade and network != TESTNET:
        return KeyCheck(
            False,
            f"Rejected: key has {', '.join(trade)} permission. LiqGuard only accepts read-only "
            f"mainnet keys. {REBIND_HELP}",
            perms,
            True,
        )

    note = "" if restrictions.get("ipRestrict") else " Tip: restrict the key to trusted IPs."
    if network == TESTNET:
        return KeyCheck(True, "Testnet key accepted (trading allowed for tier-1 protective actions)." + note, perms, True)
    return KeyCheck(True, "Read-only key verified." + note, perms, True)
