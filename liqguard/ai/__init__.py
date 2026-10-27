"""Muse (Meta AI) client with a deterministic mock. The LLM only phrases numbers computed in `liqguard.risk`."""

from .muse import MetaLLMClient, get_news_stub

__all__ = ["MetaLLMClient", "get_news_stub"]
