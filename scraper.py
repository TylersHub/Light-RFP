from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.fetch_esbd import ESBDScraper
from src.render_html import render_report
from src.score_relevance import score_solicitation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape and rank relevant ESBD solicitations for LightRFP."
    )
    parser.add_argument(
        "--output",
        default="output/esbd_results.html",
        help="Path to the generated HTML report.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="Number of ranked solicitations to include in the report.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=None,
        help="Optional cap on how many live ESBD solicitations to enrich before ranking.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scraper = ESBDScraper()

    try:
        solicitations = scraper.collect_solicitations(max_candidates=args.max_candidates)
    except Exception as exc:  # pragma: no cover
        print(f"Scrape failed: {exc}", file=sys.stderr)
        return 1

    scored = [score_solicitation(solicitation) for solicitation in solicitations]
    ranked = sorted(
        scored,
        key=lambda item: (
            item.relevance_score,
            item.posting_date or "",
            item.due_datetime or "",
        ),
        reverse=True,
    )

    refreshed_ranked = []
    for solicitation in ranked[: max(args.top_n * 2, args.top_n)]:
        needs_refresh = any(
            [
                not solicitation.brief_description,
                not solicitation.contact_name,
                not solicitation.contact_email,
                not solicitation.contact_phone,
            ]
        )
        if needs_refresh:
            refreshed = scraper.enrich_solicitation(solicitation)
            if refreshed:
                solicitation = score_solicitation(refreshed)
        refreshed_ranked.append(solicitation)

    remaining_ranked = ranked[len(refreshed_ranked) :]
    ranked = sorted(
        [*refreshed_ranked, *remaining_ranked],
        key=lambda item: (
            item.relevance_score,
            item.posting_date or "",
            item.due_datetime or "",
        ),
        reverse=True,
    )
    top_results = ranked[: args.top_n]

    output_path = Path(args.output)
    render_report(
        results=top_results,
        output_path=output_path,
        total_candidates=len(scored),
        top_n=args.top_n,
        source_url=scraper.listing_url,
    )
    print(f"Wrote report to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
