# Performance Notes

This document explains how the scraper balances speed with accuracy and what the current runtime looks like with the code as it exists now.

## Current approach

The scraper uses a staged pipeline:

1. Fetch the full current open ESBD listing set from the live paginated listing service.
2. Score that full listing set.
3. Fetch full detail records for a large shortlist of likely contenders.
4. Re-score that shortlist using the added detail.
5. Download and extract PDF text for a smaller top-tier subset.
6. Re-score again with PDF text included.
7. Re-fetch live detail records for the likely finalists before locking the final report.
8. If enabled, run Gemini summaries only for the final top-20 results.
9. Render the HTML report.

This keeps full listing coverage while limiting the expensive detail, PDF, and AI work to records that can realistically affect the final report.

## What is cached

The scraper does use caching, but only for parts of the pipeline that are expensive and reasonably stable during a short window.

### Detail payload cache

- Location: `data/processed/detail_cache/`
- Contents: ESBD detail-service JSON payloads
- Time-to-live: `1 hour`

These are used to avoid re-requesting the same detail record repeatedly during nearby runs.

### PDF extraction cache

- Location: JSON sidecar files next to downloaded PDFs in `data/pdfs/`
- Contents:
  - extracted text
  - extraction status
  - scanned/image-based flag
  - page count
- Time-to-live: `1 hour`

These are used to avoid re-parsing the same local PDF file during nearby runs.

### AI summary cache

- Location: `data/processed/ai_summary_cache.json`
- Contents: successful Gemini summaries keyed by model and extracted PDF text hash

This only helps when the same PDF-backed report result appears again with unchanged extracted text and the previous Gemini response was good enough to keep.

## What is not cached

- The ESBD listing pages are fetched live on every run.
- The likely finalists go through a live detail refresh before the final top 20 is locked.
- When AI summaries are enabled, the final report results go through a final PDF download/extraction pass before summarization.

So the performance improvements are not coming from freezing the whole scrape. The scraper still checks the live site each run where freshness matters most.

## Why it is faster now

The main performance improvements are:

1. Parallel listing-page fetches instead of walking the ESBD listing pages one at a time.
2. One detail fetch per solicitation instead of repeated fetch loops.
3. Reuse of recent detail payloads and PDF extraction results when they are still fresh.
4. Gemini batching that only runs for the final report results, not for earlier candidates.

## Why this is still safe

- The full open ESBD listing set is still analyzed every run.
- Detail fetches still happen before final output for a much larger set than the final 20.
- PDF extraction still happens before the final ranking is locked.
- The final likely winners get a live detail refresh before the report is written.
- The caches are short-lived, which limits staleness.
- If a parallel page fetch fails, the scraper falls back to refetching that page directly.

## Current measured runtimes

Measured live on April 3, 2026 with the current code:

- `python scraper.py`
  - total runtime: about `23.5s`
- `python scraper.py --enable-ai-summaries --output output\esbd_results.html`
  - total runtime: about `57.4s`

Representative no-AI stage timings from the current code:

- listing collection: `9.7s`
- initial scoring: `0.9s`
- detail lookup for 160 candidates: `0.2s`
- post-detail rescoring: `0.4s`
- PDF extraction for 40 candidates: `0.7s`
- final scoring: `5.7s`
- live detail refresh for 40 finalists: `5.9s`

Representative AI-enabled stage timings from the current code:

- listing collection: `9.3s`
- initial scoring: `0.8s`
- detail lookup for 160 candidates: `0.2s`
- post-detail rescoring: `0.4s`
- PDF extraction for 40 candidates: `0.7s`
- final scoring: `5.5s`
- live detail refresh for 40 finalists: `5.8s`
- final-report PDF refresh for 20 results: `3.4s`
- AI summarization for 20 report results: `31.3s`

Exact runtimes will vary based on:

- network conditions
- the number of currently open ESBD solicitations
- whether the detail and PDF caches are already warm
- Gemini response time and rate limiting

## Trade-offs

- A warm-cache repeat run is faster than a first run on a clean machine.
- The shortlist-first design is a performance trade-off: it assumes the strongest early relevance signals are already visible in listing metadata before full detail and PDF work.
- AI summary speed depends heavily on Gemini latency and quota behavior, so the AI-enabled run is still meaningfully slower than the no-AI run.
