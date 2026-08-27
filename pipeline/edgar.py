"""EDGAR client: daily form indexes, Form 4 submission fetch, company metadata.

SEC fair-access rules: max 10 req/s, declare a User-Agent with contact info.
Set EDGAR_USER_AGENT, e.g.:  export EDGAR_USER_AGENT="Peak LLC owen@example.com"
"""
from __future__ import annotations

import os
import re
import time
import datetime as dt
from dataclasses import dataclass

import requests

BASE = "https://www.sec.gov"
DATA = "https://data.sec.gov"
_MIN_INTERVAL = 0.13  # ~7.5 req/s, under SEC's 10/s ceiling
_last_request = 0.0

_session = requests.Session()


def _headers() -> dict:
    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        raise RuntimeError(
            "Set EDGAR_USER_AGENT to 'YourName/Company contact@email' (SEC requires it)."
        )
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


_RETRY_STATUS = (403, 429, 500, 502, 503, 504)


def _get(url: str, tries: int = 4) -> requests.Response:
    """Throttled GET with exponential backoff on transient/rate-limit responses."""
    global _last_request
    last_exc: Exception | None = None
    for attempt in range(tries):
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = _session.get(url, headers=_headers(), timeout=30)
            _last_request = time.monotonic()
        except requests.RequestException as e:  # connection reset, timeout
            last_exc = e
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in _RETRY_STATUS and attempt < tries - 1:
            time.sleep(2 ** attempt * 2)  # 2s, 4s, 8s
            continue
        resp.raise_for_status()
        return resp
    if last_exc:
        raise last_exc
    resp.raise_for_status()
    return resp


@dataclass
class IndexEntry:
    cik: str
    company: str
    date_filed: str
    path: str  # e.g. edgar/data/320193/0000320193-26-000001.txt

    @property
    def accession(self) -> str:
        m = re.search(r"(\d{10}-\d{2}-\d{6})", self.path)
        return m.group(1) if m else ""


def quarter(d: dt.date) -> int:
    return (d.month - 1) // 3 + 1


def parse_idx(text: str) -> list[IndexEntry]:
    """Parse a form.YYYYMMDD.idx body into Form 4 entries.

    Columns: Form Type | Company Name | CIK | Date Filed | File Name.
    Company names can contain runs of 2+ spaces, so fields are read from the
    RIGHT (path, date, cik are always the last three) instead of the left.
    """
    entries: list[IndexEntry] = []
    in_body = False
    for line in text.splitlines():
        if line.startswith("----"):
            in_body = True
            continue
        if not in_body or not line.strip():
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5 or parts[0] != "4":
            continue
        path, date_filed, cik = parts[-1], parts[-2], parts[-3]
        # Guards: a malformed row must be skipped, never fetched.
        if not path.startswith("edgar/") or not path.endswith(".txt"):
            continue
        if not cik.isdigit() or not re.fullmatch(r"\d{8}", date_filed):
            continue
        company = " ".join(parts[1:-3]).strip()
        entries.append(IndexEntry(cik=cik, company=company, date_filed=date_filed, path=path))
    return entries


def daily_form4_index(day: dt.date) -> list[IndexEntry]:
    """All Form 4 filings in EDGAR's daily index for one date. [] on non-business days."""
    if day.weekday() >= 5:  # Sat/Sun — no index file exists, don't even ask
        return []
    url = f"{BASE}/Archives/edgar/daily-index/{day.year}/QTR{quarter(day)}/form.{day:%Y%m%d}.idx"
    try:
        text = _get(url).text
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        if code in (403, 404):
            # EDGAR serves 403 (not 404) for index files that don't exist,
            # e.g. market holidays. Treat as an empty day.
            print(f"  note {day}: no daily index (holiday?) — skipping")
            return []
        raise
    return parse_idx(text)


def download_bytes(url: str) -> bytes:
    """Throttled, retrying binary GET — for the bulk dataset zips."""
    return _get(url).content


_XML_RE = re.compile(r"<XML>(.*?)</XML>", re.DOTALL | re.IGNORECASE)


def fetch_form4_xml(entry: IndexEntry) -> str | None:
    """Fetch the full .txt submission and extract the embedded ownershipDocument XML."""
    url = f"{BASE}/Archives/{entry.path}"
    text = _get(url).text
    for m in _XML_RE.finditer(text):
        chunk = m.group(1).strip()
        if "<ownershipDocument" in chunk:
            return chunk
    return None


_meta_cache: dict[str, dict] = {}
_ticker_map: dict[str, str] | None = None


def ticker_map() -> dict[str, str]:
    """CIK (no leading zeros) -> primary ticker, from SEC's master list. Fetched once."""
    global _ticker_map
    if _ticker_map is None:
        try:
            j = _get(f"{BASE}/files/company_tickers.json").json()
            _ticker_map = {str(row["cik_str"]): str(row["ticker"]).upper()
                           for row in j.values() if row.get("ticker")}
        except Exception:
            _ticker_map = {}
    return _ticker_map


def resolve_ticker(cik: str) -> str:
    """Fall back for filings that leave issuerTradingSymbol blank/NONE."""
    if not cik or not cik.strip().isdigit():
        return ""
    return ticker_map().get(str(int(cik)), "")


def company_meta(cik: str) -> dict:
    """SIC code + name from the submissions API. Cached per run."""
    cik10 = cik.zfill(10)
    if cik10 in _meta_cache:
        return _meta_cache[cik10]
    try:
        j = _get(f"{DATA}/submissions/CIK{cik10}.json").json()
        meta = {"sic": str(j.get("sic") or ""), "sic_desc": j.get("sicDescription") or "",
                "name": j.get("name") or ""}
    except Exception:
        meta = {"sic": "", "sic_desc": "", "name": ""}
    _meta_cache[cik10] = meta
    return meta
