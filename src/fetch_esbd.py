from __future__ import annotations

import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import (
    BASE_URL,
    ESBD_SERVICE_URL,
    LISTING_URL,
    OPEN_STATUSES,
    OPEN_STATUS_FILTER,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)
from .models import Solicitation
from .parse_esbd import parse_detail_page, parse_service_listing_response


class ESBDScraper:
    def __init__(self) -> None:
        self.base_url = BASE_URL
        self.listing_url = LISTING_URL
        self.session = self._build_session()
        self.service_url = self.discover_service_url()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def fetch_html(self, url: str) -> str:
        response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text

    def discover_service_url(self) -> str:
        try:
            listing_html = self.fetch_html(self.listing_url)
            listing_soup = BeautifulSoup(listing_html, "lxml")
            bundle_url = ""
            for script_tag in listing_soup.select("script[src]"):
                src = script_tag.get("src", "")
                if "shopping_2.js" in src:
                    bundle_url = urljoin(self.base_url, src)
                    break
            if not bundle_url:
                return ESBD_SERVICE_URL

            bundle = self.fetch_html(bundle_url)
            match = re.search(
                r"extensions\['[^']+'\]\s*=\s*function\(\)\{\s*"
                r"function getExtensionAssetsPath\(asset\)\{\s*"
                r"return '([^']+)' \+ asset;\s*\};"
                r".{0,200000}?getExtensionAssetsPath\('services/ESBD\.Service\.ss'\)",
                bundle,
                re.S,
            )
            if not match:
                return ESBD_SERVICE_URL

            asset_prefix = match.group(1).lstrip("/")
            return urljoin(
                self.base_url,
                f"/app/{asset_prefix}services/ESBD.Service.ss",
            )
        except requests.RequestException:
            return ESBD_SERVICE_URL

    def fetch_listing_payload(self, page: int) -> dict:
        response = self.session.post(
            self.service_url,
            json={"page": page, "status": OPEN_STATUS_FILTER, "urlRoot": "esbd"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()

    def enrich_solicitation(self, record: Solicitation) -> Solicitation | None:
        if not record.detail_url:
            return record

        detail_html = self.fetch_html(record.detail_url)
        detail_soup = BeautifulSoup(detail_html, "lxml")
        enriched_record = parse_detail_page(
            detail_soup=detail_soup,
            fallback=record,
            base_url=self.base_url,
        )
        if enriched_record.status.strip() not in OPEN_STATUSES:
            return None
        return enriched_record

    def collect_solicitations(self, max_candidates: int | None = None) -> list[Solicitation]:
        enriched: list[Solicitation] = []
        current_page = 1
        total_pages = 1

        while current_page <= total_pages:
            payload = self.fetch_listing_payload(current_page)
            listing_records, total_pages = parse_service_listing_response(
                payload, self.base_url
            )

            for record in listing_records:
                if record.status.strip() not in OPEN_STATUSES:
                    continue

                enriched_record = self.enrich_solicitation(record)
                if not enriched_record:
                    continue

                enriched.append(enriched_record)
                if max_candidates is not None and len(enriched) >= max_candidates:
                    return enriched

            current_page += 1

        return enriched
