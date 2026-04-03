from __future__ import annotations

from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import BASE_URL, LISTING_URL, OPEN_STATUSES, REQUEST_TIMEOUT_SECONDS, USER_AGENT
from .models import Solicitation
from .parse_esbd import build_agency_lookup, parse_detail_page, parse_listing_page


class ESBDScraper:
    def __init__(self) -> None:
        self.base_url = BASE_URL
        self.listing_url = LISTING_URL
        self.listing_urls = [
            self.listing_url,
            f"{self.listing_url}?status={quote_plus('Posted')}",
            f"{self.listing_url}?status={quote_plus('Addendum Posted')}",
        ]
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def fetch_html(self, url: str) -> str:
        response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text

    def collect_solicitations(self, max_candidates: int = 72) -> list[Solicitation]:
        enriched: list[Solicitation] = []
        seen_detail_urls: set[str] = set()

        for listing_url in self.listing_urls:
            listing_html = self.fetch_html(listing_url)
            listing_soup = BeautifulSoup(listing_html, "lxml")
            agency_lookup = build_agency_lookup(listing_soup)
            listing_records = parse_listing_page(listing_soup, self.base_url)

            for record in listing_records:
                if record.status.strip() not in OPEN_STATUSES:
                    continue
                if record.detail_url in seen_detail_urls:
                    continue

                detail_html = self.fetch_html(record.detail_url)
                detail_soup = BeautifulSoup(detail_html, "lxml")
                enriched_record = parse_detail_page(
                    detail_soup=detail_soup,
                    fallback=record,
                    agency_lookup=agency_lookup,
                    base_url=self.base_url,
                )

                if enriched_record.status.strip() not in OPEN_STATUSES:
                    continue

                seen_detail_urls.add(record.detail_url)
                enriched.append(enriched_record)
                if len(enriched) >= max_candidates:
                    return enriched

        return enriched
