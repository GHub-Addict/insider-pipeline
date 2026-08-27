"""CLI.

  python -m pipeline.cli ingest --start 2026-08-11 --end 2026-08-26
  python -m pipeline.cli bulk-ingest --start-year 2010 --start-q 1 --end-year 2024 --end-q 4
  python -m pipeline.cli screen --window 14 --min-insiders 2 [--json out.json]
  python -m pipeline.cli backtest --start 2010-01-01 --end 2024-12-31 --price-source csv:prices.csv

`ingest` walks EDGAR's daily indexes and is resumable/idempotent: re-running
skips completed days. For backfilling years of history, `bulk-ingest` is much
faster: it pulls SEC's quarterly Insider Transactions Data Sets (~80 zip
files back to 2006) instead of one filing at a time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from . import backtest as bt
from . import bulk_loader, edgar, filters, parser, store
from .cluster import detect


def cmd_ingest(args: argparse.Namespace) -> None:
    conn = store.connect(args.db)
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    day = start
    while day <= end:
        key = day.isoformat()
        if store.day_done(conn, key) and not args.force:
            print(f"{key}: already ingested, skipping")
            day += dt.timedelta(days=1)
            continue
        entries = edgar.daily_form4_index(day)
        n_txns = 0
        for i, e in enumerate(entries):
            try:
                xml = edgar.fetch_form4_xml(e)
                if not xml:
                    continue
                txns = parser.parse_form4(xml, accession=e.accession)
                # keep only purchases at ingest time to keep the DB lean
                purchases = [t for t in txns if t.code == "P" and t.acquired]
                for t in purchases:
                    # issuerTradingSymbol is optional on Form 4; recover it from CIK
                    # so real listed issuers aren't dropped as "no listed ticker".
                    if not t.ticker or t.ticker in ("NONE", "N/A", "N.A."):
                        t.ticker = edgar.resolve_ticker(t.issuer_cik)
                if purchases:
                    store.save(conn, purchases)
                    n_txns += len(purchases)
            except Exception as ex:  # one bad filing never stops the run
                print(f"  warn {e.accession}: {ex}", file=sys.stderr)
            if i and i % 250 == 0:
                print(f"  {key}: {i}/{len(entries)} filings...")
        store.mark_day(conn, key, len(entries))
        print(f"{key}: {len(entries)} Form 4s, {n_txns} open-market purchases stored")
        day += dt.timedelta(days=1)


def cmd_bulk_ingest(args: argparse.Namespace) -> None:
    conn = store.connect(args.db)
    start = (args.start_year, args.start_q)
    end = (args.end_year, args.end_q)
    for year, q in bulk_loader.quarters(start, end):
        key = f"{year}Q{q}"
        if store.quarter_done(conn, key) and not args.force:
            print(f"{key}: already ingested, skipping")
            continue
        try:
            zip_path = bulk_loader.download_quarter(year, q, args.cache_dir, force=args.redownload)
        except Exception as ex:  # missing quarter (holiday-adjacent gaps, not-yet-published, etc.)
            print(f"  warn {key}: download failed ({ex})", file=sys.stderr)
            continue
        try:
            txns = bulk_loader.load_zip(zip_path, resolve_tickers=not args.offline)
        except Exception as ex:
            print(f"  warn {key}: parse failed ({ex})", file=sys.stderr)
            continue
        purchases = [t for t in txns if t.code == "P" and t.acquired]
        if purchases:
            store.save(conn, purchases)
        store.mark_quarter(conn, key, len(txns))
        print(f"{key}: {len(txns)} transactions, {len(purchases)} open-market purchases stored")


def cmd_backtest(args: argparse.Namespace) -> None:
    conn = store.connect(args.db)
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)

    if args.price_source == "yfinance":
        provider = bt.YFinancePriceProvider(
            acknowledge_survivorship_bias=args.i_understand_survivorship_bias
        )
    elif args.price_source.startswith("csv:"):
        provider = bt.CSVPriceProvider(
            args.price_source[len("csv:"):], survivorship_bias_free=args.survivorship_bias_free
        )
    else:
        raise SystemExit("--price-source must be 'yfinance' or 'csv:<path>'")

    trades = bt.replay(conn, start, end, provider, window_days=args.window,
                        min_insiders=args.min_insiders, top_n=args.top, step_days=args.step)
    report = bt.summarize(trades, provider, holdout_start=args.holdout_start)
    print(json.dumps(report, indent=2))

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"report": report, "trades": [vars(t) for t in trades]}, f, indent=2)
        print(f"\nWrote {args.json}")


def cmd_screen(args: argparse.Namespace) -> None:
    conn = store.connect(args.db)
    since = (dt.date.today() - dt.timedelta(days=args.window + 5)).isoformat()
    txns = store.load_purchases(conn, since=since)

    # SIC lookup for fund/SPAC kills (one metadata call per unique issuer)
    sics = {}
    for cik in {t.issuer_cik for t in txns}:
        sics[cik] = edgar.company_meta(cik)["sic"] if not args.offline else ""

    survivors, killed = filters.apply(txns, sics)
    clusters = detect(survivors, window_days=args.window, min_insiders=args.min_insiders)

    print(f"\n{len(txns)} purchases -> {len(survivors)} after filters "
          f"({len(killed)} killed) -> {len(clusters)} clusters\n")
    for c in clusters[: args.top]:
        s = c.summary()
        flags = f"  ⚑ {', '.join(s['flags'])}" if s["flags"] else ""
        print(f"{s['score']:>6.2f}  {s['ticker']:<6} {s['company'][:38]:<38} "
              f"{s['insiders']} insiders  ${s['total_value']:>12,}  "
              f"ΔOwn {s['median_delta_own_pct']:.0f}%{flags}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump([c.summary() for c in clusters], f, indent=2)
        print(f"\nWrote {args.json} — feed the top tickers to the kill-list red-team UI.")

    if args.show_killed:
        print("\n-- killed --")
        for t, reason in killed[:50]:
            print(f"  {t.ticker:<6} {t.owner_name[:28]:<28} ${t.value:>12,.0f}  {reason}")


def main() -> None:
    p = argparse.ArgumentParser(prog="insider-pipeline")
    sub = p.add_subparsers(required=True)

    pi = sub.add_parser("ingest", help="pull Form 4s from EDGAR daily indexes into sqlite")
    pi.add_argument("--start", required=True)
    pi.add_argument("--end", required=True)
    pi.add_argument("--db", default="insider.db")
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(fn=cmd_ingest)

    pb = sub.add_parser("bulk-ingest", help="pull SEC's quarterly insider transactions data sets into sqlite")
    pb.add_argument("--start-year", type=int, required=True)
    pb.add_argument("--start-q", type=int, choices=[1, 2, 3, 4], required=True)
    pb.add_argument("--end-year", type=int, required=True)
    pb.add_argument("--end-q", type=int, choices=[1, 2, 3, 4], required=True)
    pb.add_argument("--db", default="insider.db")
    pb.add_argument("--cache-dir", default="sec_bulk_cache", help="where quarterly zips are downloaded/read from")
    pb.add_argument("--force", action="store_true", help="re-ingest quarters already marked done")
    pb.add_argument("--redownload", action="store_true", help="re-download zips even if cached")
    pb.add_argument("--offline", action="store_true", help="skip ticker-symbol resolution (no network)")
    pb.set_defaults(fn=cmd_bulk_ingest)

    pt = sub.add_parser("backtest", help="replay the screen historically and score forward returns vs VTI")
    pt.add_argument("--start", required=True)
    pt.add_argument("--end", required=True)
    pt.add_argument("--price-source", required=True,
                     help="'csv:<path>' (bring your own survivorship-bias-free prices) or 'yfinance' (discouraged)")
    pt.add_argument("--survivorship-bias-free", action="store_true",
                     help="assert the csv: source includes delisted tickers through their last trade")
    pt.add_argument("--i-understand-survivorship-bias", action="store_true",
                     help="required to use --price-source yfinance; see README.md#backtest")
    pt.add_argument("--window", type=int, default=14)
    pt.add_argument("--min-insiders", type=int, default=2)
    pt.add_argument("--top", type=int, default=5, help="top-N clusters per as-of date to trade")
    pt.add_argument("--step", type=int, default=7, help="days between as-of replay dates")
    pt.add_argument("--holdout-start", default=bt.DEFAULT_HOLDOUT_START,
                     help="report this period separately; never tune thresholds on it")
    pt.add_argument("--db", default="insider.db")
    pt.add_argument("--json", help="write full trade list + report to a JSON file")
    pt.set_defaults(fn=cmd_backtest)

    ps = sub.add_parser("screen", help="run filters + cluster detection on stored data")
    ps.add_argument("--window", type=int, default=14)
    ps.add_argument("--min-insiders", type=int, default=2)
    ps.add_argument("--top", type=int, default=25)
    ps.add_argument("--db", default="insider.db")
    ps.add_argument("--json", help="write cluster summaries to a JSON file")
    ps.add_argument("--show-killed", action="store_true")
    ps.add_argument("--offline", action="store_true", help="skip SIC lookups (no network)")
    ps.set_defaults(fn=cmd_screen)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
