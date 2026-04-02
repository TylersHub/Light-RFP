from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Attachment:
    name: str
    url: str
    description: str = ""


@dataclass
class Solicitation:
    title: str
    solicitation_id: str
    status: str
    agency_number: str = ""
    agency_name: str = ""
    posting_date: str = ""
    due_date: str = ""
    due_time: str = ""
    due_datetime: str = ""
    description: str = ""
    brief_description: str = ""
    category_classification: str = ""
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    bid_response_email: str = ""
    detail_url: str = ""
    attachment_urls: list[Attachment] = field(default_factory=list)
    addendum_text: str = ""
    raw_text_blob: str = ""
    matched_categories: list[str] = field(default_factory=list)
    score_explanation: list[str] = field(default_factory=list)
    relevance_score: float = 0.0
