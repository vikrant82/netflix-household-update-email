# wrapper_script.py
import subprocess
import time
import sys
import os
import signal as os_signal # Renamed to avoid conflict with signal module used for handlers
import logging
from datetime import datetime, timedelta
from typing import Optional # Added import for Optional

# --- Configuration ---
TARGET_SCRIPT_NAME = "netflix_household_update.py" # The script to run and restart
# How often to restart the target script (e.g., every 4 hours)
RESTART_INTERVAL_HOURS = 1
RESTART_INTERVAL_SECONDS = RESTART_INTERVAL_HOURS * 60 * 60
# How long to wait for graceful shutdown before forcefully killing
GRACEFUL_TERMINATION_TIMEOUT_SECONDS = 15
# Log file for this wrapper script
WRAPPER_LOG_FILE = "wrapper.log"
# --- End Configuration ---

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(WRAPPER_LOG_FILE),
        logging.StreamHandler(sys.stdout) # Also print to console
    ]
)
# --- End Logging Setup ---

def start_target_script():
    """Starts the target Python script using the same interpreter."""
    python_executable = sys.executable # Use the same python running this wrapper
    logging.info(f"Attempting to start script: {TARGET_SCRIPT_NAME} using {python_executable}")
    try:
        # Start the process. stdout/stderr will go where the target script directs them (e.g., its own log file)
        process = subprocess.Popen([python_executable, TARGET_SCRIPT_NAME])
        logging.info(f"Successfully started {TARGET_SCRIPT_NAME} with PID: {process.pid}")
        return process
    except FileNotFoundError:
        logging.error(f"Error: Could not find the script '{TARGET_SCRIPT_NAME}'. Make sure it's in the correct directory or the path is correct.")
        return None
    except Exception as e:
        logging.error(f"Failed to start {TARGET_SCRIPT_NAME}: {e}", exc_info=True)
        return None

def stop_target_script(process: subprocess.Popen):
    """Attempts to gracefully stop the process, then forcefully kills if necessary."""
    if process is None or process.poll() is not None:
        logging.info("Target process is already stopped.")
        return

    pid = process.pid
    logging.info(f"Attempting graceful termination (SIGTERM) for PID: {pid} ({TARGET_SCRIPT_NAME})")
    try:
        process.terminate() # Sends SIGTERM on Unix, TerminateProcess on Windows
        try:
            # Wait for the process to terminate
            process.wait(timeout=GRACEFUL_TERMINATION_TIMEOUT_SECONDS)
            logging.info(f"Process {pid} terminated gracefully.")
        except subprocess.TimeoutExpired:
            logging.warning(f"Process {pid} did not terminate gracefully after {GRACEFUL_TERMINATION_TIMEOUT_SECONDS} seconds. Forcefully killing (SIGKILL)...")
            process.kill() # Sends SIGKILL on Unix, TerminateProcess on Windows
            try:
                # Give it a moment to die after SIGKILL
                process.wait(timeout=5)
                logging.info(f"Process {pid} killed.")
            except subprocess.TimeoutExpired:
                logging.error(f"Process {pid} failed to be killed even with SIGKILL. Manual intervention might be required.")
            except Exception as e:
                 logging.error(f"Error waiting for process {pid} after kill: {e}", exc_info=True)
        except Exception as e:
             logging.error(f"Error waiting for process {pid} termination: {e}", exc_info=True)

    except Exception as e:
        logging.error(f"An error occurred while trying to terminate process {pid}: {e}", exc_info=True)
        # As a fallback, try killing again if an error happened during terminate/wait
        try:
            if process.poll() is None:
                logging.info(f"Fallback: Attempting kill again for PID {pid}")
                process.kill()
                process.wait(timeout=5)
        except Exception as kill_e:
            logging.error(f"Fallback kill attempt for PID {pid} also failed: {kill_e}", exc_info=True)


# --- End Logging Setup ---

# --- Global variable for current process ---
current_process: Optional[subprocess.Popen] = None

# --- Signal Handling for Wrapper ---
def signal_handler(signum, frame):
    """Handles SIGINT and SIGTERM for the wrapper script."""
    global current_process
    logging.info(f"Wrapper script received signal {os_signal.Signals(signum).name}. Initiating shutdown...")
    if current_process and current_process.poll() is None:
        logging.info("Attempting to stop target script...")
        stop_target_script(current_process)
    else:
        logging.info("Target script not running or already stopped.")
    logging.info("Wrapper script shutting down.")
    sys.exit(0) # Exit gracefully

# --- Main Loop ---
if __name__ == "__main__":
    os_signal.signal(os_signal.SIGINT, signal_handler)
    os_signal.signal(os_signal.SIGTERM, signal_handler)

    logging.info("Wrapper script started.")
    logging.info(f"Target script: {TARGET_SCRIPT_NAME}")
    logging.info(f"Restart interval: {RESTART_INTERVAL_HOURS} hours ({RESTART_INTERVAL_SECONDS} seconds)")

    # current_process is now a global variable
    try:
        while True:
            # Start the script
            # Ensure current_process is updated globally if start_target_script is successful
            proc = start_target_script()
            if proc is None:
                logging.error("Failed to start target script. Retrying in 60 seconds...")
                time.sleep(60)
                continue # Retry starting
            current_process = proc # Assign to global current_process

            if current_process is None: # Should not happen if proc was not None, but as a safeguard
                logging.error("Target script somehow None after successful start. Retrying in 60 seconds...")
                time.sleep(60)
                continue # Retry starting

            start_time = datetime.now()
            restart_time = start_time + timedelta(seconds=RESTART_INTERVAL_SECONDS)
            logging.info(f"Target script running. Will restart around {restart_time.strftime('%Y-%m-%d %H:%M:%S')}")

            # Wait until the restart interval is up OR the process exits prematurely
            wait_seconds = RESTART_INTERVAL_SECONDS
            try:
                # wait() blocks until the process terminates or the timeout occurs
                current_process.wait(timeout=wait_seconds)
                # If wait() returned before the timeout, the process exited on its own
                exit_code = current_process.poll() # Should not be None here
                logging.warning(f"Target script PID {current_process.pid} exited prematurely with code {exit_code}. Restarting immediately.")
                current_process = None # Ensure it's marked as stopped

            except subprocess.TimeoutExpired:
                # This is the normal case: the interval is up
                logging.info(f"Restart interval reached for PID {current_process.pid}. Stopping script...")
                stop_target_script(current_process)
                current_process = None # Mark as stopped

            except Exception as e:
                # Handle unexpected errors during wait
                logging.error(f"An error occurred while waiting for PID {current_process.pid}: {e}", exc_info=True)
                logging.info("Attempting to stop the process before restarting.")
                stop_target_script(current_process)
                current_process = None
                logging.info("Waiting 30 seconds before attempting restart after error...")
                time.sleep(30) # Pause before restarting after an error

            # Small delay before looping to restart, avoids busy-looping if start fails repeatedly
            if current_process is None: # Only sleep if we are about to restart
                 time.sleep(2)

    except KeyboardInterrupt:
        logging.info("Ctrl+C detected. Shutting down wrapper and target script...")
    except Exception as e:
        logging.error(f"An unhandled error occurred in the wrapper's main loop: {e}", exc_info=True)
    finally:
        logging.info("Wrapper script initiating shutdown.")
        if current_process and current_process.poll() is None:
            logging.info("Ensuring target script is stopped on wrapper exit...")
            stop_target_script(current_process)
        logging.info("Wrapper script finished.")
