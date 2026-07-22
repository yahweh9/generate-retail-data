"""
Synthetic raw POS transaction generator for a 3-store apparel retailer
(Punggol, Orchard Central, Bugis).

Designed to be invoked once per day (e.g. by a GitHub Actions cron) and
produce that single day's messy raw export, landing in a dt=YYYY-MM-DD
partition the way a real S3-backed Bronze layer would. Every random
draw is keyed off (SEED, purpose, index) via numpy SeedSequence, so
running a single date in isolation reproduces exactly what a full
backfill would have produced for that date -- no run-to-run state.

Usage:
    python generate_retail_sales.py --backfill        # initial history
    python generate_retail_sales.py --date 2026-07-22  # one day (cron)
    python generate_retail_sales.py                    # defaults to yesterday
"""

import argparse
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SEED = 42
START_DATE = date(2026, 6, 1)
NUM_DAYS = 30
OUTPUT_ROOT = "output"

# Mon -> Sun daily revenue targets per store (date.weekday() indexes into this).
# Rough gauge, not a fixed constant -- see build_week_drift().
STORE_WEEKDAY_TARGETS = {
    "Punggol":         [18_000, 19_000, 20_000, 20_000, 22_000, 28_000, 30_000],
    "Orchard Central": [28_000, 28_000, 35_000, 35_000, 35_000, 44_000, 43_000],
    "Bugis":           [24_000, 26_000, 30_000, 30_000, 33_000, 35_000, 35_000],
}

WEEKLY_DRIFT_RANGE = (0.88, 1.15)   # week-level target multiplier (good week / slow week)
DAILY_JITTER = (0.85, 1.0, 1.15)    # finer day-to-day noise on top of the week's target

# hour bucket -> share of daily transactions (11:00-22:00, store closes 22:00)
WEEKDAY_HOUR_WEIGHTS = {
    11: 0.04, 12: 0.06, 13: 0.05, 14: 0.05, 15: 0.05, 16: 0.06,
    17: 0.08, 18: 0.10, 19: 0.15, 20: 0.18, 21: 0.13, 22: 0.05,
}
WEEKEND_HOUR_WEIGHTS = {
    11: 0.07, 12: 0.09, 13: 0.09, 14: 0.08, 15: 0.08, 16: 0.08,
    17: 0.08, 18: 0.09, 19: 0.11, 20: 0.12, 21: 0.08, 22: 0.03,
}

PAYMENT_METHODS = ["Card", "PayNow", "NETS", "Cash"]
PAYMENT_WEIGHTS = [0.45, 0.25, 0.15, 0.15]

# weekly rotating promo: chain-wide, long-tail items only
OFFER_ITEMS_PER_WEEK = (5, 9)          # np.random high is exclusive -> 5 to 8 items
OFFER_DISCOUNT_RANGE = (0.20, 0.35)    # 20-35% off
OFFER_WEIGHT_MULTIPLIER = (2.5, 4.0)   # demand boost while on offer

# Bronze-layer messiness injected into the daily export.
MESSINESS = {
    "duplicate_row_rate": 0.006,     # POS double-scan / retry
    "null_payment_rate": 0.02,       # failed payment capture
    "null_product_name_rate": 0.004, # barcode scan failure
    "casing_mangle_rate": 0.18,      # register/POS text inconsistency
    "whitespace_rate": 0.08,         # stray leading/trailing whitespace
    "price_as_string_rate": 0.03,    # price exported with a currency symbol
}

# 10 named products per (category, gender) group = 80 total, each paired with
# a standardized price (S$) matching its real Uniqlo Singapore equivalent as of
# Jul 2026 (uniqlo.com/sg) -- e.g. basic tee 14.90, Dry-EX tee 19.90, polo 29.90,
# jeans 59.90, shorts 19.90-29.90, boxer briefs 14.90, bra 29.90, socks 5.90-14.90.
# First 4 of each group are "hero/basics" items -> the top-selling tier.
PRODUCT_NAMES = {
    ("Tops", "Men"): [
        ("Airism Cotton Crew Neck T-Shirt", 14.90), ("Supima Cotton Polo Shirt", 29.90),
        ("Dry-EX Crew Neck T-Shirt", 19.90), ("Oxford Long Sleeve Shirt", 29.90),
        ("Heattech Crew Neck Long Sleeve Tee", 14.90), ("Fleece Full-Zip Hoodie", 39.90),
        ("Ultra Light Down Jacket", 79.90), ("Linen Blend Short Sleeve Shirt", 29.90),
        ("Flannel Checked Long Sleeve Shirt", 29.90), ("Merino Blend V-Neck Sweater", 39.90),
    ],
    ("Tops", "Women"): [
        ("Airism Cotton Crew Neck T-Shirt", 14.90), ("Ribbed Long Sleeve Turtleneck", 19.90),
        ("Silky Satin Blouse", 39.90), ("Heattech Crew Neck Long Sleeve Tee", 14.90),
        ("Merino Blend Cardigan", 39.90), ("Linen Blend Short Sleeve Shirt", 29.90),
        ("Fleece Pullover Hoodie", 29.90), ("Puff Sleeve Blouse", 29.90),
        ("Drape Sleeveless Top", 19.90), ("Ultra Light Down Vest", 59.90),
    ],
    ("Bottoms", "Men"): [
        ("Ezy Ankle Pants", 29.90), ("Slim Fit Chino Trousers", 29.90),
        ("Selvedge Regular Fit Jeans", 59.90), ("Dry Stretch Shorts", 19.90),
        ("Wide Fit Cargo Pants", 39.90), ("Jersey Jogger Pants", 29.90),
        ("Smart Ankle Pants", 39.90), ("Relaxed Fit Denim Shorts", 29.90),
        ("Linen Blend Trousers", 39.90), ("Sweat Jogger Pants", 29.90),
    ],
    ("Bottoms", "Women"): [
        ("Ultra Stretch Skinny Jeans", 39.90), ("High-Rise Wide Leg Pants", 39.90),
        ("Ezy Ankle Pants", 29.90), ("Denim Straight Jeans", 49.90),
        ("Pleated Midi Skirt", 29.90), ("Jersey Jogger Pants", 29.90),
        ("Linen Blend Wide Pants", 39.90), ("Smart Ankle Pants", 39.90),
        ("Sweat Shorts", 19.90), ("High Waist Denim Shorts", 29.90),
    ],
    ("Underwear", "Men"): [
        ("Airism Boxer Briefs", 14.90), ("Cotton Blend Trunks", 14.90),
        ("Airism Mesh Boxer Briefs", 14.90), ("Cotton Crew Neck Undershirt", 14.90),
        ("Seamless Boxer Briefs", 14.90), ("Airism Sleeveless Undershirt", 9.90),
        ("Stretch Cotton Boxers", 14.90), ("Airism V-Neck Undershirt", 9.90),
        ("Heattech Long Johns", 19.90), ("Cotton Blend Briefs", 9.90),
    ],
    ("Underwear", "Women"): [
        ("Airism Bikini Shorts", 9.90), ("Wireless Bra", 29.90),
        ("Cotton Blend Briefs", 9.90), ("Airism T-Shirt Bra", 29.90),
        ("Seamless Shorts", 9.90), ("Lace Trim Briefs", 9.90),
        ("Heattech Camisole", 14.90), ("Airism Camisole", 14.90),
        ("Cotton Boyshorts", 9.90), ("Wireless Bralette", 29.90),
    ],
    ("Socks", "Men"): [
        ("Ankle Socks 3-Pack", 14.90), ("Pile Crew Socks", 5.90),
        ("Airism Low-Cut Socks", 5.90), ("Ribbed Crew Socks", 5.90),
        ("Sport Ankle Socks", 5.90), ("Heattech Socks", 9.90),
        ("Striped Crew Socks", 5.90), ("Cotton Blend Dress Socks", 9.90),
        ("No-Show Sneaker Socks", 5.90), ("Wool Blend Socks", 9.90),
    ],
    ("Socks", "Women"): [
        ("Ankle Socks 3-Pack", 14.90), ("Pile Crew Socks", 5.90),
        ("Airism Low-Cut Socks", 5.90), ("Ribbed Crew Socks", 5.90),
        ("Sport Ankle Socks", 5.90), ("Heattech Socks", 9.90),
        ("Striped Crew Socks", 5.90), ("Lace Trim Ankle Socks", 5.90),
        ("No-Show Sneaker Socks", 5.90), ("Wool Blend Socks", 9.90),
    ],
}

HERO_COUNT_PER_GROUP = 4  # ~32 hero/basics items across the 8 groups

# RNG purpose tags -- keeps each concern on its own independent, addressable stream
TAG_CATALOG, TAG_OFFERS, TAG_DRIFT, TAG_DAY, TAG_MESSY = range(5)


def make_rng(*parts: int) -> np.random.Generator:
    """Deterministic, order-independent RNG stream keyed by (SEED, *parts)."""
    return np.random.default_rng(np.random.SeedSequence([SEED, *parts]))


# ---------------------------------------------------------------------------
# CATALOG (static across all days -- always derived from the same seed)
# ---------------------------------------------------------------------------

def build_catalog() -> pd.DataFrame:
    rng = make_rng(TAG_CATALOG)
    rows = []
    for (category, gender), items in PRODUCT_NAMES.items():
        prefix = {"Tops": "TOP", "Bottoms": "BTM", "Underwear": "UND", "Socks": "SOK"}[category]
        gender_code = "M" if gender == "Men" else "W"
        for i, (name, price) in enumerate(items):
            is_hero = i < HERO_COUNT_PER_GROUP
            weight = rng.uniform(3.0, 6.0) if is_hero else rng.uniform(0.3, 1.5)
            appearance_prob = rng.uniform(0.90, 1.0) if is_hero else rng.uniform(0.15, 0.55)
            rows.append({
                "product_id": f"{prefix}-{gender_code}-{i + 1:02d}",
                "product_name": f"{gender}'s {name}",
                "category": category,
                "gender": gender,
                "unit_price": price,
                "popularity_weight": weight,
                "appearance_prob": appearance_prob,
                "is_hero": is_hero,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# PER-WEEK STATE (offers + target drift)
# ---------------------------------------------------------------------------

def week_start_friday(d: date) -> date:
    """Friday that starts d's Fri-Thu offer week (e.g. Mon/Tue/.../Thu all
    map back to the Friday before them; Friday maps to itself)."""
    days_since_friday = (d.weekday() - 4) % 7  # Mon=0 ... Sun=6, Friday=4
    return d - timedelta(days=days_since_friday)


def build_week_offers(offer_week_key: int, catalog: pd.DataFrame) -> dict:
    """offer_week_key should be week_start_friday(day).toordinal() -- offers
    rotate Friday-to-Thursday, independent of START_DATE/day_offset."""
    rng = make_rng(TAG_OFFERS, offer_week_key)
    long_tail = catalog[~catalog["is_hero"]]
    n_items = int(rng.integers(*OFFER_ITEMS_PER_WEEK))
    chosen_idx = rng.choice(long_tail.index.to_numpy(), size=n_items, replace=False)

    offers = {}
    for idx in chosen_idx:
        row = catalog.loc[idx]
        discount = rng.uniform(*OFFER_DISCOUNT_RANGE)
        offers[row["product_id"]] = {
            "discount_price": round(row["unit_price"] * (1 - discount), 2),
            "weight_multiplier": rng.uniform(*OFFER_WEIGHT_MULTIPLIER),
        }
    return offers


def build_week_drift(week_index: int) -> dict:
    rng = make_rng(TAG_DRIFT, week_index)
    return {store: rng.uniform(*WEEKLY_DRIFT_RANGE) for store in STORE_WEEKDAY_TARGETS}


# ---------------------------------------------------------------------------
# TIME-OF-DAY SAMPLING
# ---------------------------------------------------------------------------

def sample_timestamp(rng: np.random.Generator, day: date) -> pd.Timestamp:
    is_weekend = day.weekday() >= 5
    weights = WEEKEND_HOUR_WEIGHTS if is_weekend else WEEKDAY_HOUR_WEIGHTS
    hours = list(weights.keys())
    probs = np.array(list(weights.values()))
    probs = probs / probs.sum()

    h = rng.choice(hours, p=probs)
    minute = int(rng.integers(0, 60))
    second = int(rng.integers(0, 60))
    return pd.Timestamp(day) + pd.Timedelta(hours=int(h), minutes=minute, seconds=second)


# ---------------------------------------------------------------------------
# ONE STORE-DAY SIMULATION
# ---------------------------------------------------------------------------

def simulate_store_day(rng: np.random.Generator, store: str, day: date, catalog: pd.DataFrame,
                        week_offers: dict, weekly_mult: float) -> list:
    weekday_idx = day.weekday()  # 0=Mon ... 6=Sun, matches STORE_WEEKDAY_TARGETS order
    week_adjusted_target = STORE_WEEKDAY_TARGETS[store][weekday_idx] * weekly_mult
    daily_factor = rng.triangular(*DAILY_JITTER)
    day_target = week_adjusted_target * daily_factor

    active_mask = rng.random(len(catalog)) < catalog["appearance_prob"].values
    active = catalog[active_mask].copy()
    if active.empty:
        active = catalog.iloc[rng.choice(len(catalog), size=5, replace=False)].copy()

    active["on_offer"] = active["product_id"].isin(week_offers.keys())
    active["effective_price"] = active.apply(
        lambda r: week_offers[r["product_id"]]["discount_price"] if r["on_offer"] else r["unit_price"], axis=1)
    active["effective_weight"] = active.apply(
        lambda r: r["popularity_weight"] * week_offers[r["product_id"]]["weight_multiplier"]
        if r["on_offer"] else r["popularity_weight"], axis=1)

    active["norm_weight"] = active["effective_weight"] / active["effective_weight"].sum()
    active["dollar_alloc"] = active["norm_weight"] * day_target
    active["units"] = np.maximum(1, np.round(active["dollar_alloc"] / active["effective_price"])).astype(int)
    active = active[active["dollar_alloc"] >= active["effective_price"] * 0.4]  # drop products that barely register

    # explode into individual unit "pool", then shuffle and group into baskets of 1-3 items
    pool = active.loc[active.index.repeat(active["units"])].reset_index(drop=True)
    shuffle_order = rng.permutation(len(pool))
    pool = pool.iloc[shuffle_order].reset_index(drop=True)

    rows = []
    i = 0
    n = len(pool)
    basket_sizes = rng.integers(1, 4, size=max(n, 1))
    b = 0
    order_counter = 0
    while i < n:
        size = min(basket_sizes[b % len(basket_sizes)], n - i)
        basket_items = pool.iloc[i:i + size]
        order_counter += 1
        order_id = f"{store[:3].upper()}-{day.strftime('%Y%m%d')}-{order_counter:04d}"
        ts = sample_timestamp(rng, day)
        payment = rng.choice(PAYMENT_METHODS, p=PAYMENT_WEIGHTS)

        for line_no, (_, item) in enumerate(basket_items.iterrows(), start=1):
            qty = 1
            if item["category"] in ("Underwear", "Socks") and rng.random() < 0.15:
                qty = 2  # occasional multi-pack top-up
            rows.append({
                "order_id": order_id,
                "line_item_id": line_no,
                "timestamp": ts,
                "store": store,
                "product_id": item["product_id"],
                "product_name": item["product_name"],
                "category": item["category"],
                "gender": item["gender"],
                "unit_price": item["effective_price"],
                "original_unit_price": item["unit_price"],
                "on_offer": bool(item["on_offer"]),
                "quantity": qty,
                "line_total": round(item["effective_price"] * qty, 2),
                "payment_method": payment,
            })
        i += size
        b += 1

    return rows


# ---------------------------------------------------------------------------
# BRONZE-LAYER MESSINESS
# ---------------------------------------------------------------------------

def apply_messiness(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    n = len(df)

    dup_mask = rng.random(n) < MESSINESS["duplicate_row_rate"]
    if dup_mask.any():
        df = pd.concat([df, df[dup_mask]], ignore_index=True)
        n = len(df)

    null_payment = rng.random(n) < MESSINESS["null_payment_rate"]
    df.loc[null_payment, "payment_method"] = np.nan

    null_name = rng.random(n) < MESSINESS["null_product_name_rate"]
    df.loc[null_name, "product_name"] = np.nan

    casing_funcs = [str.upper, str.lower, str.title]
    for col in ["store", "category", "gender", "payment_method"]:
        mangle_idx = df.index[rng.random(n) < MESSINESS["casing_mangle_rate"]]
        for i in mangle_idx:
            val = df.at[i, col]
            if isinstance(val, str):
                df.at[i, col] = casing_funcs[rng.integers(0, len(casing_funcs))](val)

    for col in ["store", "product_name"]:
        ws_idx = df.index[rng.random(n) < MESSINESS["whitespace_rate"]]
        for i in ws_idx:
            val = df.at[i, col]
            if isinstance(val, str):
                df.at[i, col] = f"  {val}  "

    for col in ["unit_price", "line_total"]:
        df[col] = df[col].astype(object)
        price_idx = df.index[rng.random(n) < MESSINESS["price_as_string_rate"]]
        for i in price_idx:
            df.at[i, col] = f"${df.at[i, col]}"

    shuffle_order = rng.permutation(len(df))
    return df.iloc[shuffle_order].reset_index(drop=True)


# ---------------------------------------------------------------------------
# ONE DAY, ALL STORES
# ---------------------------------------------------------------------------

def generate_day(day: date):
    day_offset = (day - START_DATE).days
    drift_week_index = day_offset // 7          # target-drift week (unchanged, arbitrary anchor)
    offer_week_key = week_start_friday(day).toordinal()  # offer week: Friday -> following Thursday

    catalog = build_catalog()
    week_offers = build_week_offers(offer_week_key, catalog)
    weekly_mults = build_week_drift(drift_week_index)

    all_rows = []
    for store_idx, store in enumerate(STORE_WEEKDAY_TARGETS):
        day_rng = make_rng(TAG_DAY, day_offset, store_idx)
        all_rows.extend(simulate_store_day(day_rng, store, day, catalog, week_offers, weekly_mults[store]))

    df = pd.DataFrame(all_rows)
    df = apply_messiness(df, make_rng(TAG_MESSY, day_offset))

    out_dir = Path(OUTPUT_ROOT) / f"dt={day.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sales_{day.isoformat()}.csv"
    df.to_csv(out_path, index=False)

    return df, out_path, weekly_mults


def print_summary(day: date, df: pd.DataFrame, out_path: Path, weekly_mults: dict):
    print(f"[{day.isoformat()}] wrote {len(df):,} rows -> {out_path}")

    store_clean = df["store"].astype(str).str.strip().str.lower()
    line_total_clean = pd.to_numeric(
        df["line_total"].astype(str).str.replace("$", "", regex=False), errors="coerce")

    weekday_idx = day.weekday()
    for store in STORE_WEEKDAY_TARGETS:
        target = STORE_WEEKDAY_TARGETS[store][weekday_idx] * weekly_mults[store]
        actual = line_total_clean[store_clean == store.lower()].sum()
        print(f"  {store}: week-adjusted target=${target:,.0f}  actual=${actual:,.0f} ({actual/target:.0%})")

    print(f"  duplicate rows: {df.duplicated().sum()}  "
          f"null payment: {df['payment_method'].isna().sum()}  "
          f"null product_name: {df['product_name'].isna().sum()}")


# ---------------------------------------------------------------------------
# CLI / MAIN
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Generate one (or many) day's raw retail sales CSV.")
    p.add_argument("--date", type=str, help="Single date YYYY-MM-DD to generate (e.g. daily cron run).")
    p.add_argument("--backfill", action="store_true",
                   help=f"Generate all {NUM_DAYS} days starting {START_DATE.isoformat()} (initial history).")
    return p.parse_args()


def main():
    args = parse_args()
    if args.date:
        days = [date.fromisoformat(args.date)]
    elif args.backfill:
        days = [START_DATE + timedelta(days=d) for d in range(NUM_DAYS)]
    else:
        days = [date.today() - timedelta(days=1)]  # default: "yesterday", mimics a nightly batch job

    for day in days:
        df, out_path, weekly_mults = generate_day(day)
        print_summary(day, df, out_path, weekly_mults)


if __name__ == "__main__":
    main()
