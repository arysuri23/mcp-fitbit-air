"""Command line entry points.

`auth` is interactive and browser-based. `serve` starts the MCP server on
stdio and must never write to stdout.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .auth import AuthError, run_auth_flow
from .config import Config, ConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-fitbit-air")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="Authenticate with Google and store a refresh token")
    sub.add_parser("serve", help="Run the MCP server on stdio")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "auth":
        try:
            run_auth_flow(config)
        except AuthError as exc:
            print(f"Authentication failed: {exc}\n{exc.remedy}", file=sys.stderr)
            return 1
        print(f"Authenticated. Token saved to {config.token_path}", file=sys.stderr)
        return 0

    if args.command == "serve":
        from .server import run_server

        run_server()
        return 0

    return 2
