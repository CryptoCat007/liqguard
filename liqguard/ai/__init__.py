"""Muse (Meta AI) client with a deterministic mock. The LLM only phrases numbers computed in `liqguard.risk`."""

from .muse import MOCK, MUSE, RULES, Instant, MetaLLMClient, get_news_stub
from .rescue import RESCUE_EXPLANATION_SCHEMA, mock_rescue_explanation, validate_rescue_explanation
from .service import MuseService, PortfolioMuse, Suggestion
from .stops import STOP_EXPLANATION_SCHEMA, mock_stop_explanation, validate_stop_explanation

__all__ = [
    "Instant",
    "MOCK",
    "MUSE",
    "RULES",
    "MuseService",
    "PortfolioMuse",
    "Suggestion",
    "MetaLLMClient",
    "get_news_stub",
    "RESCUE_EXPLANATION_SCHEMA",
    "mock_rescue_explanation",
    "validate_rescue_explanation",
    "STOP_EXPLANATION_SCHEMA",
    "mock_stop_explanation",
    "validate_stop_explanation",
]
