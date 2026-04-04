from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import time
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
    DETAIL_CACHE_DIR,
    DETAIL_CACHE_TTL_SECONDS,
    OPEN_STATUSES,
    OPEN_STATUS_FILTER,
    PDF_DOWNLOAD_DIR,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    MAX_LISTING_WORKERS,
)
from .models import Solicitation
from .parse_esbd import (
    parse_detail_page,
    parse_detail_payload,
    parse_service_listing_response,
)
from .pdf_utils import download_and_extract_solicitation_pdfs


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
        self.detail_cache_dir = Path(DETAIL_CACHE_DIR)
        self.detail_cache_ttl_seconds = DETAIL_CACHE_TTL_SECONDS
        self.max_listing_workers = MAX_LISTING_WORKERS

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
        self, detailed_record: Solicitation, fallback: Solicitation
    ) -> bool:
        has_contact = any(
            [
                detailed_record.contact_name,
                detailed_record.contact_email,
                detailed_record.contact_phone,
                detailed_record.bid_response_email,
            ]
        )
        has_description = bool(detailed_record.description or detailed_record.brief_description)
        has_classification = bool(detailed_record.category_classification)
        has_attachments = bool(detailed_record.attachment_urls)

        improved_over_fallback = any(
            [
                detailed_record.category_classification
                and detailed_record.category_classification != fallback.category_classification,
                detailed_record.description,
                has_contact,
                has_attachments,
            ]
        )

        return improved_over_fallback and (has_contact or has_description or has_attachments or has_classification)

    def fetch_detail_payload(
        self,
        solicitation_id: str,
        session: requests.Session | None = None,
        use_cache: bool = True,
    ) -> dict:
        cache_path = self.detail_cache_dir / f"{solicitation_id}.json"
        cached_payload = self._read_cached_payload(cache_path) if use_cache else None
        if cached_payload is not None:
            return cached_payload

        client = session or self.session
        response = client.get(
            self.details_service_url,
            params={"identification": solicitation_id, "urlRoot": "esbd"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        self._write_cached_payload(cache_path, payload)
        return payload

    def _read_cached_payload(self, cache_path: Path) -> dict | None:
        if not cache_path.exists():
            return None
        try:
            cache_age_seconds = time.time() - cache_path.stat().st_mtime
            if cache_age_seconds > self.detail_cache_ttl_seconds:
                return None
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_cached_payload(self, cache_path: Path, payload: dict) -> None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=True),
                encoding="utf-8",
            )
        except OSError:
            return

    def fetch_solicitation_details(
        self,
        record: Solicitation,
        session: requests.Session | None = None,
        use_cache: bool = True,
    ) -> Solicitation | None:
        if not record.detail_url:
            return record

        detailed_record = record
        try:
            detail_payload = self.fetch_detail_payload(
                record.solicitation_id,
                session=session,
                use_cache=use_cache,
            )
            detailed_record = parse_detail_payload(
                payload=detail_payload,
                fallback=record,
                base_url=self.base_url,
            )
        except (requests.RequestException, ValueError):
            detailed_record = record

        if detailed_record.status.strip() not in OPEN_STATUSES:
            return None
        if self.is_detail_parse_complete(detailed_record, record):
            return detailed_record

        try:
            detail_html = self.fetch_html(record.detail_url, session=session)
            detail_soup = BeautifulSoup(detail_html, "lxml")
            html_record = parse_detail_page(
                detail_soup=detail_soup,
                fallback=detailed_record,
                base_url=self.base_url,
            )
            if html_record.status.strip() not in OPEN_STATUSES:
                return None
            if self.is_detail_parse_complete(html_record, detailed_record):
                return html_record
            return html_record
        except requests.RequestException:
            return detailed_record

    def collect_listing_solicitations(
        self, max_candidates: int | None = None
    ) -> list[Solicitation]:
        listings: list[Solicitation] = []
        first_payload = self.fetch_listing_payload(1)
        first_page_records, total_pages = parse_service_listing_response(
            first_payload, self.base_url
        )

        ordered_pages: dict[int, list[Solicitation]] = {1: first_page_records}
        if total_pages > 1:
            worker_count = max(1, min(self.max_listing_workers, total_pages - 1))

            def task(page: int) -> tuple[int, list[Solicitation]]:
                payload = self.fetch_listing_payload(page)
                page_records, _ = parse_service_listing_response(payload, self.base_url)
                return page, page_records

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {
                    executor.submit(task, page): page
                    for page in range(2, total_pages + 1)
                }
                for future in as_completed(future_map):
                    page_number = future_map[future]
                    try:
                        page, page_records = future.result()
                    except Exception:
                        payload = self.fetch_listing_payload(page_number)
                        page_records, _ = parse_service_listing_response(
                            payload, self.base_url
                        )
                        page = page_number
                    ordered_pages[page] = page_records

        for page in range(1, total_pages + 1):
            for record in ordered_pages.get(page, []):
                if record.status.strip() not in OPEN_STATUSES:
                    continue
                listings.append(record)
                if max_candidates is not None and len(listings) >= max_candidates:
                    return listings

        return listings

    def fetch_details_for_records(
        self,
        records: list[Solicitation],
        max_workers: int | None = None,
        use_cache: bool = True,
    ) -> list[Solicitation]:
        if not records:
            return []

        worker_count = max_workers or self.max_detail_workers
        worker_count = max(1, min(worker_count, len(records)))

        def task(record: Solicitation) -> Solicitation | None:
            session = self._build_session()
            return self.fetch_solicitation_details(
                record,
                session=session,
                use_cache=use_cache,
            )

        detailed_records: list[Solicitation | None] = [None] * len(records)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(task, record): index
                for index, record in enumerate(records)
            }
            for future in as_completed(future_map):
                index = future_map[future]
                try:
                    detailed_records[index] = future.result()
                except Exception:
                    detailed_records[index] = records[index]

        return [record for record in detailed_records if record]

    def download_and_extract_pdfs_for_solicitation(
        self,
        record: Solicitation,
        session: requests.Session | None = None,
        use_cache: bool = True,
    ) -> Solicitation:
        client = session or self.session
        return download_and_extract_solicitation_pdfs(
            session=client,
            record=record,
            base_dir=self.pdf_download_dir,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            use_cache=use_cache,
        )

    def download_and_extract_pdfs_for_records(
        self,
        records: list[Solicitation],
        max_workers: int | None = None,
        use_cache: bool = True,
    ) -> list[Solicitation]:
        if not records:
            return []

        worker_count = max_workers or self.max_pdf_workers
        worker_count = max(1, min(worker_count, len(records)))

        def task(record: Solicitation) -> Solicitation:
            session = self._build_session()
            return self.download_and_extract_pdfs_for_solicitation(
                record,
                session=session,
                use_cache=use_cache,
            )

        records_with_pdfs: list[Solicitation | None] = [None] * len(records)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(task, record): index
                for index, record in enumerate(records)
            }
            for future in as_completed(future_map):
                index = future_map[future]
                try:
                    records_with_pdfs[index] = future.result()
                except Exception:
                    records_with_pdfs[index] = records[index]

        return [record for record in records_with_pdfs if record]

    def collect_solicitations(self, max_candidates: int | None = None) -> list[Solicitation]:
        listings = self.collect_listing_solicitations(max_candidates=max_candidates)
        return self.fetch_details_for_records(listings)
