# Auction Scout

Auction Scout is a free, image-first feed for finding overlooked auctions across configurable categories. It searches ShopGoodwill nationally, nearby government-surplus inventory from GSA Auctions and GovDeals, and CTBids inventory that is either nearby or marked shippable. Its current category hunts cover **Minerals & Geology**, **Sealed Vintage Media**, **Vintage Electron Tubes**, **Vintage Pens**, **Estate Tobacco Pipes**, **Glass Insulators**, and a broad **CTBids Estate Auctions** watch.

The intended recurring cost is **$0**: the site is plain HTML/CSS/JavaScript on GitHub Pages, and the hourly data refresh runs in GitHub Actions. No account, API key, database, backend, paid API, or credit card is required.

> This is an independent research tool. It is not affiliated with or endorsed by ShopGoodwill, GSA Auctions, GovDeals, or CTBids. It uses only public listing data available without authentication and does not bypass CAPTCHAs, access controls, or anti-bot protections.

## What you get

- An image-first responsive gallery, sorted by a cross-category, urgency-aware review priority
- Filters for source, category, price, time remaining, keyword, seller, target terms, photos, bids, and opportunity status
- Nearby government-surplus and CTBids pickup feeds fixed to ZIP **38635** within **50 miles**, plus CTBids lots marked shippable nationwide
- Visible Buy It Now pricing with an availability filter, maximum-price filter, and lowest-price sorting
- Current price, bids, delivery method, photo count, and Buy It Now information on every card
- Fast card feed with full-resolution images and complete scoring audits loaded only when a listing is inspected
- Watch/Bid/Dismiss decisions stored privately in the current browser, plus new-since-last-visit and evidence filters
- Same-seller closing clusters, bounded zero-cost image OCR, and optional outcome tracking
- Public machine-readable index at `data/index.json`, with lazy detail buckets and a high-priority view
- Incremental hourly refreshes with delays, timeouts, bounded retry/backoff, and graceful partial failure
- A capped archive of expired listings and their last observed price
- Tests for scoring, deduplication, expiration, image URL handling, and malformed responses

## Project structure

```text
.
├── .github/workflows/refresh.yml  # hourly + manual refresh
├── data/                          # canonical generated feeds
├── docs/                          # GitHub Pages site
│   ├── data/                      # compact index, detail buckets, clusters, and status
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── scraper/
│   ├── config.json                # search terms, weights, API limits
│   ├── scrape.py                  # refresh/orchestration/archive
│   ├── scoring.py                 # explainable scoring
│   ├── ocr.py                     # bounded image-text extraction
│   ├── shopgoodwill.py            # ShopGoodwill public-source adapter
│   ├── government.py              # GSA Auctions + GovDeals adapters
│   └── ctbids.py                  # CTBids public-source adapter
├── tests/
├── README.md
└── requirements.txt
```

## How the scraper works

The current ShopGoodwill storefront calls a public Buyer API. Auction Scout uses the same unauthenticated read-only routes:

- `POST https://buyerapi.shopgoodwill.com/api/Search/ItemListing` for active search results
- `GET https://buyerapi.shopgoodwill.com/api/itemDetail/GetItemDetailModelByItemId/{item_id}` for the description, seller, shipping metadata, and all image paths

The detail response supplies `imageServer` plus semicolon-separated `imageUrlString` and `thumbnailUrlString` values. ShopGoodwill currently returns backslashes in those paths; the client normalizes them into valid HTTPS CDN URLs. API auction timestamps are timezone-naive Pacific wall-clock times, so the feed records them with the `America/Los_Angeles` UTC offset before the browser converts them to each visitor's local time.

Each run searches the editable terms, merges duplicates by item ID, updates search-level price/bid/end-time fields, and requests full details only for records that do not already have them. The number of new detail calls is capped per run. Existing records are retained until their end time even if they move outside the first page of search results. Expired records move to the capped archive with their final observed price.

Each run asks GSA Auctions, GovDeals, and CTBids for active listings within 50 miles of 38635, then separately asks CTBids for listings explicitly marked shippable, ordered by ending time and bounded by the configured page limit. Results are deduplicated and namespaced by source so overlapping numeric IDs cannot collide. Government lots favor visible equipment signals; CTBids lots favor visible collector, material, maker, and estate-collection signals. Pickup-only CTBids lots are retained only from the nearby search.

The ranking deliberately avoids assigning resale prices. Each category has its own evidence rules for valuable makers, models, construction details, lot composition, condition language, and obvious risk signals. Only the strongest few signals contribute, specific phrases supersede generic substrings, description-only evidence receives less weight, and boilerplate shipping/return text is removed first. Cheapness and extra photo count do not create value evidence; too few photos remain a quality risk, and bid count contributes only inside the final 12 hours.

The site keeps two separate, auditable numbers. The **evidence score** is the category-specific keyword, image, OCR, and risk calculation. The **review priority** calibrates that score against the category's own high-potential threshold and field percentile, then adds a small time-to-close adjustment and factual friction such as unavailable combined shipping. This makes the default ordering useful across unlike categories without pretending a pen score and an insulator score share a resale-value scale. Both sets of reasons remain visible, and neither number is an appraisal. Inspecting a card lazily fetches its full images, description, and per-hunt audit trail from one of 64 stable detail buckets.

The workflow uses Tesseract and a small local image-color check to inspect a fixed number of listing images per run, caching results in `data/ocr_cache.json`. OCR can surface visible model numbers, tube codes, brands, and markings that titles miss. For glass insulators, the color check can flag a dominant blue, purple, amber, yellow/olive, or green/teal hue. Both are clue sources, not visual authentication.

The data-source code is isolated in `scraper/shopgoodwill.py`, `scraper/government.py`, and `scraper/ctbids.py`, so a source change does not require rewriting the pipeline or site.

## Run locally

You need Python 3.10 or newer. Python 3.12 is used in GitHub Actions.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m scraper.scrape
python -m http.server 8000 --directory docs
```

Then open [http://localhost:8000](http://localhost:8000). Use the local web server instead of double-clicking `docs/index.html`, because browsers usually block `fetch()` from `file://` pages.

For a quick, lighter data check, limit full-detail requests:

```powershell
python -m scraper.scrape --max-detail-requests 10
```

The scraper writes identical feeds to `data/` and `docs/data/`. The first location is convenient for code and AI clients; the second is what GitHub Pages publishes.

## Add or customize hunt categories

Edit [`scraper/config.json`](scraper/config.json):

- `hunts` contains independently enabled categories. Each hunt has an `id`, label, search terms, and scoring-profile name.
- `local_search` sets the ZIP code and radius used for nearby inventory, while `sources` enables and rate-limits GSA Auctions, GovDeals, and CTBids independently. CTBids also supports a separate nationwide shippable pass.
- `scoring_profiles` keeps each hunt's ranking logic separate. A listing found by multiple hunts receives a score within each one. Focused category hunts take precedence over broad source-level hunts, and scores are compared relative to each category's own threshold.
- `search_terms` inside a hunt controls that category's queries.
- `seller_sweeps` provides a small ending-soon fallback for proven sellers whose lots are sometimes missing from ShopGoodwill keyword results. Sweep results are filtered against the hunt's `domain_keywords` before they enter the feed.
- `priority_keywords` rewards collector-oriented wording, but configurable group caps prevent generic words such as “mixed,” “lot,” and “assorted” from stacking into a false high score.
- `seller_bonuses` can encode a narrowly scoped source-confidence adjustment when repeated visual review shows that a seller's domain-specific lots are consistently stronger than their generic descriptions.
- `lower_priority_keywords` strongly lowers dyed, coated, carved, decorative, metaphysical, and other retail/decor noise.
- `target_keywords` and `premium_keywords` add category-specific bonuses.
- `bid_bonuses` and `bid_penalties` apply only near closing time (12 hours by default), when bidding begins to carry useful opportunity information.
- `collector_evidence_keywords` rewards provenance, locality labels, specimen labels, and field-collected material.
- `photo_penalties` and `high_priority_minimum_photos` enforce inspection-quality gates without rewarding a listing merely for having more photos. `shipping_rules` represents known delivery friction. A zero listed shipping value is treated as unresolved calculated shipping, not as free shipping.
- `high_priority_threshold` controls the numeric cutoff, while the high-priority quality gate also requires enough photos and at least one strong collector, target, provenance, construction, or equipment signal.
- `max_detail_requests_per_run`, delays, timeouts, and page limits control request volume.

To add a future category, copy the Minerals & Geology entry in `hunts`, give it a unique ID and terms, then add its named profile under `scoring_profiles`. No scraper or frontend change is required. Listings publish `hunt_categories`, `primary_hunt`, and `hunt_scores` so rankings remain explainable.

Weights may be positive or negative. Final scores are clamped to 0–100, and every listing includes `score_reasons` so the result is auditable.

Watch/Bid/Dismiss decisions and the last-visit snapshot use browser storage and never publish your decisions to GitHub.

## Publish on GitHub Pages

1. Create an empty GitHub repository, for example `shopgoodwill-auction-scout`. Public is simplest and has the most generous free Actions behavior.
2. From this project folder, initialize and push it (replace `YOUR_USERNAME`):

   ```powershell
   git init
   git add .
   git commit -m "Initial Auction Scout"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/shopgoodwill-auction-scout.git
   git push -u origin main
   ```

3. On GitHub, open **Settings → Pages**.
4. Under **Build and deployment**, choose **Deploy from a branch**.
5. Select branch **main**, folder **/docs**, then **Save**.
6. Open **Actions → Refresh auction listings → Run workflow** once to confirm the manual refresh.
7. In **Settings → Actions → General**, keep workflow permissions at **Read and write permissions** if the repository policy does not honor the workflow's own `contents: write` declaration.

After Pages finishes, the site URL is:

```text
https://YOUR_USERNAME.github.io/shopgoodwill-auction-scout/
```

The public feeds are:

```text
https://YOUR_USERNAME.github.io/shopgoodwill-auction-scout/data/index.json
https://YOUR_USERNAME.github.io/shopgoodwill-auction-scout/data/high_priority.json
https://YOUR_USERNAME.github.io/shopgoodwill-auction-scout/data/archive.json
https://YOUR_USERNAME.github.io/shopgoodwill-auction-scout/data/clusters.json
https://YOUR_USERNAME.github.io/shopgoodwill-auction-scout/data/status.json
```

Each index record names a `detail_bucket`; fetch `data/details/{detail_bucket}.json` and select the record by `item_id` for full images, description, and the per-hunt scoring audit.

All frontend data URLs are relative, so the site works correctly under a GitHub Pages repository subpath.

## Refresh schedule and cost

`.github/workflows/refresh.yml` runs at minute 17 of every hour and also supports the **Run workflow** button. GitHub schedules can be delayed during busy periods; an exact start time is not guaranteed.

For a public repository, standard GitHub-hosted Actions runners and GitHub Pages are normally free. Private repositories receive a monthly Actions allowance; an hourly workflow may consume a meaningful share of it. Check the current [GitHub Actions billing documentation](https://docs.github.com/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions) and [GitHub Pages limits](https://docs.github.com/pages/getting-started-with-github-pages/github-pages-limits) for your account. GitHub may disable scheduled workflows in public repositories after long periods of repository inactivity; manual dispatch and a new commit can reactivate them.

## Troubleshooting

### The workflow finds no listings

Open the latest Actions run and inspect the scraper warnings. A temporary ShopGoodwill failure is handled conservatively: retries are limited, existing unexpired data is preserved, and `status.json` records partial search failures. Try **Run workflow** later rather than increasing retries aggressively.

### ShopGoodwill changed its API

Check the browser's developer-tools Network panel while performing a normal public search and opening a listing. Update only the request/response translation in `scraper/shopgoodwill.py`, then run the tests. Do not add login automation, CAPTCHA solving, or access-control bypasses.

### Images do not display

Open the item's detail bucket under `docs/data/details/`, then open one of its `images` URLs directly. If the CDN path format changed, update `normalize_image_url()` and its test. A seller may also remove images when an auction closes.

### The scheduled workflow cannot push

Check **Settings → Actions → General → Workflow permissions**, and make sure branch protection allows `github-actions[bot]` to update generated data. You can instead run locally and commit `data/` plus `docs/data/` manually.

### The page works at `/` but not at the repository URL

Keep asset and feed paths relative (`./app.js`, `./data/index.json`). This project already does that. Do not add a leading slash unless deploying to a custom root domain.

## Known limitations

- ShopGoodwill and CTBids do not document these as supported public APIs, and any storefront can change fields or routes without notice.
- GSA Auctions, GovDeals, or the nearby CTBids pass may legitimately return zero active items in the selected local radius. The source status shown on the page distinguishes an empty result from a failed refresh.
- Search currently reads the newest first page for each term to keep hourly request volume modest. Incremental runs accumulate still-active records, but the very first run may not include every older matching auction.
- Full details are capped per run. Lower-scoring new records may temporarily show one search-result image and `detail_status: "pending"`; later runs continue the queue.
- Shipping is seller- and destination-dependent. Calculated shipping remains unresolved until the auction site provides a destination-specific quote.
- Potential scores are triage signals, not appraisals. Photo condition, authenticity, exact variant, and untested status can materially change value.
- OCR is deliberately incremental and can misread labels. A machine without Tesseract still runs normally and reuses existing cached results.
- Glass-insulator color detection is deliberately conservative and labels only broad hue families; lighting, backgrounds, irradiation, staining, and photography can still mislead it.
- Search relevance belongs to ShopGoodwill and can be broad. Per-hunt scoring and filters surface likely opportunities without silently hiding other results.
- Original CDN images are linked, not copied. They can disappear after ShopGoodwill purges a listing.

## Responsible-use defaults

The hourly job makes one search request per term/page, waits between all requests, caps new detail requests, uses short bounded backoff only for temporary failures, and never authenticates. If ShopGoodwill blocks or rate-limits a method, leave the limits conservative and allow the job to fail gracefully.
