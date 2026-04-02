from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser

from .models import Attachment, Solicitation


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def safe_parse_date(value: str) -> str:
    value = normalize_whitespace(value)
    if not value:
        return ""
    try:
        return date_parser.parse(value).isoformat()
    except (ValueError, OverflowError):
        return value


def first_sentences(text: str, limit: int = 2) -> str:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    return " ".join(parts[:limit]).strip()


def build_agency_lookup(soup: BeautifulSoup) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for option in soup.select('select[name="agency"] option'):
        text = normalize_whitespace(option.get_text(" ", strip=True))
        if " - " not in text:
            continue
        agency_name, agency_number = text.rsplit(" - ", 1)
        lookup[agency_number.strip()] = agency_name.strip()
    return lookup


def extract_label_value_pairs(container: Tag) -> dict[str, str]:
    result: dict[str, str] = {}
    for cell in container.select(".esbd-result-cell"):
        label_tag = cell.find("strong")
        value_tag = cell.find("p")
        if not label_tag or not value_tag:
            continue
        label = normalize_whitespace(label_tag.get_text(" ", strip=True)).rstrip(":")
        value = normalize_whitespace(value_tag.get_text(" ", strip=True))
        result[label] = value
    return result


def parse_listing_page(soup: BeautifulSoup, base_url: str) -> list[Solicitation]:
    rows: list[Solicitation] = []
    for row in soup.select(".esbd-result-row"):
        title_link = row.select_one(".esbd-result-title a")
        if not title_link:
            continue

        title = normalize_whitespace(title_link.get_text(" ", strip=True))
        detail_url = urljoin(base_url, title_link.get("href", ""))

        values: dict[str, str] = {}
        for paragraph in row.select("p"):
            strong = paragraph.find("strong")
            if not strong:
                continue
            label = normalize_whitespace(strong.get_text(" ", strip=True)).rstrip(":")
            paragraph_text = normalize_whitespace(paragraph.get_text(" ", strip=True))
            value = normalize_whitespace(paragraph_text.replace(strong.get_text(" ", strip=True), "", 1))
            values[label] = value

        due_date = values.get("Due Date", "")
        due_time = values.get("Due Time", "")
        rows.append(
            Solicitation(
                title=title,
                solicitation_id=values.get("Solicitation ID", ""),
                status=values.get("Status", ""),
                agency_number=values.get("Agency/Texas SmartBuy Member Number", ""),
                posting_date=safe_parse_date(values.get("Posting Date", "")),
                due_date=due_date,
                due_time=due_time,
                due_datetime=safe_parse_date(f"{due_date} {due_time}".strip()),
                detail_url=detail_url,
            )
        )

    return rows


def parse_detail_page(
    detail_soup: BeautifulSoup,
    fallback: Solicitation,
    agency_lookup: dict[str, str],
    base_url: str,
) -> Solicitation:
    title_tag = detail_soup.select_one(".esbd-result-title h4")
    title = normalize_whitespace(title_tag.get_text(" ", strip=True)) if title_tag else fallback.title

    columns = detail_soup.select(".esbd-result-body-columns .esbd-result-column")
    values: dict[str, str] = {}
    for column in columns:
        values.update(extract_label_value_pairs(column))

    description = ""
    addendum_text = ""
    for section in detail_soup.select(".esbd-full-width"):
        label_tag = section.find("strong")
        rich_text = section.select_one(".rich-text-editor-content")
        if not label_tag or not rich_text:
            continue
        label = normalize_whitespace(label_tag.get_text(" ", strip=True)).rstrip(":")
        value = normalize_whitespace(rich_text.get_text(" ", strip=True))
        if label == "Solicitation Description":
            description = value
        elif label == "Addendum":
            addendum_text = value

    attachments: list[Attachment] = []
    for row in detail_soup.select(".esbd-attachment-row"):
        link = row.select_one('[data-action="downloadURL"]')
        if not link:
            continue
        cells = row.select("p")
        description_text = ""
        if len(cells) >= 3:
            description_text = normalize_whitespace(cells[2].get_text(" ", strip=True))
        attachments.append(
            Attachment(
                name=normalize_whitespace(link.get_text(" ", strip=True)),
                url=urljoin(base_url, link.get("data-href", "")),
                description=description_text,
            )
        )

    solicitation_id = values.get("Solicitation ID", fallback.solicitation_id)
    status = values.get("Status", fallback.status)
    agency_number = values.get(
        "Agency/Texas SmartBuy Member Number", fallback.agency_number
    )
    posting_date = safe_parse_date(
        values.get("Solicitation Posting Date", fallback.posting_date)
    )
    due_date = values.get("Response Due Date", fallback.due_date)
    due_time = values.get("Response Due Time", fallback.due_time)
    due_datetime = safe_parse_date(f"{due_date} {due_time}".strip())
    category_classification = values.get("Class/Item Code", "")
    agency_name = agency_lookup.get(agency_number, agency_number)
    brief_description = first_sentences(description)

    raw_blob = " ".join(
        [
            title,
            solicitation_id,
            status,
            agency_name,
            category_classification,
            description,
            addendum_text,
            " ".join(attachment.name for attachment in attachments),
            " ".join(attachment.description for attachment in attachments),
        ]
    )

    return Solicitation(
        title=title,
        solicitation_id=solicitation_id,
        status=status,
        agency_number=agency_number,
        agency_name=agency_name,
        posting_date=posting_date,
        due_date=due_date,
        due_time=due_time,
        due_datetime=due_datetime,
        description=description,
        brief_description=brief_description,
        category_classification=category_classification,
        contact_name=values.get("Contact Name", ""),
        contact_email=values.get("Contact Email", ""),
        contact_phone=values.get("Contact Number", ""),
        bid_response_email=values.get("Bid Response Email", ""),
        detail_url=fallback.detail_url,
        attachment_urls=attachments,
        addendum_text=addendum_text,
        raw_text_blob=normalize_whitespace(raw_blob),
    )
