# Frontend

A Streamlit app to browse the tracked books, their price and stock history, and the scraper's run log.

## Run locally

```bash
cd frontend
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

It opens at http://localhost:8501. Run it from inside `frontend/` so Streamlit picks up the theme in `.streamlit/config.toml`.

No setup or secrets are needed: the app reads Supabase over HTTPS with the project's **publishable key**, which is public by design. It can only do what the database allows the `anon` role, which is reading the public tables. It cannot write anything, and it never uses the database password. To point the app at another project, set `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` as environment variables.

## What's in it

| Tab | Shows |
|---|---|
| Browse | Search, category filter, sort, "in stock" toggle; book cards with a detail view (price history chart, stock, description). |
| Insights | Biggest categories with average price, rating distribution, price bands. |
| Pipeline | Every scrape run: status, books seen, new books, price/stock changes, errors, site response time (p95). |

The sandbox site has two placeholder categories, "Default" and "Add a comment". The app shows both as "Uncategorized"; the database keeps the original names.

## Files

| File | Purpose |
|---|---|
| `app.py` | The page: layout, filters, charts. |
| `data.py` | Read-only data access (Supabase REST API, pagination past 1,000 rows). |
| `style.css`, `.streamlit/config.toml` | Apple-inspired design: system font, light grey canvas, white cards, one blue accent. |
| `tests/` | Data-layer tests and headless app tests with fake data. |

## Tests

```bash
python -m pytest -q
```

They need no network or database: the app runs headless against fake data shaped like the real tables.
