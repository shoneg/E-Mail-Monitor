# mailflow-monitor

> Disclaimer: This project is completely vibe coded. Review, test, and operate it with the same care you would apply to any generated or externally contributed production code.

`mailflow-monitor` checks configured email delivery paths end to end. It sends a unique test message through each route, searches the expected IMAP mailbox or mailboxes for the exact token, updates a local JSON state file, and sends alert, recovery, and aliveness notifications when configured.

## Architecture

The project is a Python 3.11+ package with a `src/` layout and an installable CLI named `mailflow-monitor`. Runtime code uses only the Python standard library for SMTP, IMAP, MIME parsing, TLS, TOML parsing, logging, and CLI handling.

Main components:

- `config.py` loads TOML, expands `${ENV_VAR}` references, resolves relative paths, and validates references.
- `smtp_client.py` sends messages with strict TLS defaults.
- `imap_client.py` searches configured mailboxes for the exact `X-Mailflow-Monitor-Token`.
- `monitor.py` runs routes and updates state.
- `notifications.py` applies alert, recovery, and aliveness policy.
- `state.py` stores JSON state with atomic writes and a lock file.

## Delivery Address vs Verification Mailbox

Routes deliberately separate `to` from `expect_at`.

`to` is the real SMTP delivery target. `expect_at` is the IMAP account where the message must later be found. In direct paths both are often the same account. For an addy.io/AnonAddy alias they differ: SMTP delivers to the alias, but the monitor verifies the forwarded message in the external destination mailbox.

## Requirements

- Python 3.11 or newer
- SMTP and IMAP access for the accounts used by the routes
- Dedicated test accounts are strongly recommended

## Installation

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

For a production user install:

```bash
python -m pip install .
```

## Configuration

Copy the example and edit it:

```bash
cp config.example.toml config.toml
cp .env.example .env
```

`config.toml`, `.env`, `var/`, and local logs are ignored by Git.

Passwords can be stored directly in `config.toml`, but environment variables are safer. Any string value can reference `${VARIABLE_NAME}`. If a referenced variable is missing, validation fails with a clear error.

Relative paths such as `state_file`, `lock_file`, and `ca_file` are resolved relative to the configuration file location.

## Configuration Reference

`[monitor]` controls state, locking, logging, timeouts, polling, and optional cleanup. `cleanup_received_test_messages = false` is the safe default.

`[addresses.<id>]` defines a named email address. SMTP and IMAP sections are optional because some addresses only send, only receive, or only act as aliases.

TLS modes:

- `ssl`: TLS from connection start
- `starttls`: plain connection followed by TLS upgrade
- `plain`: only accepted when `allow_insecure_plaintext = true`

Certificate verification is always enabled. Use `ca_file` for private CAs. There is no silent option to disable verification.

`[[routes]]` defines a test path. `from` must reference an address with SMTP. Each delivery `to` must exist. Each `expect_at` entry must reference an address with IMAP.

`[notifications.alerts]` sends immediate and repeated incident alerts. `repeat_after_seconds` rate-limits ongoing incidents. `send_recovery_message` controls recovery notifications.

`[notifications.aliveness]` sends periodic short health messages. With `only_when_healthy = true`, aliveness is sent only after the current complete run succeeds.

Notification recipients can be direct email addresses or `account:<id>` references.

## Example Paths

External to Stalwart:

```toml
[[routes]]
id = "external-to-stalwart"
from = "external_sender"

[[routes.deliveries]]
to = "stalwart_recipient"
expect_at = ["stalwart_recipient"]
```

Stalwart to external:

```toml
[[routes]]
id = "stalwart-to-external"
from = "stalwart_sender"

[[routes.deliveries]]
to = "external_recipient"
expect_at = ["external_recipient"]
```

Stalwart via addy.io/AnonAddy alias:

```toml
[[routes]]
id = "stalwart-via-anonaddy"
from = "stalwart_sender"

[[routes.deliveries]]
to = "anonaddy_alias"
expect_at = ["external_recipient"]
```

## Manual Execution

```bash
mailflow-monitor validate-config --config ./config.toml
mailflow-monitor check --config ./config.toml
mailflow-monitor check --config ./config.toml --route stalwart-via-anonaddy
mailflow-monitor check --config ./config.toml --json
```

Text summaries are written to stdout. Detailed logs are written to stderr.

Exit codes:

- `0`: all executed routes succeeded
- `1`: at least one route failed
- `2`: configuration error
- `3`: internal runtime error or notification delivery failure

## systemd User Timer

Install the unit files into your user systemd directory:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/mailflow-monitor.service ~/.config/systemd/user/
cp deploy/systemd/mailflow-monitor.timer ~/.config/systemd/user/
```

Place `config.toml` and optional `.env` in `~/.config/mailflow-monitor/`, or edit the service file paths.

Enable and inspect the timer:

```bash
systemctl --user daemon-reload
systemctl --user enable --now mailflow-monitor.timer
systemctl --user status mailflow-monitor.timer
journalctl --user -u mailflow-monitor.service
```

The example timer runs every five minutes. The test frequency is controlled by the timer. Aliveness frequency remains a configuration setting.

## Cron Alternative

```cron
*/5 * * * * cd /home/you/.config/mailflow-monitor && . .env && /home/you/.local/bin/mailflow-monitor check --config config.toml
```

## Logging and Troubleshooting

Set `monitor.log_level = "DEBUG"` for detailed diagnostics. The monitor never intentionally logs passwords, tokens from configuration, or full SMTP/IMAP credentials. Route failures include route ID, direction, affected account where applicable, and error class.

If a state file is corrupted, the program fails instead of guessing state. Fix or remove the state file after confirming the operational impact.

## Security and Operations

- Run the alert SMTP account independently from the systems being monitored where possible, especially independently from Stalwart and addy.io.
- Use dedicated test mailboxes and restrict their permissions.
- Do not disable certificate checks. Use `ca_file` for private PKI.
- Aliveness means the monitor last completed successfully; it does not replace host monitoring.
- Keep `.env`, `config.toml`, `var/`, and logs out of version control.

## Cleanup

Received test messages are never deleted unless `cleanup_received_test_messages = true`. When cleanup is enabled, the IMAP client deletes only messages whose headers contain the exact current token. Leave cleanup disabled until you have verified routing and mailbox selection.
