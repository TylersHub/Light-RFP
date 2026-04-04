from __future__ import annotations

from rapidfuzz import fuzz

from .categories import CATEGORY_KEYWORDS, NEGATIVE_KEYWORDS
from .models import Solicitation


def count_keyword_hits(text: str, keywords: list[str]) -> int:
    return sum(1 for keyword in keywords if keyword.lower() in text)


def fuzzy_bonus(text: str, category: str, keywords: list[str]) -> float:
    candidates = [category, *keywords[:5]]
    return max(fuzz.partial_ratio(text, candidate.lower()) for candidate in candidates) / 25.0


def score_solicitation(solicitation: Solicitation) -> Solicitation:
    title = solicitation.title.lower()
    classification = solicitation.category_classification.lower()
    description = solicitation.description.lower()
    pdf_text = solicitation.pdf_text_blob.lower()
    attachments = " ".join(
        f"{attachment.name} {attachment.description}"
        for attachment in solicitation.attachment_urls
    ).lower()
    combined = solicitation.raw_text_blob.lower()

    matched: list[tuple[str, float, list[str]]] = []

    for category, keywords in CATEGORY_KEYWORDS.items():
        title_hits = count_keyword_hits(title, keywords)
        classification_hits = count_keyword_hits(classification, keywords)
        description_hits = count_keyword_hits(description, keywords)
        pdf_hits = count_keyword_hits(pdf_text, keywords)
        attachment_hits = count_keyword_hits(attachments, keywords)
        direct_category_hits = int(category.lower() in combined)
        bonus = fuzzy_bonus(combined, category, keywords)

        score = (
            5 * title_hits
            + 4 * classification_hits
            + 3 * description_hits
            + 2 * pdf_hits
            + 2 * attachment_hits
            + 3 * direct_category_hits
            + bonus
        )

        reasons: list[str] = []
        if title_hits:
            reasons.append(f"title hits={title_hits}")
        if classification_hits:
            reasons.append(f"classification hits={classification_hits}")
        if description_hits:
            reasons.append(f"description hits={description_hits}")
        if pdf_hits:
            reasons.append(f"pdf hits={pdf_hits}")
        if attachment_hits:
            reasons.append(f"attachment hits={attachment_hits}")
        if direct_category_hits:
            reasons.append("category phrase match")
        if bonus >= 2.5:
            reasons.append(f"fuzzy bonus={bonus:.1f}")

        if score > 0:
            matched.append((category, score, reasons))

    penalty_hits = [term for term in NEGATIVE_KEYWORDS if term in combined]
    penalty = 4 * len(penalty_hits)

    matched.sort(key=lambda item: item[1], reverse=True)
    top_matches = matched[:3]
    solicitation.matched_categories = [item[0] for item in top_matches]
    solicitation.score_explanation = [
        f"{category}: {', '.join(reasons)}"
        for category, _, reasons in top_matches
        if reasons
    ]
    solicitation.relevance_score = round(
        sum(item[1] for item in top_matches) - penalty, 2
    )
    if penalty_hits:
        solicitation.score_explanation.append(
            f"negative signals: {', '.join(sorted(penalty_hits))}"
        )

    return solicitation

