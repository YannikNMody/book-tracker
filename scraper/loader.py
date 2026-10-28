#!/usr/bin/env python3
"""
loader.py: load scraper output (JSON) into the Supabase database.

Each run is loaded in ONE database transaction:
    scrape_runs row -> staging_observations (bulk COPY) -> scrape_errors
    -> apply_run() merges into categories / books / price_history / stock_history
Either all of it lands or none of it does.

Examples
    python loader.py output/run_20261028T124756Z.json
    python loader.py output/*.json                # several files, applied oldest first
    python loader.py output/run_x.json --dry-run  # do everything, then roll back

Connection: DATABASE_URL in a .env file (see .env.example in the repo root).

Exit codes: 0 = loaded (or already loaded), 1 = error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger("loader")

# Must match the staging_observations columns (minus id, run_id)
STAGING_COLUMNS = (
    "external_id", "title", "category_name", "rating", "description", "product_type",
    "num_reviews", "image_url", "product_url", "price_excl_tax", "price_incl_tax",
    "currency", "in_stock", "quantity", "scraped_at",
)
ERROR_COLUMNS = ("url", "http_status", "error_type", "message", "attempt", "occurred_at")
LOCAL_HOST_HINTS = ("localhost", "127.0.0.1", "host=/", "%2F")


class LoadError(Exception):
    """A problem with the input file or the target database, explained for humans."""


def run_uuid_for(run: dict) -> uuid.UUID:
    """The scraper stamps every run with a UUID. Files from scraper < 0.2.0 have none,
    so derive a stable one: loading the same old file twice still gives the same UUID."""
    if run.get("run_uuid"):
        return uuid.UUID(run["run_uuid"])
    key = f'{run.get("source_base_url")}|{run.get("started_at")}|{run.get("finished_at")}'
    return uuid.uuid5(uuid.NAMESPACE_URL, key)


def validate(result: dict, label: str) -> None:
    for key in ("run", "observations", "errors"):
        if key not in result:
            raise LoadError(f"{label}: missing '{key}', is this a scraper output file?")
    for key in ("source_base_url", "started_at", "status"):
        if key not in result["run"]:
            raise LoadError(f"{label}: run.{key} missing")


def _ts(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def connect(database_url: Optional[str] = None) -> psycopg.Connection:
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise LoadError("DATABASE_URL is not set. Copy .env.example to .env in the repo root and fill it in.")
    if "[YOUR-PASSWORD]" in url or "YOUR-PASSWORD" in url:
        raise LoadError("DATABASE_URL still contains the password placeholder.")
    options = {"connect_timeout": 15, "application_name": "book-tracker-loader",
               # Supabase's transaction pooler (port 6543) can't use prepared statements
               "prepare_threshold": None}
    if "sslmode" not in url and not any(hint in url for hint in LOCAL_HOST_HINTS):
        options["sslmode"] = "require"
    try:
        return psycopg.connect(url, **options)
    except psycopg.OperationalError as exc:
        raise LoadError(f"Could not connect to the database: {str(exc).strip()}") from exc


def load_result(conn: psycopg.Connection, result: dict, *, dry_run: bool = False,
                label: str = "<result>") -> dict:
    """Load one scraper result. Returns a summary dict."""
    validate(result, label)
    run = result["run"]
    run_uuid = run_uuid_for(run)
    summary: dict = {}

    with conn.transaction() as tx:
        with conn.cursor() as cur:
            cur.execute("select id from public.sources where base_url = %s", (run["source_base_url"],))
            row = cur.fetchone()
            if row is None:
                raise LoadError(f"{label}: unknown source {run['source_base_url']!r}; add it to the sources table first.")
            source_id = row[0]

            cur.execute(
                """
                insert into public.scrape_runs
                    (run_uuid, source_id, started_at, finished_at, status, is_full_run,
                     config, pages_scraped, books_seen, error_count, stats)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (run_uuid) do nothing
                returning id
                """,
                (run_uuid, source_id, _ts(run["started_at"]), _ts(run.get("finished_at")),
                 run["status"], bool(run.get("is_full_run")), Jsonb(run.get("config", {})),
                 run.get("pages_scraped", 0), run.get("books_seen", 0),
                 run.get("error_count", 0), Jsonb(run.get("stats", {}))),
            )
            row = cur.fetchone()
            if row is None:
                return {"status": "skipped", "reason": "already loaded", "run_uuid": str(run_uuid)}
            run_id = row[0]

            columns = ", ".join(STAGING_COLUMNS)
            with cur.copy(f"copy public.staging_observations (run_id, {columns}) from stdin") as copy:
                for obs in result["observations"]:
                    copy.write_row((run_id, *(obs.get(c) for c in STAGING_COLUMNS)))

            if result["errors"]:
                cur.executemany(
                    f"insert into public.scrape_errors (run_id, {', '.join(ERROR_COLUMNS)}) "
                    f"values (%s, {', '.join(['%s'] * len(ERROR_COLUMNS))})",
                    [(run_id, *(err.get(c) for c in ERROR_COLUMNS)) for err in result["errors"]],
                )

            cur.execute("select public.apply_run(%s)", (run_id,))
            summary = dict(cur.fetchone()[0])
            summary.update(status="loaded", run_id=run_id, run_uuid=str(run_uuid),
                           scrape_status=run["status"], errors_stored=len(result["errors"]))

            if dry_run:
                summary["status"] = "dry_run_rolled_back"
                raise psycopg.Rollback(tx)

    return summary


def format_summary(name: str, summary: dict) -> str:
    if summary["status"] == "skipped":
        return f"{name}: already loaded, skipped"
    prefix = "DRY RUN (rolled back) " if summary["status"] == "dry_run_rolled_back" else ""
    return (f"{prefix}{name}: run {summary['run_id']} | books seen {summary['books_seen']}, "
            f"new {summary['books_new']} | price changes {summary['price_changes']} "
            f"(+{summary['prices_first_recorded']} first) | stock changes {summary['stock_changes']} "
            f"(+{summary['stock_first_recorded']} first) | delisted {summary['books_delisted']} "
            f"({summary['delisting']}) | errors stored {summary['errors_stored']}")


def load_dotenv_if_available() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    load_dotenv(find_dotenv(usecwd=True))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Load scraper JSON output into the database.")
    parser.add_argument("files", nargs="+", type=Path, help="scraper output file(s)")
    parser.add_argument("--dry-run", action="store_true", help="run everything, then roll back")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    load_dotenv_if_available()

    try:
        results = []
        for path in args.files:
            try:
                results.append((path, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError) as exc:
                raise LoadError(f"{path}: cannot read JSON ({exc})") from exc
        # History only makes sense oldest-first
        results.sort(key=lambda item: _ts(item[1].get("run", {}).get("started_at")) or datetime.min)

        with connect() as conn:
            for path, result in results:
                summary = load_result(conn, result, dry_run=args.dry_run, label=path.name)
                log.info(format_summary(path.name, summary))
    except LoadError as exc:
        log.error("%s", exc)
        return 1
    except psycopg.Error as exc:
        message = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
        log.error("Database error: %s (nothing from this file was saved)", message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
