from __future__ import annotations

BASE_URL = "https://www.txsmartbuy.gov"
LISTING_PATH = "/esbd"
LISTING_URL = f"{BASE_URL}{LISTING_PATH}"
ESBD_SERVICE_URL = (
    f"{BASE_URL}/app/extensions/CPA/CPAMain/1.0.0/services/ESBD.Service.ss"
)
OPEN_STATUSES = {"Posted", "Addendum Posted"}
OPEN_STATUS_FILTER = "1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT_SECONDS = 30
