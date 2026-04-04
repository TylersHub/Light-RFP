from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
import time

from src.ai_summary import GeminiSummarizer
from src.config import (
    DETAIL_CANDIDATE_MULTIPLIER,
    MIN_DETAIL_CANDIDATES,
    MIN_PDF_CANDIDATES,
    PDF_CANDIDATE_MULTIPLIER,
)
from src.fetch_esbd import ESBDScraper
from src.render_html import render_report
from src.score_relevance import score_solicitation


def load_local_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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
        help="Optional cap on how many live ESBD listing records to analyze before ranking.",
    )
    parser.add_argument(
        "--enable-ai-summaries",
        action="store_true",
        help="Generate AI summaries from extracted PDF text using the Gemini API.",
    )
    parser.add_argument(
        "--gemini-model",
        default=None,
        help="Optional Gemini model override for AI summaries.",
    )
    parser.add_argument(
        "--ai-context-chars-per-record",
        type=int,
        default=None,
        help="Optional override for how many characters of PDF-derived context to send to Gemini per report result.",
    )
    return parser.parse_args()


def main() -> int:
    load_local_env()
    args = parse_args()
    scraper = ESBDScraper()
    overall_started_at = time.perf_counter()

    try:
        started_at = time.perf_counter()
        listing_solicitations = scraper.collect_listing_solicitations(
            max_candidates=args.max_candidates
        )
        print(
            f"Collected {len(listing_solicitations)} live listing candidates "
            f"in {time.perf_counter() - started_at:.1f}s"
        )
    except Exception as exc:  # pragma: no cover
        print(f"Scrape failed: {exc}", file=sys.stderr)
        return 1

    started_at = time.perf_counter()
    pre_scored = [score_solicitation(solicitation) for solicitation in listing_solicitations]
    pre_ranked = sorted(
        pre_scored,
        key=lambda item: (
            item.relevance_score,
            item.posting_date or "",
            item.due_datetime or "",
        ),
        reverse=True,
    )
    print(f"Initial scoring finished in {time.perf_counter() - started_at:.1f}s")
    detail_candidate_count = min(
        len(pre_ranked),
        max(args.top_n * DETAIL_CANDIDATE_MULTIPLIER, MIN_DETAIL_CANDIDATES),
    )

    try:
        started_at = time.perf_counter()
        detailed_candidates = scraper.fetch_details_for_records(
            pre_ranked[:detail_candidate_count]
        )
        print(
            f"Detail lookup finished for {len(detailed_candidates)} top-ranked listing candidates "
            f"in {time.perf_counter() - started_at:.1f}s"
        )
    except Exception as exc:  # pragma: no cover
        print(f"Detail lookup failed: {exc}", file=sys.stderr)
        return 1

    started_at = time.perf_counter()
    ranked = sorted(
        [score_solicitation(solicitation) for solicitation in detailed_candidates],
        key=lambda item: (
            item.relevance_score,
            item.posting_date or "",
            item.due_datetime or "",
        ),
        reverse=True,
    )
    print(f"Post-detail rescoring finished in {time.perf_counter() - started_at:.1f}s")
    pdf_candidate_count = min(
        len(ranked),
        max(args.top_n * PDF_CANDIDATE_MULTIPLIER, MIN_PDF_CANDIDATES),
    )

    try:
        started_at = time.perf_counter()
        pdf_candidates = scraper.download_and_extract_pdfs_for_records(
            ranked[:pdf_candidate_count]
        )
        print(
            f"PDF extraction finished for {len(pdf_candidates)} highest-ranked detail-reviewed candidates "
            f"in {time.perf_counter() - started_at:.1f}s"
        )
    except Exception as exc:  # pragma: no cover
        print(f"PDF extraction failed: {exc}", file=sys.stderr)
        return 1

    pdf_by_id = {
        solicitation.solicitation_id: solicitation for solicitation in pdf_candidates
    }
    merged_candidates = [
        pdf_by_id.get(solicitation.solicitation_id, solicitation)
        for solicitation in ranked
    ]

    started_at = time.perf_counter()
    final_ranked = sorted(
        [score_solicitation(solicitation) for solicitation in merged_candidates],
        key=lambda item: (
            item.relevance_score,
            item.posting_date or "",
            item.due_datetime or "",
        ),
        reverse=True,
    )
    print(f"Final scoring finished in {time.perf_counter() - started_at:.1f}s")
    live_refresh_count = min(len(final_ranked), max(args.top_n * 2, 40))
    started_at = time.perf_counter()
    refreshed_finalists = scraper.fetch_details_for_records(
        final_ranked[:live_refresh_count],
        use_cache=False,
    )
    refreshed_by_id = {
        solicitation.solicitation_id: solicitation for solicitation in refreshed_finalists
    }
    refreshed_candidates = [
        refreshed_by_id.get(solicitation.solicitation_id, solicitation)
        for solicitation in final_ranked
    ]
    final_ranked = sorted(
        [score_solicitation(solicitation) for solicitation in refreshed_candidates],
        key=lambda item: (
            item.relevance_score,
            item.posting_date or "",
            item.due_datetime or "",
        ),
        reverse=True,
    )
    print(
        f"Live detail refresh finished for {len(refreshed_finalists)} likely final-report candidates "
        f"in {time.perf_counter() - started_at:.1f}s"
    )
    top_results = final_ranked[: args.top_n]

    if args.enable_ai_summaries:
        started_at = time.perf_counter()
        top_results = scraper.download_and_extract_pdfs_for_records(
            top_results,
            use_cache=True,
        )
        top_results = sorted(
            [score_solicitation(solicitation) for solicitation in top_results],
            key=lambda item: (
                item.relevance_score,
                item.posting_date or "",
                item.due_datetime or "",
            ),
            reverse=True,
        )[: args.top_n]
        print(
            f"Final-report PDF refresh finished for {len(top_results)} final report results "
            f"in {time.perf_counter() - started_at:.1f}s"
        )

    if args.enable_ai_summaries:
        summarizer = GeminiSummarizer(
            model=args.gemini_model,
            context_chars_per_record=args.ai_context_chars_per_record,
        )
        if not summarizer.enabled:
            print(
                "AI summaries were requested but GEMINI_API_KEY was not provided.",
                file=sys.stderr,
            )
        else:
            started_at = time.perf_counter()
            top_results = summarizer.summarize_solicitations(top_results)
            print(
                f"AI summarization finished for {len(top_results)} report results "
                f"in {time.perf_counter() - started_at:.1f}s"
            )

    output_path = Path(args.output)
    started_at = time.perf_counter()
    render_report(
        results=top_results,
        output_path=output_path,
        total_candidates=len(pre_scored),
        top_n=args.top_n,
        source_url=scraper.listing_url,
    )
    print(f"HTML rendering finished in {time.perf_counter() - started_at:.1f}s")
    print(f"Wrote report to {output_path}")
    print(f"Total runtime: {time.perf_counter() - overall_started_at:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
