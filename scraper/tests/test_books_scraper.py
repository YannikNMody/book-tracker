"""
Tests for books_scraper.py. No internet needed: a local HTTP server serves
pages with the same HTML structure as books.toscrape.com.

Run from the scraper/ folder:  python -m pytest -q
"""

import json
import sys
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import books_scraper as bs  # noqa: E402


# ---------------------------------------------------------------------
# HTML fixtures mirroring the real site's markup
# ---------------------------------------------------------------------

HEAD = '<html><head><meta http-equiv="content-type" content="text/html; charset=UTF-8" /></head><body>'


def listing_html(items, next_href=None):
    """items: (href, title, price, rating_word)"""
    articles = "".join(
        f"""<li><article class="product_pod">
              <div class="image_container"><a href="{href}"><img src="x.jpg" class="thumbnail"></a></div>
              <p class="star-rating {rating}"><i class="icon-star"></i></p>
              <h3><a href="{href}" title="{title}">{title[:20]}...</a></h3>
              <div class="product_price"><p class="price_color">£{price}</p>
                <p class="instock availability"><i class="icon-ok"></i> In stock</p></div>
            </article></li>"""
        for href, title, price, rating in items
    )
    pager = '<ul class="pager"><li class="current">Page x</li>'
    if next_href:
        pager += f'<li class="next"><a href="{next_href}">next</a></li>'
    pager += "</ul>"
    return f"{HEAD}<ol class='row'>{articles}</ol>{pager}</body></html>"


def product_html(title, upc, price, availability="In stock (22 available)", category="Poetry",
                 rating="Three", description="It's hard to imagine a world without this book. ...more",
                 reviews="0"):
    return f"""{HEAD}
    <ul class="breadcrumb">
      <li><a href="../../index.html">Home</a></li>
      <li><a href="../category/books_1/index.html">Books</a></li>
      <li><a href="../category/books/poetry_23/index.html">{category}</a></li>
      <li class="active">{title}</li>
    </ul>
    <article class="product_page"><div class="row">
      <div class="col-sm-6"><div id="product_gallery" class="carousel"><div class="thumbnail">
        <div class="carousel-inner"><div class="item active">
          <img src="../../media/cache/fe/72/{upc}.jpg" alt="{title}" />
        </div></div></div></div></div>
      <div class="col-sm-6 product_main">
        <h1>{title}</h1>
        <p class="price_color">£{price}</p>
        <p class="instock availability"><i class="icon-ok"></i> {availability}</p>
        <p class="star-rating {rating}"><i class="icon-star"></i></p>
      </div></div>
      <div id="product_description" class="sub-header"><h2>Product Description</h2></div>
      <p>{description}</p>
      <div class="sub-header"><h2>Product Information</h2></div>
      <table class="table table-striped">
        <tr><th>UPC</th><td>{upc}</td></tr>
        <tr><th>Product Type</th><td>Books</td></tr>
        <tr><th>Price (excl. tax)</th><td>£{price}</td></tr>
        <tr><th>Price (incl. tax)</th><td>£{price}</td></tr>
        <tr><th>Tax</th><td>£0.00</td></tr>
        <tr><th>Availability</th><td>{availability}</td></tr>
        <tr><th>Number of reviews</th><td>{reviews}</td></tr>
      </table>
    </article></body></html>"""


def soup(html):
    return BeautifulSoup(html.encode("utf-8"), bs.HTML_PARSER)


# ---------------------------------------------------------------------
# Local mock site
# ---------------------------------------------------------------------

class MockSite:
    """Serves {path: html} as UTF-8 bytes with NO charset header, like the real site.
    flaky: {path: n} returns 503 for the first n requests to that path."""

    def __init__(self, pages, flaky=None):
        self.pages, self.flaky = pages, dict(flaky or {})
        self.hits: dict[str, int] = {}
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.lstrip("/")
                site.hits[path] = site.hits.get(path, 0) + 1
                if site.flaky.get(path, 0) > 0:
                    site.flaky[path] -= 1
                    self.send_response(503)
                    self.end_headers()
                    return
                if path not in site.pages:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = site.pages[path].encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def two_page_site(broken=False, duplicate_upc=False):
    """page-1: books 1-2, page-2: books 3(-5). Catalogue pages use links relative to /catalogue/."""
    pages = {
        "catalogue/page-1.html": listing_html(
            [("book-one_1/index.html", "Book One", "51.77", "Three"),
             ("book-two_2/index.html", "Book Two", "1,053.74", "Five")],
            next_href="page-2.html"),
        "catalogue/book-one_1/index.html": product_html("Book One", "upc001", "51.77"),
        "catalogue/book-two_2/index.html": product_html("Book Two", "upc002", "1,053.74", rating="Five"),
        "catalogue/book-three_3/index.html": product_html("Book Three", "upc003", "10.00",
                                                           availability="Out of stock"),
    }
    page_two = [("book-three_3/index.html", "Book Three", "10.00", "One")]
    if broken:
        page_two.append(("missing-book_4/index.html", "Missing", "9.99", "Two"))
    if duplicate_upc:
        page_two.append(("book-one-reprint_5/index.html", "Book One Reprint", "51.77", "Three"))
        pages["catalogue/book-one-reprint_5/index.html"] = product_html("Book One Reprint", "upc001", "51.77")
    pages["catalogue/page-2.html"] = listing_html(page_two)
    return pages


def fast_config(base_url, **overrides):
    defaults = dict(base_url=base_url, delay=0, workers=4, timeout=5, max_retries=2, backoff=0)
    defaults.update(overrides)
    return bs.ScrapeConfig(**defaults)


# ---------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("£51.77", (Decimal("51.77"), "GBP")),
    ("Â£51.77", (Decimal("51.77"), "GBP")),          # mis-decoded pound sign
    ("£1,053.74", (Decimal("1053.74"), "GBP")),
    ("$9", (Decimal("9.00"), "USD")),
    ("free", (None, None)),
    (None, (None, None)),
])
def test_parse_price(text, expected):
    assert bs.parse_price(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("In stock (22 available)", (True, 22)),
    ("\n   In stock (1 available)  ", (True, 1)),
    ("In stock", (True, None)),
    ("Out of stock", (False, 0)),
    ("Unknown", (None, None)),
    (None, (None, None)),
])
def test_parse_availability(text, expected):
    assert bs.parse_availability(text) == expected


def test_listing_links_resolve_from_home_page_and_catalogue_pages():
    home = soup(listing_html([("catalogue/a-light-in-the-attic_1000/index.html", "A", "1.00", "One")],
                             next_href="catalogue/page-2.html"))
    urls, nxt = bs.parse_listing_page(home, "https://books.toscrape.com/index.html")
    assert urls == ["https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"]
    assert nxt == "https://books.toscrape.com/catalogue/page-2.html"

    page2 = soup(listing_html([("a-light-in-the-attic_1000/index.html", "A", "1.00", "One")],
                              next_href="page-3.html"))
    urls, nxt = bs.parse_listing_page(page2, "https://books.toscrape.com/catalogue/page-2.html")
    assert urls == ["https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"]
    assert nxt == "https://books.toscrape.com/catalogue/page-3.html"


def test_last_listing_page_has_no_next():
    _, nxt = bs.parse_listing_page(soup(listing_html([])), "https://x/catalogue/page-50.html")
    assert nxt is None


def test_parse_product_page_all_fields():
    url = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
    row = bs.parse_product_page(soup(product_html("A Light in the Attic", "a897fe39b1053632", "51.77")), url)
    assert row == {
        "external_id": "a897fe39b1053632",
        "title": "A Light in the Attic",
        "category_name": "Poetry",
        "rating": 3,
        "description": "It's hard to imagine a world without this book.",
        "product_type": "Books",
        "num_reviews": 0,
        "image_url": "https://books.toscrape.com/media/cache/fe/72/a897fe39b1053632.jpg",
        "product_url": url,
        "price_excl_tax": Decimal("51.77"),
        "price_incl_tax": Decimal("51.77"),
        "currency": "GBP",
        "in_stock": True,
        "quantity": 22,
    }


def test_parse_product_page_keys_match_staging_table():
    sql = (Path(__file__).resolve().parents[2] / "supabase" / "migrations").glob("*_init_schema.sql")
    schema = next(sql).read_text()
    block = schema.split("create table public.staging_observations")[1].split(");")[0]
    columns = {line.split()[0] for line in block.splitlines()[1:] if line.strip() and not line.strip().startswith(("unique", "--"))}
    row = bs.parse_product_page(soup(product_html("T", "u1", "1.00")), "https://x/catalogue/t/index.html")
    row["scraped_at"] = "now"
    assert set(row) == columns - {"id", "run_id"}


def test_parse_product_page_missing_upc_raises():
    html = product_html("T", "", "1.00")
    with pytest.raises(bs.ParseError, match="UPC"):
        bs.parse_product_page(soup(html), "https://x/")


def test_parse_product_page_wrong_layout_raises():
    with pytest.raises(bs.ParseError, match="product_main"):
        bs.parse_product_page(soup(HEAD + "<p>maintenance</p></body></html>"), "https://x/")


def test_guess_next_page():
    assert bs.guess_next_page("https://x/catalogue/page-9.html") == "https://x/catalogue/page-10.html"
    assert bs.guess_next_page("https://x/index.html") is None


# ---------------------------------------------------------------------
# End-to-end against the mock site
# ---------------------------------------------------------------------

def test_full_run_success():
    with MockSite(two_page_site()) as site:
        result = bs.scrape(fast_config(site.base_url))
    run = result["run"]
    assert run["status"] == "success"
    assert run["is_full_run"] is True
    assert run["pages_scraped"] == 2
    assert [o["external_id"] for o in result["observations"]] == ["upc001", "upc002", "upc003"]
    assert result["observations"][1]["price_incl_tax"] == Decimal("1053.74")
    assert result["observations"][2]["in_stock"] is False
    assert result["errors"] == []


def test_retries_recover_from_temporary_503():
    with MockSite(two_page_site(), flaky={"catalogue/book-two_2/index.html": 2}) as site:
        result = bs.scrape(fast_config(site.base_url, max_retries=2))
        hits = site.hits["catalogue/book-two_2/index.html"]
    assert hits == 3
    assert result["run"]["status"] == "success"
    assert len(result["observations"]) == 3
    assert [e["http_status"] for e in result["errors"]] == [503, 503]  # logged even though it recovered


def test_retries_give_up_and_mark_partial():
    with MockSite(two_page_site(), flaky={"catalogue/book-two_2/index.html": 99}) as site:
        result = bs.scrape(fast_config(site.base_url, max_retries=2))
    assert result["run"]["status"] == "partial"
    assert result["run"]["is_full_run"] is False
    assert len(result["errors"]) == 3  # first try + 2 retries


def test_404_is_logged_not_retried_and_run_is_partial():
    with MockSite(two_page_site(broken=True)) as site:
        result = bs.scrape(fast_config(site.base_url))
        hits = site.hits["catalogue/missing-book_4/index.html"]
    assert hits == 1
    assert result["run"]["status"] == "partial"
    assert result["run"]["is_full_run"] is False
    assert len(result["observations"]) == 3
    assert result["errors"][0]["http_status"] == 404


def test_duplicate_upc_is_kept_once():
    with MockSite(two_page_site(duplicate_upc=True)) as site:
        result = bs.scrape(fast_config(site.base_url))
    ids = [o["external_id"] for o in result["observations"]]
    assert ids.count("upc001") == 1
    assert result["run"]["stats"]["duplicates"] == 1


def test_limit_stops_early_and_is_not_a_full_run():
    with MockSite(two_page_site()) as site:
        result = bs.scrape(fast_config(site.base_url, limit=1))
        page_two_hits = site.hits.get("catalogue/page-2.html", 0)
    assert len(result["observations"]) == 1
    assert page_two_hits == 0
    assert result["run"]["status"] == "success"
    assert result["run"]["is_full_run"] is False


def test_pages_cap():
    with MockSite(two_page_site()) as site:
        result = bs.scrape(fast_config(site.base_url, pages=1))
    assert result["run"]["pages_scraped"] == 1
    assert len(result["observations"]) == 2
    assert result["run"]["is_full_run"] is False


def test_start_page_past_the_end_fails_cleanly():
    with MockSite(two_page_site()) as site:
        result = bs.scrape(fast_config(site.base_url, start_page=9))
    assert result["run"]["status"] == "failed"
    assert result["observations"] == []


def test_failed_listing_page_continues_with_guessed_next_page():
    with MockSite(two_page_site(), flaky={"catalogue/page-1.html": 99}) as site:
        result = bs.scrape(fast_config(site.base_url, max_retries=0))
    assert [o["external_id"] for o in result["observations"]] == ["upc003"]
    assert result["run"]["status"] == "partial"


def test_rate_limit_holds_across_parallel_workers():
    import time
    with MockSite(two_page_site()) as site:
        start = time.monotonic()
        result = bs.scrape(fast_config(site.base_url, delay=0.1, workers=4))
        elapsed = time.monotonic() - start
    assert result["run"]["stats"]["requests"] == 5
    assert elapsed >= 0.4  # 5 requests, 0.1 s apart, even with 4 workers


def test_rate_limiter_spacing():
    import time
    limiter = bs.RateLimiter(0.05)
    start = time.monotonic()
    for _ in range(5):
        limiter.wait()
    assert time.monotonic() - start >= 0.19


def test_cli_writes_json_and_returns_exit_code(tmp_path):
    out = tmp_path / "run.json"
    with MockSite(two_page_site(broken=True)) as site:
        code = bs.main(["--base-url", site.base_url, "--delay", "0", "--backoff", "0",
                        "--output", str(out), "-q"])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == bs.EXIT_PARTIAL
    assert set(data) == {"run", "observations", "errors"}
    assert data["observations"][0]["price_incl_tax"] == "51.77"   # exact string, not float
    assert data["run"]["config"]["scraper_version"] == bs.SCRAPER_VERSION
