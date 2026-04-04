from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

import requests

from .config import (
    AI_SUMMARY_BATCH_SIZE,
    AI_SUMMARY_BATCH_CHAR_BUDGET,
    AI_SUMMARY_CACHE_PATH,
    AI_SUMMARY_CONTEXT_CHARS_PER_RECORD,
    AI_SUMMARY_MAX_RETRIES,
    AI_SUMMARY_MIN_INTERVAL_SECONDS,
    AI_SUMMARY_TIMEOUT_SECONDS,
    DEFAULT_GEMINI_MODEL,
    GEMINI_API_BASE_URL,
)
from .models import Solicitation
from .parse_esbd import first_sentences, normalize_whitespace

HIGH_SIGNAL_PHRASES = {
    "scope of work",
    "summary of work",
    "project description",
    "statement of work",
    "services include",
    "work includes",
    "contractor shall",
    "project consists",
    "shall furnish",
    "shall provide",
    "installation",
    "replacement",
    "renovation",
    "repair",
    "maintenance",
    "construction",
    "abatement",
    "roofing",
    "hvac",
    "electrical",
    "plumbing",
    "asphalt",
    "landscaping",
    "janitorial",
    "wastewater",
}

LOW_SIGNAL_PHRASES = {
    "table of contents",
    "instructions to proposers",
    "terms and conditions",
    "hub subcontracting plan",
    "evaluation criteria",
    "proposal format",
    "insurance requirements",
    "submission of proposals",
}

HIGH_SIGNAL_ATTACHMENT_TERMS = {
    "statement of work",
    "scope of work",
    "solicitation",
    "invitation",
    "request for proposal",
    "request for qualifications",
    "request for bid",
    "advertisement",
    "bid package",
    "specification",
    "specifications",
    "project manual",
    "drawings",
    "plans",
    "addendum",
}

LOW_SIGNAL_ATTACHMENT_TERMS = {
    "terms and conditions",
    "checklist",
    "application",
    "w-9",
    "w9",
    "hub",
    "insurance",
    "direct deposit",
    "certification",
    "sample contract",
    "standard terms",
    "questionnaire",
    "vendor information",
}


def title_keywords(title: str) -> list[str]:
    stop_words = {
        "request",
        "proposal",
        "proposals",
        "invitation",
        "bids",
        "services",
        "service",
        "project",
        "contract",
        "contracts",
        "solicitation",
        "formal",
        "competitive",
        "sealed",
    }
    words = re.findall(r"[a-z]{4,}", title.lower())
    return [word for word in words if word not in stop_words][:8]


def split_into_chunks(
    text: str,
    chunk_chars: int = 1600,
    overlap_chars: int = 180,
) -> list[str]:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return []

    chunks: list[str] = []
    start = 0
    text_length = len(cleaned)

    while start < text_length:
        target_end = min(text_length, start + chunk_chars)
        end = target_end

        if target_end < text_length:
            boundary = cleaned.rfind(" ", start + chunk_chars // 2, target_end)
            if boundary != -1:
                end = boundary

        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= text_length:
            break

        next_start = max(start + 1, end - overlap_chars)
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks


def attachment_signal_score(name: str, description: str) -> int:
    lowered = normalize_whitespace(f"{name} {description}").lower()
    score = 0
    for term in HIGH_SIGNAL_ATTACHMENT_TERMS:
        if term in lowered:
            score += 8
    for term in LOW_SIGNAL_ATTACHMENT_TERMS:
        if term in lowered:
            score -= 10
    if lowered.endswith(".pdf"):
        score += 1
    return score


def score_chunk(chunk: str, title_terms: list[str]) -> int:
    lowered = chunk.lower()
    score = 0

    for phrase in HIGH_SIGNAL_PHRASES:
        if phrase in lowered:
            score += 6

    for phrase in LOW_SIGNAL_PHRASES:
        if phrase in lowered:
            score -= 12

    if "table of contents" in lowered:
        score -= 14
    if "cover page" in lowered:
        score -= 8
    if "exported on" in lowered:
        score -= 6

    for term in title_terms:
        if term in lowered:
            score += 2

    if re.search(r"\b(scope|summary|description|services|work|project)\b", lowered):
        score += 3
    if len(chunk.split()) >= 80:
        score += 1

    return score


def build_fallback_blob_context(record: Solicitation, max_chars: int) -> str:
    full_text = normalize_whitespace(record.pdf_text_blob)
    if not full_text:
        return ""

    chunks = split_into_chunks(full_text)
    if not chunks:
        return full_text[:max_chars]

    terms = title_keywords(record.title)
    chunk_scores = {index: score_chunk(chunks[index], terms) for index in range(len(chunks))}
    ranked_indices = sorted(
        range(len(chunks)),
        key=lambda index: (chunk_scores[index], -index),
        reverse=True,
    )

    selected_indices: list[int] = []
    seen: set[int] = set()

    def add_index(index: int) -> None:
        if 0 <= index < len(chunks) and index not in seen:
            seen.add(index)
            selected_indices.append(index)

    total_chunks = len(chunks)
    bands = [
        ("Early document excerpt", 0, max(1, total_chunks // 3)),
        ("Middle document excerpt", total_chunks // 3, max(total_chunks // 3 + 1, 2 * total_chunks // 3)),
        ("Late document excerpt", 2 * total_chunks // 3, total_chunks),
    ]

    labeled_sections: list[tuple[int, str]] = []
    for label, start, end in bands:
        band_indices = list(range(start, min(end, total_chunks)))
        if not band_indices:
            continue
        best_index = max(band_indices, key=lambda index: (chunk_scores[index], -index))
        if chunk_scores[best_index] <= 0:
            continue
        add_index(best_index)
        labeled_sections.append((best_index, label))

    for index in ranked_indices:
        if chunk_scores[index] <= 0:
            continue
        add_index(index)
        if len(selected_indices) >= 6:
            break

    if not selected_indices:
        fallback_index = max(range(len(chunks)), key=lambda index: (chunk_scores[index], -index))
        add_index(fallback_index)
        labeled_sections.append((fallback_index, "Representative document excerpt"))

    label_map = {index: label for index, label in labeled_sections}
    sections: list[str] = []
    current_length = 0
    high_signal_ordinal = 1
    for index in sorted(selected_indices):
        label = label_map.get(index)
        if not label:
            label = f"Additional high-signal excerpt {high_signal_ordinal}"
            high_signal_ordinal += 1
        section = f"{label}:\n{chunks[index]}"
        projected = current_length + len(section) + 2
        if projected > max_chars:
            continue
        sections.append(section)
        current_length = projected

    context = "\n\n".join(sections)
    return context[:max_chars]


def build_document_context(record: Solicitation, max_chars: int) -> str:
    terms = title_keywords(record.title)
    sections: list[str] = []
    current_length = 0

    attachments = [
        attachment
        for attachment in record.attachment_urls
        if attachment.is_pdf and attachment.pdf_text
    ]
    ranked_attachments = sorted(
        attachments,
        key=lambda attachment: (
            attachment_signal_score(attachment.name, attachment.description),
            attachment.pdf_text_length,
        ),
        reverse=True,
    )

    for attachment in ranked_attachments[:4]:
        attachment_header = normalize_whitespace(
            f"Attachment: {attachment.name}. {attachment.description}"
        ).strip()
        if not attachment_header:
            attachment_header = f"Attachment: {attachment.name}"

        chunks = split_into_chunks(attachment.pdf_text, chunk_chars=1300, overlap_chars=140)
        if not chunks:
            continue

        attachment_base_score = attachment_signal_score(attachment.name, attachment.description)
        ranked_chunks = sorted(
            chunks,
            key=lambda chunk: (
                score_chunk(chunk, terms) + attachment_base_score,
                len(chunk),
            ),
            reverse=True,
        )

        selected_chunks: list[str] = []
        for chunk in ranked_chunks:
            if score_chunk(chunk, terms) + attachment_base_score <= 0:
                continue
            selected_chunks.append(chunk)
            if len(selected_chunks) >= 2:
                break

        if not selected_chunks:
            selected_chunks.append(chunks[0])

        section = f"{attachment_header}\n" + "\n".join(selected_chunks)
        projected = current_length + len(section) + 2
        if projected > max_chars:
            continue
        sections.append(section)
        current_length = projected

        if len(sections) >= 4:
            break

    if sections:
        return "\n\n".join(sections)[:max_chars]

    return build_fallback_blob_context(record, max_chars)


def extract_response_text(payload: dict) -> str:
    parts: list[str] = []
    for candidate in payload.get("candidates", []) or []:
        content = candidate.get("content", {})
        if not isinstance(content, dict):
            continue
        for part in content.get("parts", []) or []:
            text = normalize_whitespace(part.get("text", ""))
            if text:
                parts.append(text)
    return normalize_whitespace(" ".join(parts))


def clean_summary_text(text: str) -> str:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return ""

    cleaned = re.sub(
        r"^(here is (the )?(json|summary)( requested)?[:\-]?\s*)",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"^(summary[:\-]?\s*)", "", cleaned, flags=re.I)
    cleaned = cleaned.strip("`\"' ")
    return normalize_whitespace(cleaned)


def shorten_words(text: str, max_words: int) -> str:
    words = normalize_whitespace(text).split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(",;:") + "..."


def summary_needs_retry(summary: str) -> bool:
    cleaned = clean_summary_text(summary)
    if not cleaned:
        return True

    word_count = len(cleaned.split())
    sentence_count = len(re.findall(r"[.!?](?:\s|$)", cleaned))
    has_terminal_punctuation = bool(re.search(r"[.!?]$", cleaned))

    return word_count < 18 or sentence_count < 2 or not has_terminal_punctuation


def extract_scope_phrase(text: str) -> str:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return ""

    cleaned = re.sub(
        r"\b(?:Early|Middle|Late|Additional high-signal|Representative document) excerpt:\s*",
        "",
        cleaned,
        flags=re.I,
    )

    patterns = [
        r"(?:project description|summary of work|scope of work|statement of work|event summary|description|work includes|services include)[:\s-]+(.+?)(?:[.!?]|$)",
        r"(?:contractor shall|shall furnish|shall provide)[:\s-]+(.+?)(?:[.!?]|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.I)
        if match:
            candidate = shorten_words(match.group(1), 32)
            if candidate:
                return candidate

    return shorten_words(first_sentences(cleaned, limit=1), 32)


def scope_phrase_is_usable(phrase: str) -> bool:
    cleaned = normalize_whitespace(phrase).strip(". ")
    if len(cleaned.split()) < 6:
        return False

    lowered = cleaned.lower()
    bad_starts = (
        "of ",
        "the ",
        "whether ",
        "organization ",
        "currency ",
        "event ",
        "type ",
        "table ",
        "request for qualifications solicitation",
    )
    bad_contains = (
        "table of contents",
        "organization uh",
        "currency us dollar",
        "whether or not your firm served",
    )

    if lowered.startswith(bad_starts):
        return False
    if any(fragment in lowered for fragment in bad_contains):
        return False
    return True


def build_fallback_summary(record: Solicitation) -> str:
    context = build_document_context(record, max_chars=3500)
    scope_phrase = extract_scope_phrase(record.pdf_text_blob) or extract_scope_phrase(context)
    agency = record.agency_name or record.agency_number or "the issuing agency"
    if record.brief_description and len(record.brief_description.split()) >= 8:
        first_sentence = record.brief_description.rstrip(". ") + "."
    else:
        first_sentence = f"{record.title} is an open solicitation from {agency}."

    if scope_phrase_is_usable(scope_phrase):
        second_sentence = f"PDF excerpts indicate the scope includes {scope_phrase.rstrip('.')}."
    elif record.category_classification:
        second_sentence = (
            "The attached bid documents add scope and submission detail for work aligned with "
            f"{record.category_classification.rstrip('. ')}."
        )
    else:
        second_sentence = "The attached PDF package provides additional scope and bid-package detail beyond the ESBD listing."

    return f"{first_sentence} {second_sentence}"


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


def build_batch_summary_prompt(
    records: list[Solicitation],
    context_map: dict[str, str],
    strict: bool = False,
) -> str:
    sections: list[str] = []
    for record in records:
        sections.append(
            "\n".join(
                [
                    f"Solicitation ID: {record.solicitation_id}",
                    f"Title: {record.title}",
                    f"Agency: {record.agency_name or record.agency_number}",
                    f"Classification: {record.category_classification}",
                    f"ESBD Brief Description: {record.brief_description}",
                    "PDF Excerpts:",
                    context_map.get(record.solicitation_id, ""),
                ]
            )
        )

    strict_suffix = ""
    if strict:
        strict_suffix = (
            "\n\nImportant: some summaries were previously missing or too short. "
            "Every returned summary must be exactly 2 complete sentences with no fragmentary wording."
        )

    return (
        "Summarize each government RFP below using the PDF excerpts and listing metadata. "
        "For each solicitation, return exactly one line in this format:\n"
        "SOLICITATION_ID|||2-sentence summary\n\n"
        "Example:\n"
        "ABC-123|||Sentence one about the work. Sentence two about the project context.\n"
        "XYZ-999|||Sentence one about the requested services. Sentence two about the facility or deliverables.\n\n"
        "Rules:\n"
        "- Return only lines in that exact delimiter format.\n"
        "- No bullets, headings, markdown, JSON, or explanatory text.\n"
        "- Each summary must be exactly 2 complete sentences and about 45 to 95 words total.\n"
        "- Focus on the actual scope of work, project or facility context, and major services or deliverables.\n"
        "- Do not mention that the summary came from extracted PDF text."
        f"{strict_suffix}\n\n"
        + "\n\n===\n\n".join(sections)
    )


def build_summary_batches(
    records: list[Solicitation],
    max_chars_per_record: int,
    batch_char_budget: int,
    batch_size: int,
) -> list[tuple[list[Solicitation], dict[str, str]]]:
    prepared: list[tuple[Solicitation, str]] = []
    for record in records:
        context = build_document_context(record, max_chars=max_chars_per_record)
        prepared.append((record, context))

    batches: list[tuple[list[Solicitation], dict[str, str]]] = []
    current_records: list[Solicitation] = []
    current_context_map: dict[str, str] = {}
    current_chars = 0

    for record, context in prepared:
        estimated_chars = len(context) + len(record.title) + len(record.brief_description) + 400
        would_exceed_budget = current_records and current_chars + estimated_chars > batch_char_budget
        would_exceed_size = current_records and len(current_records) >= batch_size
        if would_exceed_budget or would_exceed_size:
            batches.append((current_records, current_context_map))
            current_records = []
            current_context_map = {}
            current_chars = 0

        current_records.append(record)
        current_context_map[record.solicitation_id] = context
        current_chars += estimated_chars

    if current_records:
        batches.append((current_records, current_context_map))

    return batches


def parse_batch_summaries(text: str, expected_ids: set[str]) -> dict[str, str]:
    cleaned = text.replace("```", "")
    results: dict[str, str] = {}
    current_id = ""
    current_parts: list[str] = []

    def flush_current() -> None:
        if not current_id:
            return
        summary = clean_summary_text(" ".join(current_parts))
        if current_id in expected_ids and summary:
            results[current_id] = first_sentences(summary, limit=2)

    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|||" in line:
            flush_current()
            left, right = line.split("|||", 1)
            possible_id = normalize_whitespace(left)
            current_id = possible_id if possible_id in expected_ids else ""
            current_parts = [right.strip()] if current_id else []
            continue
        if current_id:
            current_parts.append(line)

    flush_current()
    return results


class GeminiSummarizer:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        context_chars_per_record: int | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
        self.model = (model or os.getenv("GEMINI_MODEL", "")).strip() or DEFAULT_GEMINI_MODEL
        self.timeout_seconds = AI_SUMMARY_TIMEOUT_SECONDS
        self.min_interval_seconds = AI_SUMMARY_MIN_INTERVAL_SECONDS
        self.max_retries = AI_SUMMARY_MAX_RETRIES
        self.batch_size = AI_SUMMARY_BATCH_SIZE
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
        digest = hashlib.sha256(
            normalize_whitespace(record.pdf_text_blob).encode("utf-8")
        ).hexdigest()[:16]
        return f"{self.model}:{record.solicitation_id}:{digest}"

    def _get_cached_summary(self, record: Solicitation) -> str:
        item = self.cache.get(self._cache_key(record), {})
        return normalize_whitespace(str(item.get("summary", "")))

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

    def request_batch_summaries(
        self,
        records: list[Solicitation],
        context_map: dict[str, str],
        strict: bool = False,
    ) -> dict[str, str]:
        last_error: requests.RequestException | None = None
        expected_ids = {record.solicitation_id for record in records}

        for attempt in range(self.max_retries):
            self._wait_for_rate_limit_window()
            response = requests.post(
                f"{GEMINI_API_BASE_URL}/{self.model}:generateContent",
                headers={
                    "x-goog-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "systemInstruction": {
                        "parts": [
                            {
                                "text": (
                                    "You are a procurement analyst helping a vendor marketplace understand public RFPs. "
                                    "Write specific, complete, evidence-based summaries of the requested work. "
                                    "Return only delimiter-formatted result lines in the format "
                                    "SOLICITATION_ID|||summary. Do not return fragments, bullets, headings, "
                                    "markdown, JSON, or any explanatory text."
                                )
                            }
                        ]
                    },
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": build_batch_summary_prompt(
                                        records=records,
                                        context_map=context_map,
                                        strict=strict,
                                    )
                                }
                            ],
                        }
                    ],
                    "generationConfig": {
                        "maxOutputTokens": min(4096, max(768, 220 * len(records))),
                        "temperature": 0.0,
                    },
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
            return parse_batch_summaries(
                extract_response_text(response.json()),
                expected_ids=expected_ids,
            )

        if last_error:
            raise last_error
        raise requests.RequestException("Gemini summary request failed without a response")

    def summarize_solicitations(self, records: list[Solicitation]) -> list[Solicitation]:
        updated_records = list(records)
        pending: list[Solicitation] = []

        for record in updated_records:
            if not self.enabled:
                record.ai_summary_error = "GEMINI_API_KEY not provided"
                continue
            if not record.pdf_text_blob:
                record.ai_summary_error = "No extracted PDF text available"
                continue

            cached_summary = self._get_cached_summary(record)
            if cached_summary:
                record.ai_summary = cached_summary
                record.ai_summary_model = self.model
                record.ai_summary_error = ""
                record.ai_summary_source = "cache"
                continue

            pending.append(record)

        batches = build_summary_batches(
            records=pending,
            max_chars_per_record=self.context_chars_per_record,
            batch_char_budget=AI_SUMMARY_BATCH_CHAR_BUDGET,
            batch_size=self.batch_size,
        )

        for batch, context_map in batches:
            try:
                summaries = self.request_batch_summaries(
                    batch,
                    context_map=context_map,
                    strict=False,
                )
                missing = [
                    record
                    for record in batch
                    if summary_needs_retry(summaries.get(record.solicitation_id, ""))
                ]

                if missing:
                    retry_context_map = {
                        record.solicitation_id: context_map.get(record.solicitation_id, "")
                        for record in missing
                    }
                    retry_summaries = self.request_batch_summaries(
                        missing,
                        context_map=retry_context_map,
                        strict=True,
                    )
                    summaries.update(retry_summaries)

                for record in batch:
                    summary = summaries.get(record.solicitation_id, "")
                    if summary and not summary_needs_retry(summary):
                        record.ai_summary = summary
                        record.ai_summary_model = self.model
                        record.ai_summary_error = ""
                        record.ai_summary_source = "gemini"
                        self._store_cached_summary(record, summary)
                    else:
                        record.ai_summary = build_fallback_summary(record)
                        record.ai_summary_error = (
                            "Model returned an incomplete summary; fallback summary used"
                        )
                        record.ai_summary_source = "fallback"
            except requests.RequestException as exc:
                for record in batch:
                    record.ai_summary = build_fallback_summary(record)
                    record.ai_summary_error = (
                        f"AI summary request failed: {exc}; fallback summary used"
                    )
                    record.ai_summary_source = "fallback"

        if self._cache_dirty:
            self._save_cache()
            self._cache_dirty = False

        return updated_records
