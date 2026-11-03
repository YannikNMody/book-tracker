"""Deterministic fake data shaped exactly like data.fetch_*() output. Used by tests (no network)."""

import random

import pandas as pd

import data

CATEGORIES = {  # roughly the real distribution on books.toscrape.com
    "Default": 152, "Nonfiction": 110, "Sequential Art": 75, "Add a comment": 67, "Fiction": 65,
    "Young Adult": 54, "Fantasy": 48, "Romance": 35, "Mystery": 32, "Food and Drink": 30,
    "Childrens": 29, "Historical Fiction": 26, "Classics": 19, "Poetry": 19, "History": 18,
    "Womens Fiction": 17, "Horror": 17, "Science Fiction": 16, "Science": 14, "Music": 13,
    "Business": 12, "Thriller": 11, "Philosophy": 11, "Travel": 11, "Biography": 5,
    "Humor": 10, "Autobiography": 9, "Art": 8, "Religion": 7, "Psychology": 7,
    "Christian Fiction": 6, "Spirituality": 6, "New Adult": 6, "Self Help": 5, "Sports and Games": 5,
    "Health": 4, "Politics": 3, "Christian": 3, "Contemporary": 3, "Historical": 2,
    "Short Stories": 1, "Suspense": 1, "Novels": 1, "Cultural": 1, "Adult Fiction": 1,
    "Parenting": 1, "Crime": 1, "Erotica": 1, "Paranormal": 1, "Academic": 1,
}
WORDS_A = ["Silent", "Last", "Hidden", "Little", "Golden", "Broken", "Midnight", "Wild", "Secret",
           "Lost", "Northern", "Paper", "Glass", "Velvet", "Salt", "Iron", "Quiet", "Electric"]
WORDS_B = ["Garden", "Light", "River", "House", "Kingdom", "Letters", "Attic", "Summer", "Ocean",
           "Library", "Mountain", "Machine", "Orchard", "City", "Fire", "Promise", "Window", "Road"]


def books(n: int = 1000, seed: int = 7) -> pd.DataFrame:
    rng = random.Random(seed)
    names = [c for c, k in CATEGORIES.items() for _ in range(k)]
    rows = []
    for i in range(1, n + 1):
        title = f"{rng.choice(WORDS_A)} {rng.choice(WORDS_B)}"
        if rng.random() < 0.3:
            title += f": A Novel (Book {rng.randint(1, 5)})"
        rows.append({
            "id": i, "title": title, "category": names[(i - 1) % len(names)],
            "rating": rng.randint(1, 5), "price": f"{rng.uniform(10, 59.99):.2f}", "currency": "GBP",
            "in_stock": True, "quantity": rng.choice([1, 2, 3, 5, 7, 12, 14, 16, 19, 22]),
            "image_url": f"https://books.toscrape.com/media/cache/{i:04d}.jpg",
            "product_url": f"https://books.toscrape.com/catalogue/book_{i}/index.html",
            "is_active": True,
            "price_since": "2026-10-28T12:39:06+00:00", "first_seen_at": "2026-10-28T12:39:06+00:00",
            "last_seen_at": "2026-11-03T14:47:52+00:00",
        })
    rows[0].update(title="A Light in the Attic", category="Poetry", rating=3, price="51.77", quantity=22)
    return data.clean_books(rows)


def book_detail(book_id: int) -> dict:
    prices = pd.DataFrame([{"price_incl_tax": 51.77, "price_excl_tax": 51.77, "currency": "GBP",
                            "valid_from": pd.Timestamp("2026-10-28T12:39:06Z"), "valid_to": pd.NaT}])
    if book_id == 2:  # one book with a price change, to exercise the chart
        prices = pd.DataFrame([
            {"price_incl_tax": 53.74, "price_excl_tax": 53.74, "currency": "GBP",
             "valid_from": pd.Timestamp("2026-10-28T12:39:06Z"), "valid_to": pd.Timestamp("2026-10-31T04:17:00Z")},
            {"price_incl_tax": 47.99, "price_excl_tax": 47.99, "currency": "GBP",
             "valid_from": pd.Timestamp("2026-10-31T04:17:00Z"), "valid_to": pd.NaT},
        ])
    stock = pd.DataFrame([{"in_stock": True, "quantity": 22,
                           "valid_from": pd.Timestamp("2026-10-28T12:39:06Z"), "valid_to": pd.NaT}])
    description = ("It's hard to imagine a world without A Light in the Attic. This now-classic collection "
                   "of poetry and drawings from Shel Silverstein celebrates its 20th anniversary with this "
                   "special edition. Silverstein's humorous and creative verse can amuse the dowdiest of readers.")
    return {"book": {"id": book_id, "description": description, "product_type": "Books", "num_reviews": 0},
            "prices": prices, "stock": stock}


def runs() -> pd.DataFrame:
    rows = [
        {"id": 7, "started_at": "2026-11-03T14:47:52Z", "finished_at": "2026-11-03T14:56:56Z", "status": "success",
         "is_full_run": True, "pages_scraped": 50, "books_seen": 1000, "books_new": 0, "price_changes": 0,
         "stock_changes": 0, "error_count": 0, "applied_at": "2026-11-03T14:57:01Z",
         "stats": {"p95_response_ms": 103.5, "requests": 1050}},
        {"id": 6, "started_at": "2026-10-28T12:39:06Z", "finished_at": "2026-10-28T12:47:56Z", "status": "success",
         "is_full_run": True, "pages_scraped": 50, "books_seen": 1000, "books_new": 940, "price_changes": 0,
         "stock_changes": 0, "error_count": 0, "applied_at": "2026-11-03T13:16:40Z",
         "stats": {"p95_response_ms": 363.8, "requests": 1050}},
        {"id": 5, "started_at": "2026-10-28T12:32:22Z", "finished_at": "2026-10-28T12:33:05Z", "status": "success",
         "is_full_run": False, "pages_scraped": 3, "books_seen": 60, "books_new": 55, "price_changes": 0,
         "stock_changes": 0, "error_count": 0, "applied_at": "2026-11-03T13:16:39Z",
         "stats": {"p95_response_ms": 542.2, "requests": 63}},
        {"id": 4, "started_at": "2026-10-28T12:31:32Z", "finished_at": "2026-10-28T12:31:37Z", "status": "success",
         "is_full_run": False, "pages_scraped": 1, "books_seen": 5, "books_new": 5, "price_changes": 0,
         "stock_changes": 0, "error_count": 0, "applied_at": "2026-11-03T13:16:38Z",
         "stats": {"p95_response_ms": 845.3, "requests": 6}},
    ]

    class FakeReader:
        def get(self, resource, params):
            return rows

    return data.fetch_runs(reader=FakeReader())
