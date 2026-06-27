from __future__ import annotations

import pytest

from mailflow_monitor.cli import build_parser


def test_check_log_level_argument_is_case_insensitive() -> None:
    args = build_parser().parse_args(["check", "--log-level", "debug"])

    assert args.log_level == "DEBUG"


def test_validate_config_rejects_unknown_log_level() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["validate-config", "--log-level", "verbose"])

    assert exc_info.value.code == 2
