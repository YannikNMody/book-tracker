#!/usr/bin/env python3
"""
books_scraper.py: scrape https://books.toscrape.com into a JSON file.

Standalone: no database involved. The output is shaped to match the
Supabase schema so that step 4 only has to load it:

    output["run"]            -> one row in  scrape_runs
    output["observations"]   -> rows in     staging_observations
    output["errors"]         -> rows in     scrape_errors

Examples
    python books_scraper.py --limit 5            # first 5 books
    python books_scraper.py --pages 3            # first 3 listing pages (60 books)
    python books_scraper.py                      # whole catalogue
    python books_scraper.py --delay 0 --workers 16   # stress test (deliberately impolite)
    python books_scraper.py --load               # scrape, save JSON, then load into the database

Exit codes: 0 = success, 3 = partial (some pages failed), 1 = failed (no data),
            4 = scraped and saved, but loading into the database failed
                (retry later with: python loader.py <the saved file>).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:  # lxml is faster; html.parser ships with Python
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

SCRAPER_VERSION = "0.2.0"
DEFAULT_BASE_URL = "https://books.toscrape.com/"
USER_AGENT = f"BookTrackerBot/{SCRAPER_VERSION} (student project; python-requests)"

RATING_WORDS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR"}
PRICE_RE = re.compile(r"([£$€])\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
QUANTITY_RE = re.compile(r"\((\d+)\s+available\)")
PAGE_NUMBER_RE = re.compile(r"page-(\d+)\.html$")
DESCRIPTION_SUFFIX_RE = re.compile(r"\s*\.\.\.\s*more$")

EXIT_SUCCESS, EXIT_FAILED, EXIT_PARTIAL, EXIT_LOAD_FAILED = 0, 1, 3, 4

log = logging.getLogger("books_scraper")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =====================================================================
# Parsing: pure functions, no network. These are what breaks if the
# site's HTML changes, so they are covered by tests.
# =====================================================================

class ParseError(Exception):
    """The page was fetched but did not contain what we expected."""


def parse_price(text: Optional[str]) -> tuple[Optional[Decimal], Optional[str]]:
    """'£51.77' -> (Decimal('51.77'), 'GBP'). Also survives mis-decoded 'Â£51.77'."""
    if not text:
        return None, None
    match = PRICE_RE.search(text)
    if not match:
        return None, None
    symbol, number = match.groups()
    try:
        value = Decimal(number.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None, None
    return value, CURRENCY_SYMBOLS.get(symbol)


def parse_availability(text: Optional[str]) -> tuple[Optional[bool], Optional[int]]:
    """'In stock (22 available)' -> (True, 22); 'Out of stock' -> (False, 0)."""
    if not text:
        return None, None
    normalized = " ".join(text.split()).lower()
    if "out of stock" in normalized:
        return False, 0
    if "in stock" in normalized:
        match = QUANTITY_RE.search(normalized)
        return True, int(match.group(1)) if match else None
    return None, None


def parse_rating(tag) -> Optional[int]:
    """<p class="star-rating Three"> -> 3"""
    if tag is None:
        return None
    for css_class in tag.get("class", []):
        if css_class in RATING_WORDS:
            return RATING_WORDS[css_class]
    return None


def parse_listing_page(soup: BeautifulSoup, page_url: str) -> tuple[list[str], Optional[str]]:
    """Return (absolute product URLs, absolute next-page URL or None).

    Links are relative and differ between the home page and catalogue pages,
    so they are always resolved against the URL of the page they came from.
    """
    product_urls = [
        urljoin(page_url, a["href"])
        for a in soup.select("article.product_pod h3 a[href]")
    ]
    next_link = soup.select_one("ul.pager li.next a[href]")
    next_url = urljoin(page_url, next_link["href"]) if next_link else None
    return product_urls, next_url


def parse_product_page(soup: BeautifulSoup, url: str) -> dict:
    """Return one row shaped like the staging_observations table."""
    main = soup.select_one("div.product_main")
    if main is None:
        raise ParseError("product_main block not found (layout changed?)")

    title_tag = main.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else None

    info: dict[str, str] = {}
    for row in soup.select("table.table-striped tr"):
        header, cell = row.find("th"), row.find("td")
        if header and cell:
            info[header.get_text(strip=True)] = cell.get_text(" ", strip=True)

    external_id = info.get("UPC")
    if not title:
        raise ParseError("title missing")
    if not external_id:
        raise ParseError("UPC missing")

    price_excl, currency_excl = parse_price(info.get("Price (excl. tax)"))
    price_incl, currency_incl = parse_price(info.get("Price (incl. tax)"))
    if price_incl is None:  # fall back to the big price under the title
        price_tag = main.select_one("p.price_color")
        price_incl, currency_incl = parse_price(price_tag.get_text() if price_tag else None)
    if price_incl is None:
        raise ParseError("price missing")

    availability_text = info.get("Availability")
    if availability_text is None:
        availability_tag = main.select_one("p.availability")
        availability_text = availability_tag.get_text() if availability_tag else None
    in_stock, quantity = parse_availability(availability_text)

    crumbs = [li.get_text(strip=True) for li in soup.select("ul.breadcrumb li")]
    category = crumbs[-2] if len(crumbs) >= 3 else None  # Home > Books > Category > Title

    description = None
    anchor = soup.select_one("#product_description")
    if anchor is not None:
        paragraph = anchor.find_next_sibling("p")
        if paragraph is not None:
            description = DESCRIPTION_SUFFIX_RE.sub("", paragraph.get_text(" ", strip=True)) or None

    image = soup.select_one("#product_gallery img[src]")
    image_url = urljoin(url, image["src"]) if image is not None else None

    reviews = info.get("Number of reviews")
    num_reviews = int(reviews) if reviews and reviews.isdigit() else None

    # Key order mirrors the staging_observations columns
    return {
        "external_id": external_id,
        "title": title,
        "category_name": category,
        "rating": parse_rating(main.select_one("p.star-rating")),
        "description": description,
        "product_type": info.get("Product Type"),
        "num_reviews": num_reviews,
        "image_url": image_url,
        "product_url": url,
        "price_excl_tax": price_excl,
        "price_incl_tax": price_incl,
        "currency": currency_incl or currency_excl,
        "in_stock": in_stock,
        "quantity": quantity,
    }


# =====================================================================
# Fetching: rate limit, retries with backoff, error log, request stats
# =====================================================================

class FetchFailed(Exception):
    def __init__(self, url: str, status: Optional[int]):
        super().__init__(f"{url} failed (status={status})")
        self.url, self.status = url, status


class RateLimiter:
    """At least `interval` seconds between request starts, across all threads."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self.interval
        if delay > 0:
            time.sleep(delay)


class Fetcher:
    RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(self, *, delay: float, timeout: float, max_retries: int, backoff: float):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.limiter = RateLimiter(delay)
        self._local = threading.local()  # one HTTP session per thread
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()
        self.errors: list[dict] = []
        self.durations_ms: list[float] = []
        self.status_counts: dict[str, int] = {}

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html"})
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def record_error(self, url, http_status, error_type, message, attempt) -> None:
        entry = {
            "url": url,
            "http_status": http_status,
            "error_type": error_type,
            "message": (message or "")[:500],
            "attempt": attempt,
            "occurred_at": utcnow(),
        }
        with self._lock:
            self.errors.append(entry)
        log.warning("%s %s (attempt %d): %s", error_type, url, attempt, entry["message"])

    def _record_stat(self, key: str, started: float) -> None:
        with self._lock:
            self.status_counts[key] = self.status_counts.get(key, 0) + 1
            self.durations_ms.append((time.monotonic() - started) * 1000)

    def get(self, url: str) -> tuple[BeautifulSoup, str]:
        """Return (parsed page, final URL) or raise FetchFailed after retries."""
        attempts = self.max_retries + 1
        last_status = None
        for attempt in range(1, attempts + 1):
            self.limiter.wait()
            started = time.monotonic()
            try:
                response = self._session().get(url, timeout=self.timeout)
            except requests.Timeout as exc:
                self._record_stat("timeout", started)
                self.record_error(url, None, "timeout", str(exc), attempt)
                last_status = None
            except requests.ConnectionError as exc:
                self._record_stat("connection_error", started)
                self.record_error(url, None, "connection_error", str(exc), attempt)
                last_status = None
            except requests.RequestException as exc:
                self._record_stat("request_error", started)
                self.record_error(url, None, "request_error", str(exc), attempt)
                last_status = None
            else:
                last_status = response.status_code
                self._record_stat(str(response.status_code), started)
                if response.status_code == 200:
                    # Pass bytes, not response.text: the site sends no charset header,
                    # so requests would guess Latin-1 and turn £ into Â£. BeautifulSoup
                    # reads the <meta charset> instead.
                    return BeautifulSoup(response.content, HTML_PARSER), response.url
                self.record_error(url, response.status_code, "http_error",
                                  f"HTTP {response.status_code} {response.reason}", attempt)
                if response.status_code not in self.RETRY_STATUSES:
                    break  # 404 etc.: retrying will not help
            if attempt < attempts:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
        raise FetchFailed(url, last_status)

    def stats(self) -> dict:
        durations = sorted(self.durations_ms)

        def percentile(p: float) -> Optional[float]:
            if not durations:
                return None
            return round(durations[min(len(durations) - 1, math.ceil(p * len(durations)) - 1)], 1)

        return {
            "requests": len(durations),
            "status_counts": dict(sorted(self.status_counts.items())),
            "avg_response_ms": round(sum(durations) / len(durations), 1) if durations else None,
            "p50_response_ms": percentile(0.50),
            "p95_response_ms": percentile(0.95),
            "max_response_ms": round(durations[-1], 1) if durations else None,
        }

    def close(self) -> None:
        for session in self._sessions:
            session.close()


# =====================================================================
# Orchestration
# =====================================================================

@dataclass
class ScrapeConfig:
    base_url: str = DEFAULT_BASE_URL
    limit: Optional[int] = None        # max books
    pages: Optional[int] = None        # max listing pages
    start_page: int = 1
    delay: float = 0.5                 # seconds between requests (global)
    workers: int = 4                   # parallel product-page fetches
    timeout: float = 15.0
    max_retries: int = 3
    backoff: float = 1.0               # 1s, 2s, 4s, ...


def listing_page_url(base_url: str, page: int) -> str:
    return urljoin(base_url, f"catalogue/page-{page}.html")


def guess_next_page(url: str) -> Optional[str]:
    """If a listing page fails we lose its 'next' link; try page-(n+1) instead."""
    match = PAGE_NUMBER_RE.search(url)
    if not match:
        return None
    return url[: match.start(1)] + str(int(match.group(1)) + 1) + url[match.end(1):]


def scrape(cfg: ScrapeConfig) -> dict:
    started_at = utcnow()
    fetcher = Fetcher(delay=cfg.delay, timeout=cfg.timeout,
                      max_retries=cfg.max_retries, backoff=cfg.backoff)

    listed_urls: list[str] = []
    seen_urls: set[str] = set()
    failed_urls: list[str] = []
    futures = []
    pages_attempted = pages_scraped = 0
    reached_end = interrupted = False

    def scrape_product(url: str) -> tuple[str, Optional[dict]]:
        try:
            soup, final_url = fetcher.get(url)
            row = parse_product_page(soup, final_url)
            row["scraped_at"] = utcnow()
            return url, row
        except FetchFailed:
            return url, None
        except ParseError as exc:
            fetcher.record_error(url, 200, "parse_error", str(exc), 1)
            return url, None
        except Exception as exc:  # never let one bad page kill the run
            fetcher.record_error(url, None, "unexpected_error", repr(exc), 1)
            return url, None

    def limit_reached() -> bool:
        return cfg.limit is not None and len(listed_urls) >= cfg.limit

    executor = ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="product")
    try:
        url: Optional[str] = listing_page_url(cfg.base_url, cfg.start_page)
        consecutive_failures = 0
        while url and not limit_reached():
            if cfg.pages is not None and pages_attempted >= cfg.pages:
                break
            pages_attempted += 1
            try:
                soup, final_url = fetcher.get(url)
            except FetchFailed as exc:
                failed_urls.append(url)
                if exc.status == 404:
                    log.error("Listing page not found: %s; stopping.", url)
                    break
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    log.error("3 listing pages failed in a row; stopping.")
                    break
                url = guess_next_page(url)
                continue
            consecutive_failures = 0
            pages_scraped += 1

            product_urls, next_url = parse_listing_page(soup, final_url)
            if not product_urls:
                log.warning("No products found on %s (layout changed?)", final_url)
            for product_url in product_urls:
                if limit_reached():
                    break
                if product_url in seen_urls:
                    continue
                seen_urls.add(product_url)
                listed_urls.append(product_url)
                futures.append(executor.submit(scrape_product, product_url))
            log.info("Listing page %d done: %d books queued so far", pages_attempted, len(listed_urls))

            if next_url is None:
                reached_end = True
            url = next_url

        wait(futures)
    except KeyboardInterrupt:
        interrupted = True
        log.warning("Interrupted: cancelling queued pages and saving what was scraped.")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        fetcher.close()

    # Collect results in catalogue order; de-duplicate on the site's identifier
    observations: dict[str, dict] = {}
    duplicates = not_scraped = 0
    for future in futures:
        if future.cancelled() or not future.done():
            not_scraped += 1
            continue
        url, row = future.result()
        if row is None:
            failed_urls.append(url)
        elif row["external_id"] in observations:
            duplicates += 1
            log.warning("Duplicate UPC %s at %s (kept first)", row["external_id"], url)
        else:
            observations[row["external_id"]] = row

    complete = not failed_urls and not interrupted and not_scraped == 0
    if not observations:
        status = "failed"
    elif complete:
        status = "success"
    else:
        status = "partial"

    # Only a complete, unrestricted pass may later be used to mark books as delisted
    is_full_run = (status == "success" and reached_end and cfg.start_page == 1
                   and cfg.limit is None and cfg.pages is None)

    stats = fetcher.stats()
    stats.update({
        "listed_urls": len(listed_urls),
        "failed_urls": len(failed_urls),
        "not_scraped": not_scraped,
        "duplicates": duplicates,
        "reached_end": reached_end,
        "interrupted": interrupted,
    })

    run = {
        "run_uuid": str(uuid.uuid4()),  # lets the loader recognise a file it already loaded
        "source_base_url": cfg.base_url,
        "started_at": started_at,
        "finished_at": utcnow(),
        "status": status,
        "is_full_run": is_full_run,
        "config": {**asdict(cfg), "scraper_version": SCRAPER_VERSION},
        "pages_scraped": pages_scraped,
        "books_seen": len(observations),
        "error_count": len(fetcher.errors),
        "stats": stats,
    }
    return {"run": run, "observations": list(observations.values()), "errors": fetcher.errors}


# =====================================================================
# CLI
# =====================================================================

def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape books.toscrape.com into a JSON file shaped for the Supabase schema.")
    parser.add_argument("--limit", type=_positive_int, help="stop after N books")
    parser.add_argument("--pages", type=_positive_int, help="stop after N listing pages (20 books each)")
    parser.add_argument("--start-page", type=_positive_int, default=1, help="listing page to start from (default 1)")
    parser.add_argument("--delay", type=_non_negative_float, default=0.5,
                        help="seconds between requests across all workers (default 0.5)")
    parser.add_argument("--workers", type=_positive_int, default=4, help="parallel product-page fetches (default 4)")
    parser.add_argument("--timeout", type=_non_negative_float, default=15.0, help="request timeout in seconds (default 15)")
    parser.add_argument("--max-retries", type=int, default=3, help="retries per page on errors (default 3)")
    parser.add_argument("--backoff", type=_non_negative_float, default=1.0,
                        help="first retry wait in seconds, doubles each retry (default 1)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)  # for tests
    parser.add_argument("--output", type=Path, help="output JSON path (default output/run_<timestamp>.json)")
    parser.add_argument("--load", action="store_true",
                        help="after saving, load the run into the database (needs DATABASE_URL in .env)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    return parser


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)  # keep exact cents; Postgres numeric accepts strings
    raise TypeError(f"not JSON serialisable: {type(value)}")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    if args.max_retries < 0:
        log.error("--max-retries must be >= 0")
        return EXIT_FAILED
    if args.delay < 0.2:
        log.warning("Delay %.2fs: this is stress-test territory, not polite scraping.", args.delay)

    cfg = ScrapeConfig(
        base_url=args.base_url if args.base_url.endswith("/") else args.base_url + "/",
        limit=args.limit, pages=args.pages, start_page=args.start_page,
        delay=args.delay, workers=args.workers, timeout=args.timeout,
        max_retries=args.max_retries, backoff=args.backoff,
    )
    result = scrape(cfg)

    output = args.output or Path("output") / f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")

    run, stats = result["run"], result["run"]["stats"]
    log.info("Status: %s | books: %d | pages: %d | errors logged: %d | requests: %d | p95: %s ms | full run: %s",
             run["status"], run["books_seen"], run["pages_scraped"], run["error_count"],
             stats["requests"], stats["p95_response_ms"], run["is_full_run"])
    log.info("Saved to %s", output)

    if args.load:
        import psycopg
        import loader
        loader.load_dotenv_if_available()
        try:
            with loader.connect() as conn:
                summary = loader.load_result(conn, result, label=output.name)
            log.info(loader.format_summary(output.name, summary))
        except (loader.LoadError, psycopg.Error) as exc:
            log.error("Loading failed: %s", exc)
            log.error("The scrape is saved. Retry with: python loader.py %s", output)
            return EXIT_LOAD_FAILED

    return {"success": EXIT_SUCCESS, "partial": EXIT_PARTIAL}.get(run["status"], EXIT_FAILED)


if __name__ == "__main__":
    sys.exit(main())
