from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import build_app
from .bridges.feishu import FeishuConfigError
from .config import ConfigError, load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="dsh-feishu-bridge")
    parser.add_argument(
        "--config", default=None, help="Path to a YAML config file (optional)"
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Python logging level (default INFO)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = load_settings(args.config)
        app = build_app(settings)
    except (ConfigError, FeishuConfigError, RuntimeError) as exc:
        print(f"dsh-feishu-bridge: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
