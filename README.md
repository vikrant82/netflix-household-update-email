# Netflix Household Updater

*Netflix Household Updater* is an easy to use Python script to automatically fetch Netflix household location update 
emails from your mailbox and click the confirmation link instantly.
The script runs indefinitely and can easily be deployed on a Raspberry Pi or server.

## Features

- **IMAP IDLE support** - Instant email notifications (push) instead of polling for near-zero latency
- **Automatic fallback** - Uses 5-second polling if IMAP server doesn't support IDLE
- **Memory leak protection** - Periodic WebDriver refresh and bounded email cache
- **Log rotation** - Automatic log file rotation (10MB max, 5 backups)
- **Automatic household confirmation** - Clicks Netflix update link without manual intervention
- **Email organization** - Moves processed Netflix emails to a separate folder (optional)
- **Runs indefinitely** - Designed for long-term unattended operation

## Quickstart

Basic installation instruction

### Prerequisites

**Chrome/Chromium Browser:**
The script requires Chrome or Chromium browser to be installed on your system.

**ChromeDriver (Automatic - Recommended):**
The script uses Selenium 4's automatic ChromeDriver management by default, which automatically downloads the correct ChromeDriver version matching your Chrome browser. No manual setup needed!

**ChromeDriver (Manual - Optional):**
If you prefer manual control, you can:
1. Download ChromeDriver manually from https://chromedriver.chromium.org/downloads
2. Set the path in `config.ini`: `ExecutablePath = /path/to/chromedriver`

**Note:** The `chromedriver-py` package is no longer recommended due to frequent version mismatches.

### Installation

To install all dependencies, simply install the requirements first:

    python -m pip install -r requirements.txt

### Usage

Make sure to fill out the config.ini file with the correct parameters for your email mailbox and Netflix credentials first!

#### Manual Run with Virtual Environment

For testing or manual operation, it's recommended to use a virtual environment:

```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
# On Linux/macOS:
source .venv/bin/activate
# On Windows:
# .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the script
python netflix_household_update.py
```

The script will:
1. Connect to your IMAP server
2. Automatically detect if your email provider supports IMAP IDLE (push notifications)
3. If IDLE is supported (Gmail, Outlook, most modern providers): Wait for instant email notifications
4. If IDLE is NOT supported: Poll the mailbox every 5 seconds (configurable in config.ini)
5. Process Netflix household update emails immediately and click the confirmation link
6. Run indefinitely with automatic resource management (WebDriver refresh every 6 hours)

The script can be stopped by pressing **Ctrl+C**

**Note:** Most email providers (Gmail, Outlook, iCloud, etc.) support IMAP IDLE, giving you instant notifications with zero waiting time!

#### Running in Background (Optional)

If you want to run the script in the background without keeping a terminal open:

**Using screen (recommended for remote servers):**
```bash
# Install screen if needed
sudo apt-get install screen  # Debian/Ubuntu
# or
brew install screen  # macOS

# Start a screen session
screen -S netflix

# Run the script (in the screen session)
source .venv/bin/activate
python netflix_household_update.py

# Detach from screen: Press Ctrl+A, then D
# To reattach later: screen -r netflix
# To list sessions: screen -ls
```

**Using tmux:**
```bash
# Install tmux if needed
sudo apt-get install tmux  # Debian/Ubuntu

# Start a tmux session
tmux new -s netflix

# Run the script
source .venv/bin/activate
python netflix_household_update.py

# Detach from tmux: Press Ctrl+B, then D
# To reattach later: tmux attach -t netflix
```

**Using nohup (simple background process):**
```bash
# Run in background with output to nohup.out
nohup .venv/bin/python netflix_household_update.py &

# Or redirect output to custom log
nohup .venv/bin/python netflix_household_update.py > output.log 2>&1 &

# Check if it's running
ps aux | grep netflix_household_update

# Stop the process
pkill -f netflix_household_update.py
```

### Installation on Raspberry Pi

To start the script at the startup of Raspberry Pi, crontab can be used. 
For the following commands, it is assumed that the default user *pi* exists. If not, replace the username with the correct one'
The easiest way ist to use SSH connection and execute the following commands:

    cd /home/pi
    git clone https://github.com/f1shl/netflix-household-update.git
    cd netflix-household-update
    python -m venv .venv
    .venv/bin/pip install -r requirements.txt

Now update all the parameters in the config.ini with your own Email provider data.
Start the script and check if it runs without errors.
If everything works, break with **CTRL+C**

Edit crontab:

    crontab -e

Select nano as editor. Go to the end of the file and add the following line:

    @reboot /home/pi/netflix-household-update/netflix-household-update-launcher.sh &

Save with **CTRL+X**, **Y** and finally press **Return**
Now restart the Raspberry Pi:

    sudo reboot

The script should now be started after each startup and runs in an infinite loop.

## Performance & Reliability Optimizations

The script is designed to run indefinitely without requiring restarts:

### Memory Management
- **WebDriver Auto-Refresh**: Chrome WebDriver is automatically recreated every 6 hours to prevent memory leaks
- **Bounded Email Cache**: Only keeps last 100 processed email UIDs in memory (prevents unbounded growth)
- **Log Rotation**: Automatic log file rotation with max 10MB per file, 5 backup files (50MB total max)

### Efficiency
- **IMAP IDLE Push Notifications**: When supported by email provider, eliminates constant polling
  - Instant email processing (0-second delay)
  - Single persistent connection instead of 17,000+ daily connections
  - Minimal CPU and network usage
- **Smart Fallback**: Automatically falls back to 5-second polling if IDLE not supported

### Configuration Options

Add these to your `config.ini` under the `[GENERAL]` section:

```ini
[GENERAL]
# Polling interval in seconds (only used if IMAP IDLE is not supported)
PollingIntervalSeconds = 5

# Move processed emails to a subfolder
MoveEmailsToMailbox = True
MailboxName = Netflix
```

## Troubleshooting

- **Script stops after a day**: This should no longer happen due to memory management improvements
- **No email notifications**: Check that your config.ini has correct IMAP credentials and server
- **IDLE not working**: Script will automatically fall back to polling - check logs for "IDLE not supported" message
- **ChromeDriver version mismatch** (e.g., "This version of ChromeDriver only supports Chrome version X"):
  - **Solution**: Set `UseChromedriverPy = False` in config.ini (or remove the line entirely)
  - This enables Selenium's automatic ChromeDriver management which always downloads the correct version
  - The script will handle ChromeDriver automatically on next run
- **Gmail IMAP issues**: Ensure you're using an App Password (not your regular Gmail password) and IMAP is enabled in Gmail settings