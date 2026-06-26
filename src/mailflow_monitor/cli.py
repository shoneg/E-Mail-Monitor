"""Command line interface for mailflow-monitor."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .logging_utils import configure_logging
from .models import ConfigError
from .monitor import MailflowMonitor, render_text_summary, result_to_json

EXIT_OK = 0
EXIT_ROUTE_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_RUNTIME_ERROR = 3

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""

    parser = argparse.ArgumentParser(prog="mailflow-monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="validate configuration only")
    validate.add_argument("--config", default="config.toml", help="path to TOML configuration")

    check = subparsers.add_parser("check", help="run mailflow checks")
    check.add_argument("--config", default="config.toml", help="path to TOML configuration")
    check.add_argument(
        "--route",
        action="append",
        help="route ID to run; can be used multiple times",
    )
    check.add_argument("--json", action="store_true", help="print machine-readable JSON result")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        configure_logging(config.monitor.log_level)
        if args.command == "validate-config":
            print(f"Configuration OK: {config.config_path}")
            return EXIT_OK

        monitor = MailflowMonitor(config)
        result = monitor.check(route_ids=args.route)
        print(result_to_json(result) if args.json else render_text_summary(result))
        if result.notification_failed:
            return EXIT_RUNTIME_ERROR
        return EXIT_OK if result.success else EXIT_ROUTE_FAILED
    except ConfigError as exc:
        configure_logging("INFO")
        LOGGER.error("%s", exc)
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as exc:
        LOGGER.exception("Internal runtime error")
        print(f"Runtime error: {exc.__class__.__name__}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
