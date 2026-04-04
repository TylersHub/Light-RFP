# Light-RFP

Command-line scraper for the LightRFP take-home assessment. It fetches live ESBD solicitations from the Texas Electronic State Business Daily, ranks them against LightRFP's vendor-service categories, and writes a browser-friendly HTML report.

## What this project does

- Fetches current ESBD solicitation listings from `https://www.txsmartbuy.gov/esbd`
- Treats `Posted` and `Addendum Posted` solicitations as currently open opportunities
- Uses the ESBD details service to look up the strongest candidates and collect the required metadata
- Downloads PDF bid attachments for a shortlist of strong candidates
- Extracts text from text-based PDFs and flags PDFs that appear to be scanned or image-based
- Optionally generates AI summaries from extracted PDF text when a Gemini API key is provided
- Only generates summaries for the final report results, not every candidate examined during scraping
- Reuses short-lived cached ESBD detail payloads and cached PDF extraction results on repeat runs for much faster performance
- Scores each opportunity against the LightRFP vendor-service categories
- Analyzes the full set of open ESBD solicitations exposed by the live paginated ESBD service by default
- Outputs the top 20 ranked results as `output/esbd_results.html`

## Setup

These steps assume you have already cloned or downloaded the repo and opened a terminal in the project root, which is the folder that contains `README.md`, `requirements.txt`, and `scraper.py`.

### 1. Open PowerShell in the project folder

```
cd path\to\Light-RFP
```

### 2. Create a virtual environment inside this repo

This creates a local `venv` directory in the project root.

```
python -m venv venv
```

### 3. Activate the virtual environment

In most Windows terminals, this works:

```
venv\Scripts\activate
```

If PowerShell blocks script execution, use:

```
venv\Scripts\Activate.ps1
```

If activation works, your terminal prompt will usually start with `(venv)`.

### 4. Install dependencies

```
python -m pip install -r requirements.txt
```

### 5. Optional: create a local `.env`

The scraper will automatically load environment variables from a local `.env` file if one exists.

To use AI summaries, create a `.env` file in the project root and copy the format from:

`.env.example`

Then add your Gemini settings:

```env
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-2.5-flash
```

### 6. Run the scraper

```
python scraper.py
```

Optional flags:

```
python scraper.py --top-n 20 --max-candidates 50 --output output/esbd_results.html
```

`--max-candidates` limits how many live ESBD listing records are analyzed during the initial ranking stage.

Useful AI flag:

```powershell
python scraper.py --enable-ai-summaries --ai-context-chars-per-record 5000
```

`--ai-context-chars-per-record` controls how much PDF-derived context is sent to Gemini for each final report result.

### 7. Optional: enable AI summaries

PDF extraction runs automatically. AI summaries are optional and only run when `--enable-ai-summaries` is used and a Gemini API key is available.

If you created a `.env` file in Step 5, you can usually just run:

```powershell
python scraper.py --enable-ai-summaries
```

You can also set the API key directly in the terminal for one session:

In PowerShell:

```powershell
$env:GEMINI_API_KEY="your_api_key_here"
python scraper.py --enable-ai-summaries
```

In Command Prompt:

```bat
set GEMINI_API_KEY=your_api_key_here
python scraper.py --enable-ai-summaries
```

Optional model override:

```powershell
python scraper.py --enable-ai-summaries --gemini-model gemini-2.5-flash
```

If no API key is supplied, the scraper still runs successfully, but AI summaries are skipped.

### 8. Open the generated HTML report

After the script finishes, open:

`output/esbd_results.html`

The scraper prints stage timings while it runs so you can see where time is being spent. The first run on a clean machine, or the first run after cache expiry, is slower because it rebuilds local caches under `data/processed/` and `data/pdfs/`. Repeat runs are usually much faster.

So if the first run takes a little longer than expected, that is normal. Later runs are typically faster because the scraper can reuse those short-lived caches.

## Quick Start

```
cd path\to\Light-RFP
python -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
python scraper.py
```

## Methodology

I began with the public ESBD page specified in the assignment: `https://www.txsmartbuy.gov/esbd`. After inspecting the site behavior, I found that the search UI is backed by a paginated JSON listing service and a structured details service, so the scraper uses those live ESBD services instead of relying on only the visible HTML results.

The scraper requests every page of currently open ESBD solicitations from the live listing service, scores the full live open-solicitation set, looks up a generous shortlist of the strongest candidates through the ESBD details service, and then downloads PDF attachments for a smaller top-tier subset before producing the final top 20. It also performs one final PDF refresh for the final report results so the ranked output stays consistent whether AI summaries are enabled or not. Listing-page collection runs in parallel so the full open set can still be analyzed without waiting on a long serial page walk. The score uses weighted keyword and fuzzy matching across title, classification, description, attachment names, and extracted PDF text, then applies penalties to clearly unrelated software-only, medical, legal, or insurance-oriented bids.

When `--enable-ai-summaries` is used and `GEMINI_API_KEY` is set, the scraper sends extracted PDF text only for the final report results to the Gemini API to generate concise two-sentence summaries. To keep the runtime reasonable and avoid quota spikes, the scraper batches multiple final-report solicitations into each Gemini request and rate-limits those batch requests. Detail payloads and PDF extraction results are also cached locally under `data/processed/` and `data/pdfs/` so repeat runs do not keep redoing the same work. This AI step is optional so the project remains runnable end-to-end without requiring paid API access.

Current trade-offs:

- The shortlist-first approach is much faster, but it assumes the strongest relevance signals are already visible in the ESBD listing data, with details serving mainly as refinement.
- PDF extraction only runs on a smaller high-confidence subset for performance reasons, so attachment text is not collected for every open solicitation.
- Detail and PDF caches are intentionally short-lived so repeat runs stay fast without drifting too far from the live site.
- Scanned or image-based PDFs are detected heuristically by extractable-text density, but this project does not OCR them yet.
- AI summaries are optional and depend on the presence of a Gemini API key.
- Summary quality still depends on the quality of the attached PDFs; forms, standard terms, and boilerplate-heavy packages can produce weaker summaries than solicitations with a clear scope-of-work document.
- The scraper discovers the ESBD service path from the live site bundle and falls back to a known service path, but a major Texas SmartBuy frontend redesign could still require a small update.

