from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from pypdf import PdfReader

from .models import Attachment, Solicitation
from .parse_esbd import first_sentences, normalize_whitespace


def is_pdf_attachment(attachment: Attachment) -> bool:
    name = attachment.name.lower()
    url_path = urlparse(attachment.url).path.lower()
    return name.endswith(".pdf") or url_path.endswith(".pdf")


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._") or "attachment"


def build_attachment_path(
    base_dir: Path,
    solicitation_id: str,
    attachment: Attachment,
) -> Path:
    solicitation_dir = base_dir / safe_filename(solicitation_id or "unknown_solicitation")
    filename = safe_filename(attachment.name or "attachment")
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return solicitation_dir / filename


def build_pdf_preview(text: str, max_words: int = 45) -> str:
    preview = first_sentences(text, limit=2)
    if not preview:
        return ""
    words = preview.split()
    if len(words) <= max_words:
        return preview
    return " ".join(words[:max_words]).rstrip(",;:") + "..."


def download_attachment(
    session: requests.Session,
    attachment: Attachment,
    solicitation_id: str,
    base_dir: Path,
    timeout_seconds: int,
) -> Path:
    target_path = build_attachment_path(base_dir, solicitation_id, attachment)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists() and target_path.stat().st_size > 0:
        return target_path

    response = session.get(attachment.url, timeout=timeout_seconds)
    response.raise_for_status()
    target_path.write_bytes(response.content)
    return target_path


def classify_pdf_text(full_text: str, page_count: int) -> tuple[str, bool]:
    if not full_text:
        return "No extractable text found", True

    average_chars_per_page = len(full_text) / max(page_count, 1)
    if len(full_text) < 120 or average_chars_per_page < 80:
        return "Likely scanned or image-based PDF", True

    return "Text extracted", False


def extract_pdf_text(file_path: Path) -> tuple[str, str, bool, int]:
    reader = PdfReader(str(file_path))
    extracted_parts: list[str] = []

    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        page_text = normalize_whitespace(page_text)
        if page_text:
            extracted_parts.append(page_text)

    full_text = normalize_whitespace(" ".join(extracted_parts))
    page_count = len(reader.pages)
    status, is_scanned = classify_pdf_text(full_text, page_count)
    return full_text, status, is_scanned, page_count


def extract_attachment_pdf(
    session: requests.Session,
    attachment: Attachment,
    solicitation_id: str,
    base_dir: Path,
    timeout_seconds: int,
) -> tuple[Attachment, str]:
    updated = Attachment(
        name=attachment.name,
        url=attachment.url,
        description=attachment.description,
        is_pdf=is_pdf_attachment(attachment),
    )

    if not updated.is_pdf:
        updated.pdf_extraction_status = "Not a PDF"
        return updated, ""

    try:
        target_path = download_attachment(
            session=session,
            attachment=updated,
            solicitation_id=solicitation_id,
            base_dir=base_dir,
            timeout_seconds=timeout_seconds,
        )
        updated.local_path = str(target_path)
        pdf_text, status, is_scanned, page_count = extract_pdf_text(target_path)
        updated.pdf_extraction_status = status
        updated.pdf_is_scanned = is_scanned
        updated.pdf_page_count = page_count
        updated.pdf_text_length = len(pdf_text)
        updated.pdf_text_preview = build_pdf_preview(pdf_text)
        updated.pdf_text = pdf_text
        return updated, pdf_text
    except requests.RequestException:
        updated.pdf_extraction_status = "Download failed"
        updated.pdf_is_scanned = False
        return updated, ""
    except Exception:
        updated.pdf_extraction_status = "PDF parsing failed"
        updated.pdf_is_scanned = False
        return updated, ""


def summarize_pdf_extraction(record: Solicitation) -> str:
    pdf_attachments = [attachment for attachment in record.attachment_urls if attachment.is_pdf]
    if not pdf_attachments:
        return "No PDF attachments found."

    text_based_count = sum(
        1 for attachment in pdf_attachments if attachment.pdf_extraction_status == "Text extracted"
    )
    scanned_count = sum(1 for attachment in pdf_attachments if attachment.pdf_is_scanned)
    failed_count = sum(
        1
        for attachment in pdf_attachments
        if attachment.pdf_extraction_status in {"Download failed", "PDF parsing failed"}
    )

    parts = [f"{len(pdf_attachments)} PDF attachment(s) found"]
    if text_based_count:
        parts.append(f"{text_based_count} text-based")
    if scanned_count:
        parts.append(f"{scanned_count} likely scanned")
    if failed_count:
        parts.append(f"{failed_count} failed")
    return ". ".join(parts) + "."


def enrich_solicitation_pdfs(
    session: requests.Session,
    record: Solicitation,
    base_dir: Path,
    timeout_seconds: int,
) -> Solicitation:
    if not record.attachment_urls:
        record.pdf_extraction_summary = "No attachments to inspect."
        return record

    updated_attachments: list[Attachment] = []
    extracted_texts: list[str] = []

    for attachment in record.attachment_urls:
        updated_attachment, pdf_text = extract_attachment_pdf(
            session=session,
            attachment=attachment,
            solicitation_id=record.solicitation_id,
            base_dir=base_dir,
            timeout_seconds=timeout_seconds,
        )
        updated_attachments.append(updated_attachment)
        if pdf_text:
            extracted_texts.append(pdf_text)

    record.attachment_urls = updated_attachments
    record.pdf_text_blob = normalize_whitespace(" ".join(extracted_texts))
    record.pdf_text_preview = build_pdf_preview(record.pdf_text_blob)
    record.pdf_extraction_summary = summarize_pdf_extraction(record)

    if record.pdf_text_blob:
        record.raw_text_blob = normalize_whitespace(
            f"{record.raw_text_blob} {record.pdf_text_blob}"
        )

    return record
