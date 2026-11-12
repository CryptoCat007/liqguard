"""Background position monitor. Framework-free: delivery goes through a `Notifier`."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol, Union

from liqguard.i18n import EN, t
from liqguard.models import Position, User
from liqguard.risk import (
    BAND_ORDER,
    PortfolioInsights,
    PreFundingWarning,
    RiskAssessment,
    RiskContext,
    RiskEngine,
    StopCheck,
    portfolio_insights,
    position_protection,
    pre_funding_warning,
    stress_band,
)

from .demo_window import DemoAlertWindow
from .escalation import ESCALATING, EscalationService, ack_action
from .manager import AlertManager
from .messages import (
    MOCK,
    MUSE,
    _usd,
    comparison_line,
    demo_tag,
    format_adl_message,
    format_correlation_message,
    format_sizing_message,
    format_stop_message,
    format_triage_message,
    priority_line,
    recommendation_line,
    side_upper,
    sizing_line,
    thresholds_line,
    venue_name,
    wick_line,
)

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {"SAFE": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "EMERGENCY": "🆘"}


Action = tuple[str, str]
"""(button label, callback data) offered with an alert."""


class Notifier(Protocol):
    async def notify(self, user_id: int, text: str, actions: Optional[list[Action]] = None, lang: str = EN,
                     demo: bool = False) -> Any:
        """Deliver `text` (and optional action buttons) to exactly one user; `lang` is the language `text` is in,
        `demo` marks an alert about the user's demo book (it gets a ⏹ Stop demo button). May return the sent
        message; if it has `edit_text`, Muse's explanation replaces the deterministic one when it arrives."""


class BookSnapshotLike(Protocol):
    positions: list[Position]
    context: RiskContext


class Explainer(Protocol):
    async def explain_risk(self, position, distance_pct, margin_to_safety, severity, language) -> str: ...


UsersProvider = Callable[[], Awaitable[list[User]]]
PositionsProvider = Callable[[User], Awaitable[Union[list[Position], BookSnapshotLike]]]


def rescue_action(position: Position, lang: str = EN) -> Action:
    return (t(lang, "alert.rescue_button"), f"rescue:{position.venue.value}:{position.symbol}")


def format_alert_message(position: Position, assessment: RiskAssessment, explanation: str,
                         protection: Optional[dict] = None, insights: Optional[PortfolioInsights] = None,
                         lang: str = EN, live: Union[bool, str] = True) -> str:
    """
    Tier alert. One recommendation line follows the distance: Muse's
    `explanation` when `live`, else the computed add-margin amount.
    """
    emoji = SEVERITY_EMOJI.get(assessment.risk_level_str, "❓")
    level = assessment.risk_level_str.lower()
    source = live if isinstance(live, str) else (MUSE if live else MOCK)
    if source == MUSE and explanation:
        recommendation = recommendation_line(lang, explanation, MUSE)
    elif assessment.margin_to_safety > 0:
        recommendation = recommendation_line(lang, t(lang, "alert.tier.rule_add_margin",
                                                     amount=f"{assessment.margin_to_safety:,.2f}"), source)
    else:
        recommendation = recommendation_line(lang, explanation or "", source)
    message = (
        t(lang, "alert.tier.head", emoji=emoji, level=t(lang, f"common.severity_upper.{level}")) + "\n\n"
        + f"**{position.symbol}** {side_upper(lang, position.side.value)}\n"
        + t(lang, "alert.tier.distance", dist=f"{assessment.distance_to_liquidation_pct:.1f}") + "\n"
        + recommendation + "\n"
    )
    extra: list[Optional[str]] = []
    if insights is not None:
        priority, top = insights.priority(position), insights.top
        comparison, sizing = insights.comparison(position), insights.sizing_for(position)
        extra = [
            priority_line(priority.to_dict() if priority else None, top.to_dict() if top else None, lang),
            comparison_line(comparison.to_dict(), lang) if comparison and comparison.correlated_market_move_pct is not None else None,
            sizing_line(sizing.to_dict(), lang) if sizing else None,
        ]
    for line in (*extra, wick_line((protection or {}).get("wicks"), lang), thresholds_line((protection or {}).get("thresholds"), lang)):
        if line:
            message += f"{line}\n"
    return message.rstrip("\n")


def format_prefunding_message(warning: dict, demo: bool = False, lang: str = EN) -> str:
    """Pre-funding warning text from `PreFundingWarning.to_dict()` (every number computed in `risk.funding`)."""
    liq_before, liq_after = warning.get("liquidation_before"), warning.get("liquidation_after")
    lines = [
        t(lang, "alert.funding.head") + demo_tag(lang, demo),
        "",
        t(lang, "alert.funding.pays", symbol=warning["symbol"], side=side_upper(lang, warning["side"]),
          venue=venue_name(lang, warning["venue"]), amount=f"{warning['payment_usd']:,.2f}",
          minutes=f"{warning['minutes_to_settlement']:.0f}", rate=f"{warning['rate_pct']:+.4f}",
          hours=f"{warning['interval_hours']:g}"),
        t(lang, "alert.funding.distance", before=f"{warning['distance_before_pct']:.1f}", after=f"{warning['distance_after_pct']:.1f}"),
    ]
    if liq_before and liq_after:
        lines.append(t(lang, "alert.funding.liq", before=_usd(liq_before, lang), after=_usd(liq_after, lang)))
    lines.append(t(lang, "alert.funding.buffer_tier" if warning.get("crosses_tier") else "alert.funding.buffer",
                   share=f"{warning['buffer_share_pct']:.1f}"))
    return "\n".join(lines)


class PositionMonitor:
    """
    Polls each user's positions and alerts that user only.

    Every alert is computed from the user's own positions and thresholds and
    delivered to `notifier.notify(user.telegram_id, ...)`; there is no
    broadcast path.
    """

    def __init__(
        self,
        users: UsersProvider,
        positions: PositionsProvider,
        notifier: Notifier,
        alert_manager: AlertManager,
        explainer: Explainer,
        poll_interval: int = 30,
        prefunding_lead_minutes: float = 30.0,
        escalation: Optional[EscalationService] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        demo_window: Optional[DemoAlertWindow] = None,
        entitlements: Any = None,
    ):
        self._users = users
        self._positions = positions
        self.notifier = notifier
        self.alert_manager = alert_manager
        self.explainer = explainer
        self.poll_interval = poll_interval
        self.prefunding_lead_minutes = prefunding_lead_minutes
        self._task: Optional[asyncio.Task] = None
        self._prefunding_sent: dict[tuple[int, str, str, datetime], datetime] = {}
        self.escalation = escalation
        self._stop_state: dict[tuple[int, str, str], tuple] = {}
        self._adl_state: dict[tuple[int, str, str], int] = {}
        self._oversized: dict[int, set[tuple[str, str]]] = {}
        self._stress_band: dict[int, Optional[str]] = {}
        self._concentrated: dict[int, bool] = {}
        self.clock = clock
        self.demo_window = demo_window
        self._muse_followups: set[asyncio.Task] = set()
        self.entitlements = entitlements
        self._last_check: dict[int, float] = {}

    async def _notify(self, user: User, text: str, actions: Optional[list[Action]] = None) -> Any:
        return await self.notifier.notify(user.telegram_id, text, actions=actions, lang=user.language,
                                          demo=user.is_demo_mode)

    async def _explain(self, method: str, fallback_method: str, *args) -> tuple[Any, Optional[Awaitable[Any]], Any]:
        """(text to send now, Muse's answer still on its way, who wrote the text now). Alerts never wait for Muse."""
        now = getattr(self.explainer, method, None)
        if now is not None:
            instant = now(*args)
            live = bool(getattr(instant, "live", False))
            return instant.result, instant.pending, getattr(instant, "source", MUSE if live else MOCK)
        later = getattr(self.explainer, fallback_method, None)
        return (await later(*args) if later is not None else None), None, True

    def _muse_followup(self, sent: Any, pending: Optional[Awaitable[Any]], render: Callable[[Any], str]) -> None:
        edit = getattr(sent, "edit_text", None)
        if pending is None or edit is None:
            return

        async def run() -> None:
            try:
                live = await pending
                if live is not None:
                    await edit(render(live), parse_mode="Markdown", reply_markup=getattr(sent, "reply_markup", None))
            except Exception as e:
                logger.info("Muse follow-up not applied: %s", type(e).__name__)

        task = asyncio.create_task(run())
        self._muse_followups.add(task)
        task.add_done_callback(self._muse_followups.discard)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            loop_s = self.entitlements.pro_poll_interval if self.entitlements and self.entitlements.paypal_enabled else self.poll_interval
            logger.info("Position monitor started (interval: %ss)", loop_s)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _loop_interval(self) -> int:
        if self.entitlements is not None and self.entitlements.paypal_enabled:
            return min(self.poll_interval, self.entitlements.pro_poll_interval)
        return self.poll_interval

    async def _due(self, user: User) -> bool:
        """Pro users are polled faster; free users keep the default interval. No PayPal → no extra skip."""
        if self.entitlements is None or not self.entitlements.paypal_enabled:
            return True
        now = self.clock().timestamp()
        last = self._last_check.get(user.telegram_id, 0.0)
        interval = await self.entitlements.poll_interval_for_user(user.telegram_id)
        if now - last < interval:
            return False
        self._last_check[user.telegram_id] = now
        return True

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_all_users()
            except Exception:
                logger.exception("Monitor loop error")
            await asyncio.sleep(self._loop_interval())

    async def check_all_users(self) -> None:
        for user in await self._users():
            try:
                await self.check_user(user)
            except Exception as e:
                logger.error("Error checking user %s: %s", user.telegram_id, type(e).__name__)

    async def check_user(self, user: User) -> int:
        """
        Check one user's positions; returns the number of alerts delivered.

        When two or more positions are due a tier alert in the same check they
        go out as one consolidated message, most urgent first. During the
        user's quiet hours only critical and emergency alerts go out; warnings
        and informational messages wait (their state is left untouched so they
        fire once quiet hours end).
        """
        if user.is_demo_mode and self.demo_window is not None and not self.demo_window.allows(user.telegram_id):
            return 0
        if not await self._due(user):
            return 0
        quiet = user.threshold.in_quiet_hours(self.clock())
        result = await self._positions(user)
        if isinstance(result, list):
            positions, context = result, None
        else:
            positions, context = result.positions, result.context
        ctx = context if context is not None else RiskContext()
        sent = 0
        protections = {(p.venue.value, p.symbol): position_protection(p, ctx, user.threshold) for p in positions}
        insights = portfolio_insights(positions, ctx, user.threshold, protections)
        due = []
        for position in positions:
            protection = protections[(position.venue.value, position.symbol)]
            info = protection.to_dict()
            engine = RiskEngine(protection.adaptive.threshold, context)
            assessment = engine.assess_position(position)
            severity = assessment.severity.value if assessment.severity else None
            if self.escalation is not None:
                if severity in ESCALATING:
                    self.escalation.tracker.update(user.telegram_id, position.venue.value, position.symbol,
                                                   assessment.distance_to_liquidation_pct)
                else:
                    self.escalation.tracker.resolve(user.telegram_id, position.venue.value, position.symbol)
            if protection.stop is not None and not quiet:
                sent += await self._check_stop(user, position, protection.stop)
            if quiet:
                pass
            elif protection.adl.warn:
                sent += await self._check_adl(user, position, info["adl"])
            else:
                self._adl_state.pop((user.telegram_id, position.venue.value, position.symbol), None)
            if assessment.severity is None or (quiet and severity not in ESCALATING):
                continue
            if self.escalation is not None and self.escalation.tracker.is_silenced(
                    user.telegram_id, position.venue.value, position.symbol, severity, assessment.distance_to_liquidation_pct):
                continue
            alert = engine.create_alert(assessment)
            if alert and self.alert_manager.process_alert(user.telegram_id, alert, user.threshold.cooldown_seconds):
                due.append((position, assessment, info))
        if len(due) >= 2:
            sent += await self._send_triage(user, due, insights)
        elif due:
            sent += await self._send_tier_alert(user, *due[0], insights)
        if quiet:
            return sent
        sent += await self._check_portfolio(user, insights)
        if context is not None:
            sent += await self._check_prefunding(user, positions, context)
        return sent

    def _open_escalation(self, user: User, position: Position, severity: Optional[str], text: str, distance: float,
                         actions: list[Action]) -> None:
        if self.escalation is not None and severity in ESCALATING:
            ack = self.escalation.open(user.telegram_id, position.venue.value, position.symbol, severity, text,
                                       user.threshold, distance, lang=user.language)
            if ack is not None:
                actions.insert(0, ack_action(ack, user.language))

    async def _send_tier_alert(self, user: User, position: Position, assessment: RiskAssessment, info: dict,
                               insights: PortfolioInsights) -> int:
        lang = user.language
        explanation, pending, live = await self._explain(
            "explain_risk_now", "explain_risk", position, assessment.distance_to_liquidation_pct,
            assessment.margin_to_safety, assessment.severity, lang,
        )
        text = format_alert_message(position, assessment, explanation, info, insights, lang, live=live)
        actions = [rescue_action(position, lang)]
        severity = assessment.severity.value if assessment.severity else None
        self._open_escalation(user, position, severity, text, assessment.distance_to_liquidation_pct, actions)
        sent = await self._notify(user, text, actions)
        self._muse_followup(sent, pending, lambda live: format_alert_message(position, assessment, live, info, insights, lang))
        return 1

    async def _send_triage(self, user: User, due: list, insights: PortfolioInsights) -> int:
        """One message for several positions; escalation and the rescue button for the one to handle first."""
        items = [t.to_dict() for t in insights.at_risk]
        severities = [a.severity.value for _, a, _ in due if a.severity]
        worst = max(severities, key=BAND_ORDER.index) if severities else None
        text = format_triage_message(items, worst, focus=insights.focus, lang=user.language)
        top = insights.top
        lead = next(((p, a) for p, a, _ in due if top and p.symbol == top.symbol and p.venue.value == top.venue), None)
        lead = lead or (due[0][0], due[0][1])
        position, assessment = lead
        actions = [rescue_action(position, user.language)]
        self._open_escalation(user, position, assessment.severity.value if assessment.severity else None, text,
                              assessment.distance_to_liquidation_pct, actions)
        await self._notify(user, text, actions)
        return 1

    async def _check_portfolio(self, user: User, insights: PortfolioInsights) -> int:
        """Sizing and correlated-stress warnings, each only when it gets worse (new oversized position, worse band)."""
        uid, sent, lang = user.telegram_id, 0, user.language
        oversized = {(s.venue, s.symbol) for s in insights.oversized}
        new = oversized - self._oversized.get(uid, set())
        self._oversized[uid] = oversized
        if new:
            checks = [s.to_dict() for s in insights.oversized if (s.venue, s.symbol) in new]
            await self._notify(user, format_sizing_message(checks, lang=lang))
            sent += 1
        worst = insights.worst_stress()
        band = stress_band(worst.first_liquidation if worst else None)
        previous = self._stress_band.get(uid)
        worse = band is not None and (previous is None or BAND_ORDER.index(band) > BAND_ORDER.index(previous))
        flagged = insights.concentration.flagged
        if worse or (flagged and not self._concentrated.get(uid, False)):
            data = insights.to_dict()
            comps = [c for c in data["comparisons"] if worst is None or c["symbol"] in worst.symbols]
            await self._notify(user, format_correlation_message(
                worst.to_dict() if worst else None, data["concentration"], comps, band, lang=lang))
            sent += 1
        if worse or band is None:
            self._stress_band[uid] = band
        self._concentrated[uid] = flagged
        return sent

    async def _check_stop(self, user: User, position: Position, check: StopCheck) -> int:
        """Warn as soon as a position has no stop or a stop the liquidation engine beats; again only if that changes."""
        key = (user.telegram_id, position.venue.value, position.symbol)
        if not check.needs_warning:
            self._stop_state.pop(key, None)
            return 0
        state = (check.status, None if check.stop_price is None else round(check.stop_price, 6))
        if self._stop_state.get(key) == state:
            return 0
        self._stop_state[key] = state
        lang = user.language
        explanation, pending = None, None
        try:
            explanation, pending, _ = await self._explain("explain_stop_fix_now", "explain_stop_fix", check, lang)
        except Exception as e:
            logger.error("Stop-loss explanation failed: %s", type(e).__name__)
        sent = await self._notify(user, format_stop_message(check.to_dict(), explanation, lang=lang), [rescue_action(position, lang)])
        self._muse_followup(sent, pending, lambda live: format_stop_message(check.to_dict(), live, lang=lang))
        return 1

    async def _check_adl(self, user: User, position: Position, adl: dict) -> int:
        key = (user.telegram_id, position.venue.value, position.symbol)
        if self._adl_state.get(key, 0) >= adl["lights"]:
            return 0
        self._adl_state[key] = adl["lights"]
        await self._notify(user, format_adl_message(
            position.symbol, position.venue.value, position.side.value, adl, position.unrealized_pnl, lang=user.language))
        return 1

    async def _check_prefunding(self, user: User, positions: list[Position], context: RiskContext) -> int:
        """One pre-funding warning per position per settlement."""
        sent = 0
        for key, settlement in list(self._prefunding_sent.items()):
            if settlement < context.now:
                del self._prefunding_sent[key]
        for position in positions:
            if not context.computable(position):
                continue
            warning: Optional[PreFundingWarning] = pre_funding_warning(
                position, context.account_for(position), context.funding_for, context.now,
                user.threshold, self.prefunding_lead_minutes,
            )
            if warning is None:
                continue
            key = (user.telegram_id, position.venue.value, position.symbol, warning.settlement_time)
            if key in self._prefunding_sent:
                continue
            self._prefunding_sent[key] = warning.settlement_time
            lang = user.language
            await self._notify(user, format_prefunding_message(warning.to_dict(), lang=lang), [rescue_action(position, lang)])
            sent += 1
        return sent
