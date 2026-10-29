#!/usr/bin/env python3
"""
LiqGuard pre-release check. Every release must pass it.

Drives the production aiogram Dispatcher with real Telegram updates against a fake Bot API
(no network, no token, temporary databases), in English and 繁體中文:

    /start and every main-menu button, every command, the language switch, the bind flow,
    a full untouched demo run, and ✅ I'm awake / 😴 Snooze / ⏹ Stop demo / /stopdemo on both
    the market simulation and the static demo book — after which no demo message may arrive.

Exit code 0 when every scenario passes, 1 otherwise (including any unhandled update or callback).

    python scripts/prerelease_check.py                 # everything
    python scripts/prerelease_check.py --list
    python scripts/prerelease_check.py book-awake sim-awake --lang en
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from liqguard.i18n import EN, ZH  # noqa: E402
from liqguard.testing.prerelease import SCENARIOS, Result, run_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenarios", nargs="*", help="Scenario names (default: all)")
    parser.add_argument("--lang", choices=("en", "zh-Hant", "both"), default="both")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show the app's log output")
    args = parser.parse_args()

    if args.list:
        for name, fn in SCENARIOS.items():
            print(f"{name:24} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0
    unknown = [n for n in args.scenarios if n not in SCENARIOS]
    if unknown:
        print(f"Unknown scenario(s): {', '.join(unknown)} (see --list)", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO if args.verbose else logging.CRITICAL,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    langs = (EN, ZH) if args.lang == "both" else (args.lang,)

    def report(r: Result) -> None:
        mark = "PASS" if r.ok else "FAIL"
        print(f"{mark}  {r.scenario:24} {r.lang:8} {r.seconds:5.1f}s  {r.detail}", flush=True)

    results = asyncio.run(run_all(args.scenarios or None, langs, report))
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f", {len(failed)} FAILED" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
