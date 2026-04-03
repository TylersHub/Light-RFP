from __future__ import annotations

from pathlib import Path

BASE_URL = "https://www.txsmartbuy.gov"
LISTING_PATH = "/esbd"
LISTING_URL = f"{BASE_URL}{LISTING_PATH}"
ESBD_SERVICE_URL = (
    f"{BASE_URL}/app/extensions/CPA/CPAMain/1.0.0/services/ESBD.Service.ss"
)
OPEN_STATUSES = {"Posted", "Addendum Posted"}
OPEN_STATUS_FILTER = "1"
DETAIL_CANDIDATE_MULTIPLIER = 8
MIN_DETAIL_CANDIDATES = 120
MAX_DETAIL_WORKERS = 8
PDF_CANDIDATE_MULTIPLIER = 2
MIN_PDF_CANDIDATES = 40
MAX_PDF_WORKERS = 4
PDF_DOWNLOAD_DIR = Path("data/pdfs")
AI_SUMMARY_TIMEOUT_SECONDS = 90
AI_SUMMARY_MAX_INPUT_CHARS = 12000
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT_SECONDS = 30
