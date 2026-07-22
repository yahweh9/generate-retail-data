# Synthetic Retail Sales Data Generator

A Python script that generates realistic, messy, day-by-day POS transaction data for a fictional 3-store apparel retailer. If you're practicing data cleaning, ETL, SQL/dbt transformations, or analytics and you're tired of practicing on datasets that are already clean, this generates raw sales data the way an actual point-of-sale export would look — nulls, duplicate rows, inconsistent casing, and all.

I am so sorry UNIQLO, I used you as a reference.....
## Who this is for

Anyone who wants a free, unlimited supply of realistic retail transaction data to practice on: data analysts learning SQL/pandas, data engineers building an ETL/ELT pipeline end to end, or anyone who wants a believable dataset for a portfolio project without waiting on a real company's data. No API keys, no accounts, no rate limits just run the script!

## What you get

- **3 stores**: Punggol, Orchard Central, Bugis — each with its own Mon-Sun revenue target curve (weekday lower, Friday/weekend higher), plus random week-to-week drift so no two weeks look identical.
- **80 products**: 40 Men's / 40 Women's, split across Tops, Bottoms, Underwear, Socks, with names and prices standardized against real Uniqlo Singapore pricing tiers (basic tee S$14.90, jeans S$59.90, socks S$5.90-14.90, etc.).
- **Realistic sales concentration**: ~30 "hero" basics (crew-neck tees, boxer briefs, ankle socks) sell almost every day and drive most of the revenue; the rest of the catalog sells thin and sporadically — the way a real store's product mix actually behaves.
- **A believable hourly traffic curve**: quiet weekday afternoons, ramping from 5pm, peaking 7-9pm, tapering to close (22:00); weekends are flatter and busier all day.
- **Weekly rotating promotions**: 5-8 long-tail products go on a 20-35% discount each week (Friday -> following Thursday), with a demand boost while active.
- **Basket-level transactions**: orders can contain 1-3 different products (not just one SKU at a random quantity), grouped under a shared `order_id`, timestamp, and payment method.
- **Injected raw-layer messiness**: duplicate rows, null `payment_method`/`product_name`, inconsistent text casing, stray whitespace, and prices occasionally exported as `"$24.90"` strings — the kind of thing that breaks a naive `.sum()` and forces you to actually clean the data before analyzing it.

## Quick start

```bash
pip install -r requirements.txt

# generate a full month of history (uses START_DATE/NUM_DAYS in the script)
python generate_retail_sales.py --backfill

# generate just one day
python generate_retail_sales.py --date 2026-06-15

# no args: defaults to "yesterday" -- handy for a daily scheduled job
python generate_retail_sales.py
```

Each run writes one CSV per day to `output/dt=<date>/sales_<date>.csv` (Hive-style partitioning), so you can load a single day, a date range, or the whole history depending on what you're practicing.

By default the script generates 30 days starting `2026-06-01` — change `START_DATE`/`NUM_DAYS` at the top of `generate_retail_sales.py` to shift or extend the range.

## Output schema

| Column | Description |
|---|---|
| `order_id` | Basket/receipt identifier (shared across line items in one checkout) |
| `line_item_id` | Line number within the basket |
| `timestamp` | Transaction time (11:00-22:00) |
| `store` | Punggol / Orchard Central / Bugis |
| `product_id` | SKU code, e.g. `TOP-M-01` |
| `product_name` | e.g. "Men's Airism Cotton Crew Neck T-Shirt" |
| `category` | Tops / Bottoms / Underwear / Socks |
| `gender` | Men / Women |
| `unit_price` | Price actually charged (reflects any active offer) |
| `original_unit_price` | Standard catalog price, unaffected by offers |
| `on_offer` | Whether this product was on that week's promotion |
| `quantity` | Units purchased of this line item |
| `line_total` | `unit_price * quantity` |
| `payment_method` | Card / PayNow / NETS / Cash |

## Customizing it for your own scenario

Everything is a plain constant near the top of `generate_retail_sales.py` — no need to touch the generation logic to reshape the dataset:

| Want to change | Edit this |
|---|---|
| How much history to generate | `START_DATE`, `NUM_DAYS` |
| Store names / revenue targets | `STORE_WEEKDAY_TARGETS` (one Mon-Sun list per store) |
| The product catalog / prices | `PRODUCT_NAMES` |
| How much of the catalog is "hero" vs. long-tail | `HERO_COUNT_PER_GROUP` |
| Store hours / peak-time shape | `WEEKDAY_HOUR_WEIGHTS`, `WEEKEND_HOUR_WEIGHTS` |
| Promotion frequency/depth | `OFFER_ITEMS_PER_WEEK`, `OFFER_DISCOUNT_RANGE`, `OFFER_WEIGHT_MULTIPLIER` |
| How messy the raw data is | `MESSINESS` (or set every rate to `0` for perfectly clean output) |
| Reproducibility | `SEED` -- same seed always produces the same data |

Reruns are deterministic: regenerating any single date always reproduces exactly the same rows, whether you generate it alone or as part of a full `--backfill`.

## File structure

```
retail-data/
├── generate_retail_sales.py   # the generator
├── requirements.txt
└── output/
    └── dt=YYYY-MM-DD/
        └── sales_YYYY-MM-DD.csv
```

## License

Free to use, modify, and build on for your own learning or portfolio projects.
