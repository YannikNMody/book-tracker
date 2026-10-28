-- =====================================================================
-- Book price tracker — initial schema
-- Target: Supabase (Postgres 15+)
-- Location in repo: supabase/migrations/20261028125334_init_schema.sql
--
-- Design principles
--   * Surrogate keys (our own ids) as primary keys; the site's identifier
--     (UPC) is stored as external_id with a unique constraint for matching.
--   * Multi-source ready: every book belongs to a source (website).
--   * History tables store a new row ONLY when a value changes
--     (valid_from / valid_to; valid_to IS NULL = current value).
--   * Scraper writes raw rows to staging_observations; a set-based merge
--     (step 4) turns them into books / price_history / stock_history.
--   * Every run and every failed request is logged.
--   * Money as numeric, never float. All timestamps timestamptz (UTC).
-- =====================================================================


-- ---------------------------------------------------------------------
-- Sources: one row per scraped website
-- ---------------------------------------------------------------------
create table public.sources (
    id          smallint generated always as identity primary key,
    name        text        not null unique,
    base_url    text        not null unique,
    created_at  timestamptz not null default now()
);

comment on table public.sources is 'Websites we scrape. Lets the system add more sites without schema changes.';


-- ---------------------------------------------------------------------
-- Scrape runs: one row per execution of the scraper
-- ---------------------------------------------------------------------
create type public.scrape_run_status as enum ('running', 'success', 'partial', 'failed');

create table public.scrape_runs (
    id              bigint generated always as identity primary key,
    source_id       smallint    not null references public.sources(id),
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    status          public.scrape_run_status not null default 'running',
    is_full_run     boolean     not null default false,  -- only full runs may mark books as delisted
    config          jsonb       not null default '{}'::jsonb,  -- CLI args: limit, pages, delay, ...
    pages_scraped   integer     not null default 0,
    books_seen      integer     not null default 0,
    books_new       integer     not null default 0,
    price_changes   integer     not null default 0,
    stock_changes   integer     not null default 0,
    error_count     integer     not null default 0,
    stats           jsonb       not null default '{}'::jsonb,  -- requests, status counts, response times (p50/p95/max)
    notes           text,
    check (finished_at is null or finished_at >= started_at)
);

create index scrape_runs_source_started_idx on public.scrape_runs (source_id, started_at desc);

comment on table public.scrape_runs is 'Audit log of scraper executions.';
comment on column public.scrape_runs.is_full_run is 'True only when the whole catalogue was scraped; partial/limited runs must never deactivate books.';


-- ---------------------------------------------------------------------
-- Scrape errors: one row per failed request / parse failure
-- ---------------------------------------------------------------------
create table public.scrape_errors (
    id           bigint generated always as identity primary key,
    run_id       bigint      not null references public.scrape_runs(id) on delete cascade,
    url          text        not null,
    http_status  smallint,                    -- null for timeouts / connection errors
    error_type   text        not null,        -- e.g. 'timeout', 'http_error', 'parse_error'
    message      text,
    attempt      smallint    not null default 1,
    occurred_at  timestamptz not null default now()
);

create index scrape_errors_run_idx on public.scrape_errors (run_id);

comment on table public.scrape_errors is 'Every failed request or parse, incl. retries. Answers "did the site crash, and where?"';


-- ---------------------------------------------------------------------
-- Categories
-- ---------------------------------------------------------------------
create table public.categories (
    id          integer generated always as identity primary key,
    source_id   smallint    not null references public.sources(id),
    name        text        not null,
    url         text,
    created_at  timestamptz not null default now(),
    unique (source_id, name)
);


-- ---------------------------------------------------------------------
-- Books: one row per book; descriptive fields are overwritten in place
-- ---------------------------------------------------------------------
create table public.books (
    id                 bigint generated always as identity primary key,
    source_id          smallint    not null references public.sources(id),
    external_id        text        not null,      -- UPC on books.toscrape.com
    title              text        not null,
    category_id        integer     references public.categories(id),
    rating             smallint    check (rating between 1 and 5),
    description        text,
    product_type       text,
    num_reviews        integer     check (num_reviews >= 0),
    image_url          text,
    product_url        text        not null,
    is_active          boolean     not null default true,
    first_seen_at      timestamptz not null default now(),
    last_seen_at       timestamptz not null default now(),
    first_seen_run_id  bigint      references public.scrape_runs(id),
    last_seen_run_id   bigint      references public.scrape_runs(id),
    updated_at         timestamptz not null default now(),
    unique (source_id, external_id)
);

create index books_category_idx  on public.books (category_id);
create index books_active_idx    on public.books (is_active);

comment on column public.books.external_id is 'Identifier assigned by the source site (UPC here). Used to match re-scraped books; our own id is the primary key.';
comment on column public.books.is_active   is 'False once a full run no longer finds the book (delisted).';


-- ---------------------------------------------------------------------
-- Price history: new row only when the price changes
-- ---------------------------------------------------------------------
create table public.price_history (
    id                  bigint generated always as identity primary key,
    book_id             bigint        not null references public.books(id) on delete cascade,
    price_excl_tax      numeric(10,2) not null check (price_excl_tax >= 0),
    price_incl_tax      numeric(10,2) not null check (price_incl_tax >= 0),
    currency            char(3)       not null,   -- ISO 4217, e.g. 'GBP'
    valid_from          timestamptz   not null,
    valid_to            timestamptz,              -- null = current price
    detected_in_run_id  bigint        not null references public.scrape_runs(id),
    check (valid_to is null or valid_to >= valid_from)
);

-- At most one current price per book
create unique index price_history_one_current_idx on public.price_history (book_id) where valid_to is null;
create index price_history_book_time_idx on public.price_history (book_id, valid_from desc);

comment on table public.price_history is 'Slowly changing history: one row per price period. valid_from = first run that observed this price.';


-- ---------------------------------------------------------------------
-- Stock history: new row only when availability changes
-- ---------------------------------------------------------------------
create table public.stock_history (
    id                  bigint generated always as identity primary key,
    book_id             bigint      not null references public.books(id) on delete cascade,
    in_stock            boolean     not null,
    quantity            integer     check (quantity >= 0),
    valid_from          timestamptz not null,
    valid_to            timestamptz,
    detected_in_run_id  bigint      not null references public.scrape_runs(id),
    check (valid_to is null or valid_to >= valid_from)
);

create unique index stock_history_one_current_idx on public.stock_history (book_id) where valid_to is null;
create index stock_history_book_time_idx on public.stock_history (book_id, valid_from desc);


-- ---------------------------------------------------------------------
-- Staging: raw, one row per book per run, exactly as scraped
-- The scraper bulk-inserts here; the merge function (step 4) applies it.
-- ---------------------------------------------------------------------
create table public.staging_observations (
    id               bigint generated always as identity primary key,
    run_id           bigint      not null references public.scrape_runs(id) on delete cascade,
    external_id      text        not null,
    title            text        not null,
    category_name    text,
    rating           smallint,
    description      text,
    product_type     text,
    num_reviews      integer,
    image_url        text,
    product_url      text        not null,
    price_excl_tax   numeric(10,2),
    price_incl_tax   numeric(10,2),
    currency         char(3),
    in_stock         boolean,
    quantity         integer,
    scraped_at       timestamptz not null default now(),
    unique (run_id, external_id)
);

comment on table public.staging_observations is 'Raw scraped rows per run. Allows re-processing and debugging; prune old runs with a retention policy.';


-- ---------------------------------------------------------------------
-- Keep books.updated_at correct
-- ---------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create trigger books_set_updated_at
    before update on public.books
    for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------
-- Convenience view for the frontend: each book with its current state
-- security_invoker = true so the view respects RLS of the caller
-- ---------------------------------------------------------------------
create view public.books_current
with (security_invoker = true) as
select
    b.id,
    b.source_id,
    b.external_id,
    b.title,
    c.name            as category,
    b.rating,
    b.description,
    b.image_url,
    b.product_url,
    b.is_active,
    p.price_incl_tax  as price,
    p.currency,
    p.valid_from      as price_since,
    s.in_stock,
    s.quantity,
    b.first_seen_at,
    b.last_seen_at
from public.books b
left join public.categories    c on c.id = b.category_id
left join public.price_history p on p.book_id = b.id and p.valid_to is null
left join public.stock_history s on s.book_id = b.id and s.valid_to is null;


-- ---------------------------------------------------------------------
-- Row Level Security
-- Scraper uses the service role key (bypasses RLS).
-- Frontend uses the anon key: read-only on public data, nothing else.
-- ---------------------------------------------------------------------
alter table public.sources              enable row level security;
alter table public.scrape_runs          enable row level security;
alter table public.scrape_errors        enable row level security;
alter table public.categories           enable row level security;
alter table public.books                enable row level security;
alter table public.price_history        enable row level security;
alter table public.stock_history        enable row level security;
alter table public.staging_observations enable row level security;

create policy "public read" on public.sources       for select to anon, authenticated using (true);
create policy "public read" on public.categories    for select to anon, authenticated using (true);
create policy "public read" on public.books         for select to anon, authenticated using (true);
create policy "public read" on public.price_history for select to anon, authenticated using (true);
create policy "public read" on public.stock_history for select to anon, authenticated using (true);
create policy "public read" on public.scrape_runs   for select to anon, authenticated using (true);
-- scrape_errors and staging_observations: no policies → invisible to anon/authenticated


-- ---------------------------------------------------------------------
-- Explicit privileges (defence in depth; don't rely on platform defaults)
-- anon/authenticated: read-only on public data, nothing else.
-- service_role (scraper/loader): full access.
-- ---------------------------------------------------------------------
revoke all on all tables in schema public from anon, authenticated;

grant select on
    public.sources,
    public.categories,
    public.books,
    public.price_history,
    public.stock_history,
    public.scrape_runs,
    public.books_current
to anon, authenticated;

grant all on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;


-- ---------------------------------------------------------------------
-- Seed
-- ---------------------------------------------------------------------
insert into public.sources (name, base_url)
values ('books.toscrape.com', 'https://books.toscrape.com/');
