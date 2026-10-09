"""Run the daemon: python -m harness"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from . import config as config_mod
from .api import create_app
from .manager import Manager


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-harness daemon")
    parser.add_argument("--config-dir")
    parser.add_argument("--data-dir")
    args = parser.parse_args()

    cfg = config_mod.load(args.config_dir, args.data_dir)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(Manager(cfg))
    # One process, one event loop: the GPU scheduler and approval waiters live in memory.
    # No proxy headers: the request's client must stay the real TCP peer, which the tailscaled check looks up
    # (harness/tailscale_peer.py). uvicorn would otherwise take it from X-Forwarded-For on loopback connections.
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info", proxy_headers=False, forwarded_allow_ips="")


if __name__ == "__main__":
    main()
