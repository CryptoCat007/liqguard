"""
FastAPI dashboard.

- `/` + `/api/demo/*` + `/ws/demo`: public demo, clearly labelled DEMO DATA,
  backed by the public simulation (owner None) which never reaches Telegram.
- `/me` + `/api/me/*` + `/ws/me`: the signed-in user's own simulation,
  connected exchanges, positions and protective-action audit log. Every
  handler derives the user id from the verified session cookie only.

Read-only by design: no endpoint binds accounts or proposes, confirms or
executes trades, and no credential is ever serialized.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from liqguard.ai import MetaLLMClient
from liqguard.auth import SessionClaims
from liqguard.core import AppContext
from liqguard.risk import (
    PortfolioAnalyzer,
    RescueSimulator,
    portfolio_insights,
    position_protection,
)
from liqguard.simulation import SCENARIOS, MarketSimulation

from .auth import SessionAuth, register_auth_routes
from .security import apply_security_headers, require_csrf

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class AckRequest(BaseModel):
    ack_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class ScenarioRequest(BaseModel):
    scenario: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-z_]+$")

WS_POLICY_VIOLATION = 1008


class WebSocketHub:
    """Fan-out of one simulation's events to the browsers watching it."""

    def __init__(self, simulation: MarketSimulation):
        self.simulation = simulation
        self.clients: set[WebSocket] = set()
        self.unsubscribe = simulation.subscribe("web", self.broadcast)

    async def broadcast(self, event: dict) -> None:
        if not self.clients:
            return
        payload = json.dumps(event)
        dead = set()
        for ws in list(self.clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self.clients -= dead


class HubRegistry:
    def __init__(self):
        self._hubs: dict[int, WebSocketHub] = {}

    def attach(self, simulation: MarketSimulation):
        hub = WebSocketHub(simulation)
        self._hubs[id(simulation)] = hub

        def detach():
            hub.unsubscribe()
            self._hubs.pop(id(simulation), None)
        return detach

    def for_simulation(self, simulation: MarketSimulation) -> WebSocketHub:
        hub = self._hubs.get(id(simulation))
        if hub is None:
            self.attach(simulation)
            hub = self._hubs[id(simulation)]
        return hub


async def _ensure_summary(sim: MarketSimulation) -> None:
    if sim.summary is None:
        await sim.refresh_summary()


def create_app(
    ctx: Optional[AppContext] = None,
    *,
    demo_simulation: Optional[MarketSimulation] = None,
    llm_client: Optional[MetaLLMClient] = None,
) -> FastAPI:
    """Build the dashboard. Without `ctx` only the public demo is served (login disabled)."""
    if ctx is not None:
        public_sim = ctx.simulations.public
    else:
        public_sim = demo_simulation or MarketSimulation(llm_client or MetaLLMClient())
    if public_sim.owner_id is not None:
        raise ValueError("The public dashboard must use an owner-less simulation")

    hubs = HubRegistry()
    hubs.attach(public_sim)
    if ctx is not None:
        ctx.simulations.on_create(hubs.attach)
    auth = SessionAuth(ctx)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await public_sim.stop(source="shutdown")

    app = FastAPI(
        title="LiqGuard Dashboard",
        description="Read-only liquidation risk dashboard",
        version="0.3.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.public_simulation = public_sim
    app.state.ctx = ctx
    app.state.hubs = hubs

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        apply_security_headers(response, request.url.path)
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    register_auth_routes(app, ctx, auth)

    # ---------------------------------------------------------------- pages

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse(STATIC_DIR / "login.html")

    @app.get("/me", include_in_schema=False)
    async def me_page(claims: Optional[SessionClaims] = Depends(auth.optional)):
        if claims is None:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health():
        body = {
            "status": "ok",
            "demo": True,
            "muse_mode": public_sim.snapshot()["muse_mode"],
            "state": public_sim.state,
            "login_enabled": ctx is not None,
        }
        if ctx is not None:
            body["funding_feeds"] = ctx.market.status()
            body["wick_feeds"] = ctx.wicks.status()
        return body

    # ---------------------------------------------------------------- simulation routes

    def add_simulation_routes(prefix: str, resolve, guard):
        @app.get(f"{prefix}/snapshot")
        async def snapshot(sim: MarketSimulation = Depends(resolve)):
            await _ensure_summary(sim)
            return JSONResponse(sim.snapshot())

        @app.get(f"{prefix}/portfolio")
        async def portfolio(sim: MarketSimulation = Depends(resolve)):
            await _ensure_summary(sim)
            return JSONResponse({"overview": sim.overview.to_dict(), "summary": sim.summary})

        @app.get(f"{prefix}/alerts")
        async def alerts(sim: MarketSimulation = Depends(resolve)):
            return JSONResponse(sim.alerts)

        @app.get(f"{prefix}/advice")
        async def advice(sim: MarketSimulation = Depends(resolve)):
            return JSONResponse(sim.advice)

        @app.get(f"{prefix}/audit")
        async def audit(sim: MarketSimulation = Depends(resolve)):
            return JSONResponse(sim.audit)

        @app.get(f"{prefix}/rescue")
        async def rescue(symbol: str = Query(..., max_length=32), sim: MarketSimulation = Depends(resolve)):
            result = await sim.rescue_plan(symbol)
            if result is None:
                raise HTTPException(404, "No such position in this simulation")
            return JSONResponse(result)

        @app.post(f"{prefix}/simulate", dependencies=[Depends(require_csrf)])
        async def simulate(sim: MarketSimulation = Depends(guard)):
            started = await sim.start(source="web")
            return {"status": "started" if started else "already_running"}

        @app.post(f"{prefix}/simulate/stop", dependencies=[Depends(require_csrf)])
        async def stop(sim: MarketSimulation = Depends(guard)):
            await sim.stop(source="web")
            return {"status": "stopped"}

        @app.post(f"{prefix}/simulate/reset", dependencies=[Depends(require_csrf)])
        async def reset(sim: MarketSimulation = Depends(guard)):
            await sim.reset_and_notify()
            return {"status": "reset"}

        @app.get(f"{prefix}/insights")
        async def insights(sim: MarketSimulation = Depends(resolve)):
            """Priority triage, portfolio stress test, platform vs correlated liq, concentration and sizing."""
            return JSONResponse(sim.insights().to_dict())

        @app.post(f"{prefix}/scenario", dependencies=[Depends(require_csrf)])
        async def scenario(body: ScenarioRequest, sim: MarketSimulation = Depends(guard)):
            if body.scenario not in SCENARIOS:
                raise HTTPException(404, "Unknown scenario")
            if not await sim.switch_scenario(body.scenario, source="web"):
                raise HTTPException(409, "Stop the running simulation first")
            return {"status": "switched", "scenario": sim.scenario}

        @app.post(f"{prefix}/ack", dependencies=[Depends(require_csrf)])
        async def ack(body: AckRequest, sim: MarketSimulation = Depends(resolve)):
            """"I'm awake": stops escalating reminders for one alert of this simulation."""
            event = await sim.acknowledge(body.ack_id, source="dashboard")
            if event is None:
                raise HTTPException(404, "No pending reminder with that id")
            return {"status": "acknowledged", "symbol": event["symbol"]}

    async def public_simulation() -> MarketSimulation:
        return public_sim

    async def my_simulation(claims: SessionClaims = Depends(auth.required)) -> MarketSimulation:
        return ctx.simulations.get_or_create(claims.user_id)

    async def my_simulation_configured(claims: SessionClaims = Depends(auth.required)) -> MarketSimulation:
        sim = ctx.simulations.get_or_create(claims.user_id)
        if not sim.is_running:
            user = await ctx.storage.get_user(claims.user_id)
            if user:
                sim.configure(user.threshold, user.language)
        return sim

    add_simulation_routes("/api/demo", public_simulation, public_simulation)
    if ctx is not None:
        add_simulation_routes("/api/me", my_simulation, my_simulation_configured)

        @app.get("/api/me/session")
        async def my_session(claims: SessionClaims = Depends(auth.required)):
            return {"user_id": claims.user_id, "username": claims.username, "expires_at": claims.expires_at}

        @app.get("/api/me/account")
        async def my_account(claims: SessionClaims = Depends(auth.required)):
            user = await ctx.storage.get_user(claims.user_id)
            if user is None:
                return {"bindings": [], "positions": None, "label": "", "actions": [], "demo_mode": False}
            snapshot = await ctx.positions.snapshot_for(user)
            positions, label = snapshot.positions, snapshot.label
            overview = (
                PortfolioAnalyzer(user.threshold, snapshot.context).analyze_portfolio(positions).to_dict()
                if positions else None
            )
            protection = [position_protection(p, snapshot.context, user.threshold).to_dict() for p in positions]
            insights = portfolio_insights(positions, snapshot.context, user.threshold).to_dict() if positions else None
            return {
                "bindings": [b.to_dict() for b in user.bindings],
                "demo_mode": user.is_demo_mode,
                "label": label,
                "positions": overview,
                "protection": protection,
                "insights": insights,
                "escalations": [a.to_dict() for a in ctx.escalations.pending_for(claims.user_id) if a.acknowledged_at is None],
                "actions": await ctx.storage.list_audit(claims.user_id, limit=50),
                "protect_enabled": ctx.actions.is_enabled(),
            }

        @app.post("/api/me/live/ack", dependencies=[Depends(require_csrf)])
        async def my_live_ack(body: AckRequest, claims: SessionClaims = Depends(auth.required)):
            """Acknowledge one of the signed-in user's own live escalations; other users' ids are indistinguishable from unknown."""
            ack = ctx.escalations.acknowledge(claims.user_id, body.ack_id, time.time())
            if ack is None:
                raise HTTPException(404, "No pending reminder with that id")
            return {"status": "acknowledged", "symbol": ack.symbol}

        @app.get("/api/me/rescue/live")
        async def my_live_rescue(
            symbol: str = Query(..., max_length=32),
            venue: str = Query(..., max_length=16),
            claims: SessionClaims = Depends(auth.required),
        ):
            """Rescue options for one of the signed-in user's own positions (their demo book or exchange bindings)."""
            user = await ctx.storage.get_user(claims.user_id)
            if user is None:
                raise HTTPException(404, "No such position")
            snapshot = await ctx.positions.snapshot_for(user)
            position = next((p for p in snapshot.positions if p.symbol == symbol and p.venue.value == venue), None)
            if position is None:
                raise HTTPException(404, "No such position")
            context = snapshot.context
            plan = RescueSimulator(context.fees, user.threshold.safety_target_pct).simulate(
                position, context.account_for(position), context.funding_for(position), context.now
            )
            explanation = await ctx.llm.explain_rescue(plan, user.language)
            return JSONResponse({"plan": plan.to_dict(), "explanation": explanation})

    # ---------------------------------------------------------------- websockets

    async def serve_ws(websocket: WebSocket, sim: MarketSimulation) -> None:
        await websocket.accept()
        await _ensure_summary(sim)
        await websocket.send_json({"type": "snapshot", "data": sim.snapshot()})
        hub = hubs.for_simulation(sim)
        hub.clients.add(websocket)
        try:
            while True:
                if await websocket.receive_text() == "ping":
                    await websocket.send_text('{"type":"pong"}')
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(websocket)

    @app.websocket("/ws/demo")
    async def ws_demo(websocket: WebSocket):
        await serve_ws(websocket, public_sim)

    @app.websocket("/ws/me")
    async def ws_me(websocket: WebSocket):
        claims = await auth.websocket(websocket)
        if claims is None:
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        await serve_ws(websocket, ctx.simulations.get_or_create(claims.user_id))

    return app


class _EmbeddedServer(uvicorn.Server):
    """uvicorn.Server that leaves signal handling to the host process (the bot)."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class DashboardServer:
    """Runs the dashboard inside an existing asyncio loop (e.g. next to the bot)."""

    def __init__(self, ctx: AppContext, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self.app = create_app(ctx)
        self._server = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning", access_log=False)
        self._server = _EmbeddedServer(config)
        self._task = asyncio.create_task(self._server.serve())

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=5)
