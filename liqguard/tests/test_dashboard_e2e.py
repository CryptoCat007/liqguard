"""
End to end: the real dashboard (uvicorn) talking to a fake Meta API (also
uvicorn) over HTTP, with browsers on real WebSockets. Every request that
answers `muse_pending: true` must end with exactly one `rescue_muse` /
`report_muse` event carrying its `request_id`, on every connection and in
every language, even when the demo is stopped while Muse is answering, and a
viewer that connects afterwards gets it replayed.
"""

import asyncio
import collections
import json
import random
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI, Request

from liqguard.ai import MUSE, MetaLLMClient
from liqguard.web import create_app

from .test_muse_api import completion
from .test_muse_async import SmartMuse

SYMBOLS = ("BTCUSDT", "ETH", "SOLUSDT", "TSLAUSDT")
FOLLOW_UPS = ("rescue_muse", "report_muse")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Served:
    def __init__(self, app):
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


def fake_meta() -> tuple[FastAPI, list]:
    app, calls, smart, rng = FastAPI(), [], SmartMuse(), random.Random(7)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        system = body["messages"][0]["content"]
        kind = "rescue" if "rescue options" in system else "stop" if "stop-loss" in system else "other"
        calls.append((kind, body.get("reasoning_effort")))
        await asyncio.sleep(rng.uniform(0.4, 1.2))
        data = smart.answer(body["messages"][0]["content"], body["messages"][1]["content"])
        return completion(json.dumps(data) if data is not None else "Muse answer.")

    return app, calls


@pytest.fixture
def servers(monkeypatch):
    monkeypatch.setattr("liqguard.web.server.PUBLIC_PRECOMPUTE_TOP", 0)
    meta_app, calls = fake_meta()
    meta = _Served(meta_app)
    llm = MetaLLMClient(api_key="sk-test", base_url=f"http://127.0.0.1:{meta.port}/v1", use_mock=False)
    web = _Served(create_app(llm_client=llm))
    yield web.port, calls
    web.close()
    meta.close()


class Viewer:
    """One browser tab on /ws/demo: every follow-up it receives, by request id."""

    def __init__(self, name: str):
        self.name = name
        self.events: dict[str, list[dict]] = collections.defaultdict(list)
        self.ws = None
        self.task = None

    async def open(self, port: int, query: str = "") -> "Viewer":
        self.ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/demo{query}")
        assert json.loads(await self.ws.recv())["type"] == "snapshot"
        self.task = asyncio.create_task(self._read())
        return self

    async def _read(self) -> None:
        async for message in self.ws:
            event = json.loads(message)
            if event["type"] in FOLLOW_UPS:
                self.events[event["data"]["request_id"]].append(event)

    async def close(self) -> None:
        await self.ws.close()
        self.task.cancel()


def requests() -> list[tuple[str, str, str]]:
    """(event type, lang, url): 8 rescue texts (4 symbols x EN/zh) and 8 report buttons (rescue EN, stop-loss zh)."""
    out = []
    for s in SYMBOLS:
        out += [("rescue_muse", lang, f"/api/demo/rescue?symbol={s}&lang={lang}") for lang in ("en", "zh-Hant")]
        out.append(("report_muse", "en", f"/api/demo/position-tool?symbol={s}&tool=rescue&lang=en"))
        out.append(("report_muse", "zh-Hant", f"/api/demo/position-tool?symbol={s}&tool=stop&lang=zh-Hant"))
    return out


async def until(check, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.05)
    return check()


async def test_every_announced_request_ends_with_one_event_for_every_viewer(servers):
    port, calls = servers
    viewers = [await Viewer("en").open(port), await Viewer("en-2").open(port, "?lang=en"),
               await Viewer("zh").open(port, "?lang=zh-Hant")]
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base, timeout=30, headers={"X-LiqGuard-CSRF": "1"}) as http:
        assert (await http.post("/api/demo/simulate")).status_code == 200
        sent = requests()
        responses = await asyncio.gather(*(http.get(url) for _, _, url in sent))
        # Someone stops the demo while Muse is still answering: nothing announced may be dropped.
        assert (await http.post("/api/demo/simulate/stop")).status_code == 200

    bodies = [r.json() for r in responses]
    assert all(r.status_code == 200 for r in responses)
    announced = {b["request_id"]: (kind, lang) for (kind, lang, _), b in zip(sent, bodies) if b["muse_pending"]}
    assert len(announced) == 16 and len(set(announced)) == 16

    def complete() -> bool:
        return all(rid in v.events for v in viewers for rid in announced)

    assert await until(complete, timeout=20), {v.name: len(announced.keys() - v.events.keys()) for v in viewers}
    await asyncio.sleep(0.3)
    for v in viewers:
        assert set(v.events) == set(announced), v.name
        for rid, (kind, lang) in announced.items():
            (event,) = v.events[rid]
            assert event["type"] == kind and event["data"]["lang"] == lang
            assert event["data"].get("muse_failed", False) is False

    by_url = {url: v.events[b["request_id"]][0]["data"] for (_, _, url), b in zip(sent, bodies) for v in viewers[:1]}
    for (kind, lang, url) in sent:
        data = by_url[url]
        if kind == "report_muse":
            report = data["report"]
            assert report["muse"]["source"] == MUSE and "✦ Muse" in report["lines"][0], url
            assert "$0.00" not in report["muse"]["text"], url
        else:
            explanation = data["explanation"]
            assert explanation["source"] == MUSE and "$0.00" not in explanation["headline"], url
            if "SOLUSDT" in url or "TSLAUSDT" in url:
                assert explanation["recommended_option"] == "none", url
    explanations = [effort for kind, effort in calls if kind in ("rescue", "stop")]
    assert len(explanations) == 8 and set(explanations) == {"minimal"}  # one per plan / check, EN+zh together

    late = [await Viewer("late-en").open(port), await Viewer("late-zh").open(port, "?lang=zh-Hant")]
    assert await until(lambda: all(set(v.events) >= set(announced) for v in late), timeout=5)
    await asyncio.sleep(0.3)
    for v in late:
        assert all(len(v.events[rid]) == 1 for rid in announced), v.name
    for v in viewers + late:
        await v.close()
