# Netflix Household Updater

*Netflix Household Updater* automatically monitors your email for Netflix household location update requests and clicks the confirmation link instantly. Uses Playwright (headless Chromium) for fast, leak-free browser automation.

## Features

- **IMAP IDLE** — Instant push notifications from Gmail (no polling delay)
- **Playwright on-demand** — Persistent headless browser (~50 MB RAM), reused across emails, auto-recovers on crash
- **Clean SIGTERM** — Graceful shutdown within 1 second (no zombie processes)
- **Automatic fallback** — Falls back to 5-second polling if IDLE isn't supported
- **Log rotation** — 10 MB max per file, 5 backups
- **Email organization** — Moves processed Netflix emails to a separate folder (optional)

## How It Works

```
1. Script connects to Gmail via IMAP IDLE (push notifications)
2. Gmail notifies the script instantly when a new email arrives
3. Script extracts the Netflix update link from the email
4. Playwright navigates to the link via persistent headless Chromium and clicks the button
5. Script returns to IDLE, waiting for the next email
```

**Typical response time**: Email arrives → button clicked in ~5 seconds.

## Quickstart

### Prerequisites

- Python 3.10+
- Gmail account with [App Password](https://support.google.com/accounts/answer/185833) and IMAP enabled

### Installation

```bash
git clone https://github.com/f1shl/netflix-household-update.git
cd netflix-household-update
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### Configuration

```bash
cp config.ini.example config.ini
```

Edit `config.ini` with your credentials (Netflix account + Gmail App Password).

### Run

```bash
.venv/bin/python netflix_household_update.py
```

That's it. The script runs indefinitely, monitoring for Netflix emails via IMAP IDLE.

Stop with **Ctrl+C** — the script shuts down cleanly within 1 second.

### Running in Background

**Using screen (recommended for servers):**
```bash
screen -S netflix
.venv/bin/python netflix_household_update.py
# Detach: Ctrl+A, then D
# Reattach: screen -r netflix
```

**Using systemd (production):**
```ini
# /etc/systemd/system/netflix-updater.service
[Unit]
Description=Netflix Household Updater
After=network-online.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/netflix-household-update
ExecStart=/path/to/netflix-household-update/.venv/bin/python netflix_household_update.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable netflix-updater
sudo systemctl start netflix-updater
```

### Raspberry Pi

```bash
cd /home/pi
git clone https://github.com/f1shl/netflix-household-update.git
cd netflix-household-update
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

Then set up as a systemd service or add to crontab:
```
@reboot cd /home/pi/netflix-household-update && .venv/bin/python netflix_household_update.py &
```

## Configuration Options

| Setting | Default | Description |
|---------|---------|-------------|
| `PollingIntervalSeconds` | `5` | Polling interval (only used if IDLE unavailable) |
| `ForcePolling` | `False` | Force polling mode instead of IMAP IDLE |
| `MoveEmailsToMailbox` | `True` | Move processed emails to subfolder |
| `MailboxName` | `Netflix` | Target folder for processed emails |

## Architecture

```
netflix_household_update.py    Main script (IMAP + Playwright)
├── NetflixLocationUpdate      IMAP connection, email parsing, Playwright automation
└── NetflixScheduler           IDLE/polling loop with interruptible shutdown

wrapper.py                     Optional watchdog (auto-restart on crash)
config.ini                     Credentials and settings
status.log                     Application log (rotated)
```

### IMAP IDLE vs Polling

The script defaults to **IMAP IDLE** — a push notification mechanism where Gmail tells the script when a new email arrives (instant, ~0s delay). If IDLE fails 3 times, it automatically falls back to polling (checks every 5 seconds).

Set `ForcePolling = True` in config.ini if IDLE is unreliable in your environment.

## Troubleshooting

- **Check logs**: `tail -f status.log`
- **No emails detected**: Verify IMAP credentials and that Gmail App Password is correct
- **IDLE hangs**: Set `ForcePolling = True` in config.ini
- **Playwright issues**: Run `playwright install chromium` to reinstall browser
- **Gmail IMAP**: Ensure IMAP is enabled in Gmail settings and you're using an App Password

## Wrapper (Optional)

The `wrapper.py` script is an optional watchdog that auto-restarts `netflix_household_update.py` if it crashes. With the current Playwright-based architecture, the main script is stable enough to run standalone, but the wrapper provides an extra safety net:

```bash
.venv/bin/python wrapper.py
```
