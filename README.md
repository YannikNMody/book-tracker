# Book tracker

Tracks prices and stock of the books on [books.toscrape.com](https://books.toscrape.com) (a scraping sandbox without an API) and keeps their full history in Supabase.

```
books.toscrape.com ──scrape──▶ JSON file ──load──▶ Supabase (Postgres) ──read-only──▶ Streamlit app
                     scraper/books_scraper.py      scraper/loader.py + apply_run()     frontend/app.py
```

| Folder | What it is |
|---|---|
| `scraper/` | Scraper, loader and tests. See [scraper/README.md](scraper/README.md). |
| `frontend/` | Streamlit app to browse books, price history and scrape runs. See [frontend/README.md](frontend/README.md). |
| `supabase/migrations/` | Database schema, applied to Supabase in filename order. |
| `.github/workflows/` | `scrape.yml` runs the scraper (schedule prepared, switched off); `tests.yml` runs all tests on every push. |

## Database design

- `books`, `categories`, `sources`: the catalogue. Books are matched on the site's UPC (`external_id`); our own `id` is the primary key.
- `price_history`, `stock_history`: a new row only when a value changes (`valid_from` / `valid_to`; the current value has `valid_to = null`).
- `scrape_runs`, `scrape_errors`: audit trail of every run, incl. response-time stats.
- `staging_observations`: raw rows per run; merged by `apply_run()`, pruned after 30 days.
- `books_current`: view with each book's current price and stock, for the frontend.

Access: the frontend (anon key) can only read; only the backend (database login / service role) can write.

## Running it

- **Automatically:** GitHub Actions workflow `scrape.yml`, prepared for a daily run at 04:17 UTC. The schedule is **switched off** because the sandbox site never changes, so daily runs would only add identical snapshots. To switch it on, uncomment the two `schedule` lines in `.github/workflows/scrape.yml`. Needs the repository secret `DATABASE_URL`.
- **Manually:** Actions tab → *Daily scrape* → *Run workflow*, or locally:

```bash
cd scraper && source .venv/bin/activate
python books_scraper.py --load
```

Locally you need a `.env` file in the repo root (copy `.env.example`). Some networks (e.g. university Wi-Fi) block database connections; GitHub Actions is not affected.

The app needs no setup and works on any network, because it reads over HTTPS:

```bash
cd frontend && source .venv/bin/activate
streamlit run app.py
```
