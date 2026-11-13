"""Start the Earnings Spread Desk:  python -m earningsdesk [--demo] [--config settings.json] [--no-browser]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import warnings
import webbrowser
from pathlib import Path

from . import config as config_mod
from .server import serve
from .service import App


def main(argv=None):
    ap = argparse.ArgumentParser(prog="earningsdesk", description="Earnings calendar spread screens from IBKR data.")
    ap.add_argument("--config", default=None, help="settings file (default: settings.json in ~/EarningsDesk)")
    ap.add_argument("--demo", action="store_true", help="replay the course sample week, no TWS or internet needed")
    ap.add_argument("--no-browser", action="store_true", help="do not open the page in a browser")
    ap.add_argument("--port", type=int, default=None, help="web server port (default from settings, 8060)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    if args.config is None:
        home = Path.home() / ("EarningsDesk-demo" if args.demo else "EarningsDesk")
        home.mkdir(parents=True, exist_ok=True)
        args.config = str(home / "settings.json")
    cfg = config_mod.load(args.config)
    errors = config_mod.validate(cfg)
    if errors:
        print("Settings problems:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 2
    if sys.platform == "win32":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_run(cfg, args))
    return 0


def build_app(cfg, config_path: str, demo: bool) -> App:
    if demo:
        from . import demo as D
        cfg.data_dir = cfg.data_dir or str(Path(config_path).parent)
        cfg.earnings.finnhub_api_key = cfg.earnings.finnhub_api_key or "demo"   # the canned Finnhub feed
        cfg.strategy.screen_days_ahead = max(3, cfg.strategy.screen_days_ahead)  # screen the whole sample week
        return App(cfg, config_path, D.FakeIB, demo=True, fetcher=D.FakeFetcher(), clock=D.replay_clock)
    from ib_async import IB
    return App(cfg, config_path, IB)


async def _run(cfg, args):
    app = build_app(cfg, args.config, args.demo)
    port = args.port or cfg.server.port
    runner = await serve(app, cfg.server.host, port)
    url = f"http://{'127.0.0.1' if cfg.server.host in ('0.0.0.0', '') else cfg.server.host}:{port}/"
    logging.info("Earnings Spread Desk is running at %s%s", url, "  (demo: course sample week)" if args.demo else "")
    await app.start()
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        await asyncio.Event().wait()
    finally:
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
