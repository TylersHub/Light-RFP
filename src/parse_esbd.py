from __future__ import annotations

import math
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


def first_non_empty(*values: str) -> str:
    for value in values:
        cleaned = normalize_whitespace(value)
        if cleaned:
            return cleaned
    return ""


def extract_label_value_pairs(container: Tag) -> dict[str, str]:
    result: dict[str, str] = {}
    for cell in container.select(".esbd-result-cell"):
        label_tag = cell.find("strong")
        if not label_tag:
            continue
        label = normalize_whitespace(label_tag.get_text(" ", strip=True)).rstrip(":")
        value_parts: list[str] = []
        for child in cell.children:
            if getattr(child, "name", None) == "strong":
                continue
            if hasattr(child, "get_text"):
                text = child.get_text(" ", strip=True)
            else:
                text = str(child).strip()
            text = normalize_whitespace(text)
            if text:
                value_parts.append(text)
        value = normalize_whitespace(" ".join(value_parts))
        if not value:
            cell_text = normalize_whitespace(cell.get_text(" ", strip=True))
            label_text = normalize_whitespace(label_tag.get_text(" ", strip=True))
            value = normalize_whitespace(cell_text.replace(label_text, "", 1))
        if not value:
            continue
        result[label] = value
    return result


def build_fallback_description(
    title: str,
    category_classification: str,
    attachments: list[Attachment],
) -> str:
    attachment_text = first_non_empty(*(attachment.description for attachment in attachments))
    if attachment_text:
        return first_sentences(attachment_text)

    if category_classification:
        first_code = category_classification.split(";")[0].strip()
        return (
            f"Solicitation for {title}. "
            f"Listed classification: {first_code}."
        )

    if title:
        return f"Solicitation for {title}."

    return ""


def html_to_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "lxml")
    return normalize_whitespace(soup.get_text(" ", strip=True))


def parse_service_listing_response(
    payload: dict,
    base_url: str,
) -> tuple[list[Solicitation], int]:
    rows: list[Solicitation] = []
    lines = payload.get("lines") or []
    records_per_page = int(payload.get("recordsPerPage") or len(lines) or 24)
    total_records = int(payload.get("totalRecordsFound") or len(lines))
    total_pages = max(1, math.ceil(total_records / records_per_page))

    for line in lines:
        title = normalize_whitespace(line.get("title", ""))
        solicitation_id = normalize_whitespace(line.get("solicitationId", ""))
        status = normalize_whitespace(line.get("statusName", ""))
        agency_number = normalize_whitespace(line.get("agencyNumber", ""))
        agency_name = normalize_whitespace(line.get("agencyName", "")) or agency_number
        due_date = normalize_whitespace(line.get("responseDue", ""))
        due_time = normalize_whitespace(line.get("responseTime", ""))
        category_classification = normalize_whitespace(line.get("nigpCodes", ""))
        detail_url = urljoin(base_url, f"/esbd/{solicitation_id}") if solicitation_id else ""
        raw_blob = normalize_whitespace(
            " ".join(
                [
                    title,
                    solicitation_id,
                    status,
                    agency_name,
                    category_classification,
                ]
            )
        )

        rows.append(
            Solicitation(
                title=title,
                solicitation_id=solicitation_id,
                status=status,
                agency_number=agency_number,
                agency_name=agency_name,
                posting_date=safe_parse_date(line.get("postingDate", "")),
                due_date=due_date,
                due_time=due_time,
                due_datetime=safe_parse_date(f"{due_date} {due_time}".strip()),
                category_classification=category_classification,
                detail_url=detail_url,
                raw_text_blob=raw_blob,
            )
        )

    return rows, total_pages


def parse_detail_page(
    detail_soup: BeautifulSoup,
    fallback: Solicitation,
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
    category_classification = values.get(
        "Class/Item Code", fallback.category_classification
    )
    agency_name = fallback.agency_name or agency_number
    brief_description = first_sentences(description) or build_fallback_description(
        title=title,
        category_classification=category_classification,
        attachments=attachments,
    )

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
        contact_name=first_non_empty(
            values.get("Contact Name", ""),
            values.get("Contact", ""),
        ),
        contact_email=first_non_empty(
            values.get("Contact Email", ""),
            values.get("Email", ""),
            values.get("Bid Response Email", ""),
        ),
        contact_phone=first_non_empty(
            values.get("Contact Number", ""),
            values.get("Contact Phone", ""),
            values.get("Phone", ""),
            values.get("Phone Number", ""),
        ),
        bid_response_email=first_non_empty(
            values.get("Bid Response Email", ""),
            values.get("Contact Email", ""),
        ),
        detail_url=fallback.detail_url,
        attachment_urls=attachments,
        addendum_text=addendum_text,
        raw_text_blob=normalize_whitespace(raw_blob),
    )


def parse_detail_payload(payload: dict, fallback: Solicitation, base_url: str) -> Solicitation:
    title = normalize_whitespace(payload.get("title", "")) or fallback.title
    solicitation_id = normalize_whitespace(payload.get("solicitationId", "")) or fallback.solicitation_id
    status = normalize_whitespace(payload.get("statusName", "")) or fallback.status
    agency_number = normalize_whitespace(payload.get("agencyNumber", "")) or fallback.agency_number
    agency_name = normalize_whitespace(payload.get("agencyName", "")) or fallback.agency_name or agency_number
    due_date = normalize_whitespace(payload.get("responseDue", "")) or fallback.due_date
    due_time = normalize_whitespace(payload.get("responseTime", "")) or fallback.due_time
    due_datetime = safe_parse_date(f"{due_date} {due_time}".strip())
    category_classification = normalize_whitespace(payload.get("nigpCodes", "")) or fallback.category_classification
    description = html_to_text(payload.get("description", ""))
    addendum_text = html_to_text(payload.get("addendum", ""))

    attachments: list[Attachment] = []
    for attachment in payload.get("attachments", []) or []:
        attachments.append(
            Attachment(
                name=normalize_whitespace(attachment.get("fileName", "")),
                url=urljoin(base_url, attachment.get("fileURL", "")),
                description=normalize_whitespace(attachment.get("fileDescription", "")),
            )
        )

    brief_description = first_sentences(description) or build_fallback_description(
        title=title,
        category_classification=category_classification,
        attachments=attachments,
    )

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
        posting_date=safe_parse_date(payload.get("postingDate", "")) or fallback.posting_date,
        due_date=due_date,
        due_time=due_time,
        due_datetime=due_datetime,
        description=description,
        brief_description=brief_description,
        category_classification=category_classification,
        contact_name=normalize_whitespace(payload.get("contactName", "")),
        contact_email=normalize_whitespace(payload.get("contactEmail", "")),
        contact_phone=normalize_whitespace(payload.get("contactNumber", "")),
        bid_response_email=normalize_whitespace(payload.get("bidResponseEmail", "")),
        detail_url=fallback.detail_url,
        attachment_urls=attachments,
        addendum_text=addendum_text,
        raw_text_blob=normalize_whitespace(raw_blob),
    )
