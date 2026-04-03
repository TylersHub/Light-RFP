# Performance Notes

This document explains the performance optimization made to the ESBD scraper and why the current approach is faster while still staying accurate enough for the take-home.

## Summary

The original end-to-end pipeline was correct in spirit, but it was doing too much expensive work:

- It fetched the full live ESBD listing set
- It then enriched every open solicitation with full detail data
- Only after that did it rank results and keep the top 20

Because the live ESBD open-solicitation set was `632` records during testing on `April 3, 2026`, this meant the scraper was making hundreds of detail requests even though the final report only shows `20` results.

## Root Cause

The slowest part of the pipeline was detail enrichment, not HTML generation.

The old flow looked like this:

1. Fetch every page of the ESBD listing service
2. For every open solicitation, call the details endpoint
3. Parse and enrich all records
4. Score all enriched records
5. Keep the top 20

That approach maximized completeness early, but it was inefficient because many low-relevance records were fully enriched even though they had no realistic chance of appearing in the final report.

## New Approach

The scraper now uses a staged pipeline:

1. Fetch and score the full live listing set using the ESBD listing service
2. Sort those listing-level records by relevance
3. Fully enrich only a generous shortlist of likely contenders using the ESBD details service
4. Re-score the enriched shortlist
5. Download and parse PDF attachments only for a smaller top-tier subset
6. Re-score the PDF-enriched records
7. Optionally generate AI summaries only for the final report results
8. Output the top 20

This keeps full-portal coverage during the ranking stage while avoiding unnecessary detail requests, PDF downloads, and AI calls for weak candidates.

## Why This Is Still Safe

This optimization is intentionally conservative.

- The scraper still analyzes the full open ESBD listing set
- The shortlist is much larger than the final output size
- The listing service already contains strong relevance signals such as:
  - title
  - status
  - agency
  - due date/time
  - NIGP/classification codes
- The details service is still used before final output, so top results still get:
  - contact name
  - contact email
  - contact phone
  - description
  - attachments
  - addendum text
- PDF extraction is only used on a smaller top-tier subset, so attachment parsing improves ranking quality without forcing the scraper to download every bid package on every run.
- AI summarization is only used on the final report results, so optional LLM usage stays narrow and cost-controlled.

In the current configuration, the scraper enriches `160` detail candidates for a `top 20` report, then runs PDF extraction on a smaller `40`-record subset. That is intentionally wider than necessary to reduce the risk of excluding a result that becomes more relevant once richer text is added.

## Additional Improvement

The shortlist enrichment step now runs in a modest parallel batch instead of purely serial detail requests.

This improves runtime without becoming overly aggressive toward the source system.

## Measured Result

Observed live runtime during development:

- Before optimization: about `527` seconds
- After optimization: about `70` seconds

These numbers came from live runs against ESBD on `April 3, 2026`. Exact runtimes will vary depending on network conditions and the size of the current open-solicitation set.

## Trade-Off

The main trade-off is that the richer enrichment stages no longer run against every single open solicitation.

Instead, detail enrichment, PDF extraction, and optional AI summarization run against relevance-ranked shortlists. For this take-home, that is a better balance of:

- full live coverage
- relevance quality
- runtime
- reviewer experience

If this were extended into a production system, the shortlist size could be made configurable or adapted dynamically based on score distribution.
