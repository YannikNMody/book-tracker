# Scraper and loader

- `books_scraper.py` scrapes [books.toscrape.com](https://books.toscrape.com) into a JSON file.
- `loader.py` loads such a file into the Supabase database.
- `books_scraper.py --load` does both in one go.

## Setup

```bash
cd scraper
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

For loading, copy `.env.example` (repo root) to `.env` and fill in `DATABASE_URL`. The instructions are inside the file.

## Scrape

```bash
python books_scraper.py --limit 5        # 5 books
python books_scraper.py --pages 3        # 3 listing pages = 60 books
python books_scraper.py                  # full catalogue, ~1,050 requests, ~9 min at default delay
python books_scraper.py --load           # full catalogue, then load into the database
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
| `--load` | off | after saving, load the run into the database |
| `-v` / `-q` | | more / less logging |

**Exit codes:** `0` success, `3` partial (some pages failed), `1` failed (no data), `4` scraped and saved but loading failed (retry with `loader.py`).

### Stress test ("does the site crash?")

```bash
python books_scraper.py --pages 5 --delay 0 --workers 16
```

Compare `run.stats` (status counts, p50/p95/max response times) with a polite run.

## Load existing files

```bash
python loader.py output/run_20261028T124756Z.json     # one file
python loader.py output/*.json                        # all files, applied oldest first
python loader.py output/run_x.json --dry-run          # do everything, then undo it
```

How loading works:

1. Everything for one run happens in **one transaction**: the run row, the raw rows (`staging_observations`), the errors, and the merge. A run lands completely or not at all.
2. The merge is the database function `apply_run()` (migration `20261028132003_apply_run.sql`):
   - new books are inserted, known books refreshed (a missing value never overwrites a known one);
   - `price_history` / `stock_history` get a new row **only when a value changes**: the current row is closed (`valid_to`) and a new one opened;
   - only a complete, successful full run marks unseen books as inactive, and only if it saw at least half of the active catalogue (circuit breaker against a broken page layout wiping everything);
   - runs must be applied oldest first; the database refuses anything else.
3. Loading the same file twice does nothing (each run has a `run_uuid`).

## Output format

```json
{
  "run":          { ... },   // -> scrape_runs
  "observations": [ ... ],   // -> staging_observations, one per book
  "errors":       [ ... ]    // -> scrape_errors, every failed attempt incl. retries that later succeeded
}
```

- Prices are strings (`"51.77"`) so no cents are lost to float rounding.
- `is_full_run` is `true` only for a complete, unrestricted, error-free pass.
- Books are de-duplicated on UPC (`external_id`).

## Tests

```bash
python -m pytest -q
```

- Scraper tests need no internet: a local server mimics the real site (flaky page, missing page, duplicate UPC, pound-sign encoding trap).
- Loader tests need a **disposable** Postgres database, because they wipe it before every test. They are skipped unless `TEST_DATABASE_URL` is set, and refuse to run against a Supabase host. Never point them at the real project.
