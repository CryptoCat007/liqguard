"""
FastAPI dashboard.

- `/` + `/api/demo/*` + `/ws/demo`: public demo, clearly labelled DEMO DATA,
  backed by the public simulation (owner None) which never reaches Telegram.
- `/me` + `/api/me/*` + `/ws/me`: the signed-in user's own simulation,
  connected exchanges, positions and protective-action audit log. Every
  handler derives the user id from the verified session cookie only.

Read-only by design: no endpoint proposes, confirms or executes trades, and
no credential is ever serialized. The only write paths are the signed-in
user's own alert settings and self-serve binding (`/api/me/bindings/*`),
which accepts read-only Binance keys (trade/withdraw permission rejected) or
a Hyperliquid wallet address, stores them encrypted per user and answers
with masked hints only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from liqguard.ai import MetaLLMClient
from liqguard.auth import SessionClaims
from liqguard.core import AppContext, MuseService
from liqguard.core.binding import BindingService, security_note
from liqguard.core.position_tools import TOOLS, report
from liqguard.core.positions import BOOK_LABEL
from liqguard.core.user_settings import apply_setting, settings_dict
from liqguard.i18n import localize, normalize, t, web_catalog
from liqguard.models import Position, User
from liqguard.payments import PayPalClient, PayPalError
from liqguard.payments.webhooks import (
    WebhookHandler,
    extract_webhook_headers,
    fulfill_rescue_order,
    parse_telegram_id,
)
from liqguard.risk import (
    RescueSimulator,
    position_protection,
)
from liqguard.simulation import SCENARIOS, MarketSimulation

from .auth import SessionAuth, register_auth_routes
from .pages import remember_language, render_login, render_payment_received, request_language
from .security import apply_security_headers, require_csrf

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_PRECOMPUTE_TOP = 4
"""Positions of the public demo whose rescue / stop-loss texts are fetched at startup (2 requests at a time)."""


class AckRequest(BaseModel):
    ack_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class ScenarioRequest(BaseModel):
    scenario: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-z_]+$")


class LanguageRequest(BaseModel):
    language: str = Field(..., pattern=r"^(en|zh-Hant)$")


class SettingRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=16, pattern=r"^[a-z]+$")
    value: str = Field(..., min_length=1, max_length=8, pattern=r"^[a-z0-9.\-]+$")


class AgentCheckoutRequest(BaseModel):
    text: str = Field("", max_length=400)
    product: Optional[str] = Field(None, pattern=r"^(pro|rescue|auto)?$")


class UnbindRequest(BaseModel):
    venue: str = Field(..., pattern=r"^(binance|hyperliquid)$")
    network: str = Field("mainnet", pattern=r"^(mainnet|testnet)$")
    confirm: bool = False


TOOL_PATTERN = "^(" + "|".join(TOOLS) + ")$"


async def _credential_fields(request: Request, *names: str) -> list[str]:
    """Read credential strings by hand: FastAPI's 422 errors would echo the submitted value back."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "請以 JSON 傳送")
    if not isinstance(body, dict):
        raise HTTPException(400, "請以 JSON 傳送")
    values = []
    for name in names:
        value = body.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise HTTPException(400, "欄位缺少或格式不正確")
        values.append(value)
    return values

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
        payload = json.dumps(localize(self.simulation.language, event))
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


def _localized(sim: MarketSimulation, data) -> JSONResponse:
    """Every `Msg` in `data` rendered in the simulation's language (its Muse text is already in it)."""
    return JSONResponse(localize(sim.language, data))


async def _ensure_summary(sim: MarketSimulation) -> None:
    sim.ensure_summary()


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
        public_sim = demo_simulation or MarketSimulation(llm_client or MetaLLMClient(), open_at_warning=True)
    if public_sim.owner_id is not None:
        raise ValueError("The public dashboard must use an owner-less simulation")

    hubs = HubRegistry()
    hubs.attach(public_sim)
    public_sims: dict[str, MarketSimulation] = {public_sim.language: public_sim}

    def public_for(lang: Optional[str]) -> MarketSimulation:
        """The public demo narrated in `lang`: one independent run per language, so Muse text matches the page."""
        code = normalize(lang) if lang else public_sim.language
        sim = public_sims.get(code)
        if sim is None:
            sim = public_sims[code] = public_sim.sibling(code)
            sim.adopt_follow_ups(public_sim)
            hubs.attach(sim)
        return sim
    if ctx is not None:
        ctx.simulations.on_create(hubs.attach)
    auth = SessionAuth(ctx)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        precompute = asyncio.create_task(public_sim.precompute_top(PUBLIC_PRECOMPUTE_TOP))
        yield
        precompute.cancel()
        for sim in list(public_sims.values()):
            await sim.stop(source="shutdown")

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
        if request.method == "GET" and not request.url.path.startswith(("/api/", "/ws/", "/static/")):
            remember_language(response, request)
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    register_auth_routes(app, ctx, auth)

    # ---------------------------------------------------------------- pages

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request, error: Optional[str] = None, notice: Optional[str] = None):
        settings = ctx.settings if ctx is not None else None
        return HTMLResponse(render_login(request_language(request), settings, error, notice))

    @app.get("/me", include_in_schema=False)
    async def me_page(claims: Optional[SessionClaims] = Depends(auth.optional)):
        if claims is None:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/i18n/{lang}")
    async def web_strings(lang: str = PathParam(..., max_length=16)):
        """Dashboard UI strings (the `web.*` locale table) for the EN / 中文 toggle."""
        code = normalize(lang)
        return {"lang": code, "strings": web_catalog(code)}

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """Liveness for Docker / reverse proxies: no secrets, no user data; 503 if the database is unreachable."""
        if ctx is not None:
            try:
                async with ctx.storage.db.connect() as db:
                    await db.execute("SELECT 1")
            except Exception as e:
                logger.warning("Health check: database unreachable (%s)", type(e).__name__)
                return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
        return {"status": "ok"}

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
            body["paypal"] = ctx.settings.paypal_enabled
        return body

    @app.get("/api/billing/status")
    async def billing_status_public():
        """Whether PayPal Pro checkout is configured. Always safe; no secrets."""
        enabled = bool(ctx is not None and ctx.settings.paypal_enabled)
        plan = ctx.settings.paypal_plan_id if enabled else None
        return {"enabled": enabled, "plan_id": plan, "env": (ctx.settings.paypal_env if enabled else "sandbox")}

    @app.get("/api/grid-filter")
    async def grid_filter(q: str = Query("", max_length=200)):
        """Natural-language AG Grid filter. Muse when live; deterministic rules otherwise."""
        from liqguard.payments.agent import parse_grid_filter

        spec = parse_grid_filter(q)
        spec.setdefault("source", "rules")
        llm = ctx.llm if ctx is not None else None
        if llm is not None and not llm.is_mock and q.strip():
            try:
                raw = await asyncio.wait_for(
                    llm._chat(
                        "grid-filter",
                        [
                            {
                                "role": "system",
                                "content": (
                                    "Return JSON with optional keys max_distance_pct (number), "
                                    "side (long|short), level (emergency|critical|warning|safe), symbol. "
                                    "Omit keys you cannot infer."
                                ),
                            },
                            {"role": "user", "content": q},
                        ],
                        json_mode=True,
                        temperature=0,
                        effort="low",
                    ),
                    2.0,
                )
                if raw:
                    data = json.loads(raw)
                    for key in ("max_distance_pct", "side", "level", "symbol"):
                        if data.get(key) not in (None, ""):
                            spec[key] = data[key]
                    spec["source"] = "muse"
            except Exception:
                spec["source"] = "rules"
        return spec

    # ---------------------------------------------------------------- Muse

    shared_muse = ctx.muse if ctx is not None else public_sim.muse
    app.state.muse = shared_muse

    def muse_for(sim: MarketSimulation) -> Optional[MuseService]:
        """The Muse service Telegram uses too (`ctx.muse`); its late answers reach viewers through `sim`'s hub."""
        if sim.muse is None:
            return None
        if shared_muse is not None and sim.llm is shared_muse.llm:
            return shared_muse
        return sim.muse

    def follow_up(viewers: "MarketSimulation | Callable[[], MarketSimulation]", pending, event_type: str, build,
                  fallback, lang: str) -> str:
        """
        Deliver Muse's answer to a request that returned `muse_pending: true` as
        exactly one `event_type` event (Muse's text, or the rule text with
        `muse_failed: true`) carrying the response's `request_id`. Public demo
        answers reach every public viewer whatever their language (tagged
        `lang`); late viewers get it replayed when they connect.
        """
        request_id = uuid.uuid4().hex[:16]
        sim = viewers() if callable(viewers) else viewers
        public = any(sim is s for s in public_sims.values())
        sim.deliver_later(pending, event_type, lambda answer: {**build(answer), "request_id": request_id, "lang": lang},
                          fallback=fallback, targets=(lambda: list(public_sims.values())) if public else None)
        return request_id

    async def rescue_body(muse: MuseService, sim: "MarketSimulation | Callable[[], MarketSimulation]", plan, position: Position, lang: str,
                          wait: bool, scope: str = "sim", user_id: Optional[int] = None) -> dict:
        if wait:
            explanation = await muse.explain_rescue(plan, lang)
        else:
            instant = muse.rescue(plan, lang)
            explanation = instant.result
        upgrade = None
        if user_id is not None and ctx is not None and ctx.entitlements is not None:
            upgrade = await ctx.entitlements.upgrade_hint(
                user_id, lang, high_risk=plan.current.distance_pct <= 20 if plan.current else False,
            )
        if wait:
            return {"plan": plan.to_dict(), "explanation": explanation, "muse_pending": False, "upgrade_hint": upgrade}
        body = {"plan": plan.to_dict(), "explanation": explanation, "muse_pending": instant.pending is not None,
                "upgrade_hint": upgrade}
        if instant.pending is not None:
            key = {"scope": scope, "symbol": position.symbol, "venue": position.venue.value}
            body["request_id"] = follow_up(sim, instant.pending, "rescue_muse",
                                           lambda answer: localize(lang, {**key, "explanation": answer}), instant.result, lang)
        return body

    def quick_report(muse: Optional[MuseService], sim: "MarketSimulation | Callable[[], MarketSimulation]", tool: str, position: Position, context,
                     protection, plan, lang: str, demo: bool, scope: str = "sim") -> dict:
        """The Telegram quick report with Muse's line (rescue / stop); Muse's live line follows as `report_muse`."""
        note = muse.tool_note(tool, lang, plan=plan, check=protection.stop) if muse is not None else None

        def render(line: Optional[dict]) -> dict:
            return localize(lang, report(tool, position, context, protection.to_dict(), plan.to_dict() if plan else None,
                                         demo=demo, lang=lang, muse=line))

        body = render(note.result if note else None)
        pending = note is not None and note.pending is not None
        out = {**body, "muse_pending": pending}
        if pending:
            key = {"scope": scope, "tool": tool, "symbol": position.symbol, "venue": position.venue.value}
            out["request_id"] = follow_up(sim, note.pending, "report_muse", lambda answer: {**key, "report": render(answer)},
                                          note.result, lang)
        return out

    # ---------------------------------------------------------------- simulation routes

    def add_simulation_routes(prefix: str, resolve, guard):
        @app.get(f"{prefix}/snapshot")
        async def snapshot(sim: MarketSimulation = Depends(resolve)):
            await _ensure_summary(sim)
            return _localized(sim, sim.snapshot())

        @app.get(f"{prefix}/portfolio")
        async def portfolio(sim: MarketSimulation = Depends(resolve)):
            await _ensure_summary(sim)
            return _localized(sim, {"overview": sim.overview.to_dict(), "summary": sim.summary})

        @app.get(f"{prefix}/alerts")
        async def alerts(sim: MarketSimulation = Depends(resolve)):
            return _localized(sim, sim.alerts)

        @app.get(f"{prefix}/advice")
        async def advice(sim: MarketSimulation = Depends(resolve)):
            return _localized(sim, sim.advice)

        @app.get(f"{prefix}/audit")
        async def audit(sim: MarketSimulation = Depends(resolve)):
            return _localized(sim, sim.audit)

        @app.get(f"{prefix}/rescue")
        async def rescue(symbol: str = Query(..., max_length=32), wait: bool = False,
                         sim: MarketSimulation = Depends(resolve)):
            """
            The cached or deterministic explanation at once. While Muse is still
            answering, `muse_pending` is true and its text follows as a
            `rescue_muse` WebSocket event (or here, with `wait=1`).
            """
            position = next((p for p in sim.positions if p.symbol == symbol), None)
            plan = sim.build_rescue_plan(symbol) if position is not None else None
            if plan is None:
                raise HTTPException(404, "No such position in this simulation")
            muse = muse_for(sim)
            if muse is None:
                return _localized(sim, await sim.rescue_plan(symbol, wait=wait))
            body = await rescue_body(muse, sim, plan, position, sim.language, wait)
            return _localized(sim, body)

        @app.get(f"{prefix}/position-tool")
        async def position_tool(symbol: str = Query(..., max_length=32), tool: str = Query(..., pattern=TOOL_PATTERN),
                                lang: Optional[str] = Query(None, max_length=16),
                                sim: MarketSimulation = Depends(resolve)):
            """
            Rescue / wick / stop / funding report for one simulated position (same
            text as the Telegram buttons). Muse's line follows as `report_muse`.
            """
            position = next((p for p in sim.positions if p.symbol == symbol), None)
            if position is None:
                raise HTTPException(404, "No such position in this simulation")
            context = sim.risk_context()
            protection = sim.protection(position, context)
            plan = sim.build_rescue_plan(symbol) if tool == "rescue" else None
            code = normalize(lang) if lang else sim.language
            return JSONResponse(quick_report(muse_for(sim), sim, tool, position, context, protection, plan, code, demo=True))

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
            return _localized(sim, sim.insights().to_dict())

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

    async def public_simulation(lang: Optional[str] = Query(None, max_length=16)) -> MarketSimulation:
        return public_for(lang)

    async def my_simulation(claims: SessionClaims = Depends(auth.required)) -> MarketSimulation:
        return ctx.simulations.get_or_create(claims.user_id)

    async def my_simulation_configured(claims: SessionClaims = Depends(auth.required)) -> MarketSimulation:
        sim = ctx.simulations.get_or_create(claims.user_id)
        if not sim.is_running:
            user = await ctx.storage.get_user(claims.user_id)
            if user:
                sim.configure(user.threshold, user.language)
        return sim

    async def watched_simulation(user_id: int) -> MarketSimulation:
        """The user's own simulation narrated in their saved language (a finished run is kept as is)."""
        sim = ctx.simulations.get_or_create(user_id)
        user = await ctx.storage.get_user(user_id)
        if user is not None and normalize(user.language) != sim.language:
            sim.set_language(user.language)
            sim.summary = None
        return sim

    add_simulation_routes("/api/demo", public_simulation, public_simulation)
    if ctx is not None:
        add_simulation_routes("/api/me", my_simulation, my_simulation_configured)
        app.state.binding = BindingService(
            ctx.storage, default_threshold=ctx.settings.default_threshold, entitlements=ctx.entitlements,
        )

        async def consume_muse_or_402(user_id: int) -> None:
            """Check the free-tier Muse cap before a user-initiated Muse call; record after the caller returns."""
            e = ctx.entitlements
            if e is None or not e.paypal_enabled:
                return
            allowed, used, limit = await e.check_muse_limit(user_id)
            if allowed:
                return
            await ctx.storage.save_agent_action(user_id, {
                "tool": "muse_limit", "product": "muse", "source": "limit",
                "status": "muse_limit", "message": f"{used}/{limit}",
            })
            raise HTTPException(402, {
                "code": "muse_limit",
                "used": used,
                "limit": limit,
                "reset": e.reset_label(),
            })

        async def record_muse(user_id: int) -> None:
            e = ctx.entitlements
            if e is not None and e.paypal_enabled:
                await e.record_muse_usage(user_id)

        async def own_user(claims: SessionClaims):
            user = await ctx.storage.get_user(claims.user_id)
            if user is None:
                user = User(telegram_id=claims.user_id, username=claims.username, threshold=ctx.settings.default_threshold())
                await ctx.storage.save_user(user)
            return user

        @app.get("/api/me/settings")
        async def my_settings(claims: SessionClaims = Depends(auth.required)):
            """Same settings as Telegram 🔔 通知設定."""
            user = await own_user(claims)
            return settings_dict(user.threshold, user.language)

        @app.post("/api/me/settings", dependencies=[Depends(require_csrf)])
        async def update_my_settings(body: SettingRequest, claims: SessionClaims = Depends(auth.required)):
            user = await own_user(claims)
            updated = apply_setting(user.threshold, body.key, body.value)
            if updated is None:
                raise HTTPException(400, t(user.language, "settings.invalid"))
            await ctx.storage.update_threshold(claims.user_id, updated)
            sim = ctx.simulations.get(claims.user_id)
            if sim is not None and not sim.is_running:
                sim.configure(updated, user.language)
            return settings_dict(updated, user.language)

        @app.get("/api/me/language")
        async def my_language(claims: SessionClaims = Depends(auth.required)):
            user = await own_user(claims)
            return {"language": normalize(user.language), "explicit": user.language_explicit}

        @app.post("/api/me/language", dependencies=[Depends(require_csrf)])
        async def set_my_language(body: LanguageRequest, claims: SessionClaims = Depends(auth.required)):
            """Same per-user choice as Telegram 🌐 / /lang: bot messages, alerts and this dashboard follow it."""
            await own_user(claims)
            lang = normalize(body.language)
            await ctx.storage.set_language(claims.user_id, lang, explicit=True)
            sim = ctx.simulations.get(claims.user_id)
            if sim is not None:
                sim.set_language(lang)
            return {"language": lang, "explicit": True}

        @app.get("/api/me/bindings")
        async def my_bindings(claims: SessionClaims = Depends(auth.required)):
            user = await own_user(claims)
            return {"bindings": await app.state.binding.list(claims.user_id), "security_note": security_note(user.language)}

        @app.post("/api/me/bindings/binance", dependencies=[Depends(require_csrf)])
        async def bind_binance(request: Request, claims: SessionClaims = Depends(auth.required)):
            """Read-only Binance key: checked with Binance, trade/withdraw permission rejected, stored encrypted."""
            api_key, api_secret = await _credential_fields(request, "api_key", "api_secret")
            result = await app.state.binding.bind_binance(claims.user_id, api_key, api_secret)
            code = 200 if result.ok else (402 if result.code == "account_limit" else 400)
            return JSONResponse(result.to_dict(), status_code=code)

        @app.post("/api/me/bindings/hyperliquid", dependencies=[Depends(require_csrf)])
        async def bind_hyperliquid(request: Request, claims: SessionClaims = Depends(auth.required)):
            (address,) = await _credential_fields(request, "address")
            result = await app.state.binding.bind_hyperliquid(claims.user_id, address)
            code = 200 if result.ok else (402 if result.code == "account_limit" else 400)
            return JSONResponse(result.to_dict(), status_code=code)

        @app.post("/api/me/bindings/unbind", dependencies=[Depends(require_csrf)])
        async def unbind(body: UnbindRequest, claims: SessionClaims = Depends(auth.required)):
            if not body.confirm:
                raise HTTPException(400, "請確認後再解除綁定")
            if not await app.state.binding.unbind(claims.user_id, body.venue, body.network):
                raise HTTPException(404, "這個綁定不存在")
            return {"status": "unbound", "venue": body.venue, "network": body.network}

        @app.post("/api/me/live/snooze", dependencies=[Depends(require_csrf)])
        async def my_live_snooze(body: AckRequest, claims: SessionClaims = Depends(auth.required)):
            """😴 Pause reminders for 30 minutes on one of the signed-in user's own live alerts."""
            ack = ctx.escalations.snooze(claims.user_id, body.ack_id, time.time() + 30 * 60)
            if ack is None:
                raise HTTPException(404, "No pending reminder with that id")
            return {"status": "snoozed", "symbol": ack.symbol, "minutes": 30}

        @app.get("/api/me/position-tool/live")
        async def my_live_position_tool(
            symbol: str = Query(..., max_length=32),
            venue: str = Query(..., max_length=16),
            tool: str = Query(..., pattern=TOOL_PATTERN),
            claims: SessionClaims = Depends(auth.required),
        ):
            """Quick actions for one of the signed-in user's own positions (demo book or exchange bindings)."""
            user = await ctx.storage.get_user(claims.user_id)
            if user is None:
                raise HTTPException(404, "No such position")
            snapshot = await ctx.positions.snapshot_for(user)
            position = next((p for p in snapshot.positions if p.symbol == symbol and p.venue.value == venue), None)
            if position is None:
                raise HTTPException(404, "No such position")
            c = snapshot.context
            if tool == "rescue" and ctx.entitlements is not None and not await ctx.entitlements.can_use_full_rescue(claims.user_id):
                raise HTTPException(402, {
                    "code": "rescue_locked",
                    "message": t(normalize(user.language), "pro.upgrade.rescue_locked"),
                })
            await consume_muse_or_402(claims.user_id)
            protection = position_protection(position, c, user.threshold)
            plan = None
            if tool == "rescue":
                plan = RescueSimulator(c.fees, user.threshold.safety_target_pct).simulate(
                    position, c.account_for(position), c.funding_for(position), c.now)
            body = quick_report(ctx.muse, lambda: ctx.simulations.get_or_create(claims.user_id), tool, position, c,
                                protection, plan, normalize(user.language), demo=venue == "demo",
                                scope="live")
            await record_muse(claims.user_id)
            return JSONResponse(body)

        @app.get("/api/me/session")
        async def my_session(claims: SessionClaims = Depends(auth.required)):
            return {"user_id": claims.user_id, "username": claims.username, "expires_at": claims.expires_at}

        account_views: dict[int, int] = {}

        def account_muse(user_id: int, user: User, snapshot, book, lang: str) -> dict:
            """
            The portfolio summary and Muse's suggestion, from the same service and
            inputs as Telegram `/risk` and tier alerts. Muse's late answers come as
            `account_muse` events tagged with `view`, so an older view's answer never
            replaces a newer one.
            """
            view = account_views[user_id] = account_views.get(user_id, 0) + 1
            suggestion = ctx.muse.suggestion(user, snapshot, book.insights, lang)
            def sim() -> MarketSimulation:
                return ctx.simulations.get_or_create(user_id)

            if book.summary.pending is not None:
                follow_up(sim, book.summary.pending, "account_muse",
                          lambda answer: localize(lang, {"view": view, "summary": answer}), book.summary.result, lang)
            if suggestion is not None and suggestion.explanation.pending is not None:
                follow_up(sim, suggestion.explanation.pending, "account_muse",
                          lambda answer: localize(lang, {"view": view, "suggestion": suggestion.to_dict(
                              None if answer is suggestion.explanation.result else answer)}),
                          suggestion.explanation.result, lang)
            return {
                "view": view,
                "mode": ctx.muse.mode,
                "summary": book.summary.result,
                "suggestion": suggestion.to_dict() if suggestion else None,
                "summary_pending": book.summary.pending is not None,
                "suggestion_pending": suggestion is not None and suggestion.explanation.pending is not None,
            }

        @app.get("/api/me/account")
        async def my_account(claims: SessionClaims = Depends(auth.required)):
            user = await ctx.storage.get_user(claims.user_id)
            if user is None:
                return {"bindings": [], "positions": None, "label": "", "actions": [], "demo_mode": False}
            snapshot = await ctx.positions.snapshot_for(user)
            positions, label = snapshot.positions, snapshot.label
            lang = normalize(user.language)
            protection = [position_protection(p, snapshot.context, user.threshold).to_dict() for p in positions]
            overview = insights = muse = None
            if positions:
                await consume_muse_or_402(claims.user_id)
                book = ctx.muse.portfolio(user, snapshot, lang)
                overview, insights = book.overview.to_dict(), book.insights.to_dict()
                muse = account_muse(claims.user_id, user, snapshot, book, lang)
                ctx.muse.warm_book(user, snapshot, book.insights, lang)
                await record_muse(claims.user_id)
            return localize(lang, {
                "bindings": [b.to_dict() for b in user.bindings],
                "demo_mode": user.is_demo_mode,
                "label": t(lang, "web." + BOOK_LABEL[label].replace(".", "_")) if label in BOOK_LABEL else label,
                "positions": overview,
                "protection": protection,
                "insights": insights,
                "muse": muse,
                "escalations": [a.to_dict() for a in ctx.escalations.pending_for(claims.user_id) if a.acknowledged_at is None],
                "actions": await ctx.storage.list_audit(claims.user_id, limit=50),
                "protect_enabled": ctx.actions.is_enabled(),
                "billing": (await ctx.entitlements.get_status(claims.user_id)).to_dict() if ctx.entitlements else {"tier": "free", "is_pro": False},
            })

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
            wait: bool = False,
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
            lang = normalize(user.language)
            if ctx.entitlements is not None and not await ctx.entitlements.can_use_full_rescue(claims.user_id):
                raise HTTPException(402, {
                    "code": "rescue_locked",
                    "message": t(lang, "pro.upgrade.rescue_locked"),
                })
            await consume_muse_or_402(claims.user_id)
            body = await rescue_body(ctx.muse, lambda: ctx.simulations.get_or_create(claims.user_id), plan, position, lang,
                                     wait, scope="live", user_id=claims.user_id)
            await record_muse(claims.user_id)
            return JSONResponse(localize(lang, body))

        # ---------------------------------------------------------------- billing & subscriptions

        paypal = None
        entitlements = ctx.entitlements
        if ctx.settings.paypal_enabled:
            paypal = PayPalClient(
                ctx.settings.paypal_client_id,
                ctx.settings.paypal_client_secret,
                ctx.settings.paypal_api_base,
                ctx.settings.paypal_webhook_id,
            )

        async def _billing_notify(telegram_id: int, kind: str, resource_id: str) -> None:
            sim = ctx.simulations.get(telegram_id)
            if sim is not None:
                event = {"type": "billing", "kind": kind, "resource_id": resource_id}
                await hubs.for_simulation(sim).broadcast(event)
            hook = ctx.billing_notify
            if hook is not None:
                result = hook(telegram_id, kind, resource_id)
                if asyncio.iscoroutine(result):
                    await result

        webhook_handler = WebhookHandler(
            event_store=ctx.storage,
            subscription_store=ctx.storage,
            notify=_billing_notify,
            paypal=paypal,
        )

        def _paid_page(request: Request, claims: Optional[SessionClaims], *, kind: str, ok: bool):
            if claims is not None:
                if kind == "rescue":
                    q = "rescue=success" if ok else "rescue=error"
                else:
                    q = "billing=success" if ok else "billing=error"
                return RedirectResponse(f"/me?{q}", status_code=303)
            page_kind = "pro" if kind.startswith("pro") else "rescue"
            return HTMLResponse(render_payment_received(request_language(request), kind=page_kind, ok=ok))
        app.state.paypal = paypal
        app.state.entitlements = entitlements

        @app.get("/api/me/billing")
        async def my_billing(claims: SessionClaims = Depends(auth.required)):
            """Get user's billing status and subscription info. Free-tier JSON when PayPal is unset."""
            if entitlements is None or not ctx.settings.paypal_enabled:
                return {"enabled": False, "tier": "free", "is_pro": False, "subscriptions": []}

            status = await entitlements.get_status(claims.user_id)
            subscriptions = await ctx.storage.list_subscriptions(claims.user_id)
            agent_actions = await ctx.storage.list_agent_actions(claims.user_id)

            return {
                "enabled": True,
                **status.to_dict(),
                "subscriptions": subscriptions,
                "plan_id": ctx.settings.paypal_plan_id,
                "agent_actions": agent_actions,
            }

        @app.post("/api/me/billing/subscribe", dependencies=[Depends(require_csrf)])
        async def create_subscription(claims: SessionClaims = Depends(auth.required)):
            """Create a PayPal subscription checkout session."""
            if paypal is None or not ctx.settings.paypal_plan_id:
                raise HTTPException(503, "PayPal billing not configured")

            return_url = f"{ctx.settings.dashboard_url}/api/me/billing/return"
            cancel_url = f"{ctx.settings.dashboard_url}/me"

            try:
                result = await paypal.create_subscription(
                    plan_id=ctx.settings.paypal_plan_id,
                    return_url=return_url,
                    cancel_url=cancel_url,
                    custom_id=str(claims.user_id),
                    subscriber_email=None,
                )
                await ctx.storage.save_subscription(
                    claims.user_id,
                    result["subscription_id"],
                    ctx.settings.paypal_plan_id,
                    "PENDING",
                )
                return {"approve_url": result["approve_url"], "subscription_id": result["subscription_id"]}
            except PayPalError as e:
                logger.error("Failed to create subscription: %s", e)
                raise HTTPException(502, "PayPal error")

        @app.get("/api/me/billing/return")
        async def billing_return(
            request: Request,
            subscription_id: str = Query(...),
            claims: SessionClaims = Depends(auth.optional),
        ):
            """Handle PayPal subscription return (user approved). Session is optional."""
            if paypal is None:
                return _paid_page(request, claims, kind="pro", ok=False)

            try:
                details = await paypal.get_subscription(subscription_id)
            except PayPalError as e:
                logger.error("Failed to verify subscription: %s", e)
                return _paid_page(request, claims, kind="pro", ok=False)

            telegram_id = claims.user_id if claims is not None else parse_telegram_id(details.custom_id or "")
            if telegram_id is None and details.custom_id:
                telegram_id = await ctx.storage.get_user_by_custom_id(str(details.custom_id))
            if details.is_active() and telegram_id is not None:
                key = f"sub:{subscription_id}"
                if not await ctx.storage.has_event(key):
                    await ctx.storage.save_subscription(
                        telegram_id,
                        subscription_id,
                        details.plan_id,
                        "ACTIVE",
                        payer_email=details.payer_email,
                    )
                    await ctx.storage.save_event(
                        key, "PRO_ACTIVATED", subscription_id, telegram_id, {"source": "return"},
                    )
                    await _billing_notify(telegram_id, "pro_activated", subscription_id)
                return _paid_page(request, claims, kind="pro", ok=True)

            if claims is not None:
                return RedirectResponse("/me?billing=pending", status_code=303)
            return HTMLResponse(render_payment_received(request_language(request), kind="pro", ok=False))

        @app.post("/api/me/billing/cancel", dependencies=[Depends(require_csrf)])
        async def cancel_subscription_endpoint(claims: SessionClaims = Depends(auth.required)):
            """Cancel user's active subscription."""
            if paypal is None or entitlements is None:
                raise HTTPException(503, "PayPal billing not configured")

            status = await entitlements.get_status(claims.user_id)
            if not status.subscription_id:
                raise HTTPException(404, "No active subscription")

            success = await paypal.cancel_subscription(status.subscription_id, "User requested")
            if success:
                await ctx.storage.update_subscription_status(status.subscription_id, "CANCELLED")
            return {"cancelled": success}

        @app.post("/api/me/billing/agent", dependencies=[Depends(require_csrf)])
        async def agent_checkout_endpoint(
            body: AgentCheckoutRequest, claims: SessionClaims = Depends(auth.required),
        ):
            """Muse PayPal Agent Toolkit checkout: Rescue Pass order or Pro subscription only."""
            if paypal is None:
                raise HTTPException(503, "PayPal billing not configured")
            from liqguard.payments.agent import run_checkout

            product = body.product if body.product in ("pro", "rescue") else None
            text = (body.text or "").strip() or (
                "get me a Rescue Pass" if product == "rescue" else "upgrade to Pro"
            )
            checkout = await run_checkout(
                paypal, ctx.llm, user_id=claims.user_id, text=text, lang="en",
                plan_id=ctx.settings.paypal_plan_id, dashboard_url=ctx.settings.dashboard_url,
                product=product, store=ctx.storage,
                default_product="pro" if product is None else None,
            )
            return checkout.to_dict()

        @app.post("/api/me/billing/rescue-pass", dependencies=[Depends(require_csrf)])
        async def purchase_rescue_pass(claims: SessionClaims = Depends(auth.required)):
            """Create a one-time Rescue Pass purchase."""
            if paypal is None:
                raise HTTPException(503, "PayPal billing not configured")

            return_url = f"{ctx.settings.dashboard_url}/api/me/billing/rescue-return"
            cancel_url = f"{ctx.settings.dashboard_url}/me"

            try:
                result = await paypal.create_order(
                    amount_usd="2.99",
                    description="LiqGuard Rescue Pass",
                    return_url=return_url,
                    cancel_url=cancel_url,
                    custom_id=str(claims.user_id),
                )
                return {"approve_url": result["approve_url"], "order_id": result["order_id"]}
            except PayPalError as e:
                logger.error("Failed to create rescue pass order: %s", e)
                raise HTTPException(502, "PayPal error")

        @app.get("/api/me/billing/rescue-return")
        async def rescue_pass_return(
            request: Request,
            token: str = Query(...),
            claims: SessionClaims = Depends(auth.optional),
        ):
            """Capture the Rescue Pass order and activate Rescue Mode without a dashboard session."""
            if paypal is None:
                return _paid_page(request, claims, kind="rescue", ok=False)

            capture: dict = {}
            captured = False
            try:
                capture = await paypal.capture_order(token)
                captured = capture.get("status") == "COMPLETED"
            except PayPalError as e:
                if e.is_already_captured():
                    captured = True
                    try:
                        capture = await paypal.get_order(token)
                    except PayPalError as lookup_err:
                        logger.error("GET order after already-captured failed: %s", lookup_err)
                        capture = {}
                else:
                    logger.error("Failed to capture rescue pass order: %s", e)
                    return _paid_page(request, claims, kind="rescue", ok=False)

            if not captured:
                return _paid_page(request, claims, kind="rescue", ok=False)

            telegram_id, _granted, _until = await fulfill_rescue_order(
                ctx.storage, ctx.storage,
                order_id=token, payload=capture, paypal=paypal,
                notify=_billing_notify, source="return",
            )
            if telegram_id is None:
                logger.warning("Rescue return for %s captured but not attributed", token)
                return _paid_page(request, claims, kind="rescue", ok=False)
            return _paid_page(request, claims, kind="rescue", ok=True)

        # ---------------------------------------------------------------- PayPal webhooks

        @app.post("/webhooks/paypal")
        async def paypal_webhook(request: Request):
            """Handle PayPal subscription lifecycle webhooks."""
            if webhook_handler is None or paypal is None or not paypal.webhook_id:
                logger.error("PayPal webhook rejected: client or PAYPAL_WEBHOOK_ID missing")
                raise HTTPException(503, "PayPal webhooks not configured")

            headers = extract_webhook_headers(dict(request.headers))
            body_bytes = await request.body()

            try:
                event = json.loads(body_bytes)
            except json.JSONDecodeError:
                raise HTTPException(400, "Invalid JSON")

            verified = await paypal.verify_webhook_online(
                headers["transmission_id"],
                headers["transmission_time"],
                headers["cert_url"],
                headers["auth_algo"],
                headers["transmission_sig"],
                event,
            )

            if not verified:
                logger.warning("Webhook signature verification failed")
                raise HTTPException(401, "Invalid signature")

            result = await webhook_handler.process_event(event)
            logger.info("Processed webhook %s: %s", result.event_type, result.message)
            if not result.success:
                # Unattributed captures must not be acked — PayPal retries.
                raise HTTPException(503, result.message)

            return {"status": "ok", "event_id": result.event_id, "processed": result.success}

    # ---------------------------------------------------------------- websockets

    async def serve_ws(websocket: WebSocket, sim: MarketSimulation) -> None:
        await websocket.accept()
        await _ensure_summary(sim)
        await websocket.send_json({"type": "snapshot", "data": localize(sim.language, sim.snapshot())})
        hub = hubs.for_simulation(sim)
        # No await between these two: each follow-up reaches this viewer exactly once, live or replayed.
        hub.clients.add(websocket)
        missed = sim.recent_follow_ups()
        try:
            for event in missed:
                await websocket.send_json(localize(sim.language, event))
            while True:
                if await websocket.receive_text() == "ping":
                    await websocket.send_text('{"type":"pong"}')
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(websocket)

    @app.websocket("/ws/demo")
    async def ws_demo(websocket: WebSocket):
        await serve_ws(websocket, public_for(websocket.query_params.get("lang")))

    @app.websocket("/ws/me")
    async def ws_me(websocket: WebSocket):
        claims = await auth.websocket(websocket)
        if claims is None:
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        await serve_ws(websocket, await watched_simulation(claims.user_id))

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
