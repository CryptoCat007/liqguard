"""The pre-release scenarios (scripts/prerelease_check.py) under pytest, on the real aiogram Dispatcher."""

import logging
import tempfile
from pathlib import Path

import pytest

from liqguard.core import AppContext
from liqguard.i18n import EN, ZH
from liqguard.testing.prerelease import SCENARIOS, USER, Session, run_one


@pytest.mark.parametrize("lang", [EN, ZH])
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_prerelease_scenario(name, lang):
    result = await run_one(name, lang)
    assert result.ok, f"{name} [{lang}]: {result.detail}"


@pytest.mark.parametrize("name", ["book-awake", "book-snooze", "book-stop-button", "reminder-stop-button"])
async def test_harness_catches_demo_that_keeps_talking(monkeypatch, name):
    async def mute_nothing(self, user_id, source, stop=False):
        return False

    monkeypatch.setattr(AppContext, "mute_demo", mute_nothing)
    result = await run_one(name, EN)
    assert not result.ok


async def test_every_callback_is_logged_without_its_payload(caplog):
    caplog.set_level(logging.INFO, logger="liqguard.telegram.callbacks")
    with tempfile.TemporaryDirectory() as tmp:
        async with Session(EN, Path(tmp)) as s:
            alert = await s.start_demo_book()
            data = s.button(alert, "aa")
            await s.tap(alert, data, "I'm awake")

    lines = [r.getMessage() for r in caplog.records if r.name == "liqguard.telegram.callbacks"]
    assert any(f"Callback received: user={USER} lang=en action=ui:aa" in line for line in lines)
    assert any(f"Callback done: user={USER} action=ui:aa outcome=handled" in line for line in lines)
    assert all(data not in line for line in lines)
    assert all(data.split(":")[-1] not in line for line in lines)
