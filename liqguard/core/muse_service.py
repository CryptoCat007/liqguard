"""`MuseService` lives in `liqguard.ai.service` so the simulation can use it too; core re-exports it."""

from liqguard.ai.service import MuseService, PortfolioMuse, Suggestion

__all__ = ["MuseService", "PortfolioMuse", "Suggestion"]
