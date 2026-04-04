# Relevance Scoring

This project ranks ESBD solicitations by estimating how relevant each opportunity is to the LightRFP vendor-service categories from the take-home prompt.

## High-level idea

Each solicitation is turned into a single text profile using:

- title
- class or item code
- solicitation description
- extracted PDF text when PDF parsing succeeds
- attachment names
- attachment descriptions
- other extracted detail-page text used in the raw text blob

The scorer then compares that text against a category keyword map in [src/categories.py](src/categories.py).

## How scoring works

The main scoring logic lives in [src/score_relevance.py](src/score_relevance.py).

For each LightRFP category, the scorer calculates:

- title keyword hits
- classification keyword hits
- description keyword hits
- PDF keyword hits
- attachment keyword hits
- direct category phrase hits
- fuzzy similarity bonus

The current formula is:

```text
score =
  5 * title_hits
  + 4 * classification_hits
  + 3 * description_hits
  + 2 * pdf_hits
  + 2 * attachment_hits
  + 3 * direct_category_hits
  + fuzzy_bonus
```

## What the score is "out of"

The score is not out of a fixed number such as 10 or 100.

This implementation uses a weighted sum of matching signals, so the final value depends on how many relevant keywords appear across the solicitation title, classification, description, and attachments.

That means:

- there is no hard maximum
- the score is primarily meant for ranking results against each other
- it should be interpreted as a relative relevance signal, not a percentage

## Score range

The theoretical range is open-ended:

- the minimum can go below `0` if negative keyword penalties outweigh positive evidence
- the maximum is unbounded because a solicitation can accumulate many keyword hits

In practice, with the current ESBD data and current weights, the final top-20 report results have recently landed roughly in the `41` to `74` range during live verification, while weaker matches score lower.

So the most useful interpretation is:

- higher score = stronger estimated relevance
- lower score = weaker estimated relevance
- the number itself is less important than the ordering of results

That observed range is not a guarantee. It can move as:

- the live ESBD solicitation mix changes
- PDF text adds more matching evidence
- category weights or keyword maps are tuned

This is done independently for every category in the category map.

## Why the fields are weighted this way

The weights reflect confidence:

- Title matches are weighted highest because titles are usually the clearest summary of the work.
- Classification matches are also strong because ESBD class/item codes often describe the procurement domain directly.
- Description matches are useful but a little noisier.
- Extracted PDF text can add useful evidence, but it is weighted below title, classification, and description because PDF text can be verbose or messy.
- Attachment-name matches are helpful, but weaker than title and classification text.
- Direct category phrase matches give a small boost when the solicitation text explicitly uses the same wording as a LightRFP category.
- Fuzzy matching helps catch near-matches that do not use the exact same phrasing.

## Fuzzy matching

Fuzzy matching uses `rapidfuzz.partial_ratio`.

For each category, the scorer compares the full solicitation text blob against:

- the category name itself
- up to the first five keywords for that category

The best fuzzy score is converted into a small numeric bonus:

```text
fuzzy_bonus = best_partial_ratio / 25.0
```

That means fuzzy matching helps ranking, but it is intentionally not strong enough to dominate direct keyword evidence.

## Negative signals

Some opportunities are clearly outside the intended LightRFP service domains even if they contain a few overlapping words.

To reduce those false positives, the scorer applies penalties for terms listed in `NEGATIVE_KEYWORDS` in [src/categories.py](src/categories.py).

Examples include:

- software
- saas
- cybersecurity
- insurance
- legal
- medical
- pharmaceutical
- point of sale

The penalty formula is:

```text
penalty = 4 * number_of_negative_keyword_hits
```

The final relevance score is:

```text
final_score = sum(top_category_scores) - penalty
```

## Category selection

After scoring every category:

- categories are sorted by score descending
- the top 3 category matches are kept
- those category names become `matched_categories`
- the human-readable reasons become `score_explanation`

This gives the report both a rank and a short explanation of why the match happened.

## Score explanations

The scorer still generates internal score-explanation details such as:

- `title hits=2`
- `classification hits=1`
- `description hits=3`
- `attachment hits=1`
- `category phrase match`
- `fuzzy bonus=3.1`

Those explanations are useful for debugging and tuning, even though the current HTML report no longer displays them.

## Current trade-offs

This scorer is intentionally simple and reviewer-friendly, but it has limitations:

- It is keyword-driven, so it can miss relevant bids that use unusual wording.
- It can still over-score generic construction solicitations if they overlap multiple broad categories.
- PDF extraction only runs on a smaller high-confidence subset, so not every open solicitation benefits from attachment-body text.
- The final report results do go through a final PDF refresh before rendering, so the locked top 20 uses the strongest PDF-backed evidence available for those results.
- Negative filtering is lightweight and may need tuning as more live solicitations are observed.

## Future improvements

Strong next improvements would be:

- add better handling for class/item code semantics
- tune weights with more live examples
- separate broad construction signals from highly specific trade signals
- add a small normalization step so extremely long descriptions do not accumulate too many keyword hits
