"""SQLite persistence. One row per parsed transaction; idempotent on re-ingest."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .parser import Txn

SCHEMA = """
CREATE TABLE IF NOT EXISTS txns (
    accession TEXT, issuer_cik TEXT, issuer_name TEXT, ticker TEXT,
    owner_cik TEXT, owner_name TEXT, is_director INT, is_officer INT,
    is_ten_pct INT, officer_title TEXT, trade_date TEXT, code TEXT,
    shares REAL, price REAL, acquired INT, owned_after REAL, direct INT,
    plan_10b5_1 INT, footnotes TEXT, flags TEXT,
    PRIMARY KEY (accession, owner_cik, trade_date, code, shares, price)
);
CREATE INDEX IF NOT EXISTS idx_issuer_date ON txns (issuer_cik, trade_date);
CREATE TABLE IF NOT EXISTS ingested_days (day TEXT PRIMARY KEY, filings INT);
"""


def connect(path: str | Path = "insider.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def save(conn: sqlite3.Connection, txns: list[Txn]) -> int:
    rows = [(t.accession, t.issuer_cik, t.issuer_name, t.ticker, t.owner_cik, t.owner_name,
             int(t.is_director), int(t.is_officer), int(t.is_ten_pct), t.officer_title,
             t.trade_date, t.code, t.shares, t.price, int(t.acquired), t.owned_after,
             int(t.direct), int(t.plan_10b5_1), t.footnotes, json.dumps(t.flags))
            for t in txns]
    with conn:
        conn.executemany("INSERT OR IGNORE INTO txns VALUES (" + ",".join("?" * 20) + ")", rows)
    return len(rows)


def load_purchases(conn: sqlite3.Connection, since: str | None = None) -> list[Txn]:
    q = "SELECT * FROM txns WHERE code='P' AND acquired=1"
    args: tuple = ()
    if since:
        q += " AND trade_date >= ?"
        args = (since,)
    out = []
    for r in conn.execute(q, args):
        t = Txn(accession=r[0], issuer_cik=r[1], issuer_name=r[2], ticker=r[3],
                owner_cik=r[4], owner_name=r[5], is_director=bool(r[6]), is_officer=bool(r[7]),
                is_ten_pct=bool(r[8]), officer_title=r[9], trade_date=r[10], code=r[11],
                shares=r[12], price=r[13], acquired=bool(r[14]), owned_after=r[15],
                direct=bool(r[16]), plan_10b5_1=bool(r[17]), footnotes=r[18] or "")
        t.flags = json.loads(r[19] or "[]")
        out.append(t)
    return out


def mark_day(conn: sqlite3.Connection, day: str, filings: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO ingested_days VALUES (?,?)", (day, filings))


def day_done(conn: sqlite3.Connection, day: str) -> bool:
    return conn.execute("SELECT 1 FROM ingested_days WHERE day=?", (day,)).fetchone() is not None
