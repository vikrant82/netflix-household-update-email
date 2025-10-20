# -*- coding: utf-8 -*-
import imaplib
import email
import time
import socket
import logging
from logging.handlers import RotatingFileHandler
import configparser
import re
import datetime # Added for timed logging
import signal # Added for signal handling
import sys # Added for sys.exit
from typing import List, Optional, Tuple
from collections import deque
from selenium import webdriver
from selenium.webdriver import Keys
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
try:
	from chromedriver_py import binary_path
	CHROMEDRIVER_PY_AVAILABLE = True
except ImportError:
	CHROMEDRIVER_PY_AVAILABLE = False
	binary_path = None

# --- Constants ---
SENDER_EMAILS = ['info@account.netflix.com']
NETFLIX_LINK_START_PATTERNS = ['www.netflix.com/account/update-primary', 'www.netflix.com/account/set-primary']
BUTTON_SEARCH_ATTR_NAME = 'data-uia'
BUTTON_SEARCH_ATTR_VALUE = 'set-primary-location-action'
LOG_FILENAME = 'status.log'
IMAP_IDLE_TIMEOUT_SECONDS = 28 * 60  # 28 minutes (IMAP max is 29)

# --- Helper Functions ---
def setup_logging():
	"""Configures logging for the script with rotation."""
	# Create rotating file handler: max 10MB per file, keep 5 backups
	handler = RotatingFileHandler(
		LOG_FILENAME,
		maxBytes=10 * 1024 * 1024,  # 10 MB
		backupCount=5,
		encoding='utf8'
	)
	handler.setFormatter(logging.Formatter(
		'%(asctime)s %(levelname)-8s %(message)s',
		datefmt='%Y-%m-%d %H:%M:%S'
	))
	
	# Get root logger and configure it
	logger = logging.getLogger()
	logger.setLevel(logging.INFO)
	logger.addHandler(handler)
	
	logging.info('---------------- Script started ----------------')

def close_logging():
	"""Logs script shutdown."""
	logging.info('---------------- Script shutdown ----------------\n')

# --- Main Class ---
class NetflixLocationUpdate:
	"""
	Monitors an email inbox for Netflix household update emails,
	extracts the update link, and uses Selenium to click the confirmation button.
	Handles IMAP connection issues and prevents reprocessing of emails.
	"""
	_config: configparser.ConfigParser
	_driver: Optional[webdriver.Chrome] = None
	_mail: Optional[imaplib.IMAP4_SSL] = None
	_processed_email_uids: deque  # Bounded deque to prevent memory leak
	_webdriver_creation_time: Optional[datetime.datetime] = None
	_webdriver_max_age_seconds: int = 6 * 60 * 60  # 6 hours
	_idle_supported: Optional[bool] = None  # Cache IDLE capability check

	# Configuration attributes
	_mailbox_name: str
	_move_to_mailbox: bool
	_move_to_mailbox_name: str
	_imap_server: str
	_imap_port: int
	_imap_username: str
	_imap_password: str
	_chromedriver_path: str
	_netflix_username: str
	_netflix_password: str

	def __init__(self, config_path: str = 'config.ini'):
		self._load_config(config_path)
		self._processed_email_uids = deque(maxlen=100)  # Keep last 100 UIDs only
		self._driver = self._init_webdriver()
		self._webdriver_creation_time = datetime.datetime.now()
		self._connect_imap()
		self._ensure_target_mailbox_exists()

	def _load_config(self, config_path: str):
		"""Loads configuration from the specified INI file."""
		self._config = configparser.ConfigParser()
		if not self._config.read(config_path):
			raise FileNotFoundError(f"Configuration file '{config_path}' not found or empty.")

		required_sections = ['EMAIL', 'NETFLIX', 'CHROMEDRIVER', 'GENERAL']
		for section in required_sections:
			if section not in self._config:
				raise ValueError(f"Missing required section '{section}' in {config_path}")

		self._imap_server = self._config.get('EMAIL', 'ImapServer')
		self._imap_port = self._config.getint('EMAIL', 'ImapPort')
		self._imap_username = self._config.get('EMAIL', 'Username')
		self._imap_password = self._config.get('EMAIL', 'Password')
		self._mailbox_name = self._config.get('EMAIL', 'Mailbox', fallback='INBOX')
		self._netflix_username = self._config.get('NETFLIX', 'Username')
		self._netflix_password = self._config.get('NETFLIX', 'Password')
		
		# ChromeDriver configuration
		use_chromedriver_py = self._config.getboolean('CHROMEDRIVER', 'UseChromedriverPy', fallback=False)  # Changed default to False
		if use_chromedriver_py and CHROMEDRIVER_PY_AVAILABLE:
			self._chromedriver_path = binary_path
		elif use_chromedriver_py and not CHROMEDRIVER_PY_AVAILABLE:
			logging.warning("UseChromedriverPy=True but chromedriver-py not installed. Using Selenium's automatic ChromeDriver management.")
			self._chromedriver_path = None  # Selenium will auto-manage
		else:
			# Try to get manual path, otherwise use automatic
			try:
				self._chromedriver_path = self._config.get('CHROMEDRIVER', 'ExecutablePath')
			except (configparser.NoOptionError, configparser.NoSectionError):
				logging.info("No ExecutablePath specified. Using Selenium's automatic ChromeDriver management.")
				self._chromedriver_path = None
		
		self._move_to_mailbox = self._config.getboolean('GENERAL', 'MoveEmailsToMailbox', fallback=False)
		self._move_to_mailbox_name = self._config.get('GENERAL', 'MailboxName', fallback='Netflix')

		logging.info("Configuration loaded successfully.")

	def _init_webdriver(self) -> Optional[webdriver.Chrome]:
		"""Initializes and returns a Selenium Chrome WebDriver instance."""
		try:
			chrome_options = webdriver.ChromeOptions()
			chrome_options.add_argument("--headless")
			chrome_options.add_argument("--disable-gpu")
			chrome_options.add_argument("--no-sandbox")
			chrome_options.add_argument("--disable-dev-shm-usage")
			
			# Aggressive performance optimizations for 2-3s page loads
			chrome_options.add_argument("--disable-extensions")
			chrome_options.add_argument("--disable-images")
			chrome_options.add_argument("--blink-settings=imagesEnabled=false")
			chrome_options.add_argument("--disable-plugins")
			chrome_options.add_argument("--disable-plugins-discovery")
			chrome_options.add_argument("--disable-web-security")
			chrome_options.add_argument("--disable-features=VizDisplayCompositor")
			chrome_options.add_argument("--disable-background-networking")
			chrome_options.add_argument("--disable-background-timer-throttling")
			chrome_options.add_argument("--disable-client-side-phishing-detection")
			chrome_options.add_argument("--disable-default-apps")
			chrome_options.add_argument("--disable-hang-monitor")
			chrome_options.add_argument("--disable-popup-blocking")
			chrome_options.add_argument("--disable-prompt-on-repost")
			chrome_options.add_argument("--disable-sync")
			chrome_options.add_argument("--metrics-recording-only")
			chrome_options.add_argument("--no-first-run")
			chrome_options.page_load_strategy = 'eager'  # Don't wait for all resources
			
			# Additional speed improvements
			chrome_options.add_experimental_option("prefs", {
				"profile.managed_default_content_settings.images": 2,  # Disable images only
			})
			
			if self._chromedriver_path:
				# Use specified ChromeDriver path
				logging.info(f"Initializing WebDriver with path: {self._chromedriver_path}")
				svc = webdriver.ChromeService(executable_path=self._chromedriver_path)
				driver = webdriver.Chrome(options=chrome_options, service=svc)
			else:
				# Use Selenium's automatic ChromeDriver management (Selenium 4+)
				logging.info("Initializing WebDriver with automatic ChromeDriver management")
				driver = webdriver.Chrome(options=chrome_options)
			
			# Set aggressive timeouts for fast operation
			driver.set_page_load_timeout(5)  # Max 5 seconds for page load
			driver.implicitly_wait(0)  # Don't use implicit waits (we use explicit waits)
			
			logging.info("WebDriver initialized successfully.")
			return driver
		except Exception as e:
			logging.error(f"Failed to initialize WebDriver: {e}", exc_info=True)
			return None

	def _connect_imap(self) -> bool:
		"""Establishes a connection to the IMAP server."""
		if self._mail and self._is_imap_connected():
			# logging.info("IMAP connection already active.") # Reduced verbosity
			return True

		self._disconnect_imap() # Ensure any old connection is closed

		logging.info(f"Attempting to connect to IMAP server: {self._imap_server}")
		try:
			self._mail = imaplib.IMAP4_SSL(self._imap_server, self._imap_port)
			typ, data = self._mail.login(self._imap_username, self._imap_password)
			if typ == 'OK':
				logging.info(f"Successfully connected and logged into IMAP server {self._imap_server}")
				return True
			else:
				logging.error(f"IMAP login failed: {data}")
				self._mail = None
				return False
		except (imaplib.IMAP4.error, socket.error, OSError) as e:
			logging.error(f"Failed to connect to IMAP server {self._imap_server}: {e}")
			self._mail = None
			return False
		except Exception as e:
			logging.error(f"An unexpected error occurred during IMAP connection: {e}", exc_info=True)
			self._mail = None
			return False

	def _disconnect_imap(self):
		"""Closes the IMAP connection if it's open."""
		if self._mail:
			logging.info("Closing existing IMAP connection.")
			try:
				self._mail.close()
			except Exception as e:
				logging.warning(f"Error during IMAP close: {e}")
			try:
				self._mail.logout()
				logging.info("IMAP logout successful.")
			except Exception as e:
				logging.warning(f"Error during IMAP logout: {e}")
			finally:
				self._mail = None

	def _is_imap_connected(self) -> bool:
		"""Checks if the IMAP connection is active using NOOP."""
		if not self._mail:
			return False
		try:
			status, _ = self._mail.noop()
			return status == 'OK'
		except (imaplib.IMAP4.abort, imaplib.IMAP4.error, socket.error, BrokenPipeError, OSError):
			logging.warning("IMAP connection check (NOOP) failed.")
			return False
		except Exception as e:
			logging.error(f"Unexpected error during IMAP NOOP check: {e}", exc_info=True)
			return False

	def _check_idle_support(self) -> bool:
		"""Checks if the IMAP server supports IDLE command."""
		if self._idle_supported is not None:
			return self._idle_supported  # Return cached result
		
		if not self._mail:
			self._idle_supported = False
			return False
		
		try:
			# Need to select a mailbox first to get accurate capabilities
			try:
				self._mail.select(self._mailbox_name)
			except Exception:
				pass  # Ignore if already selected
			
			# Check server capabilities (Gmail returns IDLE as string, not bytes)
			capabilities = self._mail.capabilities
			# Check for both string and bytes version to be safe
			self._idle_supported = b'IDLE' in capabilities or 'IDLE' in capabilities
			
			if self._idle_supported:
				logging.info("IMAP server supports IDLE (push notifications)")
			else:
				logging.warning(f"IMAP server does NOT support IDLE. Capabilities: {capabilities}")
				logging.warning("Will use polling fallback.")
			return self._idle_supported
		except Exception as e:
			logging.warning(f"Failed to check IDLE capability: {e}. Assuming not supported.")
			self._idle_supported = False
			return False

	def _wait_for_new_email_idle(self) -> bool:
		"""
		Waits for new email using IMAP IDLE (push notification).
		Returns True if new emails arrived, False on timeout or error.
		"""
		if not self._mail:
			return False
		
		try:
			# Select mailbox first
			status, _ = self._mail.select(self._mailbox_name)
			if status != 'OK':
				logging.error(f"Failed to select mailbox '{self._mailbox_name}' for IDLE")
				return False
			
			# Enter IDLE mode
			tag = self._mail._new_tag().decode()
			self._mail.send(f'{tag} IDLE\r\n'.encode())
			
			# Wait for server to acknowledge IDLE
			line = self._mail.readline()
			if b'idling' not in line.lower():
				logging.warning(f"Unexpected IDLE response: {line}")
				# Send DONE to exit IDLE
				self._mail.send(b'DONE\r\n')
				self._mail.readline()  # Read response
				return False
			
			logging.info(f"Entered IDLE mode. Waiting for new emails (timeout: {IMAP_IDLE_TIMEOUT_SECONDS}s)...")
			
			# Wait for notification or timeout
			# Set socket timeout to match IDLE timeout (28 minutes)
			self._mail.sock.settimeout(IMAP_IDLE_TIMEOUT_SECONDS)
			new_email_arrived = False
			
			try:
				while True:
					line = self._mail.readline()
					if line:
						line_str = line.decode('utf-8', errors='ignore')
						# Check for EXISTS (new email) or RECENT notifications
						if 'EXISTS' in line_str or 'RECENT' in line_str:
							logging.info(f"New email notification received: {line_str.strip()}")
							new_email_arrived = True
							break
			except socket.timeout:
				# Normal timeout after 28 minutes - time to refresh IDLE
				logging.debug("IDLE timeout reached, will refresh connection")
			except Exception as e:
				logging.warning(f"Error while waiting in IDLE: {e}")
				new_email_arrived = False
			
			# Exit IDLE mode
			self._mail.send(b'DONE\r\n')
			
			# Read response to DONE
			try:
				self._mail.readline()  # Should get OK response
			except Exception as e:
				logging.warning(f"Error reading DONE response: {e}")
			
			if new_email_arrived:
				logging.info("IDLE detected new email(s)")
			else:
				logging.debug("IDLE timeout reached, refreshing connection")
			
			return new_email_arrived
			
		except Exception as e:
			logging.error(f"Error during IMAP IDLE: {e}", exc_info=True)
			# Try to send DONE in case we're still in IDLE
			try:
				self._mail.send(b'DONE\r\n')
			except Exception:
				pass
			return False

	def _ensure_target_mailbox_exists(self):
		"""Creates the target mailbox for moving emails if it doesn't exist."""
		if self._move_to_mailbox and self._mail:
			try:
				logging.info(f"Ensuring mailbox '{self._move_to_mailbox_name}' exists.")
				typ, _ = self._mail.create(self._move_to_mailbox_name)
				# Some servers might return NO if it exists, others OK. Select to confirm.
				status, _ = self._mail.select(self._move_to_mailbox_name)
				if status == 'OK':
					logging.info(f"Mailbox '{self._move_to_mailbox_name}' is ready.")
				else:
					# If create was ok but select failed, log warning.
					if typ == 'OK':
						logging.warning(f"Mailbox '{self._move_to_mailbox_name}' created but could not be selected.")
					# If create failed and select failed, log warning (might exist but unusable)
					else:
						logging.warning(f"Mailbox '{self._move_to_mailbox_name}' may not exist or is not selectable.")
				# Select back the main mailbox regardless
				self._mail.select(self._mailbox_name)
			except Exception as e:
				logging.warning(f"Could not ensure mailbox '{self._move_to_mailbox_name}' exists or is selectable: {e}")
				try: # Attempt to select back main mailbox even after error
					self._mail.select(self._mailbox_name)
				except Exception:
					logging.error("Failed to re-select main mailbox after ensure_target_mailbox error.")

	def close(self):
		"""Shuts down WebDriver and disconnects from IMAP."""
		logging.info("Shutting down resources...")
		if self._driver:
			try:
				self._driver.quit()
				logging.info("WebDriver shut down.")
			except Exception as e:
				logging.error(f"Error shutting down WebDriver: {e}", exc_info=True)
			finally:
				self._driver = None
		self._disconnect_imap()

	def check_and_process_emails(self):
		"""Main loop function: checks connection, searches, and processes emails."""
		if not self._is_imap_connected():
			logging.warning("IMAP disconnected. Attempting to reconnect...")
			if not self._connect_imap():
				logging.error("Reconnection failed. Skipping email check cycle.")
				return
			else:
				self._ensure_target_mailbox_exists() # Re-check target mailbox after reconnect

		try:
			email_ids = self._search_unseen_emails()
		except Exception as e:
			logging.error(f"Failure during email search: {e}", exc_info=True)
			# Connection might be broken, let next cycle handle reconnect
			return

		if not email_ids:
			return # No new emails

		processed_in_cycle = False
		for email_id in reversed(email_ids): # Process newest first
			try:
				self._process_email(email_id)
				processed_in_cycle = True
			except (imaplib.IMAP4.abort, imaplib.IMAP4.error, socket.error, BrokenPipeError, OSError) as e:
				logging.warning(f"IMAP connection error during processing email ID {email_id.decode()}: {e}. Aborting processing loop for this cycle.")
				break # Stop processing this batch, reconnect next cycle
			except Exception as e:
				logging.error(f"Unexpected error processing email ID {email_id.decode()}: {e}", exc_info=True)
				# Continue to the next email in this cycle

		if processed_in_cycle and self._move_to_mailbox:
			logging.info("Attempting to expunge mailbox as emails were processed and moved.")
			try:
				self._expunge_mailbox()
			except Exception as e:
				logging.error(f"Failure during mailbox expunge: {e}", exc_info=True)
				# Connection might be broken, let next cycle handle reconnect

	def _search_unseen_emails(self) -> List[bytes]:
		"""Searches the mailbox for unseen emails matching criteria."""
		if not self._mail:
			return []

		try:
			status, _ = self._mail.select(self._mailbox_name)
			if status != 'OK':
				logging.error(f"Failed to select mailbox '{self._mailbox_name}'. Status: {status}")
				raise imaplib.IMAP4.error(f"Failed to select mailbox {self._mailbox_name}") # Trigger reconnect

			# Build search criteria (simplified for clarity)
			sender_criteria_parts = [f'(FROM "{sender}")' for sender in SENDER_EMAILS]
			# Search for UNSEEN emails FROM any of the specified senders
			# Using OR for multiple senders, enclosed in parentheses
			if len(sender_criteria_parts) > 1:
				sender_search = f'(OR {" ".join(sender_criteria_parts)})'
			elif len(sender_criteria_parts) == 1:
				sender_search = sender_criteria_parts[0]
			else: # No senders specified, just get all unseen (unlikely use case here)
				sender_search = ''

			# Combine UNSEEN and sender criteria
			search_criteria = f'(UNSEEN {sender_search})'.strip() if sender_search else '(UNSEEN)'

			typ, data = self._mail.search(None, search_criteria)
			if typ != 'OK':
				logging.error(f"IMAP search failed. Status: {typ}, Data: {data}")
				return []

			email_ids = data[0].split()
			if email_ids:
				logging.info(f"Found {len(email_ids)} unseen email(s) matching criteria.")
			return email_ids

		except (imaplib.IMAP4.abort, imaplib.IMAP4.error, socket.error, BrokenPipeError, OSError) as e:
			logging.warning(f"IMAP connection error during select/search: {e}")
			raise e # Propagate to trigger reconnect logic
		except Exception as e:
			logging.error(f"Unexpected error during IMAP select/search: {e}", exc_info=True)
			raise e # Propagate unexpected errors too

	def _process_email(self, email_id: bytes):
		"""Fetches, checks, parses, and handles a single email by its sequence ID."""
		if not self._mail: return

		# 1. Fetch UID and Check Cache
		uid = self._fetch_email_uid(email_id)
		if not uid:
			logging.warning(f"Failed to fetch UID for sequence ID {email_id.decode()}. Skipping.")
			return
		# logging.debug(f"[{email_id.decode()}] Fetched UID: {uid.decode()}") # Reduced verbosity

		if uid in self._processed_email_uids:
			logging.info(f"Skipping already processed email UID {uid.decode()} (Sequence ID: {email_id.decode()})")
			return

		# 2. Add to Cache and Fetch Content
		self._processed_email_uids.append(uid)  # deque uses append, not add
		logging.info(f"Processing new email UID {uid.decode()} (Sequence ID: {email_id.decode()})")
		raw_email = self._fetch_email_content(email_id)
		if not raw_email:
			logging.warning(f"[{uid.decode()}] Failed to fetch email content. Skipping.")
			# Remove UID from cache if content fetch failed, might work next time? Or keep it?
			# Keeping it to avoid potential loops if fetch keeps failing.
			return
		# logging.debug(f"[{uid.decode()}] Fetched email content ({len(raw_email)} bytes).") # Reduced verbosity

		# 3. Mark as Seen (Important to do early)
		self._mark_email_seen(email_id)
		# logging.debug(f"[{uid.decode()}] Marked email as seen.") # Reduced verbosity

		# 4. Parse for Link
		try:
			parsed_email = email.message_from_bytes(raw_email)
			update_link = self._parse_email_for_update_link(parsed_email)
		except Exception as e:
			logging.error(f"[{uid.decode()}] Error during email parsing: {e}", exc_info=True)
			update_link = None # Ensure update_link is None if parsing fails
		# logging.debug(f"[{uid.decode()}] Parsing complete. Link found: {'Yes' if update_link else 'No'}") # Reduced verbosity

		# 5. Handle Netflix Update (if link found)
		success = False
		if update_link:
			try:
				success = self._handle_netflix_update(update_link)
				logging.info(f"[{uid.decode()}] Netflix update attempt via link successful: {success}. Link: {update_link[:50]}...") # Log success/failure
			except Exception as e:
				logging.error(f"[{uid.decode()}] Unhandled exception during _handle_netflix_update call: {e}", exc_info=True)
				success = False
		else:
			# Log if it was a Netflix email but link wasn't found/parsed
			is_from_sender = any(sender in parsed_email.get('From', '') for sender in SENDER_EMAILS)
			if is_from_sender:
				logging.warning(f"Email UID {uid.decode()} from sender matched, but no valid update link found/parsed.")

		# 6. Manage Processed Email (Copy/Delete)
		try:
			self._manage_processed_email(email_id, uid)
		except Exception as e:
			logging.error(f"[{uid.decode()}] Error during email move/delete management: {e}", exc_info=True)
			# Continue even if move/delete fails, main action (update) might have succeeded.

	def _fetch_email_uid(self, email_id: bytes) -> Optional[bytes]:
		"""Fetches the UID for a given email sequence ID."""
		if not self._mail: return None
		try:
			typ, data = self._mail.fetch(email_id, '(UID)')
			if typ == 'OK' and data and data[0]:
				# Response format is usually like: b'1 (UID 1234)'
				uid_match = re.search(br'UID\s+(\d+)', data[0])
				if uid_match:
					return uid_match.group(1)
				else:
					logging.warning(f"Could not parse UID from fetch response for ID {email_id.decode()}: {data[0].decode(errors='ignore')}")
			else:
				logging.warning(f"Failed to fetch UID for email ID {email_id.decode()}. Status: {typ}")
			return None
		except Exception as e:
			logging.error(f"Error fetching UID for email ID {email_id.decode()}: {e}", exc_info=True)
			raise e # Propagate error (likely connection issue)

	def _fetch_email_content(self, email_id: bytes) -> Optional[bytes]:
		"""Fetches the full RFC822 content for an email sequence ID."""
		if not self._mail: return None
		try:
			typ, data = self._mail.fetch(email_id, '(RFC822)')
			# Expected response: [ (b'1 (RFC822 {size}', b'Email Content...'), b')' ]
			if typ == 'OK' and data and isinstance(data[0], tuple) and len(data[0]) == 2:
				return data[0][1] # Content is the second item in the tuple
			else:
				logging.error(f"Failed to fetch content for email ID {email_id.decode()}. Status: {typ}, Data structure unexpected.")
				return None
		except Exception as e:
			logging.error(f"Error fetching content for email ID {email_id.decode()}: {e}", exc_info=True)
			raise e # Propagate error

	def _mark_email_seen(self, email_id: bytes):
		"""Marks an email as Seen."""
		if not self._mail: return
		try:
			typ, _ = self._mail.store(email_id, '+FLAGS', r'(\Seen)')
			if typ != 'OK':
				logging.warning(f"Failed to mark email ID {email_id.decode()} as seen. Status: {typ}")
		except Exception as e:
			logging.error(f"Error marking email ID {email_id.decode()} as seen: {e}", exc_info=True)
			# Don't raise, allow processing to continue if possible

	def _refresh_webdriver_if_stale(self):
		"""Recreates WebDriver if it's too old to prevent memory leaks."""
		if not self._driver or not self._webdriver_creation_time:
			return
		
		age_seconds = (datetime.datetime.now() - self._webdriver_creation_time).total_seconds()
		if age_seconds > self._webdriver_max_age_seconds:
			logging.info(f"WebDriver is {age_seconds / 3600:.1f} hours old. Recreating to prevent memory leaks...")
			try:
				self._driver.quit()
			except Exception as e:
				logging.warning(f"Error closing old WebDriver: {e}")
			
			self._driver = self._init_webdriver()
			self._webdriver_creation_time = datetime.datetime.now()
			logging.info("WebDriver recreated successfully.")

	def _parse_email_for_update_link(self, parsed_email: email.message.Message) -> Optional[str]:
		"""Parses email content to find the Netflix update link."""
		sender = parsed_email.get('From', '')
		if not any(expected_sender in sender for expected_sender in SENDER_EMAILS):
			return None # Not from the expected sender

		html_payload = ""
		for part in parsed_email.walk():
			if part.get_content_type() == "text/html":
				try:
					payload_bytes = part.get_payload(decode=True)
					charset = part.get_content_charset() or 'utf-8' # Default to utf-8
					html_payload = payload_bytes.decode(charset, errors='replace') # Use replace on error
					break # Found HTML part
				except (LookupError, UnicodeDecodeError) as e:
					logging.warning(f"Error decoding HTML part with charset {charset}: {e}. Trying fallback.")
					try: # Fallback attempt
						html_payload = payload_bytes.decode('iso-8859-1', errors='replace')
						break
					except Exception as e2:
						logging.warning(f"Fallback decoding failed: {e2}")
						continue # Try next part if any
				except Exception as e:
					logging.warning(f"Unexpected error decoding HTML part: {e}")
					continue

		if not html_payload:
			logging.warning("No valid HTML payload found in email.")
			return None

		# Find the link using start patterns
		for pattern in NETFLIX_LINK_START_PATTERNS:
			try:
				# Use regex for potentially more robust extraction
				# Look for https:// followed by the pattern, captured until the first quote
				# Making it non-greedy (.*?) and handling potential HTML entities (&)
				match = re.search(rf'https://{re.escape(pattern)}[^\s\'"]*', html_payload, re.IGNORECASE)
				if match:
					update_link = match.group(0)
					# Basic cleanup - replace common HTML entities
					update_link = update_link.replace('&', '&').strip()
					logging.info(f"Extracted potential update link: {update_link[:60]}...")
					# Basic validation
					if 'netflix.com' in update_link and '?' in update_link:
						return update_link
					else:
						logging.warning(f"Extracted string doesn't look like a valid update URL: {update_link}")
			except Exception as e:
				logging.error(f"Regex error during link search for pattern '{pattern}': {e}")
				continue # Try next pattern

		logging.error('Unable to parse a valid Netflix update link in the Email. Patterns might be outdated.')
		return None

	def _handle_netflix_update(self, update_link: str) -> bool:
		"""Uses Selenium to navigate to the link and click the update button."""
		# Refresh WebDriver if it's too old to prevent memory leaks
		self._refresh_webdriver_if_stale()
		
		if not self._driver:
			logging.error("WebDriver is not available. Cannot handle Netflix update.")
			return False

		logging.info(f"Selenium: Navigating to update link...")
		try:
			self._driver.get(update_link)
			
			# Aggressive wait times for fast operation (expecting 2-3s page loads)
			quick_wait = WebDriverWait(self._driver, 2)  # For instant checks
			fast_wait = WebDriverWait(self._driver, 5)  # For page elements

			# Check if login is needed (quick check - 2 seconds max)
			try:
				login_input = quick_wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="userLoginId"]')))
				logging.info('Login required. Attempting to login to Netflix account.')
				if self._netflix_login():
					# Wait for button after login
					fast_wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, f'button[{BUTTON_SEARCH_ATTR_NAME}="{BUTTON_SEARCH_ATTR_VALUE}"]')))
					logging.info("Login successful, proceeding to click button.")
				else:
					logging.error("Netflix login failed. Cannot proceed with update.")
					return False
			except Exception:
				# No login needed - proceed directly to button
				pass

			# Find and click the confirmation button
			button_selector = f'button[{BUTTON_SEARCH_ATTR_NAME}="{BUTTON_SEARCH_ATTR_VALUE}"]'
			logging.info(f"Looking for button: {button_selector}")
			try:
				# Wait for button to be clickable (5 seconds max)
				button = fast_wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, button_selector)))
				logging.info("Update button found. Clicking...")
				button.click()
				# Minimal wait to ensure click registers
				time.sleep(0.5)
				logging.info("Successfully clicked the update button.")
				return True
			except Exception as e:
				logging.error(f"Could not find or click button: {e}")
				# Log current URL for debugging
				logging.error(f"Current URL: {self._driver.current_url}")
				return False

		except Exception as e:
			logging.error(f"Unhandled exception during Selenium handling: {e}", exc_info=True)
			return False

	def _netflix_login(self) -> bool:
		"""Performs login on the Netflix page using configured credentials."""
		if not self._driver: return False
		try:
			wait = WebDriverWait(self._driver, 5)
			
			# Wait for all login elements to be present before interacting
			email_field = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="userLoginId"]')))
			password_field = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="password"]')))
			login_button = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'button[data-uia="login-submit-button"]')))

			logging.info("All login elements found, entering credentials...")
			email_field.clear()
			email_field.send_keys(self._netflix_username)
			password_field.clear()
			password_field.send_keys(self._netflix_password)
			logging.info("Submitting login...")
			login_button.click()
			
			# Minimal wait for either error message or successful redirect
			time.sleep(1)
			
			# Check if login failed (error message appears)
			try:
				self._driver.find_element(By.CSS_SELECTOR, '[data-uia="login-error-message"]')
				logging.error("Login failed: Error message detected on page.")
				return False
			except NoSuchElementException:
				# No error message = login successful
				logging.info("Login appears successful.")
				return True
		except Exception as e:
			logging.error(f"Error during Netflix login attempt: {e}", exc_info=True)
			logging.error(f"Current URL during login failure: {self._driver.current_url if self._driver else 'N/A'}")
			return False

	def _manage_processed_email(self, email_id: bytes, uid: bytes):
		"""Copies and/or marks an email for deletion based on config."""
		if not self._mail: return

		if self._move_to_mailbox:
			try:
				# Copy first
				logging.info(f"Attempting to copy email UID {uid.decode()} to '{self._move_to_mailbox_name}'")
				typ_copy, copy_resp = self._mail.copy(email_id, self._move_to_mailbox_name)
				if typ_copy == 'OK':
					logging.info(f"Copied email UID {uid.decode()} successfully.")
					# Then mark original for deletion
					logging.info(f"Attempting to mark email UID {uid.decode()} as deleted.")
					typ_del, del_resp = self._mail.store(email_id, '+FLAGS', r'(\Deleted)')
					if typ_del == 'OK':
						logging.info(f"Marked email UID {uid.decode()} as deleted.")
					else:
						logging.warning(f"Failed to mark email UID {uid.decode()} as deleted after copy. Status: {typ_del}, Resp: {del_resp}")
				else:
					logging.warning(f"Failed to copy email UID {uid.decode()} to '{self._move_to_mailbox_name}'. Status: {typ_copy}, Resp: {copy_resp}")
			except Exception as e:
				logging.error(f"Error copying/deleting email UID {uid.decode()}: {e}", exc_info=True)
				# Don't raise, as email was already processed (or attempted)

		# If not moving, it was already marked \Seen earlier. No further action needed here before expunge.

	def _expunge_mailbox(self):
		"""Expunges emails marked as \\Deleted from the current mailbox."""
		if not self._mail: return
		try:
			logging.info(f"Attempting to expunge mailbox '{self._mailbox_name}'.")
			typ, data = self._mail.expunge()
			if typ == 'OK':
				# data often contains a list of expunged sequence numbers, can be None or [b'']
				expunged_count = len(data[0].split()) if data and data[0] else 0
				logging.info(f"Expunge successful. {expunged_count} email(s) permanently removed.")
			else:
				logging.warning(f"Expunge command returned non-OK status: {typ}, Data: {data}")
		except Exception as e:
			logging.error(f"Error during expunge: {e}", exc_info=True)
			# Don't raise connection error here, let the main loop handle it on next cycle

# --- Scheduler Class ---
class NetflixScheduler:
	"""Runs the email checking process at a regular interval."""
	_polling_interval_sec: int
	_location_updater: NetflixLocationUpdate
	_last_log_time: Optional[datetime.datetime]

	def __init__(self, polling_interval_sec: int, location_updater: NetflixLocationUpdate):
		if polling_interval_sec < 1:
			raise ValueError("Polling interval must be at least 1 second.")
		self._polling_interval_sec = polling_interval_sec
		self._location_updater = location_updater
		self._last_log_time = None
		logging.info(f"Scheduler initialized with polling interval: {polling_interval_sec} seconds.")

	def run(self):
		"""Starts the email monitoring loop (IDLE or polling based)."""
		logging.info("Scheduler starting run loop.")
		
		# Check if IDLE is supported
		idle_supported = self._location_updater._check_idle_support()
		
		if idle_supported:
			logging.info("Using IMAP IDLE mode for instant email notifications")
			self._run_with_idle()
		else:
			logging.info(f"Using polling mode (interval: {self._polling_interval_sec}s)")
			self._run_with_polling()
		
		logging.info("Scheduler loop finished.")
	
	def _run_with_idle(self):
		"""Runs the scheduler using IMAP IDLE for push notifications."""
		log_interval_seconds = 60 * 10  # Log status every 10 minutes
		last_status_log = datetime.datetime.now()
		
		while True:
			try:
				# Log status periodically
				now = datetime.datetime.now()
				if (now - last_status_log).total_seconds() >= log_interval_seconds:
					logging.info("IDLE loop active. Waiting for email notifications...")
					last_status_log = now
				
				# Wait for new email using IDLE (blocks until email arrives or timeout)
				new_email = self._location_updater._wait_for_new_email_idle()
				
				# Process emails (whether new ones arrived or just timeout refresh)
				self._location_updater.check_and_process_emails()
				
			except (imaplib.IMAP4.abort, imaplib.IMAP4.error, socket.error, BrokenPipeError, OSError) as e:
				logging.warning(f"IMAP connection error in IDLE loop: {e}. Reconnecting...")
				time.sleep(5)  # Brief pause before reconnect
				
			except KeyboardInterrupt:
				logging.info("Keyboard interrupt received. Shutting down scheduler.")
				break
				
			except Exception as e:
				logging.error(f"Unexpected error in IDLE loop: {e}", exc_info=True)
				logging.info("Waiting 30 seconds before continuing...")
				time.sleep(30)
	
	def _run_with_polling(self):
		"""Runs the scheduler using traditional polling."""
		log_interval_seconds = 60 * 10  # Log status every 10 minutes

		while True:
			now = datetime.datetime.now()
			should_log = False
			if self._last_log_time is None or (now - self._last_log_time).total_seconds() >= log_interval_seconds:
				should_log = True

			if should_log:
				logging.info(f"Polling loop active (Interval: {self._polling_interval_sec}s). Checking emails...")
				self._last_log_time = now

			try:
				self._location_updater.check_and_process_emails()

			except (imaplib.IMAP4.abort, imaplib.IMAP4.error, socket.error, BrokenPipeError, OSError) as e:
				logging.warning(f"IMAP connection error detected in main loop: {e}. Will attempt reconnect on next cycle.")
				# Give a little extra time before next cycle after connection error
				time.sleep(self._polling_interval_sec * 2)

			except KeyboardInterrupt:
				logging.info("Keyboard interrupt received. Shutting down scheduler.")
				break

			except Exception as e:
				logging.error(f"An unexpected error occurred in the scheduler loop: {e}", exc_info=True)
				logging.info(f"Waiting for {self._polling_interval_sec * 3} seconds before continuing after unexpected error...")
				time.sleep(self._polling_interval_sec * 3) # Wait longer after unexpected errors

			# Wait for the next poll cycle
			# Subtract processing time? No, fixed interval is simpler.
			time.sleep(self._polling_interval_sec)
		
# --- Main Execution ---

# Global or accessible updater_instance for signal handlers
updater_instance_for_signal_handling: Optional[NetflixLocationUpdate] = None

def cleanup_and_exit(signum, frame):
	"""Signal handler for graceful shutdown."""
	global updater_instance_for_signal_handling
	logging.info(f"Signal {signal.Signals(signum).name} received. Initiating shutdown...")
	if updater_instance_for_signal_handling:
		try:
			updater_instance_for_signal_handling.close()
		except Exception as e:
			logging.error(f"Error during cleanup in signal handler: {e}", exc_info=True)
	close_logging()
	sys.exit(0)

if __name__ == '__main__':
	# setup_logging() is called first to ensure logging is active for all messages.
	setup_logging()

	# Register signal handlers
	signal.signal(signal.SIGTERM, cleanup_and_exit)
	signal.signal(signal.SIGINT, cleanup_and_exit)

	# Initialize to None, will be assigned in try block
	# This variable is local to the if __name__ == '__main__' block
	# The global updater_instance_for_signal_handling is used by signal handlers
	current_updater_instance: Optional[NetflixLocationUpdate] = None
	try:
		# Read polling time from config or use default.
		config = configparser.ConfigParser()
		config.read('config.ini')
		polling_time = config.getint('GENERAL', 'PollingIntervalSeconds', fallback=5)
		current_updater_instance = NetflixLocationUpdate(config_path='config.ini')
		updater_instance_for_signal_handling = current_updater_instance # Assign to global for signal handler
		scheduler = NetflixScheduler(polling_interval_sec=polling_time, location_updater=current_updater_instance) # Pass correct instance
		scheduler.run() # This call is blocking and will run until a signal or unhandled error.
	except FileNotFoundError as e:
		logging.error(f"Configuration error: {e}")
		print(f"ERROR: Configuration file 'config.ini' not found. Details in {LOG_FILENAME}.")
	except (ValueError, configparser.Error) as e:
		logging.error(f"Configuration error: {e}")
		print(f"ERROR: Problem reading configuration file 'config.ini'. Details in {LOG_FILENAME}.")
	except Exception as main_e:
		logging.error(f"Fatal unhandled error in main execution: {main_e}", exc_info=True)
		print(f"ERROR: A fatal unhandled error occurred. Check {LOG_FILENAME} for details.")
	finally:
		# This finally block handles normal exit or exceptions not caught by signal handlers.
		# The signal handlers will attempt cleanup first if a signal is received.
		# We check if the global instance (used by signals) is set,
		# as current_updater_instance might not be if init failed.
		if updater_instance_for_signal_handling:
			logging.info("Main script block finished or unhandled exception. Ensuring cleanup via finally block.")
			# No need to check for signal flags, make close() idempotent if necessary
			# or rely on the fact that if signals ran, this might be redundant but harmless.
			try:
				updater_instance_for_signal_handling.close()
			except Exception as e:
				logging.error(f"Error during cleanup in finally block: {e}", exc_info=True)
		else:
			logging.info("Updater instance was not initialized, skipping final cleanup call.")
		close_logging() # close_logging() is now called by signal handlers too, but it's fine.
		print(f"Script finished. Check {LOG_FILENAME} for details.")
