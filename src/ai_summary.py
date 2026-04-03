from __future__ import annotations

import os

import requests

from .config import (
    AI_SUMMARY_MAX_INPUT_CHARS,
    AI_SUMMARY_TIMEOUT_SECONDS,
    DEFAULT_GEMINI_MODEL,
    GEMINI_API_BASE_URL,
)
from .models import Solicitation
from .parse_esbd import first_sentences, normalize_whitespace


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


def build_summary_prompt(record: Solicitation, max_chars: int) -> str:
    pdf_text = normalize_whitespace(record.pdf_text_blob)[:max_chars]
    return (
        "Summarize this government RFP in exactly 2 concise sentences. "
        "Focus on the actual scope of work, major services or trades requested, and any notable context. "
        "Do not mention that this is based on extracted PDF text.\n\n"
        f"Solicitation title: {record.title}\n"
        f"Solicitation ID: {record.solicitation_id}\n"
        f"Agency: {record.agency_name or record.agency_number}\n"
        f"Classification: {record.category_classification}\n"
        "Extracted PDF text:\n"
        f"{pdf_text}"
    )


class GeminiSummarizer:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
        self.model = (model or os.getenv("GEMINI_MODEL", "")).strip() or DEFAULT_GEMINI_MODEL
        self.timeout_seconds = AI_SUMMARY_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def summarize_solicitation(self, record: Solicitation) -> Solicitation:
        if not self.enabled:
            record.ai_summary_error = "GEMINI_API_KEY not provided"
            return record
        if not record.pdf_text_blob:
            record.ai_summary_error = "No extracted PDF text available"
            return record

        response = requests.post(
            f"{GEMINI_API_BASE_URL}/{self.model}:generateContent",
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": build_summary_prompt(
                                    record=record,
                                    max_chars=AI_SUMMARY_MAX_INPUT_CHARS,
                                )
                            }
                        ],
                    }
                ],
                "generationConfig": {
                    "maxOutputTokens": 140,
                    "temperature": 0.2,
                },
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

        payload = response.json()
        summary = first_sentences(extract_response_text(payload), limit=2)
        if not summary:
            record.ai_summary_error = "Model returned an empty summary"
            return record

        record.ai_summary = summary
        record.ai_summary_model = self.model
        record.ai_summary_error = ""
        return record

    def summarize_solicitations(self, records: list[Solicitation]) -> list[Solicitation]:
        summarized: list[Solicitation] = []
        for record in records:
            try:
                summarized.append(self.summarize_solicitation(record))
            except requests.RequestException as exc:
                record.ai_summary_error = f"AI summary request failed: {exc}"
                summarized.append(record)
        return summarized
