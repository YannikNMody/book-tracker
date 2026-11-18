"""
Read-only data access for the frontend.

Talks to Supabase's REST API (PostgREST) with the PUBLISHABLE key. That key is
meant to be public: it can only do what the database grants the `anon` role,
which is SELECT on the public tables and views (see the first migration).
The frontend never sees the database password.
"""

from __future__ import annotations

import os
from typing import Optional

import pandas as pd
import requests

# Public by design (Supabase "publishable" key, read-only via grants + RLS).
# Override with environment variables to point the app at another project.
DEFAULT_SUPABASE_URL = "https://esxdjofukyynwsfmwufr.supabase.co"
DEFAULT_PUBLISHABLE_KEY = "sb_publishable_wOKBji3Ak2bvX9-KCsVnlQ_msUbUGAE"

PAGE_SIZE = 1000          # Supabase returns at most 1000 rows per request
TIMEOUT = 20

# The sandbox site has two placeholder categories; show them as one honest label.
CATEGORY_LABELS = {"Default": "Uncategorized", "Add a comment": "Uncategorized"}

BOOK_COLUMNS = ("id,title,category,rating,price,currency,in_stock,quantity,"
                "image_url,product_url,is_active,price_since,first_seen_at,last_seen_at")
RUN_COLUMNS = ("id,started_at,finished_at,status,is_full_run,pages_scraped,books_seen,"
               "books_new,price_changes,stock_changes,error_count,applied_at,stats")


class DataError(Exception):
    """Something went wrong talking to Supabase, explained for humans."""


class SupabaseReader:
    def __init__(self, url: Optional[str] = None, key: Optional[str] = None,
                 session: Optional[requests.Session] = None):
        self.base = (url or os.environ.get("SUPABASE_URL") or DEFAULT_SUPABASE_URL).rstrip("/") + "/rest/v1/"
        key = key or os.environ.get("SUPABASE_PUBLISHABLE_KEY") or DEFAULT_PUBLISHABLE_KEY
        self.session = session or requests.Session()
        self.session.headers.update({"apikey": key, "Authorization": f"Bearer {key}",
                                     "Accept": "application/json"})

    def get(self, resource: str, params: dict) -> list[dict]:
        """GET all rows, following pagination (Supabase caps each response at 1000 rows)."""
        rows: list[dict] = []
        start = 0
        while True:
            headers = {"Range-Unit": "items", "Range": f"{start}-{start + PAGE_SIZE - 1}"}
            try:
                response = self.session.get(self.base + resource, params=params,
                                            headers=headers, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise DataError(f"Could not reach Supabase ({type(exc).__name__}).") from None
            if response.status_code not in (200, 206):
                raise DataError(f"Supabase returned HTTP {response.status_code} for {resource}: "
                                f"{response.text[:200]}")
            page = response.json()
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            start += PAGE_SIZE


# ---------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------

def _to_datetime(df: pd.DataFrame, columns) -> pd.DataFrame:
    for column in columns:
        if column in df:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df


def clean_books(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=BOOK_COLUMNS.split(","))
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce").astype("Int64")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").astype("Int64")
    df["category"] = df["category"].fillna("Uncategorized").replace(CATEGORY_LABELS)
    df["in_stock"] = df["in_stock"].fillna(False).astype(bool)
    return _to_datetime(df, ["price_since", "first_seen_at", "last_seen_at"])


def fetch_books(reader: Optional[SupabaseReader] = None) -> pd.DataFrame:
    """All active books with their current price and stock."""
    reader = reader or SupabaseReader()
    rows = reader.get("books_current", {"select": BOOK_COLUMNS, "is_active": "eq.true", "order": "id"})
    return clean_books(rows)


def fetch_book_detail(book_id: int, reader: Optional[SupabaseReader] = None) -> dict:
    """Description plus full price and stock history for one book."""
    reader = reader or SupabaseReader()
    book = reader.get("books", {"select": "id,description,product_type,num_reviews", "id": f"eq.{int(book_id)}"})
    prices = reader.get("price_history", {
        "select": "price_incl_tax,price_excl_tax,currency,valid_from,valid_to",
        "book_id": f"eq.{int(book_id)}", "order": "valid_from"})
    stock = reader.get("stock_history", {
        "select": "in_stock,quantity,valid_from,valid_to",
        "book_id": f"eq.{int(book_id)}", "order": "valid_from"})
    price_df = _to_datetime(pd.DataFrame(prices, columns=["price_incl_tax", "price_excl_tax", "currency",
                                                          "valid_from", "valid_to"]), ["valid_from", "valid_to"])
    price_df["price_incl_tax"] = pd.to_numeric(price_df["price_incl_tax"], errors="coerce")
    stock_df = _to_datetime(pd.DataFrame(stock, columns=["in_stock", "quantity", "valid_from", "valid_to"]),
                            ["valid_from", "valid_to"])
    stock_df["quantity"] = pd.to_numeric(stock_df["quantity"], errors="coerce")
    return {"book": book[0] if book else {}, "prices": price_df, "stock": stock_df}


def fetch_runs(limit: int = 50, reader: Optional[SupabaseReader] = None) -> pd.DataFrame:
    """Most recent scrape runs, newest first, with response-time stats flattened."""
    reader = reader or SupabaseReader()
    rows = reader.get("scrape_runs", {"select": RUN_COLUMNS, "order": "started_at.desc", "limit": str(limit)})
    df = pd.DataFrame(rows, columns=RUN_COLUMNS.split(","))
    stats = df["stats"].apply(lambda s: s if isinstance(s, dict) else {})
    df["p95_ms"] = pd.to_numeric(stats.apply(lambda s: s.get("p95_response_ms")), errors="coerce")
    df["requests"] = pd.to_numeric(stats.apply(lambda s: s.get("requests")), errors="coerce")
    df = _to_datetime(df.drop(columns=["stats"]), ["started_at", "finished_at", "applied_at"])
    df["duration_min"] = (df["finished_at"] - df["started_at"]).dt.total_seconds() / 60
    return df


def clean_description(text: Optional[str]) -> Optional[str]:
    """books.toscrape.com stores most descriptions as a cut-off preview followed by the
    full text ("...and love th It's hard to imagine..."). Show only the full text.
    The database keeps the text exactly as scraped."""
    if not text:
        return text
    text = text.strip()
    head = text[:40]
    restart = text.find(head, len(head)) if len(head) == 40 else -1
    return text[restart:] if restart > 0 else text


def history_series(history: pd.DataFrame, value: str, until: pd.Timestamp) -> pd.DataFrame:
    """Turn valid_from/valid_to periods into points for a step chart that runs up to `until`."""
    if history.empty:
        return pd.DataFrame(columns=["date", value])
    points = []
    for row in history.itertuples(index=False):
        start = getattr(row, "valid_from")
        end = getattr(row, "valid_to")
        end = until if pd.isna(end) else end
        points.append({"date": start, value: getattr(row, value)})
        points.append({"date": max(end, start), value: getattr(row, value)})
    return pd.DataFrame(points)
