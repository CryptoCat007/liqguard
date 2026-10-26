"""
FastAPI web dashboard for LiqGuard (demo / judges view).

Read-only by design: the API exposes portfolio state, alerts, Muse advice and the
audit log, plus demo-simulation controls. There are no endpoints that bind
accounts, propose, confirm or execute trades, and no credentials are ever
included in responses.
"""

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from liqguard.llm import MetaLLMClient
from liqguard.services.simulation import MarketSimulation

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'"
)


class WebSocketHub:
    """Fan-out of simulation events to connected browsers."""

    def __init__(self):
        self.clients: set[WebSocket] = set()

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


def create_app(
    simulation: Optional[MarketSimulation] = None,
    llm_client: Optional[MetaLLMClient] = None,
) -> FastAPI:
    """Build the dashboard app around a (possibly shared) MarketSimulation."""
    sim = simulation or MarketSimulation(llm_client or MetaLLMClient())
    hub = WebSocketHub()
    sim.subscribe("web", hub.broadcast)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await sim.stop(source="shutdown")

    app = FastAPI(
        title="LiqGuard Dashboard",
        description="Read-only liquidation risk dashboard (demo data by default)",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.simulation = sim
    app.state.hub = hub

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health():
        snap = sim.snapshot()
        return {"status": "ok", "demo": True, "muse_mode": snap["muse_mode"], "state": snap["state"]}

    @app.get("/api/snapshot")
    async def snapshot():
        if sim.summary is None:
            await sim.refresh_summary()
        return JSONResponse(sim.snapshot())

    @app.get("/api/portfolio")
    async def portfolio():
        if sim.summary is None:
            await sim.refresh_summary()
        return JSONResponse({"overview": sim.overview.to_dict(), "summary": sim.summary})

    @app.get("/api/alerts")
    async def alerts():
        return JSONResponse(sim.alerts)

    @app.get("/api/advice")
    async def advice():
        return JSONResponse(sim.advice)

    @app.get("/api/audit")
    async def audit():
        return JSONResponse(sim.audit)

    @app.post("/api/simulate")
    async def simulate():
        started = await sim.start(source="web")
        return {"status": "started" if started else "already_running"}

    @app.post("/api/simulate/stop")
    async def stop():
        await sim.stop(source="web")
        return {"status": "stopped"}

    @app.post("/api/simulate/reset")
    async def reset():
        await sim.reset_and_notify()
        return {"status": "reset"}

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        if sim.summary is None:
            await sim.refresh_summary()
        await websocket.send_json({"type": "snapshot", "data": sim.snapshot()})
        hub.clients.add(websocket)
        try:
            while True:
                msg = await websocket.receive_text()
                if msg == "ping":
                    await websocket.send_text('{"type":"pong"}')
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(websocket)

    return app


class _EmbeddedServer(uvicorn.Server):
    """uvicorn.Server that leaves signal handling to the host process (the bot)."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class DashboardServer:
    """Runs the dashboard inside an existing asyncio loop (e.g. next to the bot)."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        simulation: Optional[MarketSimulation] = None,
        llm_client: Optional[MetaLLMClient] = None,
    ):
        self.host = host
        self.port = port
        self.app = create_app(simulation, llm_client)
        self.simulation: MarketSimulation = self.app.state.simulation
        self._server = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        self._server = _EmbeddedServer(config)
        self._task = asyncio.create_task(self._server.serve())
        logger.info(f"Dashboard running at http://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=5)
