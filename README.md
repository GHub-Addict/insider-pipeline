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

## Backtest (next phase)

The same `ingest` command backfills history — run it over 2010→2024 (electronic
Form 4 data is reliable from ~2003; expect the backfill to take days at SEC rate
limits, so run it on a box you can leave alone). Then:

1. Replay: for each historical date, run `screen` as-of that date.
2. Join top-scored clusters with forward 6/12-month total returns — using a
   survivorship-bias-free price source (delisted names included), or the backtest lies.
3. Compare vs VTI with realistic slippage, especially on small caps.
4. Iterate on the constants in `cluster.py` / `filters.py` — but hold out 2022–2024
   untouched for final validation, or you'll overfit the recipe to the past.

## Tests

```bash
python tests/test_smoke.py   # offline — parser, filters, scoring
```

## SEC fair use

Max 10 req/s (client throttles to ~7.5), honest User-Agent, and don't hammer
outside business needs. Data is public domain; be a good citizen anyway.
