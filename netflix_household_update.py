# -*- coding: utf-8 -*-
"""
Netflix Household Update — Playwright Edition

Monitors a Gmail inbox for Netflix household update emails,
extracts the confirmation link, and uses Playwright (headless Chromium)
to click the "Update" button.

Key design:
- Persistent browser: Chromium stays alive for the script's lifetime (~50-100MB),
  eliminating 3s startup cost on each email. Auto-recovers if browser crashes.
- IMAP IDLE with 1s socket timeout: instant email notifications + fast SIGTERM response
- Skip redundant SELECT after IDLE: saves ~0.5s per email detection
- threading.Event for interruptible shutdown: clean SIGTERM within 1 second
"""

import imaplib
import email
import time
import socket
import logging
from logging.handlers import RotatingFileHandler
import configparser
import re
import datetime
import signal
import sys
import threading
from typing import List, Optional
from collections import deque

# --- Constants ---
SENDER_EMAILS = ["info@account.netflix.com"]
NETFLIX_LINK_START_PATTERNS = [
    "www.netflix.com/account/update-primary",
    "www.netflix.com/account/set-primary",
]
BUTTON_SELECTOR = 'button[data-uia="set-primary-location-action"]'
LOGIN_EMAIL_SELECTOR = 'input[name="userLoginId"]'
LOGIN_PASSWORD_SELECTOR = 'input[name="password"]'
LOGIN_SUBMIT_SELECTOR = 'button[data-uia="login-submit-button"]'
LOGIN_ERROR_SELECTOR = '[data-uia="login-error-message"]'

LOG_FILENAME = "status.log"
IMAP_IDLE_TIMEOUT_SECONDS = 15 * 60  # 15 min per IDLE session
IMAP_IDLE_MAX_FAILURES = 3
IDLE_SOCKET_TIMEOUT = 1  # seconds — controls max SIGTERM response time
IMAP_COMMAND_TIMEOUT = (
    30  # seconds — timeout for non-IDLE IMAP commands (store, copy, etc.)
)

# Global shutdown event: set by signal handler, checked in all blocking loops
shutdown_event = threading.Event()


# --- Logging ---
def setup_logging():
    """Configures logging with rotation (10 MB, 5 backups)."""
    handler = RotatingFileHandler(
        LOG_FILENAME, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logging.info("---------------- Script started ----------------")


def close_logging():
    logging.info("---------------- Script shutdown ----------------\n")


# --- Main Class ---
class NetflixLocationUpdate:
    """Monitors inbox for Netflix household emails and clicks the update button via Playwright."""

    def __init__(self, config_path: str = "config.ini"):
        self._load_config(config_path)
        self._processed_email_uids: deque = deque(maxlen=100)
        self._mail: Optional[imaplib.IMAP4_SSL] = None
        self._idle_supported: Optional[bool] = None
        self._idle_failure_count: int = 0
        self._idle_disabled: bool = False
        # Persistent browser — launched once, reused for script lifetime
        self._pw_instance = None
        self._pw_browser = None
        self._browser_lock = threading.Lock()
        self._connect_imap()
        self._ensure_target_mailbox_exists()

    def _load_config(self, config_path: str):
        self._config = configparser.ConfigParser()
        if not self._config.read(config_path):
            raise FileNotFoundError(
                f"Configuration file '{config_path}' not found or empty."
            )

        for section in ["EMAIL", "NETFLIX"]:
            if section not in self._config:
                raise ValueError(
                    f"Missing required section '{section}' in {config_path}"
                )

        self._imap_server = self._config.get("EMAIL", "ImapServer")
        self._imap_port = self._config.getint("EMAIL", "ImapPort")
        self._imap_username = self._config.get("EMAIL", "Username")
        self._imap_password = self._config.get("EMAIL", "Password")
        self._mailbox_name = self._config.get("EMAIL", "Mailbox", fallback="INBOX")
        self._netflix_username = self._config.get("NETFLIX", "Username")
        self._netflix_password = self._config.get("NETFLIX", "Password")

        self._move_to_mailbox = self._config.getboolean(
            "GENERAL", "MoveEmailsToMailbox", fallback=False
        )
        self._move_to_mailbox_name = self._config.get(
            "GENERAL", "MailboxName", fallback="Netflix"
        )

        logging.info("Configuration loaded.")

    # --- IMAP Connection ---

    def _connect_imap(self) -> bool:
        if self._mail and self._is_imap_connected():
            return True
        self._disconnect_imap()

        logging.info(f"Connecting to IMAP server: {self._imap_server}")
        try:
            self._mail = imaplib.IMAP4_SSL(self._imap_server, self._imap_port)
            typ, data = self._mail.login(self._imap_username, self._imap_password)
            if typ == "OK":
                logging.info(f"Connected to IMAP server {self._imap_server}")
                self._mail.sock.settimeout(IMAP_COMMAND_TIMEOUT)
                return True
            logging.error(f"IMAP login failed: {data}")
            self._mail = None
            return False
        except (imaplib.IMAP4.error, socket.error, OSError) as e:
            logging.error(f"Failed to connect to IMAP: {e}")
            self._mail = None
            return False

    def _disconnect_imap(self):
        if self._mail:
            logging.info("Closing IMAP connection.")
            try:
                self._mail.close()
            except Exception:
                pass
            try:
                self._mail.logout()
            except Exception:
                pass
            self._mail = None

    def _is_imap_connected(self) -> bool:
        if not self._mail:
            return False
        try:
            status, _ = self._mail.noop()
            return status == "OK"
        except Exception:
            return False

    # --- IMAP IDLE ---

    def _check_idle_support(self) -> bool:
        if self._idle_supported is not None:
            return self._idle_supported
        if not self._mail:
            self._idle_supported = False
            return False
        try:
            try:
                self._mail.select(self._mailbox_name)
            except Exception:
                pass
            caps = self._mail.capabilities
            self._idle_supported = b"IDLE" in caps or "IDLE" in caps
            if self._idle_supported:
                logging.info("IMAP server supports IDLE")
            else:
                logging.warning("IMAP server does NOT support IDLE. Will poll.")
            return self._idle_supported
        except Exception as e:
            logging.warning(f"Failed to check IDLE capability: {e}")
            self._idle_supported = False
            return False

    def _wait_for_new_email_idle(self) -> bool:
        """
        Waits for new email via IMAP IDLE with interruptible shutdown checks.

        Uses IDLE_SOCKET_TIMEOUT-second socket timeouts so we can check
        shutdown_event between reads, responding to SIGTERM within seconds.
        """
        if not self._mail:
            return False
        try:
            status, _ = self._mail.select(self._mailbox_name)
            if status != "OK":
                return False

            tag = self._mail._new_tag().decode()
            self._mail.send(f"{tag} IDLE\r\n".encode())

            line = self._mail.readline()
            if b"idling" not in line.lower():
                logging.warning(f"Unexpected IDLE response: {line}")
                self._mail.send(b"DONE\r\n")
                self._mail.readline()
                return False

            logging.info(
                f"IDLE mode active (timeout: {IMAP_IDLE_TIMEOUT_SECONDS}s, "
                f"check interval: {IDLE_SOCKET_TIMEOUT}s)"
            )

            deadline = time.time() + IMAP_IDLE_TIMEOUT_SECONDS
            new_email_arrived = False
            self._mail.sock.settimeout(IDLE_SOCKET_TIMEOUT)

            try:
                while time.time() < deadline and not shutdown_event.is_set():
                    try:
                        line = self._mail.readline()
                        if line:
                            line_str = line.decode("utf-8", errors="ignore")
                            if "EXISTS" in line_str or "RECENT" in line_str:
                                logging.info(
                                    f"New email notification: {line_str.strip()}"
                                )
                                new_email_arrived = True
                                break
                    except socket.timeout:
                        continue  # Normal — check shutdown_event and loop
            except Exception as e:
                logging.warning(f"Error in IDLE wait: {e}")

            self._mail.send(b"DONE\r\n")
            try:
                self._mail.readline()
            except Exception:
                pass

            # Restore longer timeout for non-IDLE IMAP commands
            self._mail.sock.settimeout(IMAP_COMMAND_TIMEOUT)

            if new_email_arrived:
                self._idle_failure_count = 0
            return new_email_arrived

        except Exception as e:
            logging.error(f"IDLE error: {e}", exc_info=True)
            self._idle_failure_count += 1
            if self._idle_failure_count >= IMAP_IDLE_MAX_FAILURES:
                logging.error(
                    "IDLE disabled after repeated failures. Falling back to polling."
                )
                self._idle_disabled = True
            try:
                self._mail.send(b"DONE\r\n")
            except Exception:
                pass
            try:
                self._mail.sock.settimeout(IMAP_COMMAND_TIMEOUT)
            except Exception:
                pass
            return False

    # --- Email Search & Processing ---

    def _ensure_target_mailbox_exists(self):
        if self._move_to_mailbox and self._mail:
            try:
                self._mail.create(self._move_to_mailbox_name)
                status, _ = self._mail.select(self._move_to_mailbox_name)
                if status == "OK":
                    logging.info(f"Mailbox '{self._move_to_mailbox_name}' ready.")
                self._mail.select(self._mailbox_name)
            except Exception as e:
                logging.warning(
                    f"Could not ensure mailbox '{self._move_to_mailbox_name}': {e}"
                )
                try:
                    self._mail.select(self._mailbox_name)
                except Exception:
                    pass

    def check_and_process_emails(self, from_idle: bool = False):
        """Searches for unseen Netflix emails and processes them."""
        if not self._is_imap_connected():
            logging.warning("IMAP disconnected. Reconnecting...")
            if not self._connect_imap():
                return
            self._ensure_target_mailbox_exists()

        try:
            email_ids = self._search_unseen_emails(skip_select=from_idle)
        except Exception as e:
            logging.error(f"Email search failed: {e}", exc_info=True)
            return

        if not email_ids:
            return

        processed_in_cycle = False
        for email_id in reversed(email_ids):
            if shutdown_event.is_set():
                break
            try:
                self._process_email(email_id)
                processed_in_cycle = True
            except (
                imaplib.IMAP4.abort,
                imaplib.IMAP4.error,
                socket.error,
                BrokenPipeError,
                OSError,
            ) as e:
                logging.warning(f"IMAP error processing {email_id.decode()}: {e}")
                break
            except Exception as e:
                logging.error(
                    f"Error processing {email_id.decode()}: {e}", exc_info=True
                )

        if processed_in_cycle and self._move_to_mailbox:
            try:
                self._expunge_mailbox()
            except Exception as e:
                logging.error(f"Expunge failed: {e}", exc_info=True)

    def _search_unseen_emails(self, skip_select: bool = False) -> List[bytes]:
        if not self._mail:
            return []
        if not skip_select:
            status, _ = self._mail.select(self._mailbox_name)
            if status != "OK":
                raise imaplib.IMAP4.error(f"Failed to select {self._mailbox_name}")

        sender_parts = [f'(FROM "{s}")' for s in SENDER_EMAILS]
        if len(sender_parts) > 1:
            sender_search = f"(OR {' '.join(sender_parts)})"
        else:
            sender_search = sender_parts[0]

        typ, data = self._mail.search(None, f"(UNSEEN {sender_search})")
        if typ != "OK":
            return []

        email_ids = data[0].split()
        if email_ids:
            logging.info(f"Found {len(email_ids)} unseen email(s)")
        return email_ids

    def _process_email(self, email_id: bytes):
        if not self._mail:
            return

        uid = self._fetch_email_uid(email_id)
        if not uid:
            return
        if uid in self._processed_email_uids:
            logging.info(f"Skipping already processed UID {uid.decode()}")
            return

        self._processed_email_uids.append(uid)
        logging.info(f"Processing email UID {uid.decode()}")

        raw_email = self._fetch_email_content(email_id)
        if not raw_email:
            return

        self._mark_email_seen(email_id)

        parsed = email.message_from_bytes(raw_email)
        try:
            update_link = self._parse_email_for_update_link(parsed)
        except Exception as e:
            logging.error(f"[{uid.decode()}] Parse error: {e}", exc_info=True)
            update_link = None

        if update_link:
            try:
                success = self._handle_netflix_update(update_link)
                logging.info(
                    f"[{uid.decode()}] Update {'succeeded' if success else 'FAILED'}: "
                    f"{update_link[:50]}..."
                )
            except Exception as e:
                logging.error(f"[{uid.decode()}] Update error: {e}", exc_info=True)
        else:
            is_from_sender = any(s in parsed.get("From", "") for s in SENDER_EMAILS)
            if is_from_sender:
                logging.warning(
                    f"UID {uid.decode()}: Netflix email but no update link found."
                )

        try:
            self._manage_processed_email(email_id, uid)
        except Exception as e:
            logging.error(
                f"[{uid.decode()}] Email management error: {e}", exc_info=True
            )

    def _fetch_email_uid(self, email_id: bytes) -> Optional[bytes]:
        if not self._mail:
            return None
        eid = email_id.decode()
        typ, data = self._mail.fetch(eid, "(UID)")
        if typ == "OK" and data and data[0]:
            raw = data[0] if isinstance(data[0], bytes) else data[0][0]
            match = re.search(rb"UID\s+(\d+)", raw)
            if match:
                return match.group(1)
        return None

    def _fetch_email_content(self, email_id: bytes) -> Optional[bytes]:
        if not self._mail:
            return None
        eid = email_id.decode()
        typ, data = self._mail.fetch(eid, "(RFC822)")
        if typ == "OK" and data and isinstance(data[0], tuple) and len(data[0]) == 2:
            return data[0][1]
        return None

    def _mark_email_seen(self, email_id: bytes):
        if not self._mail:
            return
        try:
            self._mail.store(email_id.decode(), "+FLAGS", r"(\Seen)")
        except Exception as e:
            logging.error(f"Error marking {email_id.decode()} as seen: {e}")

    def _parse_email_for_update_link(self, parsed_email) -> Optional[str]:
        """Extracts the Netflix update link from a parsed email."""
        sender = parsed_email.get("From", "")
        if not any(s in sender for s in SENDER_EMAILS):
            return None

        for part in parsed_email.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    html = payload.decode(charset, errors="replace")
                except Exception:
                    try:
                        html = payload.decode("iso-8859-1", errors="replace")
                    except Exception:
                        continue

                for pattern in NETFLIX_LINK_START_PATTERNS:
                    match = re.search(
                        rf"https://{re.escape(pattern)}[^\s'\"]*",
                        html,
                        re.IGNORECASE,
                    )
                    if match:
                        link = match.group(0).replace("&amp;", "&").strip()
                        if "netflix.com" in link:
                            logging.info(f"Found update link: {link[:60]}...")
                            return link
        return None

    # --- Playwright (persistent browser with keep-alive) ---

    def _get_browser(self):
        """Returns a Playwright browser, reusing an existing one if available."""
        with self._browser_lock:
            if self._pw_browser and self._pw_browser.is_connected():
                return self._pw_browser

            # Close stale instances
            self._close_browser_unlocked()

            from playwright.sync_api import sync_playwright

            self._pw_instance = sync_playwright().start()
            self._pw_browser = self._pw_instance.chromium.launch(headless=True)
            logging.info("Playwright browser launched (persistent).")
            return self._pw_browser

    def _close_browser_unlocked(self):
        """Close browser/playwright without acquiring lock (caller must hold lock)."""
        if self._pw_browser:
            try:
                self._pw_browser.close()
            except Exception:
                pass
            self._pw_browser = None
        if self._pw_instance:
            try:
                self._pw_instance.stop()
            except Exception:
                pass
            self._pw_instance = None

    def close_browser(self):
        """Public method to close browser (used during shutdown)."""
        with self._browser_lock:
            self._close_browser_unlocked()

    def _handle_netflix_update(self, update_link: str) -> bool:
        """
        Uses persistent Playwright browser to navigate to the link and
        click the update button. Browser stays alive for script lifetime.
        """
        logging.info("Launching Playwright for Netflix update...")

        try:
            browser = self._get_browser()
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            try:
                page.goto(update_link, timeout=15000, wait_until="domcontentloaded")
                logging.info(f"Page loaded: {page.url}")

                # Check if login is needed
                login_field = page.locator(LOGIN_EMAIL_SELECTOR)
                if login_field.is_visible(timeout=3000):
                    logging.info("Login required. Entering credentials...")
                    login_field.fill(self._netflix_username)
                    page.locator(LOGIN_PASSWORD_SELECTOR).fill(self._netflix_password)
                    page.locator(LOGIN_SUBMIT_SELECTOR).click()
                    page.wait_for_load_state("networkidle", timeout=10000)

                    error = page.locator(LOGIN_ERROR_SELECTOR)
                    if error.is_visible(timeout=2000):
                        logging.error("Netflix login failed!")
                        return False
                    logging.info("Login successful")

                # Click the update button
                button = page.locator(BUTTON_SELECTOR)
                button.wait_for(state="visible", timeout=10000)
                logging.info("Update button found. Clicking...")
                button.click()
                page.wait_for_timeout(1000)
                logging.info("Successfully clicked the update button!")
                return True

            except Exception as e:
                logging.error(f"Playwright error: {e}", exc_info=True)
                try:
                    page.screenshot(path="playwright_error.png")
                    logging.info("Error screenshot saved to playwright_error.png")
                except Exception:
                    pass
                return False
            finally:
                context.close()
                logging.info("Browser context closed (browser stays warm).")

        except Exception as e:
            logging.error(f"Browser launch error: {e}", exc_info=True)
            # Force cleanup on error
            self.close_browser()
            return False

    # --- Email Management ---

    def _manage_processed_email(self, email_id: bytes, uid: bytes):
        if not self._mail or not self._move_to_mailbox:
            return
        eid = email_id.decode()
        try:
            typ, _ = self._mail.copy(eid, self._move_to_mailbox_name)
            if typ == "OK":
                logging.info(
                    f"Copied UID {uid.decode()} to '{self._move_to_mailbox_name}'"
                )
                self._mail.store(eid, "+FLAGS", r"(\Deleted)")
            else:
                logging.warning(f"Failed to copy UID {uid.decode()}")
        except Exception as e:
            logging.error(
                f"Error managing email UID {uid.decode()}: {e}", exc_info=True
            )

    def _expunge_mailbox(self):
        if not self._mail:
            return
        try:
            typ, data = self._mail.expunge()
            if typ == "OK":
                count = len(data[0].split()) if data and data[0] else 0
                logging.info(f"Expunged {count} email(s)")
        except Exception as e:
            logging.error(f"Expunge error: {e}", exc_info=True)

    def close(self):
        """Shuts down all resources (IMAP + persistent browser)."""
        logging.info("Shutting down resources...")
        self.close_browser()
        self._disconnect_imap()


# --- Scheduler ---
class NetflixScheduler:
    """Runs email checking via IMAP IDLE or polling, with interruptible waits."""

    def __init__(
        self,
        polling_interval_sec: int,
        updater: NetflixLocationUpdate,
        force_polling: bool = False,
    ):
        if polling_interval_sec < 1:
            raise ValueError("Polling interval must be at least 1 second.")
        self._interval = polling_interval_sec
        self._updater = updater
        self._force_polling = force_polling
        self._last_log_time: Optional[datetime.datetime] = None
        logging.info(
            f"Scheduler: interval={polling_interval_sec}s, force_polling={force_polling}"
        )

    def run(self):
        if self._force_polling:
            logging.info("Using polling mode (forced by config)")
            self._run_with_polling()
        elif self._updater._check_idle_support():
            logging.info("Using IMAP IDLE mode")
            self._run_with_idle()
        else:
            logging.info("Using polling mode (IDLE not supported)")
            self._run_with_polling()

    def _run_with_idle(self):
        log_interval = 600  # Log status every 10 minutes
        last_status_log = datetime.datetime.now()

        while not shutdown_event.is_set():
            if self._updater._idle_disabled:
                logging.error("IDLE disabled. Switching to polling.")
                self._run_with_polling()
                return

            try:
                now = datetime.datetime.now()
                if (now - last_status_log).total_seconds() >= log_interval:
                    logging.info("IDLE loop active. Waiting for emails...")
                    last_status_log = now

                self._updater._wait_for_new_email_idle()
                if not shutdown_event.is_set():
                    self._updater.check_and_process_emails(from_idle=True)

            except (
                imaplib.IMAP4.abort,
                imaplib.IMAP4.error,
                socket.error,
                BrokenPipeError,
                OSError,
            ) as e:
                logging.warning(f"IMAP error in IDLE loop: {e}. Reconnecting...")
                shutdown_event.wait(5)
            except Exception as e:
                logging.error(f"Error in IDLE loop: {e}", exc_info=True)
                shutdown_event.wait(30)

    def _run_with_polling(self):
        log_interval = 600

        while not shutdown_event.is_set():
            now = datetime.datetime.now()
            if (
                self._last_log_time is None
                or (now - self._last_log_time).total_seconds() >= log_interval
            ):
                logging.info(f"Polling active (interval: {self._interval}s)")
                self._last_log_time = now

            try:
                self._updater.check_and_process_emails()
            except (
                imaplib.IMAP4.abort,
                imaplib.IMAP4.error,
                socket.error,
                BrokenPipeError,
                OSError,
            ) as e:
                logging.warning(f"IMAP error in polling: {e}")
                shutdown_event.wait(self._interval * 2)
            except Exception as e:
                logging.error(f"Error in polling: {e}", exc_info=True)
                shutdown_event.wait(self._interval * 3)

            shutdown_event.wait(self._interval)


# --- Signal Handling & Main ---
updater_instance: Optional[NetflixLocationUpdate] = None


def signal_handler(signum, frame):
    """Sets shutdown_event for clean, interruptible shutdown (responds within seconds)."""
    logging.info(f"Signal {signal.Signals(signum).name} received. Shutting down...")
    shutdown_event.set()


if __name__ == "__main__":
    setup_logging()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        config = configparser.ConfigParser()
        config.read("config.ini")
        polling_time = config.getint("GENERAL", "PollingIntervalSeconds", fallback=5)
        force_polling = config.getboolean("GENERAL", "ForcePolling", fallback=False)

        updater_instance = NetflixLocationUpdate(config_path="config.ini")
        scheduler = NetflixScheduler(
            polling_interval_sec=polling_time,
            updater=updater_instance,
            force_polling=force_polling,
        )
        scheduler.run()

    except FileNotFoundError as e:
        logging.error(f"Configuration error: {e}")
    except (ValueError, configparser.Error) as e:
        logging.error(f"Configuration error: {e}")
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
    finally:
        if updater_instance:
            updater_instance.close()
        close_logging()
