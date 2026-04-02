# Light-RFP

Command-line scraper for the LightRFP take-home assessment. It fetches live ESBD solicitations from the Texas Electronic State Business Daily, ranks them against LightRFP's vendor-service categories, and writes a browser-friendly HTML report.

## What this project does

- Fetches current ESBD solicitation listings from `https://www.txsmartbuy.gov/esbd`
- Treats `Posted` and `Addendum Posted` solicitations as currently open opportunities
- Visits each solicitation detail page to extract the required metadata
- Scores each opportunity against the LightRFP vendor-service categories
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

In Terminal:

```
venv\Scripts\activate
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
python scraper.py --top-n 20 --max-candidates 24 --output output/esbd_results.html
```

### 6. Open the generated HTML report

After the script finishes, open:

`output/esbd_results.html`

## Quick Start

```
cd path\to\Light-RFP
python -m venv .venv
venv\Scripts\Activate
python -m pip install -r requirements.txt
python scraper.py
```

## Methodology

I began with the public ESBD page specified in the assignment: `https://www.txsmartbuy.gov/esbd`. The site returns solicitation listings and detail content in server-rendered HTML, so an HTTP-first scraper is enough for the MVP and avoids unnecessary browser automation.

The scraper pulls listing rows, follows each solicitation detail page, extracts the minimum required fields, and scores each opportunity against the vendor-service categories from the take-home prompt. The score uses weighted keyword and fuzzy matching across title, classification, description, and attachment names, then applies penalties to clearly unrelated software-only, medical, legal, or insurance-oriented bids.

Current trade-offs:

- This MVP relies on the currently visible public ESBD listing rather than reverse-engineering every pagination or export path.
- Attachment files are linked but not yet downloaded and parsed.
- Agency names are resolved from the listing-page agency selector when available, otherwise the member number is shown.
