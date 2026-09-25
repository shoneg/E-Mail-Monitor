# mailflow-monitor

> Disclaimer: This project is completely vibe coded. Review, test, and operate it with the same care you would apply to any generated or externally contributed production code.

`mailflow-monitor` checks configured email delivery paths end to end. It sends a separate
uniquely tagged test message for each delivery in a route, optionally searches expected IMAP
mailboxes for the exact delivery token, updates a local JSON state file, and sends alert,
recovery, and aliveness notifications when configured. Send-only routes can ping external
monitors such as healthchecks.io.

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

`to` is the real SMTP delivery target. `expect_at` is the IMAP account where the
message must later be found. In direct paths both are often the same account. For an
addy.io/AnonAddy alias they differ: SMTP delivers to the alias, but the monitor verifies
the forwarded message in the external destination mailbox. For send-only routes,
`expect_at` is omitted. Every delivery uses a separate message and token, so multiple
delivery paths ending in the same verification mailbox are checked independently.

## Requirements

- Python 3.11 or newer
- SMTP access for senders and IMAP access for routes that verify delivery
- Dedicated test accounts are strongly recommended

## Development Installation

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

For a production installation used by the supplied systemd user service, install the
package into a virtual environment at a stable path:

```bash
python3.11 -m venv ~/.local/share/mailflow-monitor/venv
~/.local/share/mailflow-monitor/venv/bin/python -m pip install .
```

The service invokes the executable inside this virtual environment directly.
Activating a virtual environment is only a shell convenience and is neither needed
nor available in a systemd unit.

## Configuration

Copy the example and edit it:

```bash
cp config.example.toml config.toml
cp .env.example .env
```

`config.toml`, `.env`, `var/`, and local logs are ignored by Git.

Passwords can be stored directly in `config.toml`, but environment variables are safer.
The program automatically loads an optional `.env` file from the same directory as
the selected `config.toml`. Variables already present in the process environment take
precedence. Any string value can reference `${VARIABLE_NAME}`. If a referenced
variable is missing, validation fails with a clear error.

The effective logging level is selected in this order:
`--log-level`, `MAILFLOW_MONITOR_LOG_LEVEL` from the process environment or `.env`,
then `monitor.log_level` from `config.toml`.

Relative paths such as `state_file`, `lock_file`, and `ca_file` are resolved relative to the configuration file location.

## Configuration Reference

`[monitor]` controls state, locking, logging, timeouts, polling, send intervals, and
optional cleanup. `default_send_interval_seconds` limits how often each route sends a
new message. If it is omitted, routes run on every invocation. A route-level
`send_interval_seconds` overrides the default. `poll_interval_seconds` is separate: it
only controls how often IMAP is queried while waiting for an already-sent message.
`cleanup_received_test_messages = false` is the safe default.

`[addresses.<id>]` defines a named email address. SMTP and IMAP sections are optional because some addresses only send, only receive, or only act as aliases.

TLS modes:

- `ssl`: TLS from connection start
- `starttls`: plain connection followed by TLS upgrade
- `plain`: only accepted when `allow_insecure_plaintext = true`

Certificate verification is always enabled. Use `ca_file` for private CAs. There is no silent option to disable verification.

`[[routes]]` defines a test path. `from` must reference an address with SMTP. Each
delivery `to` must exist. Each `expect_at` entry must reference an address with IMAP.
Omit `expect_at` for a send-only route; it succeeds once the SMTP server accepts the
message.

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

Send-only healthchecks.io ping:

```toml
[addresses.healthchecks_io]
address = "your-check-uuid@hc-ping.com"

[[routes]]
id = "healthchecks-io"
from = "stalwart_sender"
send_interval_seconds = 300

[[routes.deliveries]]
to = "healthchecks_io"
```

## Manual Execution

```bash
mailflow-monitor validate-config --config ./config.toml
mailflow-monitor check --config ./config.toml
mailflow-monitor check --config ./config.toml --route stalwart-via-anonaddy
mailflow-monitor check --config ./config.toml --route stalwart-via-anonaddy --force
mailflow-monitor check --config ./config.toml --log-level DEBUG
mailflow-monitor check --config ./config.toml --json
```

Text summaries are written to stdout. Detailed logs are written to stderr.
`--force` bypasses `send_interval_seconds`, which is useful for a manual test.
All due routes run concurrently, while result output retains configuration order.
Consequently, the total wait is bounded by the slowest route rather than the sum of
all route timeouts.

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

Place `config.toml` and optional `.env` in `~/.config/mailflow-monitor/`, or edit the
service file paths. The program loads `.env` automatically from the directory
containing `config.toml`.

Enable and inspect the timer:

```bash
systemctl --user daemon-reload
systemctl --user enable --now mailflow-monitor.timer
systemctl --user status mailflow-monitor.timer
journalctl --user -u mailflow-monitor.service
```

The example timer wakes the program every minute. A route is only sent when its
configured `send_interval_seconds` has elapsed; therefore the timer should run at least
as often as the shortest desired route interval. Aliveness frequency remains a
separate configuration setting.

## Cron Alternative

```cron
*/5 * * * * cd /home/you/.config/mailflow-monitor && /home/you/.local/share/mailflow-monitor/venv/bin/mailflow-monitor check --config config.toml
```

## Logging and Troubleshooting

Use `--log-level DEBUG`, set `MAILFLOW_MONITOR_LOG_LEVEL=DEBUG` in `.env`, or set
`monitor.log_level = "DEBUG"` for detailed diagnostics. The CLI option has highest
priority. The monitor never intentionally logs passwords, tokens from configuration,
or full SMTP/IMAP credentials. Route failures include route ID, direction, affected
account where applicable, and error class.

If a state file is corrupted, the program fails instead of guessing state. Fix or remove the state file after confirming the operational impact.

## Security and Operations

- Run the alert SMTP account independently from the systems being monitored where possible, especially independently from Stalwart and addy.io.
- Use dedicated test mailboxes and restrict their permissions.
- Do not disable certificate checks. Use `ca_file` for private PKI.
- Aliveness means the monitor last completed successfully; it does not replace host monitoring.
- Keep `.env`, `config.toml`, `var/`, and logs out of version control.

## Cleanup

Received test messages are never deleted unless `cleanup_received_test_messages = true`. When
cleanup is enabled, the IMAP client marks only messages containing the exact current token as
deleted. Servers supporting UIDPLUS or IMAP4rev2 are asked to expunge only that message. On older
servers, the message remains marked as deleted for later cleanup so unrelated deleted messages are
never expunged by the monitor. Leave cleanup disabled until you have verified routing and mailbox
selection.


### Cleanup retries and warnings

With `cleanup_received_test_messages = true`, finding the exact test token is a
successful delivery even if deleting the message fails. Cleanup is processed
separately after delivery checks and their notifications. Pending work and per-account
outcomes are saved in the state file and survive process restarts.

The following optional settings belong in `[monitor]`:

```toml
cleanup_retry_count = 10
cleanup_retry_interval_seconds = 60
cleanup_failure_threshold = 10
cleanup_failure_window = 15
```

There is one initial attempt and up to `cleanup_retry_count` additional attempts.
Each pending message is attempted at most once per invocation, no earlier than
`cleanup_retry_interval_seconds` after the preceding failed attempt finishes.
There is no sleeping retry loop that holds the process open for ten minutes.
Run the timer every minute for approximately one-minute retries; a five-minute
timer also works but retries less frequently. Long delivery checks or network
operations can delay retries. Route send intervals still control test mail volume.
Updating the repository timer does not update an already installed timer: copy the
unit again, run `systemctl --user daemon-reload`, and restart the timer.

Every attempt re-searches and verifies the exact token before deleting anything.
A message already absent from the configured mailboxes needs no further cleanup.
After the last failed attempt, the monitor leaves the message alone. A message
may already carry the deleted flag if marking succeeded but expunging failed.
Disabling cleanup pauses existing queued work; removing an IMAP account discards
its queued work without accessing the mailbox.

The window counts the latest **completed cleanup outcomes per IMAP account**,
not attempts or pending jobs. A warning starts once the failure threshold is
reached; a full window is not required. For 20 failures among the last 20 outcomes,
set both threshold and window to 20. All four settings must be positive integers,
and the threshold must not exceed the window.

Warnings use the sender and recipients from `[notifications.alerts]` and require
alerts to be enabled. They have the subject `Cleanup warning` and their own
cooldown using `repeat_after_seconds`. While the threshold remains exceeded,
warnings may repeat after that cooldown. They do not open delivery incidents,
trigger recovery messages, or change delivery health. Individual cleanup failures
are logged at INFO, without sending an email.

### IMAP timeouts

A route's `timeout_seconds` (or `default_timeout_seconds`) is the delivery waiting
period after SMTP sends finish. IMAP connections use a separate 30-second socket
timeout for individual blocking network operations. Until the delivery deadline,
network timeouts, refused connections and aborted IMAP connections are retried
using the route's `poll_interval_seconds`, without sending another test message.
Permanent protocol/login errors still fail immediately. If verification remains
unavailable at the deadline, the failure identifies IMAP access rather than
claiming that non-delivery was proven. A successful query with no matching mail
continues to use `DeliveryTimeoutError` at the deadline.

The delivery deadline is checked between polling passes, so in-progress network
operations may take a pass beyond it; it is not a hard process runtime limit.
