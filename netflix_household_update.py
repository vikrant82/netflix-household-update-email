# -*- coding: utf-8 -*-
import imaplib
import email
import time
import socket
import logging
import configparser
import re
import datetime # Added for timed logging
import signal # Added for signal handling
import sys # Added for sys.exit
from typing import List, Optional, Tuple
from selenium import webdriver
from selenium.webdriver import Keys
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from chromedriver_py import binary_path

# --- Constants ---
SENDER_EMAILS = ['info@account.netflix.com']
NETFLIX_LINK_START_PATTERNS = ['www.netflix.com/account/update-primary', 'www.netflix.com/account/set-primary']
BUTTON_SEARCH_ATTR_NAME = 'data-uia'
BUTTON_SEARCH_ATTR_VALUE = 'set-primary-location-action'
LOG_FILENAME = 'status.log'

# --- Helper Functions ---
def setup_logging():
	"""Configures logging for the script."""
	logging.basicConfig(filename=LOG_FILENAME, encoding='utf8', level=logging.INFO, # Changed level to INFO
						format='%(asctime)s %(levelname)-8s %(message)s',
						datefmt='%Y-%m-%d %H:%M:%S')
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
	_processed_email_uids: set[bytes]

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
		self._processed_email_uids = set()
		self._driver = self._init_webdriver()
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
		use_chromedriver_py = self._config.getboolean('CHROMEDRIVER', 'UseChromedriverPy', fallback=True)
		self._chromedriver_path = binary_path if use_chromedriver_py else self._config.get('CHROMEDRIVER', 'ExecutablePath')
		self._move_to_mailbox = self._config.getboolean('GENERAL', 'MoveEmailsToMailbox', fallback=False)
		self._move_to_mailbox_name = self._config.get('GENERAL', 'MailboxName', fallback='Netflix')

		logging.info("Configuration loaded successfully.")

	def _init_webdriver(self) -> Optional[webdriver.Chrome]:
		"""Initializes and returns a Selenium Chrome WebDriver instance."""
		try:
			logging.info(f"Initializing WebDriver with path: {self._chromedriver_path}")
			svc = webdriver.ChromeService(executable_path=self._chromedriver_path)
			chrome_options = webdriver.ChromeOptions()
			chrome_options.add_argument("--headless")
			chrome_options.add_argument("--disable-gpu")
			chrome_options.add_argument("--no-sandbox")
			chrome_options.add_argument("--disable-dev-shm-usage")
			driver = webdriver.Chrome(options=chrome_options, service=svc)
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
		self._processed_email_uids.add(uid)
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
		if not self._driver:
			logging.error("WebDriver is not available. Cannot handle Netflix update.")
			return False

		logging.info(f"Selenium: Navigating to update link...")
		try:
			self._driver.get(update_link)
			# Use WebDriverWait for key elements instead of fixed sleeps
			wait = WebDriverWait(self._driver, 20) # Increased wait time

			# Check if login is needed (wait for email field)
			try:
				# Wait briefly for login elements to appear
				login_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="userLoginId"]')))
				logging.info('Login required. Attempting to login to Netflix account.')
				if self._netflix_login():
					# Wait for potential redirect or page update after login
					# Check for the button again after potential login success
					wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, f'button[{BUTTON_SEARCH_ATTR_NAME}="{BUTTON_SEARCH_ATTR_VALUE}"]')))
					logging.info("Login successful, proceeding to click button.")
				else:
					logging.error("Netflix login failed. Cannot proceed with update.")
					return False
			except Exception: # Catches TimeoutException if login field not found
				logging.info('Login form not found, assuming already logged in or page structure differs.')
				# Proceed assuming we might be on the target page

			# Find and click the confirmation button
			button_selector = f'button[{BUTTON_SEARCH_ATTR_NAME}="{BUTTON_SEARCH_ATTR_VALUE}"]'
			logging.info(f"Attempting to find and click button with selector: {button_selector}")
			try:
				# Wait for the button to be clickable
				button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, button_selector)))
				logging.info("Update button found and clickable. Clicking...")
				button.click()
				# Add a small wait or check for a success message if possible
				time.sleep(3) # Short pause after click to allow action
				# TODO: Ideally, check for a success element here instead of sleep
				logging.info("Successfully clicked the update button.")
				return True
			except Exception as e: # Catches TimeoutException, NoSuchElementException etc.
				logging.error(f"Could not find or click the update button using selector: {button_selector}. Error: {e}", exc_info=True)
				self._save_screenshot("button_fail")
				return False

		except Exception as e:
			logging.error(f"Unhandled exception during Selenium handling: {e}", exc_info=True)
			self._save_screenshot("selenium_error")
			return False

	def _netflix_login(self) -> bool:
		"""Performs login on the Netflix page using configured credentials."""
		if not self._driver: return False
		try:
			wait = WebDriverWait(self._driver, 10)
			email_field = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="userLoginId"]')))
			password_field = self._driver.find_element(By.CSS_SELECTOR, 'input[name="password"]')
			login_button = self._driver.find_element(By.CSS_SELECTOR, 'button[data-uia="login-submit-button"]')

			email_field.clear()
			email_field.send_keys(self._netflix_username)
			password_field.clear()
			password_field.send_keys(self._netflix_password)
			login_button.click() # Use click instead of Keys.RETURN for reliability
			logging.info("Login submitted.")
			# Wait briefly for login process or error message
			time.sleep(3)
			# Check if login failed (e.g., error message appears) - Optional but good
			try:
				# Example: Check for a common login error message element
				self._driver.find_element(By.CSS_SELECTOR, '[data-uia="login-error-message"]')
				logging.error("Login failed: Error message detected on page.")
				self._save_screenshot("login_fail_message")
				return False
			except NoSuchElementException:
				# No error message found, assume login might be proceeding
				pass
			return True
		except Exception as e: # Catch TimeoutException, NoSuchElementException etc.
			logging.error(f"Error during Netflix login attempt: {e}", exc_info=True)
			self._save_screenshot("login_element_fail")
			return False

	def _save_screenshot(self, prefix: str):
		"""Saves a screenshot for debugging purposes."""
		if not self._driver: return
		try:
			filename = f"{prefix}_debug_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
			self._driver.save_screenshot(filename)
			logging.info(f"Saved screenshot: {filename}")
		except Exception as ss_err:
			logging.error(f"Failed to save screenshot: {ss_err}")

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
		"""Starts the polling loop."""
		logging.info("Scheduler starting run loop.")
		log_interval_seconds = 60 * 10 # Log status every 10 minutes

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

		logging.info("Scheduler loop finished.")
		
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
