"""Dashboard login (one-time link from /dashboard, or Telegram Login Widget) and session cookies."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse

from liqguard.auth import SessionClaims, verify_telegram_login
from liqguard.core import AppContext
from liqguard.models import User

from .pages import widget_available
from .security import require_csrf, same_origin

logger = logging.getLogger(__name__)

SESSION_COOKIE = "liqguard_session"


class SessionAuth:
    def __init__(self, ctx: Optional[AppContext]):
        self.ctx = ctx

    async def _claims_from_token(self, token: Optional[str]) -> Optional[SessionClaims]:
        if self.ctx is None:
            return None
        claims = self.ctx.sessions.verify(token)
        if claims is None:
            return None
        epoch = await self.ctx.storage.get_session_epoch(claims.user_id)
        if epoch is None or epoch != claims.epoch:
            return None
        return claims

    async def optional(self, request: Request) -> Optional[SessionClaims]:
        return await self._claims_from_token(request.cookies.get(SESSION_COOKIE))

    async def required(self, request: Request) -> SessionClaims:
        claims = await self.optional(request)
        if claims is None:
            raise HTTPException(status_code=401, detail="Sign in with /dashboard in Telegram")
        return claims

    async def websocket(self, websocket: WebSocket) -> Optional[SessionClaims]:
        if not same_origin(websocket.headers.get("origin"), websocket.headers.get("host")):
            return None
        return await self._claims_from_token(websocket.cookies.get(SESSION_COOKIE))

    async def start_session(self, user_id: int, username: Optional[str]) -> RedirectResponse:
        epoch = await self.ctx.storage.get_session_epoch(user_id) or 0
        token = self.ctx.sessions.issue(user_id, epoch, username)
        response = RedirectResponse("/me", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=self.ctx.sessions.ttl_seconds,
            httponly=True,
            secure=self.ctx.settings.secure_cookies,
            samesite="lax",
            path="/",
        )
        return response


def register_auth_routes(app: FastAPI, ctx: Optional[AppContext], auth: SessionAuth) -> None:

    def login_enabled() -> bool:
        return ctx is not None

    @app.get("/api/auth/config")
    async def auth_config():
        return {
            "login_enabled": login_enabled(),
            "bot_username": ctx.settings.bot_username if ctx else None,
            "widget_enabled": widget_available(ctx.settings if ctx else None),
            "link_ttl_seconds": ctx.settings.login_link_ttl if ctx else None,
        }

    @app.get("/auth/login", include_in_schema=False)
    async def login_with_link(token: Optional[str] = None):
        if not login_enabled():
            return RedirectResponse("/login?error=disabled", status_code=303)
        user_id = await ctx.login_links.redeem(token)
        if user_id is None:
            return RedirectResponse(f"/login?error={await ctx.login_links.failure(token)}", status_code=303)
        user = await ctx.storage.get_user(user_id)
        logger.info("Dashboard login via one-time link for user %s", user_id)
        return await auth.start_session(user_id, user.username if user else None)

    @app.get("/auth/telegram", include_in_schema=False)
    async def login_with_widget(request: Request):
        if not (login_enabled() and ctx.settings.telegram_token):
            return RedirectResponse("/login?error=disabled", status_code=303)
        identity = verify_telegram_login(dict(request.query_params), ctx.settings.telegram_token)
        if identity is None:
            return RedirectResponse("/login?error=widget", status_code=303)
        if await ctx.storage.get_user(identity.user_id) is None:
            await ctx.storage.save_user(User(telegram_id=identity.user_id, username=identity.username, threshold=ctx.settings.default_threshold()))
        logger.info("Dashboard login via Telegram widget for user %s", identity.user_id)
        return await auth.start_session(identity.user_id, identity.username)

    @app.post("/auth/logout", dependencies=[Depends(require_csrf)])
    async def logout():
        response = JSONResponse({"status": "signed_out"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response
