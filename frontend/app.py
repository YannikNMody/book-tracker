"""
Book tracker frontend (Streamlit).

Run:  cd frontend && streamlit run app.py
Reads from Supabase with the public, read-only publishable key (see data.py).
"""

from __future__ import annotations

import html
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import data

st.set_page_config(page_title="Book tracker", page_icon="📚", layout="wide")

HERE = Path(__file__).parent
PAGE = 24
CURRENCY = {"GBP": "£", "USD": "$", "EUR": "€"}
ACCENT = "#0071e3"
FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', Inter, 'Helvetica Neue', Arial, sans-serif"


# ---------------------------------------------------------------------
# Data (cached for 10 minutes; the source changes at most once a day)
# ---------------------------------------------------------------------

@st.cache_data(ttl=600, show_spinner=False)
def load_books() -> pd.DataFrame:
    return data.fetch_books()


@st.cache_data(ttl=600, show_spinner=False)
def load_runs() -> pd.DataFrame:
    return data.fetch_runs()


@st.cache_data(ttl=600, show_spinner=False)
def load_detail(book_id: int) -> dict:
    return data.fetch_book_detail(book_id)


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def esc(value) -> str:
    return html.escape("" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value))


def money(value, currency: str = "GBP") -> str:
    if value is None or pd.isna(value):
        return "–"
    return f"{CURRENCY.get(currency, currency + ' ')}{value:,.2f}"


def stars(rating) -> str:
    n = 0 if rating is None or pd.isna(rating) else int(rating)
    return f'<span class="stars" aria-label="{n} of 5 stars">{"★" * n}<span class="off">{"★" * (5 - n)}</span></span>'


def safe_url(url) -> str:
    url = "" if url is None else str(url)
    return esc(url) if url.startswith(("https://", "http://")) else ""


def cover_img(url, lazy: bool = False) -> str:
    src = safe_url(url)
    if not src:
        return ""
    loading = ' loading="lazy"' if lazy else ""
    return f'<img src="{src}" alt=""{loading}>'


def stock_pill(in_stock: bool, quantity) -> str:
    if not in_stock:
        return '<span class="pill out">Out of stock</span>'
    qty = "" if quantity is None or pd.isna(quantity) else f" · {int(quantity)}"
    return f'<span class="pill in">In stock{qty}</span>'


def fmt_date(ts, with_time: bool = False) -> str:
    if ts is None or pd.isna(ts):
        return "–"
    ts = pd.Timestamp(ts).tz_convert("Europe/Lisbon")
    return ts.strftime("%-d %b %Y, %H:%M") if with_time else ts.strftime("%-d %b %Y")


def style_chart(chart: alt.Chart, height: int = 260) -> alt.Chart:
    return (chart.properties(height=height, background="transparent")
            .configure_view(stroke=None)
            .configure_axis(labelFont=FONT, titleFont=FONT, labelColor="#6e6e73", titleColor="#6e6e73",
                            labelFontSize=12, titleFontSize=12, titleFontWeight=500, gridColor="#ececf0",
                            domain=False, tickColor="#ececf0")
            .configure_legend(labelFont=FONT, titleFont=FONT))


def panel(key: str, title: str, subtitle: str = ""):
    box = st.container(key=f"panel-{key}")
    sub = f'<p class="panel-sub">{esc(subtitle)}</p>' if subtitle else ""
    box.markdown(f'<p class="panel-title">{esc(title)}</p>{sub}', unsafe_allow_html=True)
    return box


# ---------------------------------------------------------------------
# Book detail dialog
# ---------------------------------------------------------------------

@st.dialog("Book details", width="large")
def show_book(book: dict) -> None:
    try:
        detail = load_detail(int(book["id"]))
    except data.DataError as exc:
        st.error(f"Could not load this book's history. {exc}")
        return
    currency = book.get("currency") or "GBP"

    left, right = st.columns([1, 1.6], gap="large")
    with left:
        st.markdown(f'<div class="detail-cover">{cover_img(book.get("image_url"))}</div>', unsafe_allow_html=True)
    with right:
        st.markdown(
            f'<div class="detail-cat">{esc(book.get("category"))}</div>'
            f'<div class="detail-title">{esc(book.get("title"))}</div>'
            f'{stars(book.get("rating"))}'
            f'<div class="detail-price">{money(book.get("price"), currency)}</div>'
            f'{stock_pill(book.get("in_stock"), book.get("quantity"))}',
            unsafe_allow_html=True)
        description = data.clean_description((detail["book"] or {}).get("description"))
        if description:
            st.markdown(f'<p class="muted" style="margin-top:14px">{esc(description[:420])}'
                        f'{"…" if len(description) > 420 else ""}</p>', unsafe_allow_html=True)
        url = safe_url(book.get("product_url"))
        if url:
            st.link_button("View on books.toscrape.com", url)

    prices, stock = detail["prices"], detail["stock"]
    now = pd.Timestamp.now(tz="UTC")
    changes = max(len(prices) - 1, 0)
    first_seen = prices["valid_from"].min() if not prices.empty else book.get("first_seen_at")
    st.markdown(
        '<div class="facts">'
        f'<div class="fact"><div class="k">Tracked since</div><div class="v">{fmt_date(first_seen)}</div></div>'
        f'<div class="fact"><div class="k">Price changes</div><div class="v">{changes}</div></div>'
        f'<div class="fact"><div class="k">Lowest seen</div><div class="v">{money(prices["price_incl_tax"].min(), currency)}</div></div>'
        f'<div class="fact"><div class="k">Highest seen</div><div class="v">{money(prices["price_incl_tax"].max(), currency)}</div></div>'
        '</div>', unsafe_allow_html=True)

    series = data.history_series(prices, "price_incl_tax", now)
    if not series.empty:
        low, high = series["price_incl_tax"].min(), series["price_incl_tax"].max()
        pad = max((high - low) * 0.25, high * 0.08)
        days = max((series["date"].max() - series["date"].min()).days, 1)
        ticks = {"interval": "day", "step": max(1, round(days / 6))}  # ~6 labels, one per day at most
        chart = alt.Chart(series).mark_line(interpolate="step-after", strokeWidth=2.5, color=ACCENT).encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%-d %b", grid=False, tickCount=ticks)),
            y=alt.Y("price_incl_tax:Q", title=None, scale=alt.Scale(domain=[max(low - pad, 0), high + pad]),
                    axis=alt.Axis(format=",.2f", tickCount=4)),
            tooltip=[alt.Tooltip("date:T", title="Date", format="%-d %b %Y"),
                     alt.Tooltip("price_incl_tax:Q", title="Price", format=",.2f")])
        st.markdown('<p class="panel-title" style="margin-top:18px">Price history</p>'
                    + ('<p class="panel-sub">No change since tracking started.</p>' if changes == 0 else ""),
                    unsafe_allow_html=True)
        st.altair_chart(style_chart(chart, height=200), use_container_width=True, theme=None)

    stock_changes = max(len(stock) - 1, 0)
    if not stock.empty:
        current = stock.iloc[-1]
        state = f"{int(current['quantity'])} available" if current["in_stock"] and pd.notna(current["quantity"]) \
            else ("In stock" if current["in_stock"] else "Out of stock")
        st.markdown(f'<p class="muted">Stock: {esc(state)} · {stock_changes} stock change'
                    f'{"" if stock_changes == 1 else "s"} recorded.</p>', unsafe_allow_html=True)


# ---------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------

def hero(books: pd.DataFrame, runs: pd.DataFrame) -> None:
    applied = runs["applied_at"].dropna() if not runs.empty else pd.Series(dtype="datetime64[ns, UTC]")
    checked = runs["finished_at"].max() if not runs.empty else books["last_seen_at"].max()
    st.markdown(
        '<div class="hero">'
        '<p class="eyebrow">Book tracker</p>'
        '<h1>Every book. Every price. Tracked.</h1>'
        f'<p class="sub">{len(books):,} titles from books.toscrape.com, with price and stock history '
        f'for each one. Last checked {fmt_date(checked, with_time=True)}.</p>'
        '</div>', unsafe_allow_html=True)

    total_changes = int(runs["price_changes"].fillna(0).sum()) if not runs.empty else 0
    copies = int(books["quantity"].fillna(0).sum())
    in_stock = int(books["in_stock"].sum())
    st.markdown(
        '<div class="kpis">'
        f'<div class="kpi"><div class="label">Books tracked</div><div class="value">{len(books):,}</div>'
        f'<div class="note">in {books["category"].nunique()} categories</div></div>'
        f'<div class="kpi"><div class="label">Median price</div><div class="value">{money(books["price"].median())}</div>'
        f'<div class="note">{money(books["price"].min())} – {money(books["price"].max())}</div></div>'
        f'<div class="kpi"><div class="label">In stock</div><div class="value">{in_stock:,}</div>'
        f'<div class="note">{copies:,} copies available</div></div>'
        f'<div class="kpi"><div class="label">Price changes</div><div class="value">{total_changes:,}</div>'
        f'<div class="note">across {len(applied):,} scrape runs</div></div>'
        '</div>', unsafe_allow_html=True)


def browse(books: pd.DataFrame) -> None:
    search_col, cat_col, sort_col, stock_col = st.columns([3, 2, 2, 1.3], vertical_alignment="bottom")
    query = search_col.text_input("Search", placeholder=f"Search {len(books):,} books",
                                  label_visibility="collapsed")
    categories = ["All categories"] + sorted(books["category"].unique())
    category = cat_col.selectbox("Category", categories, label_visibility="collapsed")
    sort = sort_col.selectbox("Sort", ["Title A–Z", "Price: low to high", "Price: high to low", "Best rated"],
                              label_visibility="collapsed")
    in_stock_only = stock_col.toggle("In stock", value=False)

    view = books
    if query.strip():
        view = view[view["title"].str.contains(query.strip(), case=False, regex=False, na=False)]
    if category != "All categories":
        view = view[view["category"] == category]
    if in_stock_only:
        view = view[view["in_stock"]]
    order = {"Title A–Z": (["title"], [True]), "Price: low to high": (["price", "title"], [True, True]),
             "Price: high to low": (["price", "title"], [False, True]),
             "Best rated": (["rating", "price"], [False, True])}[sort]
    view = view.sort_values(order[0], ascending=order[1], na_position="last")

    signature = (query, category, sort, in_stock_only)
    if st.session_state.get("browse_signature") != signature:
        st.session_state["browse_signature"] = signature
        st.session_state["visible"] = PAGE
    visible = st.session_state.get("visible", PAGE)

    st.markdown(f'<p class="muted" style="margin:.25rem 0 1rem">{len(view):,} '
                f'{"book" if len(view) == 1 else "books"}</p>', unsafe_allow_html=True)
    if view.empty:
        st.markdown('<p class="muted" style="text-align:center;padding:3rem 0">No books match these filters.</p>',
                    unsafe_allow_html=True)
        return

    grid = st.container(horizontal=True, gap="medium", key="grid")
    for book in view.head(visible).to_dict("records"):
        card = grid.container(key=f"card-{book['id']}", width=240)
        card.markdown(
            f'<div class="cover">{cover_img(book["image_url"], lazy=True)}</div>'
            f'<div class="card-cat">{esc(book["category"])}</div>'
            f'<div class="card-title" title="{esc(book["title"])}">{esc(book["title"])}</div>'
            f'<div class="card-meta"><span class="card-price">{money(book["price"], book["currency"])}</span>'
            f'{stars(book["rating"])}</div>'
            f'<div style="margin:8px 0 2px">{stock_pill(book["in_stock"], book["quantity"])}</div>',
            unsafe_allow_html=True)
        if card.button("Details", key=f"open-{book['id']}", type="tertiary", width="stretch"):
            show_book(book)

    if visible < len(view):
        _, middle, _ = st.columns([2, 1, 2])
        if middle.button(f"Show more ({len(view) - visible:,} left)", width="stretch"):
            st.session_state["visible"] = visible + PAGE
            st.rerun()


def insights(books: pd.DataFrame) -> None:
    by_cat = (books.groupby("category")
              .agg(books=("id", "count"), avg_price=("price", "mean"), avg_rating=("rating", "mean"))
              .reset_index().sort_values("books", ascending=False).head(15))
    by_cat["avg_label"] = by_cat["avg_price"].map(lambda v: "avg " + money(v))
    with panel("categories", "Biggest categories", "Number of books and average price, top 15 categories"):
        bars = alt.Chart(by_cat).mark_bar(cornerRadiusEnd=6, height=16, color=ACCENT).encode(
            x=alt.X("books:Q", title="Books", axis=alt.Axis(tickCount=6)),
            y=alt.Y("category:N", sort="-x", title=None, axis=alt.Axis(labelLimit=180)),
            tooltip=[alt.Tooltip("category:N", title="Category"), alt.Tooltip("books:Q", title="Books"),
                     alt.Tooltip("avg_price:Q", title="Average price", format=",.2f"),
                     alt.Tooltip("avg_rating:Q", title="Average rating", format=".1f")])
        labels = bars.mark_text(align="left", dx=6, color="#6e6e73", fontSize=12, font=FONT).encode(
            text="avg_label:N")
        st.altair_chart(style_chart(bars + labels, height=420), use_container_width=True, theme=None)

    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
    left, right = st.columns(2, gap="medium")
    with left, panel("ratings", "Ratings", "How many books have each star rating"):
        ratings = books["rating"].value_counts().reindex(range(1, 6), fill_value=0).rename_axis("rating") \
            .reset_index(name="books")
        ratings["label"] = ratings["rating"].map(lambda r: "★" * int(r))
        chart = alt.Chart(ratings).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6, color="#ff9f0a").encode(
            x=alt.X("label:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("books:Q", title=None, axis=alt.Axis(tickCount=4)),
            tooltip=[alt.Tooltip("rating:O", title="Stars"), alt.Tooltip("books:Q", title="Books")])
        st.altair_chart(style_chart(chart, height=240), use_container_width=True, theme=None)
    with right, panel("prices", "Price range", "Number of books per £5 price band"):
        priced = books.dropna(subset=["price"])
        low = (priced["price"] // 5 * 5).astype(int)
        bands = (pd.DataFrame({"low": low}).value_counts().rename("books").reset_index().sort_values("low"))
        bands["band"] = bands["low"].map(lambda v: f"{CURRENCY.get('GBP')}{v}–{v + 5}")
        chart = alt.Chart(bands).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6, color="#34c759").encode(
            x=alt.X("band:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("books:Q", title=None, axis=alt.Axis(tickCount=4)),
            tooltip=[alt.Tooltip("band:N", title="Price band"), alt.Tooltip("books:Q", title="Books")])
        st.altair_chart(style_chart(chart, height=240), use_container_width=True, theme=None)


def pipeline(runs: pd.DataFrame) -> None:
    if runs.empty:
        st.info("No scrape runs recorded yet.")
        return
    latest = runs.iloc[0]
    full_runs = runs[runs["is_full_run"]]
    st.markdown(
        '<div class="kpis">'
        f'<div class="kpi"><div class="label">Scrape runs</div><div class="value">{len(runs)}</div>'
        f'<div class="note">{len(full_runs)} full catalogue</div></div>'
        f'<div class="kpi"><div class="label">Last run</div><div class="value">{esc(str(latest["status"]).title())}</div>'
        f'<div class="note">{fmt_date(latest["started_at"], with_time=True)}</div></div>'
        f'<div class="kpi"><div class="label">Last run duration</div><div class="value">'
        f'{latest["duration_min"]:.1f} min</div><div class="note">{int(latest["requests"] or 0):,} requests</div></div>'
        f'<div class="kpi"><div class="label">Site response (p95)</div><div class="value">'
        f'{latest["p95_ms"]:.0f} ms</div><div class="note">95% of pages were faster</div></div>'
        '</div>', unsafe_allow_html=True)

    with panel("runs", "Run history", "Every scrape, newest first. A run lands in the database completely or not at all."):
        table = runs[["started_at", "status", "is_full_run", "books_seen", "books_new", "price_changes",
                      "stock_changes", "error_count", "p95_ms", "duration_min"]].copy()
        table["started_at"] = table["started_at"].dt.tz_convert("Europe/Lisbon").dt.tz_localize(None)
        table["status"] = table["status"].str.title()
        st.dataframe(table, hide_index=True, width="stretch", column_config={
            "started_at": st.column_config.DatetimeColumn("Started", format="D MMM YYYY, HH:mm"),
            "status": st.column_config.TextColumn("Status"),
            "is_full_run": st.column_config.CheckboxColumn("Full run"),
            "books_seen": st.column_config.NumberColumn("Books seen", format="%d"),
            "books_new": st.column_config.NumberColumn("New", format="%d"),
            "price_changes": st.column_config.NumberColumn("Price changes", format="%d"),
            "stock_changes": st.column_config.NumberColumn("Stock changes", format="%d"),
            "error_count": st.column_config.NumberColumn("Errors", format="%d"),
            "p95_ms": st.column_config.NumberColumn("p95 response", format="%.0f ms"),
            "duration_min": st.column_config.NumberColumn("Duration", format="%.1f min"),
        })

    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
    with panel("speed", "Site response time per run", "95th percentile of page load times. Lower is faster."):
        speed = runs.dropna(subset=["p95_ms"]).copy()
        speed["run"] = speed["started_at"].dt.tz_convert("Europe/Lisbon").dt.strftime("%-d %b %H:%M")
        speed = speed.iloc[::-1]
        chart = alt.Chart(speed).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6, color=ACCENT, size=36).encode(
            x=alt.X("run:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("p95_ms:Q", title="ms", axis=alt.Axis(tickCount=4)),
            tooltip=[alt.Tooltip("run:N", title="Run"), alt.Tooltip("p95_ms:Q", title="p95 (ms)", format=".0f"),
                     alt.Tooltip("requests:Q", title="Requests")])
        st.altair_chart(style_chart(chart, height=220), use_container_width=True, theme=None)


# ---------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------

def main() -> None:
    st.markdown(f"<style>{(HERE / 'style.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)
    try:
        with st.spinner("Loading books…"):
            books, runs = load_books(), load_runs()
    except data.DataError as exc:
        st.error(f"Could not load data from Supabase. {exc}")
        st.stop()

    if books.empty:
        st.info("No books in the database yet. Run the scraper with --load first.")
        st.stop()

    hero(books, runs)
    tab_browse, tab_insights, tab_pipeline = st.tabs(["Browse", "Insights", "Pipeline"])
    with tab_browse:
        browse(books)
    with tab_insights:
        insights(books)
    with tab_pipeline:
        pipeline(runs)

    st.markdown('<div class="foot">Data from <a href="https://books.toscrape.com" target="_blank">books.toscrape.com</a>, '
                'a sandbox for practising web scraping. Collected by a Python scraper, stored in Supabase, '
                'read-only in this app.</div>', unsafe_allow_html=True)


main()
