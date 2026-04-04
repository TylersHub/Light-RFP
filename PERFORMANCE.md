# Performance Notes

This document explains the current performance optimizations in the ESBD scraper and why the newer flow is faster without dropping the accuracy safeguards that matter for the take-home.

## Main bottlenecks that were fixed

The scraper had three major performance costs:

1. It fetched ESBD listing pages one at a time.
2. It could re-fetch the same detail payload more than once for the same solicitation.
3. It re-parsed downloaded PDFs on every run, even when the files were already on disk.

For AI-enabled runs, there was a fourth issue:

4. The Gemini batch request was too constrained on output tokens, which increased the chance of retries or weak responses.

## Current pipeline

The scraper now uses this staged flow:

1. Fetch the full open ESBD listing set from the live paginated service.
2. Score the full listing set.
3. Look up full details for a large shortlist of likely contenders.
4. Re-score that shortlist using the additional detail.
5. Download and parse PDFs for a smaller top-tier subset.
6. Re-score again with PDF text included.
7. Generate AI summaries only for the final top-20 report results when enabled.
8. Render the HTML report.

This keeps full listing coverage while limiting the expensive detail, PDF, and AI work to records that can realistically affect the final report.

## What changed

### Parallel listing-page collection

The ESBD listing service is paginated, so fetching all open solicitations serially was taking most of the runtime by itself. The scraper now fetches page 1 first to discover the total number of pages, then requests the remaining pages in parallel and restores the original page order before continuing.

### Single-pass detail lookup plus local caching

Detail lookup now does one structured detail fetch per solicitation instead of looping over the same payload repeatedly. Successful detail payloads are cached locally under `data/processed/detail_cache/` for a limited time, so repeated runs do not keep re-requesting unchanged records.

### Cached PDF extraction results

Downloaded PDFs were already being reused from disk, but their text extraction was being repeated every run. The scraper now stores a small JSON sidecar next to each downloaded PDF with:

- extractable text
- extraction status
- scanned/image-based flag
- page count

If the local PDF file has not changed and the cache is still fresh, the scraper reuses that extraction result immediately.

### Better-scoped AI summarization

Gemini summaries still only run for the final report results. The summarizer now uses:

- a shorter per-record context budget
- a larger batch character budget
- a lower minimum inter-request delay
- a batch output-token budget sized to the number of summaries requested

That makes the AI step faster and reduces needless retries.

## Why the optimizations are still safe

- The scraper still analyzes the full live open listing set from ESBD.
- Detail lookup still happens before final output for a much larger set than the final 20.
- PDF extraction still happens before the final ranking is locked.
- Cached detail and PDF data are only reused for a limited time, which keeps repeat runs fast without making the scraper permanently stale.
- If a parallel page fetch fails, the scraper falls back to refetching that page directly.

## Measured results

Observed live runtimes on April 3, 2026:

- Earlier optimized version, before the latest cache and pagination work:
  - about `2 minutes` without AI
  - about `3 minutes` with AI
- Current version after the latest optimizations:
  - about `15 seconds` without AI on a warm cache run
  - about `26 seconds` with AI on a warm cache run

One representative live run printed these stage timings:

- listing collection: `8.5s`
- initial scoring: `0.7s`
- detail lookup: `0.1s`
- PDF extraction: `0.6s`
- final scoring: `4.5s`
- AI summarization for 20 report results: `11.0s`

Exact runtimes will vary with network conditions, the number of currently open ESBD solicitations, Gemini response time, and whether local caches are already populated.

## Trade-offs

- The first run on a clean machine is still slower than a repeat run because it has to build the detail and PDF caches.
- Cached data is intentionally temporary, so repeat-run speed comes from reusing recent work rather than pretending the source never changes.
- The shortlist-first design is a practical performance choice, but it assumes the strongest early relevance signals are already visible in listing metadata before full detail lookup.
