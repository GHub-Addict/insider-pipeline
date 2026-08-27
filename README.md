# insider-pipeline

Ingests SEC Form 4 filings from EDGAR, kills the noise mechanically, and surfaces
scored insider cluster-buys. Stage 1–2 of a three-stage funnel:

```
EDGAR Form 4 feed ──> mechanical kill filters ──> cluster scoring ──> AI red-team ──> you
   (this repo)            (this repo)               (this repo)      (kill-list UI)
```

Research tool. Not investment advice. The signal it screens for (insider cluster
purchases) has academic support (Lakonishok & Lee 2001; Cohen, Malloy & Pomorski 2012)
but modest, decaying alpha — validate with the backtest before any money follows it.

## Setup

```bash
pip install requests
export EDGAR_USER_AGENT="YourName YourCompany contact@yourdomain.com"   # SEC requires this
```

## Usage

```bash
# Pull the last two weeks of Form 4s into sqlite (resumable, idempotent)
python -m pipeline.cli ingest --start 2026-08-12 --end 2026-08-26

# Filter, cluster, score, print the leaderboard
python -m pipeline.cli screen --window 14 --min-insiders 2 --show-killed

# Export for the red-team UI
python -m pipeline.cli screen --json clusters.json
```

Cron it: ingest yesterday + screen every weekday evening after EDGAR's day closes.

## What gets killed (filters.py)

Hard kills: non-P transactions, sub-$25k buys, 10b5-1 plan trades, fund/SPAC/blank-check
issuers (SIC 6770/6726/6722), filings whose footnotes reveal placements/offerings.

Soft flags (score penalty): round-dollar buys that smell like placements
(e.g. 88,235 sh × $3.40 = $299,999), first-ever director obligation buys,
indirect ownership.

## Scoring (cluster.py)

`score = 2.0·(insiders−1) + mean role weight + log₁₀($total)−4 + 1.5·median ΔOwn − 0.75·flags`

CEO/CFO/President weigh 3×, other officers 2×, directors 1×, 10% owners 0.5×
(matching the literature: officer buys carry more signal than owner averaging-down).
Every constant is exposed on purpose — the filter recipe is the product, and the
backtest exists to tune it.

## Backtest

### 1. Backfill history

`ingest` walks EDGAR's daily indexes one filing at a time — fine for a few
weeks, painfully slow for 15+ years. `bulk-ingest` instead pulls SEC's
quarterly **Insider Transactions Data Sets** — the same Form 3/4/5 data,
pre-flattened into tab-separated files, published back to January 2006 as
~80 zip files (`pipeline/bulk_loader.py`):

```bash
python -m pipeline.cli bulk-ingest --start-year 2006 --start-q 1 --end-year 2024 --end-q 4
```

Resumable/idempotent like `ingest` (tracked per quarter, `--force` to redo).
It joins SUBMISSION/REPORTINGOWNER/NONDERIV_TRANS/FOOTNOTES on
`ACCESSION_NUMBER` and maps the result into the same `Txn` shape
`parser.py` produces from the live XML feed — `filters.py` and `cluster.py`
run over bulk history completely unchanged.

### 2. Replay

`cluster.detect()` takes an `as_of` parameter for exactly this. `pipeline/backtest.py`'s
`replay()` runs the screen as-of every week across the backfilled range, keeps
the top-scored clusters, and joins them against forward 6/12-month returns —
comparing to VTI over the same window:

```bash
python -m pipeline.cli backtest \
  --start 2010-01-01 --end 2024-12-31 \
  --price-source csv:prices.csv --survivorship-bias-free
```

It reports a **tuning period** (everything before `--holdout-start`, default
`2022-01-01`) separately from the **holdout period** — the same 2022–2024
window called out below. Iterate on the constants in `cluster.py` /
`filters.py` against the tuning-period numbers only; look at the holdout
numbers exactly once, at the end, for validation. Checking them mid-iteration
and adjusting anything defeats the entire point of holding them out — you'd
just be overfitting the recipe to the past through an extra round-trip.

### 3. The survivorship bias trap — read this before trusting any number

A free price feed (yfinance included) drops delisted tickers from its
history. That's not a minor gap: companies with desperate insider buying
ahead of a death spiral are *exactly* the companies that sometimes die, and
insider-cluster screens are disproportionately likely to flag them right
before it happens. Point this backtest at a survivorship-biased source and
the worst outcomes in your sample don't show up as losses — they just
silently disappear, and the win rate that's left over is inflated. **If a
backtest built on yfinance comes back looking great, don't believe it.**
That's the specific failure mode that makes a strategy look profitable right
up until real money is behind it.

`backtest.py` won't let this happen by accident:

- `PriceProvider.forward_return()` returns `None` — never a fabricated
  number — when either endpoint is missing, and `missing_kind()` tags a
  ticker whose price series stops mid-window as `stopped_mid_window`
  (a likely delisting) distinct from one that just never had data.
  `summarize()` reports how many clusters fell into each bucket rather than
  quietly excluding them from the average.
- `CSVPriceProvider` takes a price/total-return CSV from **your own**
  delisting-inclusive source (a CRSP extract, Sharadar SEP+SFP, Norgate, or
  similar that keeps a ticker's history through its last trade) — pass
  `--survivorship-bias-free` once you've verified that's true of your file;
  the report prints a warning banner whenever it isn't set.
- `YFinancePriceProvider` refuses to even instantiate without
  `--i-understand-survivorship-bias`. It's there for a quick "does the
  recipe point at real winners among still-listed names" gut check —
  never for a number you'd act on.

Realistic slippage (especially on small caps, where these clusters cluster)
isn't modeled yet — treat backtest returns as before-cost.

## Tests

```bash
python tests/test_smoke.py   # offline — parser, filters, scoring, bulk loader, backtest replay
```

## SEC fair use

Max 10 req/s (client throttles to ~7.5), honest User-Agent, and don't hammer
outside business needs. Data is public domain; be a good citizen anyway.
