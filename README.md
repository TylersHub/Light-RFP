# Light-RFP

Command-line scraper for the LightRFP take-home assessment. It fetches live ESBD solicitations from the Texas Electronic State Business Daily, ranks them against LightRFP's vendor-service categories, and writes a browser-friendly HTML report.

## What this project does

- Fetches current ESBD solicitation listings from `https://www.txsmartbuy.gov/esbd`
- Treats `Posted` and `Addendum Posted` solicitations as currently open opportunities
- Uses the ESBD details service to enrich the strongest candidates with the required metadata
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

### 5. Run the scraper

```
python scraper.py
```

Optional flags:

```
python scraper.py --top-n 20 --max-candidates 50 --output output/esbd_results.html
```

`--max-candidates` limits how many live ESBD listing records are analyzed during the initial ranking stage.

### 6. Open the generated HTML report

After the script finishes, open:

`output/esbd_results.html`

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

The scraper requests every page of currently open ESBD solicitations from the live listing service, scores the full live open-solicitation set, and then enriches a generous shortlist of the strongest candidates through the ESBD details service before producing the final top 20. This keeps full-portal coverage while avoiding hundreds of unnecessary detail requests. The score uses weighted keyword and fuzzy matching across title, classification, description, and attachment names, then applies penalties to clearly unrelated software-only, medical, legal, or insurance-oriented bids.

Current trade-offs:

- The shortlist-first approach is much faster, but it assumes the strongest relevance signals are already visible in the ESBD listing data, with details serving mainly as refinement.
- Attachment files are linked but not yet downloaded and parsed.
- The scraper discovers the ESBD service path from the live site bundle and falls back to a known service path, but a major Texas SmartBuy frontend redesign could still require a small update.

