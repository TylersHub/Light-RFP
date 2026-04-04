from __future__ import annotations

import json
import re
from pathlib import Path
import time
from urllib.parse import urlparse

import requests
from pypdf import PdfReader

from .config import PDF_CACHE_TTL_SECONDS
from .models import Attachment, Solicitation
from .parse_esbd import normalize_whitespace


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


def pdf_cache_path(file_path: Path) -> Path:
    return file_path.with_suffix(f"{file_path.suffix}.json")


def load_cached_pdf_extraction(file_path: Path) -> tuple[str, str, bool, int] | None:
    cache_path = pdf_cache_path(file_path)
    if not cache_path.exists():
        return None

    try:
        file_stat = file_path.stat()
        cache_age_seconds = time.time() - cache_path.stat().st_mtime
        if cache_age_seconds > PDF_CACHE_TTL_SECONDS:
            return None
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("size") != file_stat.st_size:
        return None

    return (
        normalize_whitespace(str(payload.get("text", ""))),
        normalize_whitespace(str(payload.get("status", ""))),
        bool(payload.get("is_scanned", False)),
        int(payload.get("page_count", 0) or 0),
    )


def save_cached_pdf_extraction(
    file_path: Path,
    text: str,
    status: str,
    is_scanned: bool,
    page_count: int,
) -> None:
    cache_path = pdf_cache_path(file_path)
    try:
        cache_payload = {
            "size": file_path.stat().st_size,
            "status": status,
            "is_scanned": is_scanned,
            "page_count": page_count,
            "text": text,
        }
        cache_path.write_text(
            json.dumps(cache_payload, ensure_ascii=True),
            encoding="utf-8",
        )
    except OSError:
        return


def download_attachment(
    session: requests.Session,
    attachment: Attachment,
    solicitation_id: str,
    base_dir: Path,
    timeout_seconds: int,
    use_cache: bool = True,
) -> Path:
    target_path = build_attachment_path(base_dir, solicitation_id, attachment)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if use_cache and target_path.exists() and target_path.stat().st_size > 0:
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
    use_cache: bool = True,
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
            use_cache=use_cache,
        )
        updated.local_path = str(target_path)
        cached_extraction = load_cached_pdf_extraction(target_path) if use_cache else None
        if cached_extraction is not None:
            pdf_text, status, is_scanned, page_count = cached_extraction
        else:
            pdf_text, status, is_scanned, page_count = extract_pdf_text(target_path)
            save_cached_pdf_extraction(
                file_path=target_path,
                text=pdf_text,
                status=status,
                is_scanned=is_scanned,
                page_count=page_count,
            )
        updated.pdf_extraction_status = status
        updated.pdf_is_scanned = is_scanned
        updated.pdf_page_count = page_count
        updated.pdf_text_length = len(pdf_text)
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


def download_and_extract_solicitation_pdfs(
    session: requests.Session,
    record: Solicitation,
    base_dir: Path,
    timeout_seconds: int,
    use_cache: bool = True,
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
            use_cache=use_cache,
        )
        updated_attachments.append(updated_attachment)
        if pdf_text:
            extracted_texts.append(pdf_text)

    record.attachment_urls = updated_attachments
    record.pdf_text_blob = normalize_whitespace(" ".join(extracted_texts))
    record.pdf_extraction_summary = summarize_pdf_extraction(record)

    if record.pdf_text_blob:
        record.raw_text_blob = normalize_whitespace(
            f"{record.raw_text_blob} {record.pdf_text_blob}"
        )

    return record
