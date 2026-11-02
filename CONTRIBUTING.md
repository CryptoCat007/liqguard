# Contributing to LiqGuard

## Before every commit

```bash
python3 -m ruff check liqguard scripts --fix -q
python3 -m pytest -q
```

`test_dashboard_browser.py` drives the dashboard in Chromium and is skipped without Playwright.
Install it once with `pip install -e ".[browser]" && playwright install chromium` so the zh-Hant
page check runs locally too.

## Before every release

Every release must pass the pre-release check:

```bash
python3 scripts/prerelease_check.py
```

It needs no Telegram token or network. It runs the production aiogram `Dispatcher` (built by
`liqguard.telegram.app.wire_bot`, the same wiring `main.py` uses) against a fake Bot API, in English
and 繁體中文:

- `/start`, every main-menu button (and the buttons one level down) and every command;
- switching language, and the bind flow;
- a full demo simulation run;
- ✅ I'm awake, 😴 Snooze, ⏹ Stop demo and `/stopdemo` on the simulation, on the fixed demo books
  and on a demo wake-up reminder, each followed by a check that **no** further demo message is sent;
- a restart while in demo mode stays quiet.

It exits non-zero on any failure, unhandled update, unanswered button or error toast. Do not ship
if it fails, and do not weaken a check to make it pass. When you add a menu, button or demo
behaviour, add a scenario to `liqguard/testing/prerelease.py` (`SCENARIOS`); `tests/test_prerelease.py`
runs every scenario under pytest as well.

## Ground rules

- Never commit secrets (`.env`, keys, databases). Mainnet keys are read-only; execution is testnet only.
- Keep per-user isolation: a user can only see and act on their own data and buttons.
- Business logic (`risk/`, `alerts/`, `models/`) stays free of Telegram/web framework imports.
- Every user-facing string goes through `liqguard/i18n` with both `en` and `zh-Hant` entries,
  including dashboard text (`L("key")` in `app.js`, `data-i18n` in `index.html`).
