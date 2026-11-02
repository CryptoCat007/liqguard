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
        self.binding = binding or BindingService(ctx.storage, default_threshold=ctx.settings.default_threshold)
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
