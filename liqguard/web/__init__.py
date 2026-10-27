"""Web dashboard (FastAPI). Public demo at `/`, per-user view at `/me` behind Telegram-based login."""

from .server import DashboardServer, WebSocketHub, create_app

__all__ = ["DashboardServer", "WebSocketHub", "create_app"]
