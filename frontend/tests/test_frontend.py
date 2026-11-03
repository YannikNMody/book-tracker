"""
Frontend tests: data layer (simulated Supabase responses) and the Streamlit app
run headless with fake data. No network needed.

Run from the frontend/ folder:  python -m pytest -q
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import data  # noqa: E402
import fake_data  # noqa: E402


# ---------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------

class FakeResponse:
    def __init__(self, rows, status=200):
        self._rows, self.status_code, self.text = rows, status, str(rows)

    def json(self):
        return self._rows


class FakeSession:
    def __init__(self, total_rows=0, status=200):
        self.total, self.status, self.calls = total_rows, status, []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        start, end = (int(x) for x in headers["Range"].split("-"))
        rows = [{"id": i} for i in range(start, min(end + 1, self.total))]
        return FakeResponse(rows, self.status)


def test_reader_sends_publishable_key_and_only_reads():
    session = FakeSession(total_rows=3)
    reader = data.SupabaseReader(url="https://x.supabase.co", key="sb_publishable_test", session=session)
    reader.get("books_current", {"select": "id"})
    assert session.headers["apikey"] == "sb_publishable_test"
    assert session.calls[0][0] == "https://x.supabase.co/rest/v1/books_current"
    assert "sb_secret" not in data.DEFAULT_PUBLISHABLE_KEY and data.DEFAULT_PUBLISHABLE_KEY.startswith("sb_publishable_")


def test_reader_follows_pagination_past_1000_rows():
    session = FakeSession(total_rows=2500)
    rows = data.SupabaseReader(url="https://x", key="k", session=session).get("books_current", {})
    assert len(rows) == 2500
    assert [c[2]["Range"] for c in session.calls] == ["0-999", "1000-1999", "2000-2999"]


def test_reader_stops_on_exactly_one_full_page():
    session = FakeSession(total_rows=1000)
    rows = data.SupabaseReader(url="https://x", key="k", session=session).get("books_current", {})
    assert len(rows) == 1000 and len(session.calls) == 2   # second call returns 0 rows


def test_reader_explains_http_errors():
    session = FakeSession(total_rows=1, status=401)
    with pytest.raises(data.DataError, match="HTTP 401"):
        data.SupabaseReader(url="https://x", key="k", session=session).get("books_current", {})


def test_clean_books_merges_placeholder_categories_and_types():
    df = data.clean_books([
        {"id": 1, "title": "A", "category": "Default", "rating": 3, "price": "51.77", "in_stock": True, "quantity": 22},
        {"id": 2, "title": "B", "category": "Add a comment", "rating": None, "price": None, "in_stock": None},
        {"id": 3, "title": "C", "category": "Poetry", "rating": 5, "price": "10.00", "in_stock": False, "quantity": 0},
    ])
    assert list(df["category"]) == ["Uncategorized", "Uncategorized", "Poetry"]
    assert df["price"].iloc[0] == pytest.approx(51.77)
    assert list(df["in_stock"]) == [True, False, False]


def test_history_series_builds_step_points_up_to_now():
    history = pd.DataFrame([
        {"price_incl_tax": 10.0, "valid_from": pd.Timestamp("2026-10-01T00:00Z"), "valid_to": pd.Timestamp("2026-10-05T00:00Z")},
        {"price_incl_tax": 8.0, "valid_from": pd.Timestamp("2026-10-05T00:00Z"), "valid_to": pd.NaT},
    ])
    now = pd.Timestamp("2026-10-10T00:00Z")
    series = data.history_series(history, "price_incl_tax", now)
    assert list(series["price_incl_tax"]) == [10.0, 10.0, 8.0, 8.0]
    assert series["date"].iloc[-1] == now


def test_fetch_runs_flattens_stats():
    runs = fake_data.runs()
    assert list(runs["p95_ms"]) == [103.5, 363.8, 542.2, 845.3]
    assert runs["duration_min"].iloc[0] == pytest.approx(9.07, abs=0.01)


# ---------------------------------------------------------------------
# App, run headless
# ---------------------------------------------------------------------

FAKE_RUNS = fake_data.runs()   # built once, before any patching


@pytest.fixture
def app(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(data, "fetch_books", lambda reader=None: fake_data.books())
    monkeypatch.setattr(data, "fetch_runs", lambda limit=50, reader=None: FAKE_RUNS.copy())
    monkeypatch.setattr(data, "fetch_book_detail", lambda book_id, reader=None: fake_data.book_detail(book_id))
    import streamlit as st
    st.cache_data.clear()
    at = AppTest.from_file(str(FRONTEND / "app.py"), default_timeout=30)
    return at.run()


def markdown_text(at) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_app_renders_without_errors(app):
    assert not app.exception, app.exception
    assert not app.error
    text = markdown_text(app)
    assert "Every book. Every price. Tracked." in text
    assert "1,000" in text
    assert [t.label for t in app.tabs] == ["Browse", "Insights", "Pipeline"]


def test_browse_shows_first_page_and_show_more(app):
    details = [b for b in app.button if b.label == "Details"]
    assert len(details) == 24
    more = next(b for b in app.button if b.label.startswith("Show more"))
    more.click().run()
    assert len([b for b in app.button if b.label == "Details"]) == 48


def test_search_and_filters(app):
    app.text_input[0].input("light in the attic").run()
    assert "1 book" in markdown_text(app)
    app.text_input[0].input("zzzz-no-such-book").run()
    assert "No books match" in markdown_text(app)


def test_category_filter_uses_clean_labels(app):
    options = app.selectbox[0].options
    assert "Uncategorized" in options and "Default" not in options and "Add a comment" not in options
    app.selectbox[0].select("Poetry").run()
    assert not app.exception


def test_sort_by_price(app):
    app.selectbox[1].select("Price: high to low").run()
    assert not app.exception


def test_detail_dialog_opens(app):
    next(b for b in app.button if b.label == "Details").click().run()
    assert not app.exception, app.exception
    assert "Price history" in markdown_text(app)


def test_titles_are_html_escaped(monkeypatch):
    from streamlit.testing.v1 import AppTest
    evil = fake_data.books(n=3)
    evil.loc[0, "title"] = '<img src=x onerror="alert(1)">'
    monkeypatch.setattr(data, "fetch_books", lambda reader=None: evil)
    monkeypatch.setattr(data, "fetch_runs", lambda limit=50, reader=None: FAKE_RUNS.copy())
    import streamlit as st
    st.cache_data.clear()
    at = AppTest.from_file(str(FRONTEND / "app.py"), default_timeout=30).run()
    text = markdown_text(at)
    assert '<img src=x onerror' not in text and "&lt;img src=x onerror" in text


def test_supabase_error_is_shown_not_crashing(monkeypatch):
    from streamlit.testing.v1 import AppTest

    def boom(reader=None):
        raise data.DataError("Could not reach Supabase (ConnectionError).")
    monkeypatch.setattr(data, "fetch_books", boom)
    monkeypatch.setattr(data, "fetch_runs", lambda limit=50, reader=None: FAKE_RUNS.copy())
    import streamlit as st
    st.cache_data.clear()
    at = AppTest.from_file(str(FRONTEND / "app.py"), default_timeout=30).run()
    assert not at.exception
    assert "Could not load data from Supabase" in at.error[0].value
