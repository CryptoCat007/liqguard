"""Dashboard authentication: one-time login links, signed session cookies, Telegram Login Widget, CSRF."""

import time
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from liqguard.auth import (
    LoginLinkService,
    SessionSigner,
    telegram_login_hash,
    verify_telegram_login,
)
from liqguard.i18n import t
from liqguard.models import User
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE

from .conftest import ALICE, BOB, BOT_TOKEN, fake_message

CSRF = {"X-LiqGuard-CSRF": "1"}
SECRET = b"s" * 32


class Clock:
    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def widget_payload(user_id: int = ALICE, auth_date=None, bot_token: str = BOT_TOKEN) -> dict:
    data = {"id": str(user_id), "first_name": "Alice", "username": "alice", "auth_date": str(int(auth_date or time.time()))}
    data["hash"] = telegram_login_hash(data, bot_token)
    return data


@pytest.fixture
def client(ctx):
    with TestClient(create_app(ctx), base_url="https://testserver") as c:
        yield c


# --------------------------------------------------------------------------- session tokens


class TestSessionSigner:
    def test_roundtrip(self):
        signer = SessionSigner(SECRET)
        claims = signer.verify(signer.issue(ALICE, 3, "alice"))
        assert (claims.user_id, claims.epoch, claims.username) == (ALICE, 3, "alice")

    def test_short_secret_is_rejected(self):
        with pytest.raises(ValueError):
            SessionSigner(b"short")

    def test_tampered_payload_is_rejected(self):
        signer = SessionSigner(SECRET)
        alice_token = signer.issue(ALICE, 0)
        bob_payload = signer.issue(BOB, 0).split(".")[1]
        version, _, mac = alice_token.split(".")
        assert signer.verify(f"{version}.{bob_payload}.{mac}") is None

    def test_token_from_another_secret_is_rejected(self):
        assert SessionSigner(SECRET).verify(SessionSigner(b"x" * 32).issue(ALICE, 0)) is None

    def test_expired_token_is_rejected(self):
        clock = Clock()
        signer = SessionSigner(SECRET, ttl_seconds=60, clock=clock)
        token = signer.issue(ALICE, 0)
        clock.now += 61
        assert signer.verify(token) is None

    @pytest.mark.parametrize("garbage", [None, "", "abc", "v1.x.y", "v2.a.b", "v1..", "...."])
    def test_garbage_is_rejected(self, garbage):
        assert SessionSigner(SECRET).verify(garbage) is None


# --------------------------------------------------------------------------- login links


class TestLoginLinks:
    async def test_link_works_once(self, ctx):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        token = await ctx.login_links.create(ALICE)
        assert await ctx.login_links.redeem(token) == ALICE
        assert await ctx.login_links.redeem(token) is None

    async def test_link_expires(self, ctx):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        clock = Clock(time.time())
        links = LoginLinkService(ctx.storage.login_tokens, ttl_seconds=300, clock=clock)
        token = await links.create(ALICE)
        clock.now += 301
        assert await links.redeem(token) is None

    @pytest.mark.parametrize("token", [None, "", "not-a-real-token", "x" * 500])
    async def test_unknown_tokens_are_rejected(self, ctx, token):
        assert await ctx.login_links.redeem(token) is None

    async def test_only_the_hash_is_stored(self, ctx):
        import sqlite3

        await ctx.storage.save_user(User(telegram_id=ALICE))
        token = await ctx.login_links.create(ALICE)
        dump = "\n".join(sqlite3.connect(ctx.settings.db_path).iterdump())
        assert token not in dump

    async def test_revoke_invalidates_unused_links(self, ctx):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        token = await ctx.login_links.create(ALICE)
        await ctx.storage.bump_session_epoch(ALICE)
        assert await ctx.login_links.redeem(token) is None


# --------------------------------------------------------------------------- telegram widget


class TestTelegramWidget:
    def test_valid_payload(self):
        identity = verify_telegram_login(widget_payload(), BOT_TOKEN)
        assert identity.user_id == ALICE and identity.username == "alice"

    def test_wrong_bot_token(self):
        assert verify_telegram_login(widget_payload(bot_token="999:other-bot-token-xxxxxxxxxxxxxxxxxxxx"), BOT_TOKEN) is None

    def test_tampered_user_id(self):
        data = widget_payload()
        data["id"] = str(BOB)
        assert verify_telegram_login(data, BOT_TOKEN) is None

    def test_stale_payload(self):
        assert verify_telegram_login(widget_payload(auth_date=time.time() - 3600), BOT_TOKEN) is None

    def test_future_payload(self):
        assert verify_telegram_login(widget_payload(auth_date=time.time() + 3600), BOT_TOKEN) is None

    def test_missing_fields(self):
        data = widget_payload()
        del data["hash"]
        assert verify_telegram_login(data, BOT_TOKEN) is None
        assert verify_telegram_login(widget_payload(), "") is None


# --------------------------------------------------------------------------- HTTP flow


class TestDashboardLogin:
    async def test_login_link_sets_hardened_cookie(self, ctx, client):
        await ctx.storage.save_user(User(telegram_id=ALICE, username="alice"))
        token = await ctx.login_links.create(ALICE)

        response = client.get(f"/auth/login?token={token}", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/me"
        cookie = response.headers["set-cookie"].lower()
        assert SESSION_COOKIE in cookie
        assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie

        assert client.get("/api/me/session").json()["user_id"] == ALICE
        assert client.get("/me", follow_redirects=False).status_code == 200

    async def test_reused_link_is_rejected(self, ctx, client):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        token = await ctx.login_links.create(ALICE)
        client.get(f"/auth/login?token={token}", follow_redirects=False)
        client.cookies.clear()

        response = client.get(f"/auth/login?token={token}", follow_redirects=False)
        assert response.headers["location"] == "/login?error=link_used"
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")

    async def test_failed_link_says_why(self, ctx, client):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        clock = Clock(time.time())
        ctx.login_links = LoginLinkService(ctx.storage.login_tokens, ttl_seconds=300, clock=clock)
        expired = await ctx.login_links.create(ALICE)
        clock.now += 301
        assert client.get(f"/auth/login?token={expired}", follow_redirects=False).headers["location"] == "/login?error=link_expired"
        for bad in ("not-a-real-token", "x" * 500, ""):
            assert client.get(f"/auth/login?token={bad}", follow_redirects=False).headers["location"] == "/login?error=link"

    async def test_bot_link_carries_the_language_into_a_cookie(self, ctx, client):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        url = ctx.dashboard_login_url(await ctx.login_links.create(ALICE), "zh-TW")
        assert url.endswith("&lang=zh-Hant")
        client.get(url.replace("https://liqguard.test", ""), follow_redirects=False)
        response = client.get(url.replace("https://liqguard.test", ""), follow_redirects=False)
        assert response.headers["location"] == "/login?error=link_used"
        assert "liqguard_lang=zh-Hant" in response.headers["set-cookie"]
        assert "使用 Telegram 登入" in client.get("/login?error=link_used").text

    def test_me_redirects_to_login_without_session(self, client):
        response = client.get("/me", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"

    def test_widget_login_creates_session(self, ctx, client):
        response = client.get("/auth/telegram", params=widget_payload(), follow_redirects=False)
        assert response.headers["location"] == "/me"
        assert client.get("/api/me/session").json()["user_id"] == ALICE

    def test_forged_widget_login_is_rejected(self, client):
        data = widget_payload()
        data["id"] = str(BOB)
        response = client.get("/auth/telegram", params=data, follow_redirects=False)
        assert response.headers["location"] == "/login?error=widget"
        assert client.get("/api/me/session").status_code == 401

    async def test_revoke_kills_existing_sessions(self, ctx, client):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        client.get(f"/auth/login?token={await ctx.login_links.create(ALICE)}", follow_redirects=False)
        assert client.get("/api/me/session").status_code == 200

        await ctx.storage.bump_session_epoch(ALICE)
        assert client.get("/api/me/session").status_code == 401

    def test_forged_cookie_is_rejected(self, client):
        client.cookies.set(SESSION_COOKIE, SessionSigner(b"attacker" * 4).issue(ALICE, 0))
        assert client.get("/api/me/session").status_code == 401

    async def test_logout_requires_csrf_header(self, ctx, client):
        assert client.post("/auth/logout").status_code == 403
        assert client.post("/auth/logout", headers={**CSRF, "origin": "https://evil.example"}).status_code == 403
        response = client.post("/auth/logout", headers={**CSRF, "origin": "https://testserver"})
        assert response.status_code == 200
        assert SESSION_COOKIE in response.headers["set-cookie"]

    async def test_state_changes_require_csrf(self, ctx, client):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(ALICE, 0))
        assert client.post("/api/me/simulate").status_code == 403
        assert client.post("/api/me/simulate", headers={**CSRF, "origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/demo/simulate").status_code == 403

    def test_auth_config_is_public_and_secret_free(self, ctx, client):
        body = client.get("/api/auth/config").json()
        assert body["login_enabled"] is True
        assert body["bot_username"] == "liqguard_test_bot"
        assert BOT_TOKEN not in str(body)

    def test_login_disabled_without_context(self):
        with TestClient(create_app(), base_url="https://testserver") as c:
            assert c.get("/auth/login?token=x", follow_redirects=False).headers["location"] == "/login?error=disabled"
            assert c.get("/api/me/session").status_code == 404
            assert c.get("/api/health").json()["login_enabled"] is False


class TestWebSocketAuth:
    def test_rejected_without_cookie(self, client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/me", headers={"origin": "https://testserver"}) as ws:
                ws.receive_json()
        assert exc.value.code == 1008

    async def test_rejected_from_foreign_origin(self, ctx, client):
        await ctx.storage.save_user(User(telegram_id=ALICE))
        client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(ALICE, 0))
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/me", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()
        assert exc.value.code == 1008

    def test_demo_socket_stays_public(self, client):
        with client.websocket_connect("/ws/demo") as ws:
            assert ws.receive_json()["data"]["scope"] == "public"


# --------------------------------------------------------------------------- /dashboard command


class TestDashboardCommand:
    @pytest.fixture
    def handlers(self, ctx):
        from liqguard.telegram import BotHandlers

        return BotHandlers(ctx)

    async def test_private_chat_gets_a_working_link(self, ctx, handlers):
        message = fake_message(ALICE, "/dashboard")
        await handlers.cmd_dashboard(message, None)

        text = message.answer.call_args.args[0]
        assert "https://liqguard.test/auth/login?token=" in text
        token, _, lang = text.split("token=")[1].split()[0].partition("&lang=")
        assert lang == "en"
        assert await ctx.login_links.redeem(token) == ALICE

    async def test_group_chat_gets_no_link(self, ctx, handlers):
        message = fake_message(ALICE, "/dashboard", chat_type="group")
        await handlers.cmd_dashboard(message, None)
        assert "token=" not in message.answer.call_args.args[0]
        assert await ctx.storage.get_user(ALICE) is None

    async def test_revoke_bumps_epoch(self, ctx, handlers):
        await handlers.cmd_dashboard(fake_message(ALICE, "/dashboard"), None)
        before = await ctx.storage.get_session_epoch(ALICE)
        await handlers.cmd_dashboard(fake_message(ALICE, "/dashboard revoke"), MagicMock(args="revoke"))
        assert await ctx.storage.get_session_epoch(ALICE) == before + 1


# --------------------------------------------------------------------------- server-rendered login page


class TestLoginPage:
    def test_english_by_default_with_bot_link_and_steps(self, client):
        html = client.get("/login").text
        assert '<html lang="en">' in html and "Sign in with Telegram" in html
        assert '<a href="https://t.me/liqguard_test_bot" rel="noopener">@liqguard_test_bot</a>' in html
        assert "<code>/dashboard</code>" in html and "valid for 5 min" in html
        assert 'href="/login?lang=zh-Hant"' in html and "login.js" not in html

    def test_lang_query_renders_chinese_and_is_remembered(self, client):
        response = client.get("/login?lang=zh-TW&error=link_expired")
        assert '<html lang="zh-Hant-TW">' in response.text and "連結已過期" in response.text
        assert 'href="/login?error=link_expired&amp;lang=en"' in response.text
        assert "liqguard_lang=zh-Hant" in response.headers["set-cookie"]
        assert "使用 Telegram 登入" in client.get("/login").text
        client.get("/?lang=en")
        assert "Sign in with Telegram" in client.get("/login").text

    @pytest.mark.parametrize("error", ["link", "link_used", "link_expired", "widget", "session", "disabled"])
    def test_every_error_has_its_own_message(self, client, error):
        en = client.get(f"/login?error={error}&lang=en").text
        zh = client.get(f"/login?error={error}&lang=zh-Hant").text
        assert 'id="loginError"' in en and 'id="loginError"' in zh
        assert t("en", f"login.error_{error}") in en.replace("&#x27;", "'")
        assert t("zh-Hant", f"login.error_{error}") in zh

    def test_unknown_error_and_markup_are_not_reflected(self, client):
        html = client.get("/login", params={"error": "<script>alert(1)</script>", "notice": "x"}).text
        assert "<script>alert" not in html and 'id="loginError"' not in html and 'id="loginNotice"' not in html

    def test_signed_out_notice(self, client):
        assert 'id="loginNotice"' in client.get("/login?notice=signed_out").text
        assert "你已登出" in client.get("/login?notice=signed_out&lang=zh-Hant").text

    def test_widget_shown_only_for_https_public_domain(self, ctx, client):
        html = client.get("/login").text
        assert 'data-telegram-login="liqguard_test_bot"' in html and 'id="widgetDivider"' in html
        assert 'data-auth-url="https://liqguard.test/auth/telegram"' in html
        assert client.get("/api/auth/config").json()["widget_enabled"] is True
        for url in ("http://liqguard.test", "https://127.0.0.1:8765", "https://localhost", "http://127.0.0.1:8765",
                    "https://app.localhost", None):
            ctx.settings = replace(ctx.settings, public_url=url)
            html = client.get("/login").text
            assert "telegram-widget.js" not in html and 'id="widgetDivider"' not in html, url
            assert client.get("/api/auth/config").json()["widget_enabled"] is False

    def test_login_page_without_context(self):
        with TestClient(create_app(), base_url="https://testserver") as c:
            html = c.get("/login?error=disabled&lang=zh-Hant").text
            assert "未啟用登入功能" in html and "telegram-widget.js" not in html and "t.me/" not in html
