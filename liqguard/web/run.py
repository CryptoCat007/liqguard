"""
Standalone dashboard (web only, no Telegram polling).

    liqguard-dashboard --port 8765
    python -m liqguard.web.run --port 8765

It shares the database with the bot, so `/dashboard` login links issued by a
running bot work here too. Pass `--public-only` to serve just the demo page.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys

import uvicorn
from dotenv import load_dotenv

from liqguard.ai import MetaLLMClient
from liqguard.config import Settings
from liqguard.core import AppContext
from liqguard.logsafe import install_audit_log, install_log_redaction

from .server import create_app

logger = logging.getLogger(__name__)


async def _serve(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    settings = dataclasses.replace(settings, dashboard_host=args.host or settings.dashboard_host,
                                   dashboard_port=args.port or settings.dashboard_port)
    redaction = install_log_redaction(settings.telegram_token, settings.meta_api_key, settings.session_secret)
    install_audit_log(settings.audit_log_path, redaction)

    if args.public_only:
        app = create_app(llm_client=MetaLLMClient(api_key=settings.meta_api_key))
        logger.info("Public demo only (login disabled)")
    else:
        ctx = await AppContext.create(settings)
        ctx.market.start()
        app = create_app(ctx)
        logger.info("Muse: %s · master key source: %s", "mock" if ctx.llm.is_mock else "live", ctx.storage.key_source)

    host, port = settings.dashboard_host, settings.dashboard_port
    logger.info("Dashboard: http://%s:%s (public demo at /, sign-in at /login)", host, port)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    await uvicorn.Server(config).serve()


def run_dashboard() -> None:
    parser = argparse.ArgumentParser(description="LiqGuard web dashboard")
    parser.add_argument("--host", default=None, help="Bind address (default: LIQGUARD_DASHBOARD_HOST, else 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None,
                        help="Port (default: LIQGUARD_DASHBOARD_PORT, else $PORT, else 8765)")
    parser.add_argument("--public-only", action="store_true", help="Serve only the public demo page")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    load_dotenv()
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_dashboard()
