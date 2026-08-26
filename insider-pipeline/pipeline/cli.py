"""CLI.

  python -m pipeline.cli ingest --start 2026-08-11 --end 2026-08-26
  python -m pipeline.cli screen --window 14 --min-insiders 2 [--json out.json]

`ingest` is resumable and idempotent: re-running skips completed days.
Backfilling years for the backtest is the same command with a wider range
(expect it to take a while — SEC rate limits are the bottleneck, ~2-4k
Form 4s per business day).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from . import edgar, filters, parser, store
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
