"""Bulk loader for SEC's quarterly "Insider Transactions Data Sets" (Forms 3/4/5).

https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets

Quarterly flat files, extracted from the same XML-based fillable portion of
Forms 3/4/5 that the live daily-index scrape (edgar.py) reads, covering
January 2006 through the present. Downloading ~80 zip files replaces walking
15+ years of daily indexes one filing at a time.

Each zip unpacks to a handful of tab-separated tables joined on
ACCESSION_NUMBER:

  SUBMISSION.tsv      one row per accession — issuer CIK/name/ticker
  REPORTINGOWNER.tsv  one or more rows per accession — role flags, title
  NONDERIV_TRANS.tsv  one row per non-derivative transaction (Table I)
  FOOTNOTES.tsv       one row per footnote, joined back by accession

This module joins those tables and maps them into the same `Txn` dataclass
`parser.py` produces from the live XML feed, so `filters.py` and `cluster.py`
run over bulk history completely unchanged.

Column names below match SEC's documented schema (see
https://www.sec.gov/files/insider_transactions_readme.pdf). Lookups are
case-insensitive and fail loudly — listing the columns actually found — if a
required one is missing, since SEC has tweaked column casing across the
2006-present span and a silent wrong-column guess would corrupt the backtest
quietly.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from pathlib import Path

from . import edgar
from .parser import Txn

DATASET_URL = (
    "https://www.sec.gov/files/structureddata/data/"
    "insider-transactions-data-sets/{year}q{q}_form345.zip"
)
FIRST_QUARTER = (2006, 1)  # dataset starts January 2006


def quarters(start: tuple[int, int], end: tuple[int, int]):
    """Yield (year, quarter) pairs from start through end inclusive."""
    y, q = start
    while (y, q) <= end:
        yield (y, q)
        q += 1
        if q > 4:
            q = 1
            y += 1


def download_quarter(year: int, q: int, cache_dir: str | Path, force: bool = False) -> Path:
    """Download one quarterly zip into cache_dir; skip if already cached.

    Re-running a backfill (or resuming one interrupted mid-run) never
    re-downloads a quarter unless force=True — same idempotence contract as
    edgar.daily_form4_index's caller in cli.py.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{year}q{q}_form345.zip"
    if dest.exists() and not force:
        return dest
    url = DATASET_URL.format(year=year, q=q)
    dest.write_bytes(edgar.download_bytes(url))
    return dest


def _col(fieldnames: list[str], *candidates: str) -> str | None:
    """Case-insensitive lookup of the first matching column name present."""
    lookup = {f.lower(): f for f in fieldnames}
    for c in candidates:
        if c.lower() in lookup:
            return lookup[c.lower()]
    return None


def _read_tsv(zf: zipfile.ZipFile, name: str) -> tuple[list[str], list[dict]]:
    """Read one member (matched by suffix, case-insensitive) as tab-delimited rows."""
    match = next((n for n in zf.namelist() if n.upper().endswith(name.upper())), None)
    if not match:
        raise KeyError(f"{name} not found in archive (have: {zf.namelist()})")
    with zf.open(match) as f:
        text = io.TextIOWrapper(f, encoding="latin-1", newline="")
        reader = csv.DictReader(text, delimiter="\t")
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def _bool(v: str | None) -> bool:
    return str(v).strip() in ("1", "1.0", "true", "True", "Y", "y")


def _num(v: str | None) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _date(v: str | None) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return v[:10]  # best-effort: already-ISO timestamp or unrecognized format


def _cik(v: str | None) -> str:
    v = (v or "").strip()
    return v.zfill(10) if v.isdigit() else v


def load_zip(zip_path: str | Path, resolve_tickers: bool = True) -> list[Txn]:
    """Parse one quarterly zip into a flat list of Txn.

    Returns ALL non-derivative transactions (every code, buys and sells
    alike) — same contract as parser.parse_form4: filtering to open-market
    purchases happens downstream in filters.py, not here.

    Joint filings (>1 REPORTINGOWNER row per accession) use the first owner
    listed as primary, matching parser.py's "joint filings are rare" choice
    — so bulk and live ingestion agree on what a cluster's insider count is.
    """
    with zipfile.ZipFile(zip_path) as zf:
        sub_fields, sub_rows = _read_tsv(zf, "SUBMISSION.tsv")
        own_fields, own_rows = _read_tsv(zf, "REPORTINGOWNER.tsv")
        trans_fields, trans_rows = _read_tsv(zf, "NONDERIV_TRANS.tsv")
        try:
            fn_fields, fn_rows = _read_tsv(zf, "FOOTNOTES.tsv")
        except KeyError:
            fn_fields, fn_rows = [], []

    sub_acc_col = _col(sub_fields, "ACCESSION_NUMBER")
    cik_col = _col(sub_fields, "ISSUERCIK")
    name_col = _col(sub_fields, "ISSUERNAME")
    tkr_col = _col(sub_fields, "ISSUERTRADINGSYMBOL")
    if not sub_acc_col:
        raise KeyError(f"SUBMISSION.tsv missing ACCESSION_NUMBER; columns: {sub_fields}")

    submissions: dict[str, dict] = {}
    for r in sub_rows:
        acc = (r.get(sub_acc_col) or "").strip()
        submissions[acc] = {
            "cik": _cik(r.get(cik_col)) if cik_col else "",
            "name": (r.get(name_col) or "").strip() if name_col else "",
            "ticker": (r.get(tkr_col) or "").strip().upper() if tkr_col else "",
        }

    own_acc_col = _col(own_fields, "ACCESSION_NUMBER")
    owner_cik_col = _col(own_fields, "RPTOWNERCIK")
    owner_name_col = _col(own_fields, "RPTOWNERNAME")
    is_dir_col = _col(own_fields, "ISDIRECTOR")
    is_off_col = _col(own_fields, "ISOFFICER")
    is_10_col = _col(own_fields, "ISTENPERCENTOWNER")
    title_col = _col(own_fields, "OFFICERTITLE")
    if not own_acc_col:
        raise KeyError(f"REPORTINGOWNER.tsv missing ACCESSION_NUMBER; columns: {own_fields}")

    primary_owner: dict[str, dict] = {}
    for r in own_rows:
        acc = (r.get(own_acc_col) or "").strip()
        if acc in primary_owner:
            continue  # first owner listed is primary
        primary_owner[acc] = {
            "cik": _cik(r.get(owner_cik_col)) if owner_cik_col else "",
            "name": (r.get(owner_name_col) or "").strip() if owner_name_col else "",
            "is_director": _bool(r.get(is_dir_col)) if is_dir_col else False,
            "is_officer": _bool(r.get(is_off_col)) if is_off_col else False,
            "is_ten_pct": _bool(r.get(is_10_col)) if is_10_col else False,
            "title": (r.get(title_col) or "").strip() if title_col else "",
        }

    fn_acc_col = _col(fn_fields, "ACCESSION_NUMBER")
    fn_text_col = _col(fn_fields, "FOOTNOTE_TEXT")
    footnotes_by_acc: dict[str, list[str]] = {}
    if fn_acc_col and fn_text_col:
        for r in fn_rows:
            acc = (r.get(fn_acc_col) or "").strip()
            txt = (r.get(fn_text_col) or "").strip()
            if txt:
                footnotes_by_acc.setdefault(acc, []).append(txt)

    t_acc_col = _col(trans_fields, "ACCESSION_NUMBER")
    code_col = _col(trans_fields, "TRANS_CODE")
    date_col = _col(trans_fields, "TRANS_DATE")
    shares_col = _col(trans_fields, "TRANS_SHARES")
    price_col = _col(trans_fields, "TRANS_PRICEPERSHARE")
    acq_col = _col(trans_fields, "TRANS_ACQUIRED_DISP_CD")
    owned_col = _col(trans_fields, "SHRS_OWND_FOLWNG_TRANS")
    direct_col = _col(trans_fields, "DIRECT_INDIRECT_OWNERSHIP")
    required = {
        "ACCESSION_NUMBER": t_acc_col, "TRANS_CODE": code_col, "TRANS_DATE": date_col,
        "TRANS_SHARES": shares_col, "TRANS_PRICEPERSHARE": price_col,
        "TRANS_ACQUIRED_DISP_CD": acq_col, "SHRS_OWND_FOLWNG_TRANS": owned_col,
        "DIRECT_INDIRECT_OWNERSHIP": direct_col,
    }
    missing = [k for k, v in required.items() if v is None]
    if missing:
        raise KeyError(
            f"NONDERIV_TRANS.tsv missing expected columns {missing}; found: {trans_fields}"
        )

    txns: list[Txn] = []
    for r in trans_rows:
        acc = (r.get(t_acc_col) or "").strip()
        sub = submissions.get(acc)
        owner = primary_owner.get(acc)
        if not sub or not owner:
            continue  # orphaned transaction row — shouldn't happen, never crash the run
        ticker = sub["ticker"]
        if resolve_tickers and (not ticker or ticker in ("NONE", "N/A", "N.A.")):
            ticker = edgar.resolve_ticker(sub["cik"])
        txns.append(Txn(
            accession=acc,
            issuer_cik=sub["cik"],
            issuer_name=sub["name"],
            ticker=ticker,
            owner_cik=owner["cik"],
            owner_name=owner["name"],
            is_director=owner["is_director"],
            is_officer=owner["is_officer"],
            is_ten_pct=owner["is_ten_pct"],
            officer_title=owner["title"],
            trade_date=_date(r.get(date_col)),
            code=(r.get(code_col) or "").strip(),
            shares=_num(r.get(shares_col)),
            price=_num(r.get(price_col)),
            acquired=(r.get(acq_col) or "").strip().upper() == "A",
            owned_after=_num(r.get(owned_col)),
            direct=(r.get(direct_col) or "").strip().upper() == "D",
            # aff10b5One only exists on post-2023 filings and isn't broken out as
            # its own dataset column; filters.py's footnote-text regex is the
            # fallback signal for older history, same as it is for live filings
            # that leave the checkbox blank.
            plan_10b5_1=False,
            footnotes=" ".join(footnotes_by_acc.get(acc, [])).lower(),
        ))
    return txns
