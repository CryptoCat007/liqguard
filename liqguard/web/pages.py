"""
Server-rendered pages (/login and its error / signed-out variants) in the visitor's language.

The language comes from `?lang=` first, then the `liqguard_lang` cookie that the
dashboard toggle and every `?lang=` page visit write, so it carries over between
the dashboard, the login page and the one-time link redirects. Default English.
"""

from __future__ import annotations

from html import escape
from typing import Optional
from urllib.parse import urlencode, urlsplit

from fastapi import Request

from liqguard.config import Settings
from liqguard.i18n import EN, ZH, normalize, t

LANG_COOKIE = "liqguard_lang"
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600
ERRORS = ("link", "link_used", "link_expired", "widget", "session", "disabled")
NOTICES = ("signed_out",)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def request_language(request: Request) -> str:
    return normalize(request.query_params.get("lang") or request.cookies.get(LANG_COOKIE))


def remember_language(response, request: Request) -> None:
    """Persist an explicit `?lang=` choice so the next page opens in the same language."""
    if request.query_params.get("lang"):
        response.set_cookie(LANG_COOKIE, request_language(request), max_age=LANG_COOKIE_MAX_AGE,
                            samesite="lax", path="/")


def widget_available(settings: Optional[Settings]) -> bool:
    """
    The Telegram Login Widget only works on the domain set with BotFather /setdomain,
    which must be served over https; on localhost it just shows "Bot domain invalid".
    """
    if not (settings and settings.bot_username and settings.telegram_token and settings.public_url):
        return False
    url = urlsplit(settings.public_url)
    host = (url.hostname or "").lower()
    return url.scheme == "https" and bool(host) and host not in LOCAL_HOSTS and not host.endswith(".localhost")


def _fill(lang: str, key: str, html: Optional[dict[str, str]] = None, **params) -> str:
    """Escaped locale text; `html` values are trusted markup substituted after escaping."""
    html = html or {}
    text = escape(t(lang, key, **params, **{name: f"\x00{name}\x00" for name in html}))
    for name, markup in html.items():
        text = text.replace(f"\x00{name}\x00", markup)
    return text


def render_payment_received(lang: str, kind: str = "rescue", ok: bool = True) -> str:
    """Sessionless PayPal return: tell the user to go back to Telegram."""
    lang = normalize(lang)
    if ok:
        title = _fill(lang, "pro.return.title")
        heading = _fill(lang, "pro.return.body")
        detail_key = "pro.return.rescue" if kind == "rescue" else "pro.return.pro"
        detail = _fill(lang, detail_key)
    else:
        title = _fill(lang, "pro.return.error_title")
        heading = _fill(lang, "pro.return.error_body")
        detail = ""
    extra = f"<p>{detail}</p>" if detail else ""
    return f"""<!DOCTYPE html>
<html lang="{"zh-Hant-TW" if lang == ZH else "en"}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛡️</text></svg>">
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
  <main class="login-wrap">
    <section class="card login-card">
      <h1>{heading}</h1>
      {extra}
    </section>
  </main>
</body>
</html>
"""


def render_login(lang: str, settings: Optional[Settings], error: Optional[str], notice: Optional[str]) -> str:
    lang = normalize(lang)
    other = EN if lang == ZH else ZH
    query = {k: v for k, v in (("error", error if error in ERRORS else None),
                                ("notice", notice if notice in NOTICES else None)) if v}
    toggle_href = "/login?" + urlencode({**query, "lang": other})
    bot = settings.bot_username if settings else None
    bot_link = (f' <a href="https://t.me/{escape(bot)}" rel="noopener">@{escape(bot)}</a>' if bot else "")
    minutes = max(1, round((settings.login_link_ttl if settings else 300) / 60))

    banner = ""
    if error in ERRORS:
        banner = f'<div class="login-error" id="loginError" role="alert">{_fill(lang, f"login.error_{error}")}</div>'
    elif notice in NOTICES:
        banner = f'<div class="login-notice" id="loginNotice" role="status">{_fill(lang, f"login.notice_{notice}")}</div>'

    widget = ""
    if widget_available(settings):
        auth_url = escape(f"{settings.public_url}/auth/telegram", quote=True)
        widget = (
            f'<div class="login-divider" id="widgetDivider">{_fill(lang, "login.or")}</div>'
            '<div class="login-widget" id="widget">'
            '<script async src="https://telegram.org/js/telegram-widget.js?22" '
            f'data-telegram-login="{escape(bot, quote=True)}" data-size="large" data-radius="10" '
            f'data-auth-url="{auth_url}" data-lang="{"zh-hant" if lang == ZH else "en"}"></script></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="{"zh-Hant-TW" if lang == ZH else "en"}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_fill(lang, "login.title")}</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛡️</text></svg>">
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
  <main class="login-wrap">
    <section class="card login-card">
      <div class="login-top">
        <div class="brand">
          <svg class="brand-mark" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3Z" fill="url(#g)"/>
            <path d="m8.5 12 2.5 2.5 4.5-5" fill="none" stroke="#0b0f17" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5eead4"/><stop offset="1" stop-color="#6366f1"/></linearGradient></defs>
          </svg>
          <div>
            <div class="brand-name">LiqGuard</div>
            <div class="brand-sub">{_fill(lang, "login.brand_sub")}</div>
          </div>
        </div>
        <a class="btn btn-ghost btn-sm lang-btn" id="langBtn" href="{escape(toggle_href, quote=True)}" hreflang="{other}"
           lang="{"en" if other == EN else "zh-Hant-TW"}" title="{_fill(lang, "web.lang_toggle_title")}">{_fill(lang, "web.lang_toggle")}</a>
      </div>

      <h1>{_fill(lang, "login.heading")}</h1>
      {banner}
      <p>{_fill(lang, "login.intro")}</p>
      <ol class="login-steps">
        <li>{_fill(lang, "login.step_open", html={"bot": bot_link})}</li>
        <li>{_fill(lang, "login.step_send", html={"command": "<code>/dashboard</code>"})}</li>
        <li>{_fill(lang, "login.step_tap", minutes=minutes)}</li>
      </ol>
      {widget}
      <p class="login-demo">{_fill(lang, "login.demo", html={"link": f'<a href="/?lang={lang}">{_fill(lang, "login.demo_link")}</a>'})}</p>
    </section>
  </main>
</body>
</html>
"""
