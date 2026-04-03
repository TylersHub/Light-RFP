from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
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
    MAX_DETAIL_WORKERS,
    MAX_PDF_WORKERS,
    OPEN_STATUSES,
    OPEN_STATUS_FILTER,
    PDF_DOWNLOAD_DIR,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)
from .models import Solicitation
from .parse_esbd import (
    parse_detail_page,
    parse_detail_payload,
    parse_service_listing_response,
)
from .pdf_utils import enrich_solicitation_pdfs


class ESBDScraper:
    def __init__(self) -> None:
        self.base_url = BASE_URL
        self.listing_url = LISTING_URL
        self.session = self._build_session()
        self.service_url = self.discover_service_url()
        self.details_service_url = self.service_url.replace(
            "ESBD.Service.ss", "ESBD.Details.Service.ss"
        )
        self.max_detail_workers = MAX_DETAIL_WORKERS
        self.max_pdf_workers = MAX_PDF_WORKERS
        self.pdf_download_dir = Path(PDF_DOWNLOAD_DIR)

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

    def fetch_html(self, url: str, session: requests.Session | None = None) -> str:
        client = session or self.session
        response = client.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
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

    def is_detail_parse_complete(
        self, enriched_record: Solicitation, fallback: Solicitation
    ) -> bool:
        has_contact = any(
            [
                enriched_record.contact_name,
                enriched_record.contact_email,
                enriched_record.contact_phone,
                enriched_record.bid_response_email,
            ]
        )
        has_description = bool(enriched_record.description or enriched_record.brief_description)
        has_classification = bool(enriched_record.category_classification)
        has_attachments = bool(enriched_record.attachment_urls)

        improved_over_fallback = any(
            [
                enriched_record.category_classification
                and enriched_record.category_classification != fallback.category_classification,
                enriched_record.description,
                has_contact,
                has_attachments,
            ]
        )

        return improved_over_fallback and (has_contact or has_description or has_attachments or has_classification)

    def fetch_detail_payload(
        self,
        solicitation_id: str,
        session: requests.Session | None = None,
    ) -> dict:
        client = session or self.session
        response = client.get(
            self.details_service_url,
            params={"identification": solicitation_id, "urlRoot": "esbd"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()

    def enrich_solicitation(
        self,
        record: Solicitation,
        session: requests.Session | None = None,
    ) -> Solicitation | None:
        if not record.detail_url:
            return record

        last_record = record
        for _ in range(3):
            try:
                detail_payload = self.fetch_detail_payload(
                    record.solicitation_id, session=session
                )
                enriched_record = parse_detail_payload(
                    payload=detail_payload,
                    fallback=record,
                    base_url=self.base_url,
                )
            except (requests.RequestException, ValueError):
                detail_html = self.fetch_html(record.detail_url, session=session)
                detail_soup = BeautifulSoup(detail_html, "lxml")
                enriched_record = parse_detail_page(
                    detail_soup=detail_soup,
                    fallback=record,
                    base_url=self.base_url,
                )
            if enriched_record.status.strip() not in OPEN_STATUSES:
                return None
            last_record = enriched_record
            if self.is_detail_parse_complete(enriched_record, record):
                return enriched_record
        return last_record

    def collect_listing_solicitations(
        self, max_candidates: int | None = None
    ) -> list[Solicitation]:
        listings: list[Solicitation] = []
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
                listings.append(record)
                if max_candidates is not None and len(listings) >= max_candidates:
                    return listings

            current_page += 1

        return listings

    def enrich_solicitations(
        self,
        records: list[Solicitation],
        max_workers: int | None = None,
    ) -> list[Solicitation]:
        if not records:
            return []

        worker_count = max_workers or self.max_detail_workers
        worker_count = max(1, min(worker_count, len(records)))

        def task(record: Solicitation) -> Solicitation | None:
            session = self._build_session()
            return self.enrich_solicitation(record, session=session)

        enriched: list[Solicitation | None] = [None] * len(records)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(task, record): index
                for index, record in enumerate(records)
            }
            for future in as_completed(future_map):
                index = future_map[future]
                try:
                    enriched[index] = future.result()
                except Exception:
                    enriched[index] = records[index]

        return [record for record in enriched if record]

    def enrich_solicitation_with_pdfs(
        self,
        record: Solicitation,
        session: requests.Session | None = None,
    ) -> Solicitation:
        client = session or self.session
        return enrich_solicitation_pdfs(
            session=client,
            record=record,
            base_dir=self.pdf_download_dir,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )

    def enrich_solicitations_with_pdfs(
        self,
        records: list[Solicitation],
        max_workers: int | None = None,
    ) -> list[Solicitation]:
        if not records:
            return []

        worker_count = max_workers or self.max_pdf_workers
        worker_count = max(1, min(worker_count, len(records)))

        def task(record: Solicitation) -> Solicitation:
            session = self._build_session()
            return self.enrich_solicitation_with_pdfs(record, session=session)

        enriched: list[Solicitation | None] = [None] * len(records)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(task, record): index
                for index, record in enumerate(records)
            }
            for future in as_completed(future_map):
                index = future_map[future]
                try:
                    enriched[index] = future.result()
                except Exception:
                    enriched[index] = records[index]

        return [record for record in enriched if record]

    def collect_solicitations(self, max_candidates: int | None = None) -> list[Solicitation]:
        listings = self.collect_listing_solicitations(max_candidates=max_candidates)
        return self.enrich_solicitations(listings)
