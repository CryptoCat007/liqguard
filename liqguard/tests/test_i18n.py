"""Locale parity (every key in both languages, same placeholders) and the dashboard EN / 中文 toggle."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from liqguard.i18n import EN, LANGUAGES, ZH, catalog, normalize, placeholders, t, web_catalog
from liqguard.models import User
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE

from .conftest import ALICE, BOB

STATIC = Path(__file__).resolve().parents[1] / "web" / "static"
CSRF = {"X-LiqGuard-CSRF": "1"}
HAN = re.compile(r"[\u4e00-\u9fff]")
# Proper nouns, symbols and language names that read the same in both locales.
SAME_IN_BOTH = {
    "common.side.None", "common.venue.binance", "common.venue.hyperliquid", "common.venue.demo", "tools.title",
    "tools.funding.drift_item", "commands.lang", "kb.demo", "kb.language", "kb.lang_en", "kb.lang_zh",
    "alert.muse", "web.cnt_adl",
}

# English strings that deliberately show 中文 (the language picker and its labels).
NAMES_THE_OTHER_LANGUAGE = {
    "kb.lang_zh", "kb.language", "commands.lang", "lang.picker_title", "menu.help", "web.lang_toggle", "web.lang_toggle_title",
}


class TestLocaleParity:
    def test_every_key_exists_in_every_language(self):
        keys = {lang: set(catalog(lang)) for lang in LANGUAGES}
        for lang in LANGUAGES:
            assert keys[lang] == keys[EN], f"{lang}: missing {keys[EN] - keys[lang]}, extra {keys[lang] - keys[EN]}"

    def test_placeholders_match(self):
        en, zh = catalog(EN), catalog(ZH)
        mismatched = {k for k in en if placeholders(en[k]) != placeholders(zh[k])}
        assert not mismatched

    def test_no_empty_strings(self):
        for lang in LANGUAGES:
            assert not [k for k, v in catalog(lang).items() if not v.strip()]

    def test_no_untranslated_strings(self):
        en, zh = catalog(EN), catalog(ZH)
        untranslated = {k for k in en if en[k] == zh[k]} - SAME_IN_BOTH
        assert not untranslated
        assert not [k for k, v in en.items() if HAN.search(v) and k not in NAMES_THE_OTHER_LANGUAGE]

    def test_language_codes(self):
        assert normalize(None) == EN and normalize("zh-TW") == ZH and normalize("zh-hans") == ZH
        assert normalize("fr") == EN and t("xx", "web.live") == t(EN, "web.live")


def page_keys() -> set[str]:
    html = (STATIC / "index.html").read_text()
    return set(re.findall(r'data-i18n(?:-title|-aria)?="([\w.]+)"', html))


def script_keys() -> set[str]:
    js = (STATIC / "app.js").read_text()
    keys = set(re.findall(r'\bL\("(\w+)"', js))
    keys |= {f"tool_{x}" for x in ("rescue", "wick", "stop", "funding")}
    keys |= {f"setting_{x}" for x in ("warning", "critical", "emergency", "interval", "max", "quiet")}
    keys |= {f"toggle_{x}" for x in ("escalation", "adaptive", "stops")}
    return keys


class TestDashboardStrings:
    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_every_dashboard_key_is_translated(self, lang):
        strings = web_catalog(lang)
        missing = (page_keys() | script_keys()) - set(strings)
        assert not missing and len(page_keys()) > 60

    def test_dashboard_has_no_hardcoded_chinese(self):
        for name in ("index.html", "app.js"):
            text = (STATIC / name).read_text()
            leftovers = [line for line in text.splitlines() if HAN.search(line) and 'id="langBtn"' not in line]
            assert not leftovers, f"{name}: {leftovers[:3]}"


@pytest.fixture
async def users(ctx):
    for uid in (ALICE, BOB):
        await ctx.storage.save_user(User(telegram_id=uid, threshold=ctx.settings.default_threshold()))


@pytest.fixture
def dashboard(ctx, users):
    with TestClient(create_app(ctx), base_url="https://testserver") as client:
        yield client


def sign_in(client: TestClient, ctx, user_id: int) -> None:
    client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(user_id, 0))


class TestDashboardLanguageToggle:
    def test_public_catalog_defaults_to_english(self, dashboard):
        en = dashboard.get("/api/i18n/en").json()
        assert en["lang"] == EN and en["strings"]["simulate"] == "Simulate market"
        zh = dashboard.get("/api/i18n/zh-TW").json()
        assert zh["lang"] == ZH and HAN.search(zh["strings"]["simulate"])
        assert dashboard.get("/api/i18n/klingon").json()["lang"] == EN

    def test_demo_tool_report_follows_the_toggle(self, dashboard):
        en = dashboard.get("/api/demo/position-tool?symbol=BTCUSDT&tool=stop").json()
        zh = dashboard.get("/api/demo/position-tool?symbol=BTCUSDT&tool=stop&lang=zh-Hant").json()
        assert en["lang"] == EN and not HAN.search(en["title"])
        assert zh["lang"] == ZH and HAN.search(zh["title"])

    async def test_signed_in_choice_is_saved_per_user_and_shared_with_telegram(self, ctx, dashboard):
        sign_in(dashboard, ctx, ALICE)
        assert dashboard.get("/api/me/language").json() == {"language": EN, "explicit": False}
        r = dashboard.post("/api/me/language", json={"language": ZH}, headers=CSRF)
        assert r.status_code == 200 and r.json()["language"] == ZH
        alice = await ctx.storage.get_user(ALICE)
        assert alice.language == ZH and alice.language_explicit
        assert (await ctx.storage.get_user(BOB)).language == EN

        r = dashboard.get("/api/me/settings").json()
        assert r is not None
        sim = ctx.simulations.get_or_create(ALICE)
        dashboard.post("/api/me/language", json={"language": EN}, headers=CSRF)
        assert sim.language == EN and (await ctx.storage.get_user(ALICE)).language == EN

    def test_language_change_needs_session_and_csrf(self, ctx, dashboard):
        assert dashboard.post("/api/me/language", json={"language": ZH}, headers=CSRF).status_code == 401
        sign_in(dashboard, ctx, ALICE)
        assert dashboard.post("/api/me/language", json={"language": ZH}).status_code in (400, 403)
        assert dashboard.post("/api/me/language", json={"language": "fr"}, headers=CSRF).status_code == 422
