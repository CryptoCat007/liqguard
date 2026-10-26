"""
FastAPI web dashboard server for LiqGuard.

Provides:
- Live price vs liquidation charts
- Per-position risk overview
- Alert timeline
- Muse advice cards
- Action audit log
- Simulate market button for demo mode
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from liqguard.connectors.demo import DemoConnector
from liqguard.llm import MetaLLMClient
from liqguard.models import AlertSeverity, Position
from liqguard.services.advice import ActionableAdvice, AdviceCalculator
from liqguard.services.portfolio import PortfolioAnalyzer, PortfolioRiskOverview
from liqguard.services.protective_actions import (
    ActionStatus,
    AuditLogEntry,
    ProtectiveActionsManager,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class AlertEvent:
    """Alert event for timeline."""
    timestamp: datetime
    level: str
    symbol: str
    distance: float
    message: str
    advice: Optional[dict] = None
    
    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "level": self.level,
            "symbol": self.symbol,
            "distance": round(self.distance, 2),
            "message": self.message,
            "advice": self.advice,
        }


@dataclass
class PricePoint:
    """Price data point for charting."""
    timestamp: datetime
    symbol: str
    price: float
    liquidation_price: float
    
    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "price": round(self.price, 2),
            "liquidation_price": round(self.liquidation_price, 2),
        }


class DashboardState:
    """Shared state for dashboard."""
    
    def __init__(self):
        self.demo_connector = DemoConnector(scenario="normal")
        self.portfolio_analyzer = PortfolioAnalyzer()
        self.actions_manager = ProtectiveActionsManager()
        self.llm_client: Optional[MetaLLMClient] = None
        
        self.price_history: dict[str, list[PricePoint]] = {}
        self.alert_timeline: list[AlertEvent] = []
        self.advice_cards: list[dict] = []
        self.audit_log: list[dict] = []
        
        self.connected_clients: set[WebSocket] = set()
        self._simulation_task: Optional[asyncio.Task] = None
        self._running = False
        self._max_history_points = 100
    
    def set_llm_client(self, client: MetaLLMClient) -> None:
        """Set the LLM client for advice generation."""
        self.llm_client = client
    
    async def broadcast(self, message: dict) -> None:
        """Broadcast message to all connected WebSocket clients."""
        if not self.connected_clients:
            return
        
        data = json.dumps(message)
        disconnected = set()
        
        for client in self.connected_clients:
            try:
                await client.send_text(data)
            except Exception:
                disconnected.add(client)
        
        self.connected_clients -= disconnected
    
    def add_price_point(self, symbol: str, price: float, liquidation_price: float) -> PricePoint:
        """Add a price point to history."""
        point = PricePoint(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
            price=price,
            liquidation_price=liquidation_price,
        )
        
        if symbol not in self.price_history:
            self.price_history[symbol] = []
        
        self.price_history[symbol].append(point)
        
        if len(self.price_history[symbol]) > self._max_history_points:
            self.price_history[symbol] = self.price_history[symbol][-self._max_history_points:]
        
        return point
    
    def add_alert(
        self, 
        level: str, 
        position: Position, 
        distance: float,
        advice: Optional[ActionableAdvice] = None
    ) -> AlertEvent:
        """Add an alert event to timeline."""
        severity_emoji = {"warning": "⚠️", "critical": "🔴", "emergency": "🚨"}
        
        event = AlertEvent(
            timestamp=datetime.now(timezone.utc),
            level=level,
            symbol=position.symbol,
            distance=distance,
            message=f"{severity_emoji.get(level, '❓')} {position.symbol} at {distance:.1f}% from liquidation",
            advice=advice.to_dict() if advice else None,
        )
        
        self.alert_timeline.append(event)
        
        if len(self.alert_timeline) > 50:
            self.alert_timeline = self.alert_timeline[-50:]
        
        return event
    
    async def generate_advice_card(self, position: Position, level: str) -> Optional[dict]:
        """Generate advice card using LLM."""
        margin_advice = AdviceCalculator.calculate_margin_to_add(position)
        
        if not self.llm_client:
            card = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": position.symbol,
                "level": level,
                "advice": margin_advice.to_dict() if margin_advice else None,
                "explanation": f"Position at risk. Consider adding ${margin_advice.margin_to_add_usd:.2f} margin." if margin_advice.margin_to_add_usd else "Position needs attention.",
                "is_mock": True,
            }
        else:
            try:
                result = await self.llm_client.explain_risk_with_advice(
                    position, margin_advice, "en"
                )
                card = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "symbol": position.symbol,
                    "level": level,
                    "advice": margin_advice.to_dict() if margin_advice else None,
                    "explanation": result.get("explanation", ""),
                    "is_mock": result.get("is_mock", False),
                }
            except Exception as e:
                logger.error(f"Failed to generate advice: {e}")
                card = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "symbol": position.symbol,
                    "level": level,
                    "advice": margin_advice.to_dict() if margin_advice else None,
                    "explanation": f"Add margin to reduce risk. Error getting AI explanation: {e}",
                    "is_mock": True,
                }
        
        self.advice_cards.append(card)
        if len(self.advice_cards) > 20:
            self.advice_cards = self.advice_cards[-20:]
        
        return card
    
    async def get_portfolio_overview(self) -> PortfolioRiskOverview:
        """Get current portfolio overview."""
        positions = await self.demo_connector.get_positions()
        return self.portfolio_analyzer.analyze_portfolio(positions)
    
    async def start_simulation(self, telegram_callback=None) -> None:
        """Start market simulation."""
        if self._running:
            return
        
        self._running = True
        self.demo_connector.set_scenario("liquidation_path")
        self.demo_connector.reset_simulation()
        
        self.price_history.clear()
        self.alert_timeline.clear()
        self.advice_cards.clear()
        
        async def on_alert(level: str, position: Position, distance: float):
            margin_advice = AdviceCalculator.calculate_margin_to_add(position)
            
            event = self.add_alert(level, position, distance, margin_advice)
            
            card = await self.generate_advice_card(position, level)
            
            await self.broadcast({
                "type": "alert",
                "data": event.to_dict(),
            })
            
            if card:
                await self.broadcast({
                    "type": "advice_card",
                    "data": card,
                })
            
            if telegram_callback:
                try:
                    await telegram_callback(level, position, distance)
                except Exception as e:
                    logger.error(f"Telegram callback error: {e}")
        
        async def simulation_loop():
            try:
                while self._running:
                    self.demo_connector.step_simulation()
                    positions = await self.demo_connector.get_positions()
                    
                    for pos in positions:
                        point = self.add_price_point(
                            pos.symbol,
                            pos.mark_price,
                            pos.liquidation_price
                        )
                        
                        await self.broadcast({
                            "type": "price_update",
                            "data": point.to_dict(),
                        })
                    
                    overview = self.portfolio_analyzer.analyze_portfolio(positions)
                    await self.broadcast({
                        "type": "portfolio_update",
                        "data": overview.to_dict(),
                    })
                    
                    for pos in positions:
                        distance = pos.distance_to_liquidation_pct
                        
                        alert_key = f"{pos.symbol}_emergency"
                        if distance <= 5 and alert_key not in [f"{a.symbol}_emergency" for a in self.alert_timeline]:
                            await on_alert("emergency", pos, distance)
                        elif distance <= 10 and f"{pos.symbol}_critical" not in [f"{a.symbol}_critical" for a in self.alert_timeline]:
                            await on_alert("critical", pos, distance)
                        elif distance <= 20 and f"{pos.symbol}_warning" not in [f"{a.symbol}_warning" for a in self.alert_timeline]:
                            await on_alert("warning", pos, distance)
                    
                    await asyncio.sleep(2.0)
                    
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Simulation error: {e}")
            finally:
                self._running = False
        
        self._simulation_task = asyncio.create_task(simulation_loop())
    
    def stop_simulation(self) -> None:
        """Stop market simulation."""
        self._running = False
        if self._simulation_task:
            self._simulation_task.cancel()
            self._simulation_task = None


_dashboard_state: Optional[DashboardState] = None


def get_state() -> DashboardState:
    """Get global dashboard state."""
    global _dashboard_state
    if _dashboard_state is None:
        _dashboard_state = DashboardState()
    return _dashboard_state


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    state = get_state()
    yield
    state.stop_simulation()


def create_app(llm_client: Optional[MetaLLMClient] = None) -> FastAPI:
    """Create FastAPI application."""
    app = FastAPI(
        title="LiqGuard Dashboard",
        description="Real-time liquidation risk monitoring",
        version="0.1.0",
        lifespan=lifespan,
    )
    
    state = get_state()
    if llm_client:
        state.set_llm_client(llm_client)
    
    @app.get("/", response_class=HTMLResponse)
    async def index():
        """Serve the dashboard HTML."""
        return get_dashboard_html()
    
    @app.get("/api/portfolio")
    async def get_portfolio():
        """Get current portfolio overview."""
        overview = await state.get_portfolio_overview()
        return JSONResponse(overview.to_dict())
    
    @app.get("/api/positions")
    async def get_positions():
        """Get current positions."""
        positions = await state.demo_connector.get_positions()
        return JSONResponse([p.to_dict() for p in positions])
    
    @app.get("/api/price-history")
    async def get_price_history():
        """Get price history for all symbols."""
        result = {}
        for symbol, points in state.price_history.items():
            result[symbol] = [p.to_dict() for p in points]
        return JSONResponse(result)
    
    @app.get("/api/alerts")
    async def get_alerts():
        """Get alert timeline."""
        return JSONResponse([a.to_dict() for a in state.alert_timeline])
    
    @app.get("/api/advice")
    async def get_advice():
        """Get advice cards."""
        return JSONResponse(state.advice_cards)
    
    @app.get("/api/audit-log")
    async def get_audit_log():
        """Get action audit log."""
        return JSONResponse(state.audit_log)
    
    @app.post("/api/simulate")
    async def start_simulation():
        """Start market simulation."""
        if state._running:
            return JSONResponse({"status": "already_running"})
        
        await state.start_simulation()
        return JSONResponse({"status": "started"})
    
    @app.post("/api/stop-simulation")
    async def stop_simulation():
        """Stop market simulation."""
        state.stop_simulation()
        return JSONResponse({"status": "stopped"})
    
    @app.post("/api/reset")
    async def reset_simulation():
        """Reset simulation state."""
        state.stop_simulation()
        state.demo_connector.reset_simulation()
        state.price_history.clear()
        state.alert_timeline.clear()
        state.advice_cards.clear()
        return JSONResponse({"status": "reset"})
    
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time updates."""
        await websocket.accept()
        state.connected_clients.add(websocket)
        
        try:
            overview = await state.get_portfolio_overview()
            await websocket.send_json({
                "type": "initial_state",
                "data": {
                    "portfolio": overview.to_dict(),
                    "price_history": {
                        symbol: [p.to_dict() for p in points]
                        for symbol, points in state.price_history.items()
                    },
                    "alerts": [a.to_dict() for a in state.alert_timeline],
                    "advice_cards": state.advice_cards,
                },
            })
            
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                    
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            state.connected_clients.discard(websocket)
    
    return app


class DashboardServer:
    """Dashboard server wrapper for integration with Telegram bot."""
    
    def __init__(
        self, 
        host: str = "127.0.0.1", 
        port: int = 8765,
        llm_client: Optional[MetaLLMClient] = None,
    ):
        self.host = host
        self.port = port
        self.llm_client = llm_client
        self.app = create_app(llm_client)
        self._server_task: Optional[asyncio.Task] = None
    
    async def start(self) -> None:
        """Start the dashboard server."""
        import uvicorn
        
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        
        self._server_task = asyncio.create_task(server.serve())
        logger.info(f"Dashboard started at http://{self.host}:{self.port}")
    
    async def stop(self) -> None:
        """Stop the dashboard server."""
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
    
    def get_state(self) -> DashboardState:
        """Get dashboard state for external integration."""
        return get_state()


def get_dashboard_html() -> str:
    """Return the dashboard HTML page."""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LiqGuard Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
    <style>
        :root {
            --bg-primary: #0f1419;
            --bg-secondary: #1a2332;
            --bg-card: #1e2a3a;
            --text-primary: #e7e9ea;
            --text-secondary: #8b98a5;
            --accent-blue: #1d9bf0;
            --accent-green: #00ba7c;
            --accent-yellow: #f7b731;
            --accent-orange: #ff922b;
            --accent-red: #f4212e;
            --border-color: #2f3a48;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
        }
        
        .header {
            background: var(--bg-secondary);
            padding: 1rem 2rem;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 {
            font-size: 1.5rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        .header h1 span {
            color: var(--accent-blue);
        }
        
        .connection-status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.875rem;
            color: var(--text-secondary);
        }
        
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--accent-red);
        }
        
        .status-dot.connected {
            background: var(--accent-green);
        }
        
        .container {
            max-width: 1600px;
            margin: 0 auto;
            padding: 1.5rem;
        }
        
        .controls {
            display: flex;
            gap: 1rem;
            margin-bottom: 1.5rem;
        }
        
        .btn {
            padding: 0.75rem 1.5rem;
            border: none;
            border-radius: 8px;
            font-size: 0.875rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .btn-primary {
            background: var(--accent-blue);
            color: white;
        }
        
        .btn-primary:hover {
            background: #1a8cd8;
        }
        
        .btn-primary:disabled {
            background: #3a4a5a;
            cursor: not-allowed;
        }
        
        .btn-danger {
            background: var(--accent-red);
            color: white;
        }
        
        .btn-danger:hover {
            background: #d91c27;
        }
        
        .btn-secondary {
            background: var(--bg-card);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
        }
        
        .btn-secondary:hover {
            background: var(--bg-secondary);
        }
        
        .grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1.5rem;
        }
        
        @media (max-width: 1200px) {
            .grid {
                grid-template-columns: 1fr;
            }
        }
        
        .card {
            background: var(--bg-card);
            border-radius: 12px;
            border: 1px solid var(--border-color);
            overflow: hidden;
        }
        
        .card-header {
            padding: 1rem 1.25rem;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .card-title {
            font-size: 1rem;
            font-weight: 600;
        }
        
        .card-body {
            padding: 1.25rem;
        }
        
        .chart-container {
            height: 300px;
            position: relative;
        }
        
        .risk-overview {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 1rem;
        }
        
        @media (max-width: 768px) {
            .risk-overview {
                grid-template-columns: repeat(2, 1fr);
            }
        }
        
        .stat-box {
            background: var(--bg-secondary);
            padding: 1rem;
            border-radius: 8px;
            text-align: center;
        }
        
        .stat-value {
            font-size: 1.5rem;
            font-weight: 700;
            margin-bottom: 0.25rem;
        }
        
        .stat-label {
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .risk-safe { color: var(--accent-green); }
        .risk-low { color: var(--accent-green); }
        .risk-moderate { color: var(--accent-yellow); }
        .risk-high { color: var(--accent-orange); }
        .risk-critical { color: var(--accent-red); }
        
        .positions-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .positions-table th,
        .positions-table td {
            padding: 0.75rem;
            text-align: left;
            border-bottom: 1px solid var(--border-color);
        }
        
        .positions-table th {
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 600;
        }
        
        .positions-table tbody tr:hover {
            background: var(--bg-secondary);
        }
        
        .badge {
            display: inline-block;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
        }
        
        .badge-long {
            background: rgba(0, 186, 124, 0.2);
            color: var(--accent-green);
        }
        
        .badge-short {
            background: rgba(244, 33, 46, 0.2);
            color: var(--accent-red);
        }
        
        .distance-bar {
            width: 100%;
            height: 6px;
            background: var(--bg-secondary);
            border-radius: 3px;
            overflow: hidden;
        }
        
        .distance-bar-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.3s, background 0.3s;
        }
        
        .timeline {
            max-height: 400px;
            overflow-y: auto;
        }
        
        .timeline-item {
            display: flex;
            gap: 1rem;
            padding: 0.75rem 0;
            border-bottom: 1px solid var(--border-color);
        }
        
        .timeline-item:last-child {
            border-bottom: none;
        }
        
        .timeline-icon {
            width: 32px;
            height: 32px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1rem;
            flex-shrink: 0;
        }
        
        .timeline-icon.warning {
            background: rgba(247, 183, 49, 0.2);
        }
        
        .timeline-icon.critical {
            background: rgba(255, 146, 43, 0.2);
        }
        
        .timeline-icon.emergency {
            background: rgba(244, 33, 46, 0.2);
        }
        
        .timeline-content {
            flex: 1;
        }
        
        .timeline-title {
            font-size: 0.875rem;
            font-weight: 500;
            margin-bottom: 0.25rem;
        }
        
        .timeline-time {
            font-size: 0.75rem;
            color: var(--text-secondary);
        }
        
        .advice-card {
            background: var(--bg-secondary);
            border-radius: 8px;
            padding: 1rem;
            margin-bottom: 1rem;
            border-left: 4px solid var(--accent-blue);
        }
        
        .advice-card:last-child {
            margin-bottom: 0;
        }
        
        .advice-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 0.5rem;
        }
        
        .advice-symbol {
            font-weight: 600;
        }
        
        .advice-level {
            font-size: 0.75rem;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
        }
        
        .advice-level.warning {
            background: rgba(247, 183, 49, 0.2);
            color: var(--accent-yellow);
        }
        
        .advice-level.critical {
            background: rgba(255, 146, 43, 0.2);
            color: var(--accent-orange);
        }
        
        .advice-level.emergency {
            background: rgba(244, 33, 46, 0.2);
            color: var(--accent-red);
        }
        
        .advice-text {
            font-size: 0.875rem;
            color: var(--text-secondary);
            line-height: 1.5;
        }
        
        .advice-action {
            margin-top: 0.75rem;
            padding-top: 0.75rem;
            border-top: 1px solid var(--border-color);
            font-size: 0.8125rem;
        }
        
        .advice-action strong {
            color: var(--accent-green);
        }
        
        .mock-badge {
            font-size: 0.625rem;
            background: var(--bg-card);
            color: var(--text-secondary);
            padding: 0.125rem 0.375rem;
            border-radius: 4px;
            margin-left: 0.5rem;
        }
        
        .audit-log {
            max-height: 300px;
            overflow-y: auto;
        }
        
        .audit-item {
            display: flex;
            justify-content: space-between;
            padding: 0.5rem 0;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.8125rem;
        }
        
        .audit-item:last-child {
            border-bottom: none;
        }
        
        .empty-state {
            text-align: center;
            padding: 2rem;
            color: var(--text-secondary);
        }
        
        .empty-state-icon {
            font-size: 2rem;
            margin-bottom: 0.5rem;
        }
        
        .full-width {
            grid-column: 1 / -1;
        }
    </style>
</head>
<body>
    <header class="header">
        <h1>🛡️ <span>LiqGuard</span> Dashboard</h1>
        <div class="connection-status">
            <div class="status-dot" id="statusDot"></div>
            <span id="statusText">Connecting...</span>
        </div>
    </header>
    
    <main class="container">
        <div class="controls">
            <button class="btn btn-primary" id="simulateBtn" onclick="startSimulation()">
                ▶️ Simulate Market
            </button>
            <button class="btn btn-danger" id="stopBtn" onclick="stopSimulation()" disabled>
                ⏹️ Stop
            </button>
            <button class="btn btn-secondary" onclick="resetSimulation()">
                🔄 Reset
            </button>
        </div>
        
        <div class="grid">
            <!-- Portfolio Overview -->
            <div class="card full-width">
                <div class="card-header">
                    <span class="card-title">📊 Portfolio Risk Overview</span>
                    <span id="riskLevel" class="badge"></span>
                </div>
                <div class="card-body">
                    <div class="risk-overview" id="riskOverview">
                        <div class="stat-box">
                            <div class="stat-value" id="totalPositions">0</div>
                            <div class="stat-label">Positions</div>
                        </div>
                        <div class="stat-box">
                            <div class="stat-value" id="totalNotional">$0</div>
                            <div class="stat-label">Notional Value</div>
                        </div>
                        <div class="stat-box">
                            <div class="stat-value" id="totalMargin">$0</div>
                            <div class="stat-label">Margin Used</div>
                        </div>
                        <div class="stat-box">
                            <div class="stat-value" id="minDistance">--%</div>
                            <div class="stat-label">Min Distance to Liq</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Price Chart -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">📈 Price vs Liquidation</span>
                </div>
                <div class="card-body">
                    <div class="chart-container">
                        <canvas id="priceChart"></canvas>
                    </div>
                </div>
            </div>
            
            <!-- Per-Position Risk -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">⚡ Position Risk Levels</span>
                </div>
                <div class="card-body">
                    <table class="positions-table">
                        <thead>
                            <tr>
                                <th>Symbol</th>
                                <th>Side</th>
                                <th>Leverage</th>
                                <th>Distance to Liq</th>
                            </tr>
                        </thead>
                        <tbody id="positionsTable">
                            <tr>
                                <td colspan="4" class="empty-state">No positions yet</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
            
            <!-- Alert Timeline -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">🔔 Alert Timeline</span>
                </div>
                <div class="card-body timeline" id="alertTimeline">
                    <div class="empty-state">
                        <div class="empty-state-icon">🔕</div>
                        <div>No alerts yet. Start simulation to see alerts.</div>
                    </div>
                </div>
            </div>
            
            <!-- Muse Advice Cards -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">💡 AI Advice</span>
                </div>
                <div class="card-body" id="adviceCards">
                    <div class="empty-state">
                        <div class="empty-state-icon">🤖</div>
                        <div>Advice will appear when positions are at risk.</div>
                    </div>
                </div>
            </div>
            
            <!-- Audit Log -->
            <div class="card full-width">
                <div class="card-header">
                    <span class="card-title">📋 Action Audit Log</span>
                </div>
                <div class="card-body audit-log" id="auditLog">
                    <div class="empty-state">
                        <div class="empty-state-icon">📝</div>
                        <div>No actions recorded yet.</div>
                    </div>
                </div>
            </div>
        </div>
    </main>
    
    <script>
        let ws = null;
        let priceChart = null;
        let isSimulating = false;
        
        const chartColors = {
            'BTCUSDT': { price: '#f7931a', liq: 'rgba(247, 147, 26, 0.3)' },
            'ETHUSDT': { price: '#627eea', liq: 'rgba(98, 126, 234, 0.3)' },
            'SOLUSDT': { price: '#14f195', liq: 'rgba(20, 241, 149, 0.3)' },
        };
        
        function initChart() {
            const ctx = document.getElementById('priceChart').getContext('2d');
            priceChart = new Chart(ctx, {
                type: 'line',
                data: {
                    datasets: []
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        mode: 'index',
                        intersect: false,
                    },
                    plugins: {
                        legend: {
                            labels: {
                                color: '#8b98a5',
                                usePointStyle: true,
                            }
                        },
                    },
                    scales: {
                        x: {
                            type: 'time',
                            time: {
                                unit: 'second',
                                displayFormats: {
                                    second: 'HH:mm:ss'
                                }
                            },
                            grid: {
                                color: 'rgba(47, 58, 72, 0.5)',
                            },
                            ticks: {
                                color: '#8b98a5',
                            }
                        },
                        y: {
                            grid: {
                                color: 'rgba(47, 58, 72, 0.5)',
                            },
                            ticks: {
                                color: '#8b98a5',
                                callback: function(value) {
                                    return '$' + value.toLocaleString();
                                }
                            }
                        }
                    }
                }
            });
        }
        
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
            
            ws.onopen = () => {
                document.getElementById('statusDot').classList.add('connected');
                document.getElementById('statusText').textContent = 'Connected';
            };
            
            ws.onclose = () => {
                document.getElementById('statusDot').classList.remove('connected');
                document.getElementById('statusText').textContent = 'Disconnected';
                setTimeout(connectWebSocket, 3000);
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };
            
            ws.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                handleMessage(msg);
            };
        }
        
        function handleMessage(msg) {
            switch (msg.type) {
                case 'initial_state':
                    updatePortfolio(msg.data.portfolio);
                    if (msg.data.price_history) {
                        Object.entries(msg.data.price_history).forEach(([symbol, points]) => {
                            points.forEach(point => addPricePoint(point));
                        });
                    }
                    if (msg.data.alerts) {
                        msg.data.alerts.forEach(alert => addAlert(alert));
                    }
                    if (msg.data.advice_cards) {
                        msg.data.advice_cards.forEach(card => addAdviceCard(card));
                    }
                    break;
                case 'portfolio_update':
                    updatePortfolio(msg.data);
                    break;
                case 'price_update':
                    addPricePoint(msg.data);
                    break;
                case 'alert':
                    addAlert(msg.data);
                    break;
                case 'advice_card':
                    addAdviceCard(msg.data);
                    break;
            }
        }
        
        function updatePortfolio(data) {
            if (!data) return;
            
            document.getElementById('totalPositions').textContent = data.summary?.total_positions || 0;
            document.getElementById('totalNotional').textContent = formatCurrency(data.summary?.total_notional_value || 0);
            document.getElementById('totalMargin').textContent = formatCurrency(data.summary?.total_margin_used || 0);
            
            const minDist = data.risk_metrics?.min_distance_to_liquidation;
            document.getElementById('minDistance').textContent = minDist != null ? minDist.toFixed(1) + '%' : '--%';
            document.getElementById('minDistance').className = 'stat-value ' + getDistanceClass(minDist);
            
            const riskLevel = data.risk_metrics?.overall_risk_level || 'safe';
            const riskBadge = document.getElementById('riskLevel');
            riskBadge.textContent = riskLevel.toUpperCase();
            riskBadge.className = 'badge risk-' + riskLevel;
            
            if (data.positions && data.positions.length > 0) {
                updatePositionsTable(data.positions);
            }
        }
        
        function updatePositionsTable(positions) {
            const tbody = document.getElementById('positionsTable');
            tbody.innerHTML = positions.map(pos => {
                const distance = pos.distance_to_liquidation_pct;
                const distClass = getDistanceClass(distance);
                const barColor = distClass === 'risk-safe' ? 'var(--accent-green)' :
                               distClass === 'risk-moderate' ? 'var(--accent-yellow)' :
                               distClass === 'risk-high' ? 'var(--accent-orange)' : 'var(--accent-red)';
                
                return `
                    <tr>
                        <td><strong>${pos.symbol}</strong></td>
                        <td><span class="badge badge-${pos.side}">${pos.side.toUpperCase()}</span></td>
                        <td>${pos.leverage}x</td>
                        <td>
                            <div style="display: flex; align-items: center; gap: 0.5rem;">
                                <span class="${distClass}" style="width: 50px;">${distance.toFixed(1)}%</span>
                                <div class="distance-bar" style="flex: 1;">
                                    <div class="distance-bar-fill" style="width: ${Math.min(distance, 100)}%; background: ${barColor};"></div>
                                </div>
                            </div>
                        </td>
                    </tr>
                `;
            }).join('');
        }
        
        function addPricePoint(point) {
            const symbol = point.symbol;
            const colors = chartColors[symbol] || { price: '#1d9bf0', liq: 'rgba(29, 155, 240, 0.3)' };
            
            let priceDataset = priceChart.data.datasets.find(d => d.label === `${symbol} Price`);
            let liqDataset = priceChart.data.datasets.find(d => d.label === `${symbol} Liq`);
            
            if (!priceDataset) {
                priceDataset = {
                    label: `${symbol} Price`,
                    data: [],
                    borderColor: colors.price,
                    backgroundColor: 'transparent',
                    borderWidth: 2,
                    tension: 0.1,
                    pointRadius: 0,
                };
                priceChart.data.datasets.push(priceDataset);
            }
            
            if (!liqDataset) {
                liqDataset = {
                    label: `${symbol} Liq`,
                    data: [],
                    borderColor: colors.liq,
                    backgroundColor: colors.liq,
                    borderWidth: 1,
                    borderDash: [5, 5],
                    tension: 0.1,
                    pointRadius: 0,
                    fill: false,
                };
                priceChart.data.datasets.push(liqDataset);
            }
            
            const timestamp = new Date(point.timestamp);
            priceDataset.data.push({ x: timestamp, y: point.price });
            liqDataset.data.push({ x: timestamp, y: point.liquidation_price });
            
            if (priceDataset.data.length > 100) {
                priceDataset.data.shift();
                liqDataset.data.shift();
            }
            
            priceChart.update('none');
        }
        
        function addAlert(alert) {
            const timeline = document.getElementById('alertTimeline');
            
            if (timeline.querySelector('.empty-state')) {
                timeline.innerHTML = '';
            }
            
            const iconEmoji = alert.level === 'emergency' ? '🚨' : 
                             alert.level === 'critical' ? '🔴' : '⚠️';
            
            const item = document.createElement('div');
            item.className = 'timeline-item';
            item.innerHTML = `
                <div class="timeline-icon ${alert.level}">${iconEmoji}</div>
                <div class="timeline-content">
                    <div class="timeline-title">${alert.message}</div>
                    <div class="timeline-time">${formatTime(alert.timestamp)}</div>
                </div>
            `;
            
            timeline.insertBefore(item, timeline.firstChild);
            
            if (timeline.children.length > 20) {
                timeline.removeChild(timeline.lastChild);
            }
        }
        
        function addAdviceCard(card) {
            const container = document.getElementById('adviceCards');
            
            if (container.querySelector('.empty-state')) {
                container.innerHTML = '';
            }
            
            const advice = card.advice;
            const actionText = advice?.margin_to_add_usd 
                ? `Add <strong>$${advice.margin_to_add_usd.toFixed(2)}</strong> margin to reach ${advice.target_distance_pct}% safety`
                : advice?.reduce_size_pct
                ? `Reduce position by <strong>${advice.reduce_size_pct.toFixed(1)}%</strong>`
                : 'Review position risk';
            
            const cardEl = document.createElement('div');
            cardEl.className = 'advice-card';
            cardEl.innerHTML = `
                <div class="advice-header">
                    <span class="advice-symbol">${card.symbol}${card.is_mock ? '<span class="mock-badge">MOCK</span>' : ''}</span>
                    <span class="advice-level ${card.level}">${card.level.toUpperCase()}</span>
                </div>
                <div class="advice-text">${card.explanation}</div>
                <div class="advice-action">${actionText}</div>
            `;
            
            container.insertBefore(cardEl, container.firstChild);
            
            if (container.children.length > 10) {
                container.removeChild(container.lastChild);
            }
        }
        
        async function startSimulation() {
            document.getElementById('simulateBtn').disabled = true;
            document.getElementById('stopBtn').disabled = false;
            isSimulating = true;
            
            try {
                await fetch('/api/simulate', { method: 'POST' });
            } catch (err) {
                console.error('Failed to start simulation:', err);
                document.getElementById('simulateBtn').disabled = false;
                document.getElementById('stopBtn').disabled = true;
            }
        }
        
        async function stopSimulation() {
            document.getElementById('simulateBtn').disabled = false;
            document.getElementById('stopBtn').disabled = true;
            isSimulating = false;
            
            try {
                await fetch('/api/stop-simulation', { method: 'POST' });
            } catch (err) {
                console.error('Failed to stop simulation:', err);
            }
        }
        
        async function resetSimulation() {
            await stopSimulation();
            
            try {
                await fetch('/api/reset', { method: 'POST' });
                
                priceChart.data.datasets = [];
                priceChart.update();
                
                document.getElementById('alertTimeline').innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon">🔕</div>
                        <div>No alerts yet. Start simulation to see alerts.</div>
                    </div>
                `;
                
                document.getElementById('adviceCards').innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon">🤖</div>
                        <div>Advice will appear when positions are at risk.</div>
                    </div>
                `;
                
                const initialData = await fetch('/api/portfolio').then(r => r.json());
                updatePortfolio(initialData);
                
            } catch (err) {
                console.error('Failed to reset:', err);
            }
        }
        
        function formatCurrency(value) {
            return '$' + value.toLocaleString(undefined, { maximumFractionDigits: 0 });
        }
        
        function formatTime(isoString) {
            return new Date(isoString).toLocaleTimeString();
        }
        
        function getDistanceClass(distance) {
            if (distance == null) return '';
            if (distance > 20) return 'risk-safe';
            if (distance > 10) return 'risk-moderate';
            if (distance > 5) return 'risk-high';
            return 'risk-critical';
        }
        
        document.addEventListener('DOMContentLoaded', () => {
            initChart();
            connectWebSocket();
        });
    </script>
</body>
</html>'''
