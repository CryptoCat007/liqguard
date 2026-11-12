"""
The one gateway from UI feature handlers to the domain. Every lookup is keyed
by the caller's own user id; a position is found only inside that user's own
snapshot (their simulation, demo book or exchange bindings).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from liqguard.alerts import DEMO_VENUE
from liqguard.core import AppContext, BookSnapshot
from liqguard.core.binding import BindingService
from liqguard.core.positions import LABEL_SIMULATION
from liqguard.i18n import normalize
from liqguard.models import Position, User, UserThreshold
from liqguard.risk import (
    PortfolioInsights,
    RescuePlan,
    RescueSimulator,
    RiskEngine,
    portfolio_insights,
    position_protection,
)
from liqguard.simulation import MarketSimulation

from . import keyboards as kb
from .screen import Screen


@dataclass
class Found:
    user: User
    snapshot: BookSnapshot
    position: Position
    source: str


MAX_PENDING_LANGUAGES = 10_000


class UIServices:
    def __init__(self, ctx: AppContext, binding: Optional[BindingService] = None):
        self.ctx = ctx
        self.binding = binding or BindingService(
            ctx.storage, default_threshold=ctx.settings.default_threshold, entitlements=ctx.entitlements,
        )
        self._first_language: dict[int, str] = {}

    async def user(self, user_id: int) -> User:
        user = await self.ctx.storage.get_user(user_id)
        if user is None:
            user = User(telegram_id=user_id, threshold=self.ctx.settings.default_threshold(),
                        language=self._first_language.pop(user_id, normalize(None)))
            await self.ctx.storage.save_user(user)
        return user

    async def language(self, user_id: int, telegram_code: Optional[str] = None) -> str:
        """
        The user's language: their explicit choice (🌐 / /lang) if they made one,
        else their Telegram app language (zh* → zh-Hant, anything else → English),
        else English. Never creates a user (a stranger's rejected tap stays a no-op).
        """
        user = await self.ctx.storage.get_user(user_id)
        if user is None:
            lang = normalize(telegram_code)
            if telegram_code:
                if len(self._first_language) >= MAX_PENDING_LANGUAGES:
                    self._first_language.clear()
                self._first_language[user_id] = lang
            return lang
        if not user.language_explicit and telegram_code and normalize(telegram_code) != user.language:
            user.language = normalize(telegram_code)
            await self.ctx.storage.set_language(user_id, user.language, explicit=False)
            self._sync_simulation(user_id, user.language)
        return normalize(user.language)

    async def set_language(self, user_id: int, lang: str) -> str:
        user = await self.user(user_id)
        user.language, user.language_explicit = normalize(lang), True
        await self.ctx.storage.set_language(user_id, user.language, explicit=True)
        self._sync_simulation(user_id, user.language)
        return user.language

    def _sync_simulation(self, user_id: int, lang: str) -> None:
        sim = self.ctx.simulations.get(user_id)
        if sim is not None:
            sim.set_language(lang)

    async def snapshot(self, user: User) -> BookSnapshot:
        return await self.ctx.positions.snapshot_for(user)

    @staticmethod
    def source_of(snapshot: BookSnapshot, position: Position) -> str:
        return "sim" if snapshot.label == LABEL_SIMULATION else position.venue.value

    async def find(self, user_id: int, source: str, symbol: str) -> Optional[Found]:
        user = await self.user(user_id)
        snapshot = await self.snapshot(user)
        for p in snapshot.positions:
            if p.symbol == symbol and self.source_of(snapshot, p) == source:
                return Found(user, snapshot, p, source)
        return None

    @staticmethod
    def assessed(user: User, snapshot: BookSnapshot) -> list[tuple]:
        """(position, assessment, protection dict) riskiest first, assessed against adaptive tiers."""
        out = []
        for p in snapshot.positions:
            protection = position_protection(p, snapshot.context, user.threshold)
            assessment = RiskEngine(protection.adaptive.threshold, snapshot.context).assess_position(p)
            out.append((p, assessment, protection.to_dict()))
        return sorted(out, key=lambda x: x[1].distance_to_liquidation_pct)

    @staticmethod
    def protection(found: Found) -> dict:
        return position_protection(found.position, found.snapshot.context, found.user.threshold).to_dict()

    @staticmethod
    def protection_check(found: Found):
        return position_protection(found.position, found.snapshot.context, found.user.threshold)

    @property
    def muse(self):
        return self.ctx.muse

    @property
    def actions(self):
        """The protective-actions manager (proposals execute on testnet only)."""
        return self.ctx.actions

    @staticmethod
    def insights(user: User, snapshot: BookSnapshot) -> PortfolioInsights:
        return portfolio_insights(snapshot.positions, snapshot.context, user.threshold)

    def rescue_plan(self, found: Found) -> Optional[RescuePlan]:
        if found.source == "sim":
            sim = self.ctx.simulations.get(found.user.telegram_id)
            return sim.build_rescue_plan(found.position.symbol) if sim is not None else None
        c = found.snapshot.context
        return RescueSimulator(c.fees, found.user.threshold.safety_target_pct).simulate(
            found.position, c.account_for(found.position), c.funding_for(found.position), c.now)

    def warm_top(self, user: User, snapshot: BookSnapshot, insights: PortfolioInsights, lang: str,
                 n: int = 2) -> None:
        """Start Muse on the rescue and stop-loss texts of triage's top picks, the buttons the overview leads to."""
        plans, checks = [], []
        for item in insights.at_risk[:n]:
            pos = next((p for p in snapshot.positions
                        if p.symbol == item.symbol and p.venue.value == item.venue), None)
            if pos is None:
                continue
            plan = self.rescue_plan(Found(user, snapshot, pos, self.source_of(snapshot, pos)))
            if plan is not None:
                plans.append(plan)
            checks.append(position_protection(pos, snapshot.context, user.threshold).stop)
        self.muse.warm(lang, tuple(plans), tuple(checks))

    def simulation(self, user_id: int) -> Optional[MarketSimulation]:
        return self.ctx.simulations.get(user_id)

    def simulation_for(self, user: User) -> MarketSimulation:
        sim = self.ctx.simulations.get_or_create(user.telegram_id)
        if not sim.is_running:
            sim.configure(user.threshold, user.language)
        return sim

    async def save_threshold(self, user: User, threshold: UserThreshold) -> None:
        user.threshold = threshold
        await self.ctx.storage.update_threshold(user.telegram_id, threshold)
        sim = self.ctx.simulations.get(user.telegram_id)
        if sim is not None and not sim.is_running:
            sim.configure(threshold, user.language)

    async def set_demo_mode(self, user_id: int, enabled: bool, scenario: Optional[str] = None) -> None:
        await self.ctx.storage.set_demo_mode(user_id, enabled)
        if enabled and scenario:
            self.ctx.positions.demo_adapter(user_id, scenario)
            self.ctx.demo_alerts.open(user_id)
        if not enabled:
            self.ctx.positions.drop_demo(user_id)
            await self.ctx.mute_demo(user_id, source="telegram")

    async def acknowledge(self, user_id: int, ack_id: str) -> Optional[tuple[str, bool]]:
        """(symbol, is_demo) of the caller's own acknowledged alert (live monitor first, then their simulation).
        A live position stays silenced until it materially worsens; any demo alert mutes the whole demo."""
        ack = self.ctx.escalations.acknowledge(user_id, ack_id, time.time())
        if ack is not None:
            if ack.venue == DEMO_VENUE:
                await self.ctx.mute_demo(user_id, source="telegram")
                return ack.symbol, True
            return ack.symbol, False
        sim = self.ctx.simulations.get(user_id)
        event = await sim.acknowledge(ack_id, source="telegram") if sim is not None else None
        if event is None:
            return None
        await self.ctx.mute_demo(user_id, source="telegram")
        return event["symbol"], True

    async def snooze(self, user_id: int, ack_id: str, minutes: float = 30) -> Optional[tuple[str, bool]]:
        """(symbol, is_demo). A live alert is held for `minutes`; any demo alert mutes the whole demo."""
        ack = self.ctx.escalations.snooze(user_id, ack_id, time.time() + minutes * 60)
        if ack is not None:
            if ack.venue == DEMO_VENUE:
                await self.ctx.mute_demo(user_id, source="telegram")
                return ack.symbol, True
            return ack.symbol, False
        sim = self.ctx.simulations.get(user_id)
        event = await sim.snooze(ack_id, minutes, source="telegram") if sim is not None else None
        if event is None:
            return None
        await self.ctx.mute_demo(user_id, source="telegram")
        return event["symbol"], True

    async def stop_demo(self, user_id: int) -> bool:
        """End the caller's demo — simulation, demo-book alerts and every demo reminder — now; True if any was active."""
        return await self.ctx.mute_demo(user_id, source="telegram", stop=True)

    async def dashboard_link(self, user_id: int, lang: Optional[str] = None) -> Optional[tuple[str, int]]:
        """(one-time URL, minutes valid), or None when the dashboard is not enabled."""
        if not self.ctx.settings.dashboard_login_available:
            return None
        token = await self.ctx.login_links.create(user_id)
        return self.ctx.dashboard_login_url(token, lang), max(1, self.ctx.settings.login_link_ttl // 60)

    def paypal_enabled(self) -> bool:
        return bool(self.ctx.settings.paypal_enabled)

    def paypal_client(self):
        settings = self.ctx.settings
        if not settings.paypal_enabled:
            return None
        from liqguard.payments import PayPalClient

        return PayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret,
            settings.paypal_api_base, settings.paypal_webhook_id,
        )

    async def billing_status(self, user_id: int):
        entitlements = self.ctx.entitlements
        if entitlements is None:
            from liqguard.payments import ProStatus
            return ProStatus.free()
        return await entitlements.get_status(user_id)

    async def create_pro_checkout(self, user_id: int) -> Optional[str]:
        """PayPal approve URL for a Pro subscription, or None if billing is off / plan missing."""
        settings = self.ctx.settings
        if not settings.paypal_enabled or not settings.paypal_plan_id:
            return None
        from liqguard.payments import PayPalClient, PayPalError

        client = PayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret,
            settings.paypal_api_base, settings.paypal_webhook_id,
        )
        return_url = f"{settings.dashboard_url}/api/me/billing/return"
        cancel_url = f"{settings.dashboard_url}/me"
        try:
            result = await client.create_subscription(
                plan_id=settings.paypal_plan_id,
                return_url=return_url,
                cancel_url=cancel_url,
                custom_id=str(user_id),
            )
            await self.ctx.storage.save_subscription(
                user_id, result["subscription_id"], settings.paypal_plan_id, "PENDING",
            )
            return result.get("approve_url")
        except PayPalError:
            return None

    async def create_rescue_pass_checkout(self, user_id: int) -> Optional[str]:
        settings = self.ctx.settings
        if not settings.paypal_enabled:
            return None
        from liqguard.payments import PayPalClient, PayPalError

        client = PayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret,
            settings.paypal_api_base, settings.paypal_webhook_id,
        )
        try:
            result = await client.create_order(
                amount_usd="2.99",
                description="LiqGuard Rescue Pass",
                return_url=f"{settings.dashboard_url}/api/me/billing/rescue-return",
                cancel_url=f"{settings.dashboard_url}/me",
                custom_id=str(user_id),
            )
            return result.get("approve_url")
        except PayPalError:
            return None

    async def cancel_pro(self, user_id: int) -> bool:
        settings = self.ctx.settings
        entitlements = self.ctx.entitlements
        if not settings.paypal_enabled or entitlements is None:
            return False
        status = await entitlements.get_status(user_id)
        if not status.subscription_id:
            return False
        from liqguard.payments import PayPalClient

        client = PayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret,
            settings.paypal_api_base, settings.paypal_webhook_id,
        )
        ok = await client.cancel_subscription(status.subscription_id, "User requested")
        if ok:
            await self.ctx.storage.update_subscription_status(status.subscription_id, "CANCELLED")
        return ok

    async def agent_checkout(self, user_id: int, text: str, lang: str, product: Optional[str] = None,
                             default_product: Optional[str] = None):
        from liqguard.payments.agent import run_checkout

        settings = self.ctx.settings
        return await run_checkout(
            self.paypal_client(), self.ctx.llm,
            user_id=user_id, text=text, lang=lang,
            plan_id=settings.paypal_plan_id,
            dashboard_url=settings.dashboard_url,
            product=product, store=self.ctx.storage,
            default_product=default_product,
        )

    async def muse_upgrade_hint(self, user_id: int, lang: str, distance_pct: Optional[float] = None) -> Optional[str]:
        entitlements = self.ctx.entitlements
        if entitlements is None or not entitlements.paypal_enabled:
            return None
        high_risk = distance_pct is not None and distance_pct <= 20
        return await entitlements.upgrade_hint(user_id, lang, high_risk=high_risk)

    async def _log_limit(self, user_id: int, status: str, message: str, product: str = "muse") -> None:
        if hasattr(self.ctx.storage, "save_agent_action"):
            await self.ctx.storage.save_agent_action(user_id, {
                "tool": status,
                "product": product,
                "source": "limit",
                "status": status,
                "message": message,
            })

    async def deny_muse(self, req) -> Optional[Screen]:
        """If the free-tier Muse cap is hit, return the bilingual upsell screen; else None."""
        entitlements = self.ctx.entitlements
        if entitlements is None or not entitlements.paypal_enabled:
            return None
        allowed, used, limit = await entitlements.check_muse_limit(req.user_id)
        if allowed:
            return None
        await self._log_limit(req.user_id, "muse_limit", f"{used}/{limit}")
        reset = entitlements.reset_label()
        T = req.T
        return Screen(
            T("pro.upgrade.limit_reached_full", used=used, limit=limit, reset=reset),
            kb.upsell(T, req.sign),
        )

    async def record_muse(self, user_id: int) -> None:
        entitlements = self.ctx.entitlements
        if entitlements is None or not entitlements.paypal_enabled:
            return
        await entitlements.record_muse_usage(user_id)

    async def account_limit_screen(self, req, venue: str, network: str = "mainnet") -> Optional[Screen]:
        """Block starting a new bind when the account cap is full."""
        entitlements = self.ctx.entitlements
        if entitlements is None or not entitlements.paypal_enabled:
            return None
        bindings = await self.binding.list(req.user_id)
        replacing = any(
            b["venue"] == venue and (b.get("network") or "mainnet") == network for b in bindings
        )
        allowed, used, limit = await entitlements.check_account_limit(
            req.user_id, len(bindings), replacing=replacing,
        )
        if allowed:
            return None
        await self._log_limit(req.user_id, "account_limit", f"{used}/{limit}", product="account")
        T = req.T
        return Screen(T("pro.upgrade.account_limit", used=used, limit=limit), kb.upsell(T, req.sign))

    async def can_use_full_rescue(self, user_id: int) -> bool:
        entitlements = self.ctx.entitlements
        if entitlements is None or not entitlements.paypal_enabled:
            return True
        return await entitlements.can_use_full_rescue(user_id)
