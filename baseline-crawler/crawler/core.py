"""
FILE DESCRIPTION: Foundational module for global configuration and logging.
CONSOLIDATED FROM: config.py, logger.py
KEY FUNCTIONS/CLASSES: setup_logger, CompanyFormatter, Config, Logger
"""

import logging
import sys
import os
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# === CONFIGURATION SECTION ===

# Load .env at the beginning of core
load_dotenv(Path(__file__).resolve().parents[2] / '.env')

# Domains allowed to crawl
# Empty = restrict to seed domains only
ALLOWED_DOMAINS = []

# Network timeout for HTTP requests (seconds)
REQUEST_TIMEOUT = 30

# canonical data directory for the crawler
try:
    DATA_DIR = Path(__file__).resolve().parents[1] / 'data'
except NameError:
    DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

# Worker scaling parameters
MIN_WORKERS = int(os.getenv("MIN_WORKERS", 5))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 5))
MAX_PARALLEL_SITES = int(os.getenv("MAX_PARALLEL_SITES", 3))
CRAWL_DELAY = 0.1  # Seconds between requests per worker to avoid 429s
USER_AGENT = "Chrome/126.0.0.0"

# Playwright / JS Rendering Waiting Periods (seconds)
JS_GOTO_TIMEOUT = 25
JS_WAIT_TIMEOUT = 5
JS_STABILITY_TIME = 2


# === LOGGING SECTION ===

class CompanyFormatter(logging.Formatter):
    """
    FLOW: Receives a log record -> Extracts timestamp -> Formats according to company standard 
    (e.g., [ Tue Jan 06 05:32:41 AM UTC 2026 ]) -> Prepends level and context -> Returns final string.
    """
    def format(self, record):
        dt = datetime.fromtimestamp(record.created)
        timestamp = dt.strftime("%a %b %d %I:%M:%S %p UTC %Y")
        context = getattr(record, 'context', 'root')
        return f"[ {timestamp} ] : {record.levelname} : {context} : {record.getMessage()}"

def setup_logger(name="crawler", log_file=None, level=logging.INFO):
    """
    FLOW: Initializes/Retrieves logger -> Checks for existing handlers to prevent duplicates ->
    Sets propagation for child loggers -> Attaches Console and optional File handlers with CompanyFormatter.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    if logger.handlers:
        return logger

    if name != "crawler":
        logger.propagate = True
        setup_logger("crawler", log_file=log_file, level=level)
        return logger

    formatter = CompanyFormatter()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger

# Global logger instance
logger = setup_logger()

# --- Environment Checks ---
try:
    import brotli
    logger.info("[SYSTEM] Brotli library found. Decompression enabled.")
except ImportError:
    logger.warning("[SYSTEM] Brotli library NOT found. Brotli-encoded responses will fail to decompress.")
