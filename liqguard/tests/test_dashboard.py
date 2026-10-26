"""Tests for the web dashboard."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from liqguard.dashboard.server import (
    AlertEvent,
    DashboardState,
    PricePoint,
    create_app,
    get_state,
)
from liqguard.models import Position, Side, Venue


@pytest.fixture
def sample_position():
    """Create a sample position for testing."""
    return Position(
        venue=Venue.DEMO,
        symbol="BTCUSDT",
        side=Side.LONG,
        size=0.5,
        entry_price=65000.0,
        mark_price=60000.0,
        liquidation_price=58500.0,
        margin=3250.0,
        leverage=10,
        unrealized_pnl=-2500.0,
    )


@pytest.fixture
def dashboard_state():
    """Create fresh dashboard state for testing."""
    return DashboardState()


class TestPricePoint:
    """Tests for PricePoint dataclass."""
    
    def test_to_dict(self):
        point = PricePoint(
            timestamp=datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc),
            symbol="BTCUSDT",
            price=65000.50,
            liquidation_price=58500.25,
        )
        
        result = point.to_dict()
        
        assert result["symbol"] == "BTCUSDT"
        assert result["price"] == 65000.5
        assert result["liquidation_price"] == 58500.25
        assert "timestamp" in result


class TestAlertEvent:
    """Tests for AlertEvent dataclass."""
    
    def test_to_dict(self):
        event = AlertEvent(
            timestamp=datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc),
            level="critical",
            symbol="BTCUSDT",
            distance=8.5,
            message="Test alert",
            advice={"action": "add_margin"},
        )
        
        result = event.to_dict()
        
        assert result["level"] == "critical"
        assert result["symbol"] == "BTCUSDT"
        assert result["distance"] == 8.5
        assert result["message"] == "Test alert"
        assert result["advice"] == {"action": "add_margin"}


class TestDashboardState:
    """Tests for DashboardState."""
    
    def test_add_price_point(self, dashboard_state):
        point = dashboard_state.add_price_point("BTCUSDT", 65000.0, 58500.0)
        
        assert point.symbol == "BTCUSDT"
        assert point.price == 65000.0
        assert point.liquidation_price == 58500.0
        assert "BTCUSDT" in dashboard_state.price_history
        assert len(dashboard_state.price_history["BTCUSDT"]) == 1
    
    def test_price_history_limit(self, dashboard_state):
        dashboard_state._max_history_points = 10
        
        for i in range(15):
            dashboard_state.add_price_point("BTCUSDT", 65000.0 + i, 58500.0)
        
        assert len(dashboard_state.price_history["BTCUSDT"]) == 10
    
    def test_add_alert(self, dashboard_state, sample_position):
        event = dashboard_state.add_alert("critical", sample_position, 8.5)
        
        assert event.level == "critical"
        assert event.symbol == "BTCUSDT"
        assert event.distance == 8.5
        assert len(dashboard_state.alert_timeline) == 1
    
    def test_alert_timeline_limit(self, dashboard_state, sample_position):
        for i in range(60):
            dashboard_state.add_alert("warning", sample_position, 15.0)
        
        assert len(dashboard_state.alert_timeline) == 50
    
    async def test_get_portfolio_overview(self, dashboard_state):
        overview = await dashboard_state.get_portfolio_overview()
        
        assert overview.total_positions == 3
        assert overview.venues == ["demo"]
    
    async def test_broadcast_no_clients(self, dashboard_state):
        await dashboard_state.broadcast({"type": "test"})
    
    async def test_broadcast_with_clients(self, dashboard_state):
        mock_ws = AsyncMock()
        dashboard_state.connected_clients.add(mock_ws)
        
        await dashboard_state.broadcast({"type": "test", "data": "hello"})
        
        mock_ws.send_text.assert_called_once()
    
    async def test_broadcast_removes_disconnected(self, dashboard_state):
        mock_ws = AsyncMock()
        mock_ws.send_text.side_effect = Exception("disconnected")
        dashboard_state.connected_clients.add(mock_ws)
        
        await dashboard_state.broadcast({"type": "test"})
        
        assert mock_ws not in dashboard_state.connected_clients


class TestCreateApp:
    """Tests for FastAPI app creation."""
    
    def test_creates_app(self):
        app = create_app()
        
        assert app.title == "LiqGuard Dashboard"
        assert app.version == "0.1.0"
    
    def test_creates_app_with_llm_client(self):
        mock_llm = MagicMock()
        app = create_app(llm_client=mock_llm)
        
        state = get_state()
        assert state.llm_client == mock_llm


class TestDashboardEndpoints:
    """Tests for dashboard API endpoints."""
    
    @pytest.fixture
    def client(self):
        """Create test client."""
        from fastapi.testclient import TestClient
        app = create_app()
        return TestClient(app)
    
    def test_index_returns_html(self, client):
        response = client.get("/")
        
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "LiqGuard Dashboard" in response.text
    
    def test_portfolio_endpoint(self, client):
        response = client.get("/api/portfolio")
        
        assert response.status_code == 200
        data = response.json()
        assert "summary" in data
        assert "risk_metrics" in data
        assert "positions" in data
    
    def test_positions_endpoint(self, client):
        response = client.get("/api/positions")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_price_history_endpoint(self, client):
        response = client.get("/api/price-history")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
    
    def test_alerts_endpoint(self, client):
        response = client.get("/api/alerts")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_advice_endpoint(self, client):
        response = client.get("/api/advice")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_simulate_endpoint(self, client):
        response = client.post("/api/simulate")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ["started", "already_running"]
        
        client.post("/api/stop-simulation")
    
    def test_stop_simulation_endpoint(self, client):
        response = client.post("/api/stop-simulation")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "stopped"
    
    def test_reset_endpoint(self, client):
        response = client.post("/api/reset")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "reset"


class TestSimulation:
    """Tests for market simulation."""
    
    async def test_start_stop_simulation(self, dashboard_state):
        await dashboard_state.start_simulation()
        
        assert dashboard_state._running is True
        
        await asyncio.sleep(0.1)
        
        dashboard_state.stop_simulation()
        
        assert dashboard_state._running is False
    
    async def test_simulation_updates_prices(self, dashboard_state):
        await dashboard_state.start_simulation()
        
        await asyncio.sleep(2.5)
        
        dashboard_state.stop_simulation()
        
        assert len(dashboard_state.price_history) > 0
    
    async def test_double_start_ignored(self, dashboard_state):
        await dashboard_state.start_simulation()
        await dashboard_state.start_simulation()
        
        dashboard_state.stop_simulation()
