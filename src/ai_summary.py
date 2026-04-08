from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

import requests
from rapidfuzz import fuzz

from .config import (
    AI_SUMMARY_CACHE_PATH,
    AI_SUMMARY_CONTEXT_CHARS_PER_RECORD,
    AI_SUMMARY_MAX_RETRIES,
    AI_SUMMARY_MIN_INTERVAL_SECONDS,
    AI_SUMMARY_TIMEOUT_SECONDS,
    ANTHROPIC_API_VERSION,
    ANTHROPIC_MESSAGES_API_URL,
    DEFAULT_CLAUDE_MODEL,
)
from .models import Solicitation
from .parse_esbd import normalize_whitespace


def clean_summary_text(text: str) -> str:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return ""

    cleaned = re.sub(r"^summary[:\-\s]*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^here is (the )?summary[:\-\s]*", "", cleaned, flags=re.I)
    cleaned = cleaned.strip("`\"' ")
    return normalize_whitespace(cleaned)


def sentence_split(text: str) -> list[str]:
    cleaned = clean_summary_text(text)
    if not cleaned:
        return []
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]


def complete_sentence(sentence: str) -> str:
    cleaned = normalize_whitespace(sentence).rstrip(" ,;:")
    if not cleaned:
        return ""
    if cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


def finalize_summary_text(text: str) -> str:
    sentences = [complete_sentence(sentence) for sentence in sentence_split(text)]
    if len(sentences) < 2:
        return ""
    final_sentences = sentences[:2]
    summary = " ".join(final_sentences)
    word_count = len(summary.split())
    if word_count < 20:
        return ""
    return summary


def summary_is_verbatim_source(summary: str, record: Solicitation) -> bool:
    summary_clean = clean_summary_text(summary)
    if not summary_clean:
        return True

    lowered_summary = summary_clean.lower()
    if len(lowered_summary) < 80:
        return False

    source_candidates = [
        record.brief_description,
        record.description,
        record.addendum_text,
        record.pdf_text_blob[:12000],
    ]
    for source in source_candidates:
        source_clean = normalize_whitespace(source)
        if not source_clean:
            continue
        lowered_source = source_clean.lower()
        if lowered_summary in lowered_source:
            return True
        if fuzz.ratio(lowered_summary, lowered_source) >= 98:
            return True

    return False


def compact_text(value: str, max_chars: int) -> str:
    cleaned = normalize_whitespace(value)
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def split_source_units(text: str) -> list[str]:
    if not text:
        return []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [normalize_whitespace(line) for line in normalized.splitlines()]
    units: list[str] = []
    for line in lines:
        if not line:
            continue
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", line) if part.strip()]
        if parts:
            units.extend(parts)
        else:
            units.append(line)
    return units


def is_low_signal_unit(text: str) -> bool:
    lowered = normalize_whitespace(text).lower()
    if not lowered:
        return True

    bad_prefixes = (
        "solicitation type and name:",
        "solicitation number:",
        "date issued:",
        "due date",
        "pre-bid",
        "proposal specifications are available",
        "please download the attached",
        "refer inquiries in writing",
        "join from the meeting link",
        "to view this solicitation",
        "access code:",
        "meeting password:",
        "dial-in number:",
        "contact:",
        "email:",
        "phone:",
    )
    bad_contains = (
        "http://",
        "https://",
        "table of contents",
        "insurance requirements",
        "evaluation criteria",
        "proposal format",
        "hub subcontracting",
        "terms and conditions",
        "ada accommodations",
        "please register for notification",
        "questions due",
        "responses are due",
    )

    if lowered.startswith(bad_prefixes):
        return True
    if any(fragment in lowered for fragment in bad_contains):
        return True
    if len(lowered.split()) < 7:
        return True
    return False


def clean_scope_unit(text: str) -> str:
    cleaned = normalize_whitespace(text)
    cleaned = re.sub(r"^(description|project description|summary of work|scope of work)[:\-\s]+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^(the work (?:under this contract )?consists of|the project generally consists of|work includes|services include)[:\-\s]+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^(this solicitation (?:is seeking|requests)|the city is seeking|the university is soliciting)[:\-\s]+", "", cleaned, flags=re.I)
    return normalize_whitespace(cleaned)


def select_scope_unit(record: Solicitation) -> str:
    preferred_sources = [
        record.description,
        record.addendum_text,
        record.pdf_text_blob,
        record.brief_description,
    ]
    for source in preferred_sources:
        for unit in split_source_units(source):
            if is_low_signal_unit(unit):
                continue
            cleaned = clean_scope_unit(unit)
            if len(cleaned.split()) >= 7:
                return cleaned
    return ""


def build_attachment_section(record: Solicitation, max_chars: int) -> str:
    lines: list[str] = []
    current_length = 0
    for attachment in record.attachment_urls:
        parts = [attachment.name]
        if attachment.description:
            parts.append(attachment.description)
        if attachment.pdf_extraction_status:
            parts.append(f"PDF status: {attachment.pdf_extraction_status}")
        line = normalize_whitespace(" | ".join(part for part in parts if part))
        if not line:
            continue
        projected = current_length + len(line) + 1
        if projected > max_chars:
            break
        lines.append(line)
        current_length = projected
    return "\n".join(lines)


def build_pdf_section(record: Solicitation, max_chars: int) -> str:
    if not record.pdf_text_blob:
        return ""

    cleaned = record.pdf_text_blob.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [
        normalize_whitespace(paragraph)
        for paragraph in re.split(r"\n{2,}", cleaned)
        if normalize_whitespace(paragraph)
    ]
    if not paragraphs:
        paragraphs = [normalize_whitespace(cleaned)]

    selected: list[str] = []
    current_length = 0
    for paragraph in paragraphs:
        paragraph = compact_text(paragraph, 1600)
        projected = current_length + len(paragraph) + 2
        if projected > max_chars:
            break
        selected.append(paragraph)
        current_length = projected

    if not selected:
        return compact_text(normalize_whitespace(cleaned), max_chars)
    return "\n\n".join(selected)


def build_solicitation_context(record: Solicitation, max_chars: int) -> str:
    sections = [
        f"Solicitation ID: {record.solicitation_id}",
        f"Title: {record.title}",
        f"Agency: {record.agency_name or record.agency_number}",
        f"Status: {record.status}",
        f"Classification: {record.category_classification}",
        f"Posted: {record.posting_date}",
        f"Due: {record.due_date} {record.due_time}".strip(),
    ]

    if record.contact_name or record.contact_email or record.contact_phone:
        sections.append(
            normalize_whitespace(
                "Contact: "
                + " | ".join(
                    value
                    for value in [
                        record.contact_name,
                        record.contact_email,
                        record.contact_phone,
                    ]
                    if value
                )
            )
        )

    if record.brief_description:
        sections.append(f"Listing Brief Description:\n{record.brief_description}")
    if record.description:
        sections.append(f"Solicitation Description:\n{record.description}")
    if record.addendum_text:
        sections.append(f"Addendum:\n{record.addendum_text}")

    attachment_section = build_attachment_section(record, max_chars=4000)
    if attachment_section:
        sections.append(f"Attachments:\n{attachment_section}")

    pdf_section = build_pdf_section(record, max_chars=max_chars)
    if pdf_section:
        sections.append(f"Extracted PDF Text:\n{pdf_section}")

    context = "\n\n".join(section for section in sections if normalize_whitespace(section))
    if len(context) <= max_chars:
        return context

    trimmed_sections: list[str] = []
    current_length = 0
    for section in sections:
        if not normalize_whitespace(section):
            continue
        remaining = max_chars - current_length
        if remaining <= 0:
            break
        trimmed = compact_text(section, remaining)
        if not trimmed:
            break
        trimmed_sections.append(trimmed)
        current_length += len(trimmed) + 2

    return "\n\n".join(trimmed_sections)


def build_fallback_summary(record: Solicitation) -> str:
    agency = record.agency_name or record.agency_number or "the issuing agency"
    sentence_one = f"{record.title} is an open solicitation from {agency}."
    scope_unit = select_scope_unit(record)
    if scope_unit:
        scope_text = compact_text(scope_unit, 220).rstrip(".")
        sentence_two = f"The requested work includes {scope_text}."
        return f"{sentence_one} {sentence_two}"

    if record.category_classification:
        return (
            f"{sentence_one} "
            f"It is listed under {record.category_classification}."
        )

    return (
        f"{sentence_one} "
        "The available solicitation materials did not contain enough clear scope text for a stronger summary."
    )


def extract_response_text(payload: dict) -> str:
    parts: list[str] = []
    for item in payload.get("content", []) or []:
        text = normalize_whitespace(item.get("text", ""))
        if text:
            parts.append(text)
    return normalize_whitespace(" ".join(parts))


def extract_retry_delay_seconds(response: requests.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "").strip()
    if retry_after.isdigit():
        return max(float(retry_after), AI_SUMMARY_MIN_INTERVAL_SECONDS)

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    message = normalize_whitespace(str(payload))
    match = re.search(r"retry.?after[^0-9]*([0-9]+)", message, flags=re.I)
    if match:
        return max(float(match.group(1)), AI_SUMMARY_MIN_INTERVAL_SECONDS)

    return AI_SUMMARY_MIN_INTERVAL_SECONDS * (attempt + 2)


def build_summary_prompt(record: Solicitation, context: str) -> str:
    return (
        "Write a strong two-sentence summary of this government solicitation.\n\n"
        "Requirements:\n"
        "- Write exactly 2 complete sentences.\n"
        "- Summarize the actual work being requested, the project or facility context, and the main deliverables or services.\n"
        "- Use the solicitation description, addendum, attachment information, and extracted PDF text when available.\n"
        "- Prefer the most concrete scope details over administrative or legal boilerplate.\n"
        "- Paraphrase the content instead of copying sentences from the source.\n"
        "- Do not simply restate the title or reuse the first sentence of the solicitation description.\n"
        "- Do not mention that the summary came from PDF text or extracted documents.\n"
        "- Do not use bullets, labels, markdown, JSON, or quotation marks.\n\n"
        f"{context}"
    )


class ClaudeSummarizer:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        context_chars_per_record: int | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.model = (model or os.getenv("CLAUDE_MODEL", "")).strip() or DEFAULT_CLAUDE_MODEL
        self.timeout_seconds = AI_SUMMARY_TIMEOUT_SECONDS
        self.min_interval_seconds = AI_SUMMARY_MIN_INTERVAL_SECONDS
        self.max_retries = AI_SUMMARY_MAX_RETRIES
        self.context_chars_per_record = (
            context_chars_per_record or AI_SUMMARY_CONTEXT_CHARS_PER_RECORD
        )
        self._last_request_at = 0.0
        self.cache_path = Path(AI_SUMMARY_CACHE_PATH)
        self.cache = self._load_cache()
        self._cache_dirty = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _load_cache(self) -> dict[str, dict[str, str]]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): value for key, value in data.items() if isinstance(value, dict)}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def _cache_key(self, record: Solicitation) -> str:
        cache_version = "v7"
        source_text = "\n".join(
            [
                record.title,
                record.status,
                record.agency_name,
                record.category_classification,
                record.description,
                record.addendum_text,
                record.pdf_text_blob,
                " ".join(
                    normalize_whitespace(
                        f"{attachment.name} {attachment.description} {attachment.pdf_extraction_status}"
                    )
                    for attachment in record.attachment_urls
                ),
            ]
        )
        digest = hashlib.sha256(normalize_whitespace(source_text).encode("utf-8")).hexdigest()[:16]
        return f"{cache_version}:{self.model}:{record.solicitation_id}:{digest}"

    def _get_cached_summary(self, record: Solicitation) -> str:
        item = self.cache.get(self._cache_key(record), {})
        summary = normalize_whitespace(str(item.get("summary", "")))
        finalized = finalize_summary_text(summary)
        if not finalized or summary_is_verbatim_source(finalized, record):
            return ""
        return finalized

    def _store_cached_summary(self, record: Solicitation, summary: str) -> None:
        self.cache[self._cache_key(record)] = {
            "summary": normalize_whitespace(summary),
            "model": self.model,
        }
        self._cache_dirty = True

    def _wait_for_rate_limit_window(self) -> None:
        if not self._last_request_at:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)

    def request_summary(self, record: Solicitation, strict: bool = False) -> str:
        last_error: requests.RequestException | None = None
        context = build_solicitation_context(
            record,
            max_chars=self.context_chars_per_record,
        )
        prompt = build_summary_prompt(record, context)
        if strict:
            prompt += (
                "\n\nImportant: the summary must be fully complete, paraphrased, and end cleanly "
                "with exactly two finished sentences."
            )

        for attempt in range(self.max_retries):
            self._wait_for_rate_limit_window()
            response = requests.post(
                ANTHROPIC_MESSAGES_API_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": ANTHROPIC_API_VERSION,
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 300,
                    "temperature": 0.1,
                    "system": (
                        "You are a procurement analyst writing concise, accurate solicitation summaries. "
                        "Your summary must be faithful to the solicitation and must paraphrase the source "
                        "instead of echoing or copying it."
                    ),
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                        }
                    ],
                },
                timeout=self.timeout_seconds,
            )
            self._last_request_at = time.monotonic()

            if response.status_code == 429:
                last_error = requests.HTTPError(
                    f"429 Client Error: Too Many Requests for url: {response.url}",
                    response=response,
                )
                time.sleep(extract_retry_delay_seconds(response, attempt))
                continue

            response.raise_for_status()
            return extract_response_text(response.json())

        if last_error:
            raise last_error
        raise requests.RequestException("Claude summary request failed without a response")

    def summarize_solicitations(self, records: list[Solicitation]) -> list[Solicitation]:
        updated_records = list(records)

        for record in updated_records:
            if not self.enabled:
                record.ai_summary_error = "ANTHROPIC_API_KEY not provided"
                record.ai_summary = ""
                continue

            cached_summary = self._get_cached_summary(record)
            if cached_summary:
                record.ai_summary = cached_summary
                record.ai_summary_model = self.model
                record.ai_summary_error = ""
                record.ai_summary_source = "cache"
                continue

            try:
                summary = self.request_summary(record, strict=False)
                finalized = finalize_summary_text(summary)
                if not finalized or summary_is_verbatim_source(finalized, record):
                    summary = self.request_summary(record, strict=True)
                    finalized = finalize_summary_text(summary)

                if finalized:
                    record.ai_summary = finalized
                    record.ai_summary_model = self.model
                    record.ai_summary_error = ""
                    record.ai_summary_source = "claude"
                    self._store_cached_summary(record, finalized)
                else:
                    record.ai_summary = build_fallback_summary(record)
                    record.ai_summary_error = (
                        "Model returned a weak or copy-like summary; fallback summary used"
                    )
                    record.ai_summary_source = "fallback"
            except requests.RequestException as exc:
                record.ai_summary = build_fallback_summary(record)
                record.ai_summary_error = (
                    f"AI summary request failed: {exc}; fallback summary used"
                )
                record.ai_summary_source = "fallback"

        if self._cache_dirty:
            self._save_cache()
            self._cache_dirty = False

        return updated_records
