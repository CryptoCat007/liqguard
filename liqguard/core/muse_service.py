"""
The one Muse entry point for every channel.

Telegram (commands, menus, alert monitor, demo bridge) and the web dashboard
(public demo, private demo and account view) all ask Muse through the
`MuseService` on `AppContext`. They therefore share one client, one
per-input cache and one honest status, and because the inputs (overview,
triage focus, rescue plan, stop check, risk assessment) are built here the
same way for each channel, the same book asks Muse once whichever channel
looks first. Numbers are computed in `liqguard.risk`; Muse only phrases them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from liqguard.ai import Instant, MetaLLMClient
from liqguard.i18n import tr
from liqguard.models import Position, User
from liqguard.risk import (
    PortfolioAnalyzer,
    PortfolioInsights,
    PortfolioRiskOverview,
    RescuePlan,
    RiskAssessment,
    RiskEngine,
    StopCheck,
    portfolio_insights,
    position_protection,
)

from .positions import BookSnapshot


@dataclass
class PortfolioMuse:
    overview: PortfolioRiskOverview
    insights: PortfolioInsights
    summary: Instant
    """`summarize_portfolio` output: rule-based now, Muse's headline in `pending`."""


@dataclass
class Suggestion:
    """Muse's advice for the position to handle first, with the same inputs as the Telegram tier alert."""

    position: Position
    assessment: RiskAssessment
    explanation: Instant
    mock: bool
    """Whether `explanation.result` is the rule-based text (Muse's may still be pending)."""

    def to_dict(self, explanation: Optional[str] = None) -> dict:
        """The suggestion now, or with Muse's `explanation` once it arrived."""
        a = self.assessment
        return {
            "symbol": self.position.symbol,
            "venue": self.position.venue.value,
            "side": self.position.side.value,
            "severity": a.severity.value if a.severity else None,
            "distance_pct": round(a.distance_to_liquidation_pct, 2),
            "margin_to_add_usd": round(a.margin_to_safety, 2),
            "explanation": self.explanation.result if explanation is None else explanation,
            "mock": self.mock if explanation is None else False,
        }


class MuseService:
    def __init__(self, llm: MetaLLMClient):
        self.llm = llm

    @property
    def mode(self) -> str:
        return self.llm.mode

    # ------------------------------------------------------------------ portfolio

    def portfolio(self, user: User, snapshot: BookSnapshot, language: Optional[str] = None) -> PortfolioMuse:
        """Overview, triage and the one-line summary for `user`'s book (`/risk` and the dashboard account view)."""
        lang = language or user.language
        overview = PortfolioAnalyzer(user.threshold, snapshot.context).analyze_portfolio(snapshot.positions)
        insights = portfolio_insights(snapshot.positions, snapshot.context, user.threshold)
        return PortfolioMuse(overview, insights, self.llm.summarize_portfolio_now(overview, lang, insights.focus))

    def suggestion(self, user: User, snapshot: BookSnapshot, insights: PortfolioInsights,
                   language: Optional[str] = None) -> Optional[Suggestion]:
        """Advice for the position triage puts first (else the nearest one past a tier), or None if all are safe."""
        assessed = []
        for p in snapshot.positions:
            protection = position_protection(p, snapshot.context, user.threshold)
            assessed.append((p, RiskEngine(protection.adaptive.threshold, snapshot.context).assess_position(p)))
        at_risk = [(p, a) for p, a in assessed if a.severity is not None]
        if not at_risk:
            return None
        top = insights.top
        position, assessment = next(((p, a) for p, a in at_risk if top and p.symbol == top.symbol and p.venue.value == top.venue),
                                    min(at_risk, key=lambda pa: pa[1].distance_to_liquidation_pct))
        instant = self.explain_risk_now(position, assessment.distance_to_liquidation_pct, assessment.margin_to_safety,
                                        assessment.severity, language or user.language)
        return Suggestion(position, assessment, instant, self.llm.is_mock or instant.pending is not None)

    # ------------------------------------------------------------------ per-position explanations

    def explain_risk_now(self, position: Position, distance_pct: float, margin_to_safety: float, severity,
                         language: str) -> Instant:
        return self.llm.explain_risk_now(position, distance_pct, margin_to_safety, severity, language)

    async def explain_risk(self, position: Position, distance_pct: float, margin_to_safety: float, severity,
                           language: str) -> str:
        return await self.llm.explain_risk(position, distance_pct, margin_to_safety, severity, language)

    def rescue(self, plan: RescuePlan, language: str) -> Instant:
        return self.llm.explain_rescue_now(plan, language)

    async def explain_rescue(self, plan: RescuePlan, language: str) -> dict:
        return await self.llm.explain_rescue(plan, language)

    def explain_stop_fix_now(self, check: StopCheck, language: str) -> Instant:
        return self.llm.explain_stop_fix_now(check, language)

    async def explain_stop_fix(self, check: StopCheck, language: str) -> dict:
        return await self.llm.explain_stop_fix(check, language)

    def tool_note(self, tool: str, language: str, plan: Optional[RescuePlan] = None,
                  check: Optional[StopCheck] = None) -> Optional[Instant]:
        """
        The recommendation a quick report leads with: the rescue or the
        stop-loss fix in one sentence ({"text", "mock"}). Wick stress and
        funding reports are pure numbers with no Muse text.
        """
        if tool == "rescue" and plan is not None and not plan.already_safe:
            return self.rescue(plan, language).map(lambda e: _note(language, e, ("headline",)))
        if tool == "stop" and check is not None and check.needs_warning:
            return self.explain_stop_fix_now(check, language).map(lambda e: _note(language, e, ("headline",)))
        return None

    # ------------------------------------------------------------------ chat

    def answer_now(self, positions: list[Position], question: str, language: str, news: Optional[str] = None) -> Instant:
        return self.llm.answer_portfolio_question_now(positions, question, language, news)

    async def parse_screenshot(self, image: bytes) -> Optional[dict]:
        return await self.llm.parse_screenshot(image)


def _note(language: str, explanation: Any, keys: tuple[str, ...]) -> Optional[dict]:
    if not isinstance(explanation, dict):
        return None
    parts = [str(tr(language, explanation[k])).strip() for k in keys if explanation.get(k)]
    text = " ".join(p for p in parts if p)
    return {"text": text, "mock": bool(explanation.get("mock", True))} if text else None
