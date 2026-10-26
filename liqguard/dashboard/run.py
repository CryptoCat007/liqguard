"""
Standalone dashboard runner.

Run with: python -m liqguard.dashboard.run
Or: liqguard-dashboard (console script)
"""

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from liqguard.llm import MetaLLMClient

from .server import DashboardServer, get_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


async def main(host: str, port: int) -> None:
    """Run dashboard server."""
    load_dotenv()
    
    meta_api_key = os.environ.get("META_API_KEY")
    llm_client = MetaLLMClient(api_key=meta_api_key)
    
    logger.info(f"Starting LiqGuard Dashboard...")
    logger.info(f"Meta AI: {'mock mode (demo)' if llm_client.is_mock else 'live API'}")
    
    server = DashboardServer(host=host, port=port, llm_client=llm_client)
    
    try:
        await server.start()
        
        while True:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await server.stop()


def run_dashboard() -> None:
    """Console script entry point."""
    parser = argparse.ArgumentParser(description="LiqGuard Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    args = parser.parse_args()
    
    asyncio.run(main(args.host, args.port))


if __name__ == "__main__":
    run_dashboard()
