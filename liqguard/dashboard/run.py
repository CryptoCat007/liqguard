"""
Standalone dashboard runner (web only, demo data).

    liqguard-dashboard --port 8765
    python -m liqguard.dashboard.run --port 8765

To get alerts on Telegram at the same time, run the bot with `liqguard --dashboard`.
"""

import argparse
import logging
import sys

import uvicorn
from dotenv import load_dotenv

from liqguard.llm import MetaLLMClient

from .server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def run_dashboard() -> None:
    parser = argparse.ArgumentParser(description="LiqGuard web dashboard (read-only, demo data)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    args = parser.parse_args()

    load_dotenv()
    llm_client = MetaLLMClient()
    logger.info(f"Muse: {'mock mode' if llm_client.is_mock else 'live API'}")
    logger.info(f"Dashboard: http://{args.host}:{args.port}")

    uvicorn.run(create_app(llm_client=llm_client), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    run_dashboard()
