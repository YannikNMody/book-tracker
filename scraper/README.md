# Scraper

Scrapes [books.toscrape.com](https://books.toscrape.com) into a JSON file. No database yet: the output is shaped to match the Supabase tables so the loader (step 4) only has to insert it.

## Setup

```bash
cd scraper
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python books_scraper.py --limit 5        # 5 books
python books_scraper.py --pages 3        # 3 listing pages = 60 books
python books_scraper.py                  # full catalogue, ~1,050 requests, ~9 min at default delay
```

| Option | Default | Meaning |
|---|---|---|
| `--limit N` | none | stop after N books |
| `--pages N` | none | stop after N listing pages |
| `--start-page N` | 1 | listing page to start from |
| `--delay S` | 0.5 | seconds between requests, shared across all workers |
| `--workers N` | 4 | product pages fetched in parallel |
| `--timeout S` | 15 | per-request timeout |
| `--max-retries N` | 3 | retries on timeouts, connection errors, 429 and 5xx (not on 404) |
| `--backoff S` | 1 | first retry wait; doubles each retry |
| `--output PATH` | `output/run_<UTC timestamp>.json` | where to write |
| `-v` / `-q` | | more / less logging |

**Exit codes:** `0` success, `3` partial (some pages failed), `1` failed (no data).

### Stress test ("does the site crash?")

```bash
python books_scraper.py --pages 5 --delay 0 --workers 16
```

Compare `run.stats` (status counts, p50/p95/max response times) with a polite run.

## Output

```json
{
  "run":          { ... },   // -> scrape_runs
  "observations": [ ... ],   // -> staging_observations, one per book
  "errors":       [ ... ]    // -> scrape_errors, every failed attempt incl. retries that later succeeded
}
```

- Prices are strings (`"51.77"`) so no cents are lost to float rounding.
- `is_full_run` is `true` only for a complete, unrestricted, error-free pass. Only such runs may mark books as delisted.
- Books are de-duplicated on UPC (`external_id`).

## Tests

```bash
python -m pytest -q
```

The tests need no internet: they start a local server with the same HTML structure as the real site, including a flaky page, a missing page, a duplicate UPC and the pound-sign encoding trap.
