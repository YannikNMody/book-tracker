"""
Tests for loader.py and the apply_run() database function.

These need a DISPOSABLE Postgres database: every test wipes the public schema
and re-applies all migrations. They are skipped unless TEST_DATABASE_URL is set,
and refuse to run against anything that looks like a Supabase host.

    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/booktest python -m pytest -q
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import books_scraper  # noqa: E402
import loader  # noqa: E402

TEST_DB = os.environ.get("TEST_DATABASE_URL")
MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "supabase" / "migrations").glob("*.sql"))
SOURCE = "https://books.toscrape.com/"

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set (needs a disposable Postgres)")

SUPABASE_LIKE_SETUP = """
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
end $$;
drop schema if exists public cascade;
create schema public;
grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
"""


@pytest.fixture
def conn():
    assert "supabase" not in TEST_DB, "refusing to wipe a Supabase database"
    with psycopg.connect(TEST_DB, autocommit=True) as setup:
        setup.execute(SUPABASE_LIKE_SETUP)
        for migration in MIGRATIONS:
            setup.execute(migration.read_text(encoding="utf-8"))
    with psycopg.connect(TEST_DB) as connection:
        yield connection


# ---------------------------------------------------------------------
# Helpers: build results shaped exactly like scraper output
# ---------------------------------------------------------------------

def obs(upc, price="10.00", in_stock=True, quantity=5, category="Poetry", title=None, **extra):
    row = {
        "external_id": upc, "title": title or f"Book {upc}", "category_name": category,
        "rating": 3, "description": "A book.", "product_type": "Books", "num_reviews": 0,
        "image_url": f"https://books.toscrape.com/media/{upc}.jpg",
        "product_url": f"https://books.toscrape.com/catalogue/{upc}/index.html",
        "price_excl_tax": price, "price_incl_tax": price, "currency": "GBP",
        "in_stock": in_stock, "quantity": quantity, "scraped_at": None,
    }
    row.update(extra)
    return row


def result(day, observations, *, full=True, status="success", errors=()):
    ts = f"2026-10-{day:02d}T10:00:00+00:00"
    for o in observations:
        o["scraped_at"] = o["scraped_at"] or ts
    return {
        "run": {
            "run_uuid": str(uuid.uuid4()), "source_base_url": SOURCE,
            "started_at": ts, "finished_at": f"2026-10-{day:02d}T10:10:00+00:00",
            "status": status, "is_full_run": full, "config": {"limit": None},
            "pages_scraped": 1, "books_seen": len(observations), "error_count": len(errors),
            "stats": {"requests": len(observations) + 1},
        },
        "observations": observations,
        "errors": list(errors),
    }


def q(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def one(conn, sql, *params):
    return q(conn, sql, *params)[0][0]


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

@needs_db
def test_first_load_creates_books_categories_prices_stock(conn):
    summary = loader.load_result(conn, result(1, [obs("a", "51.77"), obs("b", "1053.74", category="Travel")]))
    assert summary["status"] == "loaded"
    assert summary["books_new"] == 2
    assert summary["categories_new"] == 2
    assert summary["prices_first_recorded"] == 2 and summary["price_changes"] == 0
    assert summary["stock_first_recorded"] == 2 and summary["stock_changes"] == 0
    rows = q(conn, "select external_id, category, price::text, in_stock, quantity from books_current order by 1")
    assert rows == [("a", "Poetry", "51.77", True, 5), ("b", "Travel", "1053.74", True, 5)]
    assert one(conn, "select applied_at is not null from scrape_runs") is True


@needs_db
def test_loading_the_same_file_twice_is_a_no_op(conn):
    r = result(1, [obs("a")])
    loader.load_result(conn, r)
    again = loader.load_result(conn, r)
    assert again["status"] == "skipped"
    assert one(conn, "select count(*) from scrape_runs") == 1
    assert one(conn, "select count(*) from staging_observations") == 1


@needs_db
def test_old_files_without_run_uuid_are_still_deduplicated(conn):
    r = result(1, [obs("a")])
    del r["run"]["run_uuid"]
    loader.load_result(conn, json.loads(json.dumps(r)))
    assert loader.load_result(conn, json.loads(json.dumps(r)))["status"] == "skipped"


@needs_db
def test_unchanged_second_run_adds_no_history_rows(conn):
    loader.load_result(conn, result(1, [obs("a"), obs("b")]))
    summary = loader.load_result(conn, result(2, [obs("a"), obs("b")]))
    assert summary["books_new"] == 0
    assert summary["price_changes"] == summary["prices_first_recorded"] == 0
    assert summary["stock_changes"] == summary["stock_first_recorded"] == 0
    assert one(conn, "select count(*) from price_history") == 2
    assert one(conn, "select count(*) from stock_history") == 2
    assert one(conn, "select last_seen_at::date::text from books where external_id='a'") == "2026-10-02"


@needs_db
def test_price_change_closes_old_row_and_opens_new_one(conn):
    loader.load_result(conn, result(1, [obs("a", "10.00"), obs("b", "20.00")]))
    summary = loader.load_result(conn, result(5, [obs("a", "8.50"), obs("b", "20.00")]))
    assert summary["price_changes"] == 1
    history = q(conn, """select price_incl_tax::text, valid_from::date::text, valid_to::date::text
                         from price_history p join books b on b.id = p.book_id
                         where b.external_id = 'a' order by valid_from""")
    assert history == [("10.00", "2026-10-01", "2026-10-05"), ("8.50", "2026-10-05", None)]
    assert one(conn, "select price::text from books_current where external_id='a'") == "8.50"
    assert one(conn, "select price_changes from scrape_runs order by id desc limit 1") == 1


@needs_db
def test_price_going_back_to_an_old_value_is_a_new_period(conn):
    for day, price in [(1, "10.00"), (2, "12.00"), (3, "10.00")]:
        loader.load_result(conn, result(day, [obs("a", price)]))
    assert one(conn, "select count(*) from price_history") == 3
    assert one(conn, "select count(*) from price_history where valid_to is null") == 1


@needs_db
def test_stock_change_including_quantity_only(conn):
    loader.load_result(conn, result(1, [obs("a", quantity=22), obs("b", quantity=3)]))
    summary = loader.load_result(conn, result(2, [obs("a", quantity=21), obs("b", in_stock=False, quantity=0)]))
    assert summary["stock_changes"] == 2
    assert q(conn, "select external_id, in_stock, quantity from books_current order by 1") == [
        ("a", True, 21), ("b", False, 0)]


@needs_db
def test_runs_must_be_applied_in_chronological_order(conn):
    loader.load_result(conn, result(5, [obs("a", "9.00")]))
    with pytest.raises(psycopg.errors.RaiseException, match="chronological"):
        loader.load_result(conn, result(1, [obs("a", "10.00")]))
    conn.rollback()
    # the rejected run left nothing behind
    assert one(conn, "select count(*) from scrape_runs") == 1
    assert one(conn, "select count(*) from staging_observations") == 1


@needs_db
def test_full_run_delists_books_it_did_not_see(conn):
    loader.load_result(conn, result(1, [obs("a"), obs("b"), obs("c")]))
    summary = loader.load_result(conn, result(2, [obs("a"), obs("b")]))
    assert summary["books_delisted"] == 1 and summary["delisting"] == "applied"
    assert q(conn, "select external_id from books where not is_active") == [("c",)]


@needs_db
def test_partial_or_limited_runs_never_delist(conn):
    loader.load_result(conn, result(1, [obs("a"), obs("b"), obs("c")]))
    s1 = loader.load_result(conn, result(2, [obs("a")], full=False))
    s2 = loader.load_result(conn, result(3, [obs("a"), obs("b")], full=True, status="partial"))
    assert s1["delisting"] == "not_a_full_run" and s2["delisting"] == "not_a_full_run"
    assert one(conn, "select count(*) from books where is_active") == 3


@needs_db
def test_circuit_breaker_blocks_mass_delisting(conn):
    loader.load_result(conn, result(1, [obs(str(i)) for i in range(10)]))
    summary = loader.load_result(conn, result(2, [obs("0"), obs("1")]))  # 2 of 10 seen: layout broke?
    assert summary["delisting"] == "skipped_circuit_breaker"
    assert summary["books_delisted"] == 0
    assert one(conn, "select count(*) from books where is_active") == 10


@needs_db
def test_delisted_book_is_reactivated_when_it_reappears(conn):
    loader.load_result(conn, result(1, [obs("a"), obs("b"), obs("c")]))
    loader.load_result(conn, result(2, [obs("a"), obs("b")]))
    loader.load_result(conn, result(3, [obs("a"), obs("b"), obs("c")]))
    assert one(conn, "select is_active from books where external_id='c'") is True


@needs_db
def test_missing_values_do_not_wipe_known_data(conn):
    loader.load_result(conn, result(1, [obs("a")]))
    loader.load_result(conn, result(2, [obs("a", description=None, rating=None, category=None)]))
    assert q(conn, "select description, rating, category from books_current") == [("A book.", 3, "Poetry")]


@needs_db
def test_updated_at_only_moves_when_content_changes(conn):
    loader.load_result(conn, result(1, [obs("a")]))
    conn.commit()
    first = one(conn, "select updated_at from books")
    conn.commit()
    loader.load_result(conn, result(2, [obs("a")]))
    unchanged = one(conn, "select updated_at from books")
    conn.commit()
    loader.load_result(conn, result(3, [obs("a", title="New edition")]))
    changed = one(conn, "select updated_at from books")
    assert unchanged == first
    assert changed > first


@needs_db
def test_dry_run_leaves_the_database_untouched(conn):
    summary = loader.load_result(conn, result(1, [obs("a")]), dry_run=True)
    assert summary["status"] == "dry_run_rolled_back" and summary["books_new"] == 1
    assert one(conn, "select count(*) from scrape_runs") == 0
    assert one(conn, "select count(*) from books") == 0


@needs_db
def test_errors_are_stored_with_the_run(conn):
    err = {"url": "https://x/1", "http_status": 503, "error_type": "http_error",
           "message": "HTTP 503", "attempt": 1, "occurred_at": "2026-10-01T10:01:00+00:00"}
    loader.load_result(conn, result(1, [obs("a")], status="partial", errors=[err]))
    assert q(conn, "select http_status, error_type from scrape_errors") == [(503, "http_error")]


@needs_db
def test_unknown_source_is_rejected_cleanly(conn):
    r = result(1, [obs("a")])
    r["run"]["source_base_url"] = "https://other.example/"
    with pytest.raises(loader.LoadError, match="unknown source"):
        loader.load_result(conn, r)
    conn.rollback()
    assert one(conn, "select count(*) from scrape_runs") == 0


@needs_db
def test_frontend_roles_cannot_run_apply_run_or_write(conn):
    loader.load_result(conn, result(1, [obs("a")]))
    conn.commit()
    for role in ("anon", "authenticated"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"set local role {role}; select public.apply_run(1)")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"set local role {role}; update public.books set title = 'x'")
        conn.rollback()
    conn.execute("set local role anon")
    assert one(conn, "select count(*) from books_current") == 1


@needs_db
def test_end_to_end_scrape_mock_site_then_load(conn):
    from test_books_scraper import MockSite, fast_config, two_page_site
    with MockSite(two_page_site()) as site:
        scraped = books_scraper.scrape(fast_config(site.base_url))
    scraped["run"]["source_base_url"] = SOURCE  # mock server URL -> registered source
    as_json = json.loads(json.dumps(scraped, default=books_scraper._json_default))
    summary = loader.load_result(conn, as_json)
    assert summary["books_new"] == 3
    assert q(conn, "select external_id, price::text, in_stock from books_current order by 1") == [
        ("upc001", "51.77", True), ("upc002", "1053.74", True), ("upc003", "10.00", False)]
    assert one(conn, "select is_full_run from scrape_runs") is True
    assert one(conn, "select (stats->'apply'->>'books_new')::int from scrape_runs") == 3


@needs_db
def test_cli_loads_files_oldest_first(tmp_path, conn, monkeypatch):
    newer, older = result(5, [obs("a", "9.00")]), result(1, [obs("a", "10.00")])
    (tmp_path / "b_newer.json").write_text(json.dumps(newer))
    (tmp_path / "a_older.json").write_text(json.dumps(older))
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    code = loader.main([str(tmp_path / "b_newer.json"), str(tmp_path / "a_older.json"), "-q"])
    assert code == 0
    assert q(conn, """select price_incl_tax::text from price_history order by valid_from""") == [("10.00",), ("9.00",)]


def test_cli_reports_missing_database_url(tmp_path, monkeypatch):
    (tmp_path / "r.json").write_text(json.dumps(result(1, [obs("a")])))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(loader, "load_dotenv_if_available", lambda: None)
    assert loader.main([str(tmp_path / "r.json"), "-q"]) == 1


@needs_db
def test_scraper_load_flag(tmp_path, conn, monkeypatch):
    from test_books_scraper import MockSite, two_page_site
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    # register the mock server as a source so the run can be loaded
    with MockSite(two_page_site()) as site:
        conn.execute("insert into sources (name, base_url) values ('mock', %s)", (site.base_url,))
        conn.commit()
        code = books_scraper.main(["--base-url", site.base_url, "--delay", "0", "--backoff", "0",
                                   "--output", str(tmp_path / "run.json"), "--load", "-q"])
    assert code == books_scraper.EXIT_SUCCESS
    assert one(conn, "select count(*) from books") == 3


# ---------------------------------------------------------------------
# Connection string handling (no database needed)
# ---------------------------------------------------------------------

@pytest.mark.parametrize("password", ["a/b$c,d", "p@ss:w#rd?x", "plainPassword123", "50%off/now"])
def test_password_with_special_characters_is_encoded(password):
    url = f"postgresql://postgres.abc:{password}@aws-0-eu-west-3.pooler.supabase.com:5432/postgres"
    from psycopg.conninfo import conninfo_to_dict
    parsed = conninfo_to_dict(loader.normalize_database_url(url))
    assert parsed["password"] == password
    assert parsed["user"] == "postgres.abc"
    assert parsed["host"] == "aws-0-eu-west-3.pooler.supabase.com"
    assert parsed["dbname"] == "postgres"


def test_already_encoded_password_is_not_double_encoded():
    url = "postgresql://u:a%2Fb%24c@host:5432/db"
    from psycopg.conninfo import conninfo_to_dict
    assert conninfo_to_dict(loader.normalize_database_url(url))["password"] == "a/b$c"


def test_quotes_and_whitespace_around_url_are_ignored():
    url = ' "postgresql://u:pw@host:5432/db" '
    assert loader.normalize_database_url(url) == "postgresql://u:pw@host:5432/db"


def test_connection_errors_never_show_the_password():
    secret = "S3cr/et$pass,word"
    url = f"postgresql://postgres.abc:{secret}@127.0.0.1:1/postgres"   # nothing listens on port 1
    with pytest.raises(loader.LoadError) as info:
        loader.connect(url)
    message = str(info.value)
    assert secret not in message
    assert "S3cr" not in message and "%2F" not in message
    assert "Could not connect" in message


def test_placeholder_password_is_rejected():
    with pytest.raises(loader.LoadError, match="placeholder"):
        loader.connect("postgresql://u:[YOUR-PASSWORD]@host:5432/db")
