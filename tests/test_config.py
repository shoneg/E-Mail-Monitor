from __future__ import annotations

from pathlib import Path

import pytest

from mailflow_monitor.config import load_config
from mailflow_monitor.models import ConfigError, TlsMode


def test_parses_and_validates_example_config(loaded_example_config) -> None:
    config = loaded_example_config

    assert len(config.routes) == 4
    alias_route = next(route for route in config.routes if route.id == "stalwart-via-anonaddy")
    assert alias_route.deliveries[0].to == "anonaddy_alias"
    assert alias_route.deliveries[0].expect_at == ("external_recipient",)
    assert alias_route.send_interval_seconds == 3600
    assert config.addresses["stalwart_sender"].smtp.password == "stalwart-smtp"
    assert config.monitor.state_file.endswith("var/state.json")


def test_send_only_route_does_not_require_expect_at(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[monitor]
default_send_interval_seconds = 300

[addresses.sender]
address = "sender@example.net"
[addresses.sender.smtp]
host = "smtp.example.net"
port = 587
tls_mode = "starttls"
username = "sender@example.net"
password = "secret"

[addresses.healthcheck]
address = "check-id@hc-ping.com"

[[routes]]
id = "healthchecks-io"
from = "sender"
send_interval_seconds = 60
[[routes.deliveries]]
to = "healthcheck"
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.monitor.default_send_interval_seconds == 300
    assert config.routes[0].send_interval_seconds == 60
    assert config.routes[0].deliveries[0].expect_at == ()


def test_send_interval_must_be_positive(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[monitor]
default_send_interval_seconds = 0

[addresses.sender]
address = "sender@example.net"
[addresses.sender.smtp]
host = "smtp.example.net"
port = 587
tls_mode = "starttls"
username = "sender@example.net"
password = "secret"

[addresses.target]
address = "target@example.net"

[[routes]]
id = "route"
from = "sender"
[[routes.deliveries]]
to = "target"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="default_send_interval_seconds"):
        load_config(path)


def test_missing_environment_variable_fails(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[monitor]
state_file = "var/state.json"
lock_file = "var/lock"

[addresses.sender]
address = "sender@example.net"
[addresses.sender.smtp]
host = "smtp.example.net"
port = 587
tls_mode = "starttls"
username = "sender@example.net"
password = "${MISSING_PASSWORD}"

[addresses.recipient]
address = "recipient@example.net"
[addresses.recipient.imap]
host = "imap.example.net"
port = 993
tls_mode = "ssl"
username = "recipient@example.net"
password = "secret"
mailboxes = ["INBOX"]

[[routes]]
id = "route"
from = "sender"
[[routes.deliveries]]
to = "recipient"
expect_at = ["recipient"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="MISSING_PASSWORD"):
        load_config(path)


def test_environment_expansion_happens_after_toml_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dangerous_value = 'quote" and newline\n[[routes]]\nid = "not-a-real-route"'
    monkeypatch.setenv("SPECIAL_PASSWORD", dangerous_value)
    path = tmp_path / "config.toml"
    path.write_text(
        """
[monitor]
state_file = "var/state.json"
lock_file = "var/lock"

[addresses.sender]
address = "sender@example.net"
[addresses.sender.smtp]
host = "smtp.example.net"
port = 587
tls_mode = "starttls"
username = "sender@example.net"
password = "${SPECIAL_PASSWORD}"

[addresses.recipient]
address = "recipient@example.net"
[addresses.recipient.imap]
host = "imap.example.net"
port = 993
tls_mode = "ssl"
username = "recipient@example.net"
password = "secret"
mailboxes = ["INBOX"]

[[routes]]
id = "route"
from = "sender"
[[routes.deliveries]]
to = "recipient"
expect_at = ["recipient"]
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.addresses["sender"].smtp.password == dangerous_value


def test_invalid_route_reference_reports_config_path(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[monitor]
state_file = "var/state.json"
lock_file = "var/lock"

[addresses.sender]
address = "sender@example.net"
[addresses.sender.smtp]
host = "smtp.example.net"
port = 587
tls_mode = "starttls"
username = "sender@example.net"
password = "secret"

[[routes]]
id = "route"
from = "sender"
[[routes.deliveries]]
to = "missing"
expect_at = ["missing"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"routes\[0\]\.deliveries\[0\]\.to"):
        load_config(path)


def test_plain_tls_requires_explicit_insecure_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[monitor]
state_file = "var/state.json"
lock_file = "var/lock"

[addresses.sender]
address = "sender@example.net"
[addresses.sender.smtp]
host = "smtp.example.net"
port = 25
tls_mode = "plain"
username = "sender@example.net"
password = "secret"

[addresses.recipient]
address = "recipient@example.net"
[addresses.recipient.imap]
host = "imap.example.net"
port = 993
tls_mode = "ssl"
username = "recipient@example.net"
password = "secret"
mailboxes = ["INBOX"]

[[routes]]
id = "route"
from = "sender"
[[routes.deliveries]]
to = "recipient"
expect_at = ["recipient"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="allow_insecure_plaintext"):
        load_config(path)


def test_plain_tls_allowed_with_explicit_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[monitor]
state_file = "var/state.json"
lock_file = "var/lock"

[addresses.sender]
address = "sender@example.net"
[addresses.sender.smtp]
host = "smtp.example.net"
port = 25
tls_mode = "plain"
allow_insecure_plaintext = true
username = "sender@example.net"
password = "secret"

[addresses.recipient]
address = "recipient@example.net"
[addresses.recipient.imap]
host = "imap.example.net"
port = 143
tls_mode = "plain"
allow_insecure_plaintext = true
username = "recipient@example.net"
password = "secret"
mailboxes = ["INBOX"]

[[routes]]
id = "route"
from = "sender"
[[routes.deliveries]]
to = "recipient"
expect_at = ["recipient"]
""",
        encoding="utf-8",
    )

    config = load_config(path)
    assert config.addresses["sender"].smtp.tls_mode is TlsMode.PLAIN
