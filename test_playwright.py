#!/usr/bin/env python3
"""
Playwright Test Script for Netflix Household Update
Monitors IMAP for a Netflix email, extracts the update link,
and uses Playwright to click the confirmation button.

Usage: .venv/bin/python test_playwright.py
"""

import imaplib
import email
import re
import time
import configparser
import sys
from datetime import datetime

from playwright.sync_api import sync_playwright

# --- Configuration ---
SENDER_EMAILS = ["info@account.netflix.com"]
LINK_PATTERNS = [
    "www.netflix.com/account/update-primary",
    "www.netflix.com/account/set-primary",
]
BUTTON_SELECTOR = 'button[data-uia="set-primary-location-action"]'
LOGIN_EMAIL_SELECTOR = 'input[name="userLoginId"]'
LOGIN_PASSWORD_SELECTOR = 'input[name="password"]'
LOGIN_SUBMIT_SELECTOR = 'button[data-uia="login-submit-button"]'


def load_config():
    config = configparser.ConfigParser()
    config.read("config.ini")
    return {
        "imap_server": config.get("EMAIL", "ImapServer"),
        "imap_port": config.getint("EMAIL", "ImapPort"),
        "imap_username": config.get("EMAIL", "Username"),
        "imap_password": config.get("EMAIL", "Password"),
        "mailbox": config.get("EMAIL", "Mailbox", fallback="INBOX"),
        "netflix_username": config.get("NETFLIX", "Username"),
        "netflix_password": config.get("NETFLIX", "Password"),
    }


def extract_link_from_email(raw_email: bytes) -> str | None:
    """Extract Netflix update link from raw email bytes."""
    parsed = email.message_from_bytes(raw_email)
    sender = parsed.get("From", "")
    if not any(s in sender for s in SENDER_EMAILS):
        return None

    for part in parsed.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            html = payload.decode(charset, errors="replace")

            for pattern in LINK_PATTERNS:
                match = re.search(
                    rf'https://{re.escape(pattern)}[^\s\'"]*', html, re.IGNORECASE
                )
                if match:
                    link = match.group(0).replace("&amp;", "&").strip()
                    if "netflix.com" in link:
                        return link
    return None


def wait_for_netflix_email(cfg, timeout_minutes=10):
    """Poll IMAP for a new Netflix email with update link. Returns the link or None."""
    print(f"[{datetime.now():%H:%M:%S}] Connecting to IMAP {cfg['imap_server']}...")
    mail = imaplib.IMAP4_SSL(cfg["imap_server"], cfg["imap_port"])
    mail.login(cfg["imap_username"], cfg["imap_password"])
    print(
        f"[{datetime.now():%H:%M:%S}] Connected. Watching for Netflix emails (timeout: {timeout_minutes}min)..."
    )

    deadline = time.time() + timeout_minutes * 60
    seen_uids = set()

    while time.time() < deadline:
        mail.select(cfg["mailbox"])
        # Search for unseen emails from Netflix
        sender_criteria = " ".join(f'(FROM "{s}")' for s in SENDER_EMAILS)
        if len(SENDER_EMAILS) > 1:
            search = f"(UNSEEN (OR {sender_criteria}))"
        else:
            search = f"(UNSEEN {sender_criteria})"

        _, data = mail.search(None, search)
        email_ids = data[0].split()

        for eid in email_ids:
            # Get UID
            _, uid_data = mail.fetch(eid, "(UID)")
            uid_match = re.search(rb"UID\s+(\d+)", uid_data[0])
            if not uid_match:
                continue
            uid = uid_match.group(1)
            if uid in seen_uids:
                continue
            seen_uids.add(uid)

            # Fetch content
            _, msg_data = mail.fetch(eid, "(RFC822)")
            if msg_data and isinstance(msg_data[0], tuple):
                raw = msg_data[0][1]
                link = extract_link_from_email(raw)
                if link:
                    print(
                        f"[{datetime.now():%H:%M:%S}] ✅ Found update link in UID {uid.decode()}"
                    )
                    print(f"   Link: {link[:80]}...")
                    # Mark as seen
                    mail.store(eid, "+FLAGS", r"(\Seen)")
                    mail.logout()
                    return link
                else:
                    print(
                        f"[{datetime.now():%H:%M:%S}] ℹ️  Email UID {uid.decode()} from Netflix but no update link"
                    )

        remaining = int(deadline - time.time())
        print(
            f"[{datetime.now():%H:%M:%S}] No new update email yet. Waiting 10s... ({remaining}s remaining)"
        )
        time.sleep(10)

    mail.logout()
    return None


def click_update_with_playwright(
    link: str, netflix_user: str, netflix_pass: str
) -> bool:
    """Use Playwright to navigate to the link and click the update button."""
    print(f"\n[{datetime.now():%H:%M:%S}] 🎭 Starting Playwright...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        try:
            print(f"[{datetime.now():%H:%M:%S}] Navigating to Netflix update page...")
            page.goto(link, timeout=15000, wait_until="domcontentloaded")
            print(f"[{datetime.now():%H:%M:%S}] Page loaded. URL: {page.url}")

            # Check if login is needed
            login_field = page.locator(LOGIN_EMAIL_SELECTOR)
            if login_field.is_visible(timeout=3000):
                print(
                    f"[{datetime.now():%H:%M:%S}] 🔐 Login required. Entering credentials..."
                )
                login_field.fill(netflix_user)
                page.locator(LOGIN_PASSWORD_SELECTOR).fill(netflix_pass)
                page.locator(LOGIN_SUBMIT_SELECTOR).click()
                page.wait_for_load_state("networkidle", timeout=10000)
                print(f"[{datetime.now():%H:%M:%S}] Login submitted. URL: {page.url}")

                # Check for login error
                error = page.locator('[data-uia="login-error-message"]')
                if error.is_visible(timeout=2000):
                    print(f"[{datetime.now():%H:%M:%S}] ❌ Login failed!")
                    return False
                print(f"[{datetime.now():%H:%M:%S}] ✅ Login successful")
            else:
                print(
                    f"[{datetime.now():%H:%M:%S}] No login needed, proceeding directly"
                )

            # Find and click the update button
            print(
                f"[{datetime.now():%H:%M:%S}] Looking for update button: {BUTTON_SELECTOR}"
            )
            button = page.locator(BUTTON_SELECTOR)
            button.wait_for(state="visible", timeout=10000)
            print(f"[{datetime.now():%H:%M:%S}] ✅ Button found! Clicking...")
            button.click()

            # Brief pause to ensure click registers
            page.wait_for_timeout(1000)
            print(
                f"[{datetime.now():%H:%M:%S}] ✅ Successfully clicked the update button!"
            )
            print(f"[{datetime.now():%H:%M:%S}] Final URL: {page.url}")
            return True

        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] ❌ Error: {e}")
            # Take screenshot for debugging
            try:
                page.screenshot(path="test_playwright_error.png")
                print(
                    f"[{datetime.now():%H:%M:%S}] Screenshot saved to test_playwright_error.png"
                )
            except Exception:
                pass
            # Dump page content for debugging
            try:
                content = page.content()
                with open("test_playwright_page.html", "w") as f:
                    f.write(content)
                print(
                    f"[{datetime.now():%H:%M:%S}] Page HTML saved to test_playwright_page.html"
                )
            except Exception:
                pass
            return False

        finally:
            context.close()
            browser.close()
            print(f"[{datetime.now():%H:%M:%S}] 🧹 Browser closed cleanly")


def main():
    print("=" * 60)
    print("Netflix Household Update - Playwright Test")
    print("=" * 60)

    cfg = load_config()
    print(f"IMAP: {cfg['imap_server']}")
    print(f"Netflix user: {cfg['netflix_username']}")
    print()

    # Step 1: Wait for email
    print("Step 1: Watching for Netflix update email...")
    print("   (Send the test email now!)")
    print()
    link = wait_for_netflix_email(cfg, timeout_minutes=10)

    if not link:
        print("\n⏰ Timeout: No Netflix update email received in 10 minutes.")
        print("You can also run with a direct link:")
        print("  .venv/bin/python test_playwright.py <netflix-update-url>")
        sys.exit(1)

    # Step 2: Click with Playwright
    print(f"\nStep 2: Using Playwright to click the update button...")
    success = click_update_with_playwright(
        link, cfg["netflix_username"], cfg["netflix_password"]
    )

    if success:
        print(
            "\n🎉 TEST PASSED - Playwright successfully clicked the Netflix update button!"
        )
    else:
        print("\n❌ TEST FAILED - Check the error output above")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    # Allow direct URL as argument for quick testing
    if len(sys.argv) > 1:
        url = sys.argv[1]
        print(f"Direct URL mode: {url[:60]}...")
        cfg = load_config()
        success = click_update_with_playwright(
            url, cfg["netflix_username"], cfg["netflix_password"]
        )
        sys.exit(0 if success else 1)
    main()
