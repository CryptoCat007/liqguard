"""
Render the real dashboard in Chromium and read it like a user would.

The zh-Hant page, after a full demo run with every section unfolded, a
position expanded and a quick-action report open, must show no English
apart from tickers, exchange names and units. Skipped when Playwright or
its Chromium build is not installed (`pip install playwright && playwright
install chromium`).
"""

import socket
import threading
import time

import pytest
import uvicorn

from liqguard.ai import MetaLLMClient
from liqguard.simulation import MarketSimulation
from liqguard.web import create_app

from .zh_scan import english_words

sync_api = pytest.importorskip("playwright.sync_api")

LANG_TOGGLE = "langBtn"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(sim):
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(demo_simulation=sim), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    if not srv.started:
        pytest.skip("dashboard server did not start")
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def server():
    yield from _serve(MarketSimulation(MetaLLMClient(use_mock=True), tick_interval=0.03, escalation_seconds_per_minute=0.01))


@pytest.fixture(scope="module")
def live_server():
    from .test_muse_async import SmartMuse, live_llm

    yield from _serve(MarketSimulation(live_llm(SmartMuse(delay=0.4)), tick_interval=0.03, escalation_seconds_per_minute=0.01))


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        yield b
        b.close()


def _visible_english(page) -> list[tuple[str, list[str]]]:
    text = page.evaluate("() => document.body.innerText")
    attrs = page.evaluate(
        f"""() => [...document.querySelectorAll('[title],[placeholder],[aria-label]')]
        .filter((n) => n.id !== '{LANG_TOGGLE}')
        .flatMap((n) => [n.title, n.placeholder, n.getAttribute('aria-label')]).filter(Boolean)"""
    )
    return [(line.strip(), english_words(line)) for line in [*text.splitlines(), *attrs] if english_words(line)]


def _run_demo(page, base: str, lang: str) -> None:
    page.goto(f"{base}/?lang={lang}")
    page.wait_for_function("() => document.querySelectorAll('#positions .prow').length > 0", timeout=15000)
    page.click("#simulateBtn")
    page.wait_for_function("() => S.state === 'finished'", timeout=60000)
    page.evaluate("() => document.querySelectorAll('details.fold').forEach((d) => { d.open = true; })")
    page.click("#positions .prow")
    page.wait_for_selector(".pdetail")
    page.click("#heroRescueBtn")
    page.wait_for_selector("#rescueBody .rs-grid", timeout=15000)


def test_zh_dashboard_shows_no_english(server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    _run_demo(page, server, "zh-Hant")
    assert page.evaluate("() => document.documentElement.lang") == "zh-Hant-TW"
    assert page.locator("#timeline .tl-item").count() > 5 and page.locator("#audit .au").count() > 5
    leftovers = _visible_english(page)

    page.click(".pdetail .tool-btn[data-tool='wick']")
    page.wait_for_selector("#toolBody .tool-line:not(.muted)")
    leftovers += _visible_english(page)
    page.click("#toolClose")

    assert not errors, errors
    assert not leftovers, leftovers[:15]


def test_en_dashboard_keeps_english(server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _run_demo(page, server, "en")
    assert page.inner_text("#riskLevel") in {"SAFE", "WARNING", "DANGER"}
    assert page.inner_text("#positions .prow-risk") in {"Safe", "Warning", "Critical", "Emergency"}
    assert "Total cost" in page.inner_text("#rescueBody")


def test_language_follows_the_user_between_dashboard_and_login(server, browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.goto(f"{server}/")
    page.wait_for_function("() => document.documentElement.lang === 'en'", timeout=15000)
    page.click(f"#{LANG_TOGGLE}")
    page.wait_for_function("() => document.documentElement.lang === 'zh-Hant-TW'", timeout=15000)

    page.goto(f"{server}/login?error=link_expired")
    assert page.evaluate("() => document.documentElement.lang") == "zh-Hant-TW"
    assert "連結已過期" in page.inner_text("#loginError")
    assert not _visible_english(page)
    color = page.evaluate("() => getComputedStyle(document.querySelector('.login-card a:not(.btn)')).color")
    assert color == "rgb(94, 234, 212)"

    page.click(f"#{LANG_TOGGLE}")
    page.wait_for_function("() => document.documentElement.lang === 'en'")
    page.goto(f"{server}/")
    page.wait_for_function("() => document.documentElement.lang === 'en'", timeout=15000)
    context.close()


def test_muse_badge_is_honest_and_cards_update_in_place(live_server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{live_server}/?lang=zh-Hant")
    page.wait_for_function("() => document.querySelectorAll('#positions .prow').length > 0", timeout=15000)
    page.wait_for_function("() => document.getElementById('musePill').dataset.mode === 'live'", timeout=15000)
    assert page.inner_text("#musePill") == "Muse · 線上"
    page.click("#simulateBtn")
    page.wait_for_function("() => S.state === 'finished'", timeout=60000)
    page.wait_for_function(
        "() => [...document.querySelectorAll('#advice [data-rec=advice]')].some((n) => n.dataset.live === 'true')",
        timeout=15000)
    live = page.locator("#advice [data-rec=advice][data-live=true]").first
    assert live.locator(".muse-mark").text_content() == "✦ Muse"
    assert page.locator("#advice .adv-text, #advice .adv-summary").count() == 0
    for card in page.locator("#advice [data-advice]").all():
        assert card.locator(".rec-line").count() == 1
    cards = page.locator("#advice [data-advice]")
    ids = cards.evaluate_all("(nodes) => nodes.map((n) => n.dataset.advice)")
    assert len(ids) == len(set(ids)) > 0
    assert not errors, errors


def test_quick_report_and_rescue_take_muse_text_from_the_socket(live_server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    refetches: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: refetches.append(r.url) if "wait=1" in r.url else None)
    page.goto(f"{live_server}/?lang=en")
    page.wait_for_function("() => document.querySelectorAll('#positions .prow').length > 0", timeout=15000)
    page.wait_for_function("() => ws && ws.readyState === WebSocket.OPEN", timeout=15000)

    page.evaluate("() => openTool('stop', 'ETH', 'hyperliquid', false)")
    page.wait_for_selector("#toolBody .tool-line")
    first = page.locator("#toolBody > *").first
    if "muse" not in (first.get_attribute("class") or ""):
        # Rule text while Muse answers: a key is configured, so it carries no "(mock)" tag.
        assert first.inner_text().startswith("💡 ") and first.locator(".muse-mark").count() == 0
    page.wait_for_function(
        "() => document.querySelector('#toolBody .tool-line.muse .rec-text')?.textContent === '💡 Muse reviewed the stop.'",
        timeout=15000)
    assert page.inner_text("#toolBody .tool-line.muse .muse-mark") == "✦ Muse"
    assert page.locator("#toolBody .tool-line.muse").count() == 1
    page.click("#toolClose")

    page.evaluate("() => loadRescue('BTCUSDT', { scroll: false })")
    page.wait_for_function("() => S.rescue?.explanation?.headline === 'Muse compares the rescue options.'", timeout=15000)
    page.wait_for_selector("#rescueBody [data-rec=rescue][data-live=true]")
    assert page.inner_text("#rescueBody [data-rec=rescue] .rec-text") == "💡 Muse compares the rescue options."
    assert page.inner_text("#rescueBody").count("Muse compares the rescue options.") == 1
    assert page.locator("#rescueBody .rs-muse").count() == 0
    assert not refetches, refetches
    assert not errors, errors
