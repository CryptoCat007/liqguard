"""Muse (Meta AI) client with a deterministic mock. The LLM only phrases numbers computed in `liqguard.risk`."""

from .muse import MetaLLMClient, get_news_stub
from .rescue import RESCUE_EXPLANATION_SCHEMA, mock_rescue_explanation, validate_rescue_explanation

__all__ = [
    "MetaLLMClient",
    "get_news_stub",
    "RESCUE_EXPLANATION_SCHEMA",
    "mock_rescue_explanation",
    "validate_rescue_explanation",
]
