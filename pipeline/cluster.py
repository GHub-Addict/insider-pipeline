"""Cluster detection: group surviving purchases by issuer over a trailing window,
score by breadth (distinct insiders), seniority, size, and conviction (ΔOwn).

Score = breadth term + seniority term + size term + conviction term - flag penalty.
All constants are deliberately visible and tunable — this recipe IS the product,
and it's what the backtest will iterate on.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field

from .parser import Txn

W_BREADTH = 2.0      # per distinct insider beyond the first
W_SENIORITY = 1.0    # x mean role weight
W_SIZE = 1.0         # x log10(total $) - 4  (so $100k≈1, $1M≈2, $10M≈3)
W_CONVICTION = 1.5   # x capped median ΔOwn%
FLAG_PENALTY = 0.75  # per flagged transaction


@dataclass
class Cluster:
    ticker: str
    issuer_name: str
    issuer_cik: str
    txns: list[Txn] = field(default_factory=list)

    @property
    def insiders(self) -> int:
        return len({t.owner_cik for t in self.txns})

    @property
    def total_value(self) -> float:
        return sum(t.value for t in self.txns)

    @property
    def median_delta_own(self) -> float:
        return statistics.median(min(t.delta_own_pct, 100.0) for t in self.txns)

    @property
    def first_date(self) -> str:
        return min(t.trade_date for t in self.txns)

    @property
    def last_date(self) -> str:
        return max(t.trade_date for t in self.txns)

    @property
    def flag_count(self) -> int:
        return sum(len(t.flags) for t in self.txns)

    @property
    def score(self) -> float:
        breadth = W_BREADTH * (self.insiders - 1)
        seniority = W_SENIORITY * statistics.mean(t.role_weight for t in self.txns)
        size = W_SIZE * max(0.0, math.log10(max(self.total_value, 1)) - 4)
        conviction = W_CONVICTION * (self.median_delta_own / 100.0)
        return round(breadth + seniority + size + conviction - FLAG_PENALTY * self.flag_count, 2)

    def summary(self) -> dict:
        return {
            "ticker": self.ticker,
            "company": self.issuer_name,
            "score": self.score,
            "insiders": self.insiders,
            "total_value": round(self.total_value),
            "median_delta_own_pct": self.median_delta_own,
            "window": f"{self.first_date}..{self.last_date}",
            "flags": sorted({f for t in self.txns for f in t.flags}),
            "buyers": sorted({f"{t.owner_name} ({t.officer_title or ('Dir' if t.is_director else '10%')})"
                              for t in self.txns}),
        }


def detect(txns: list[Txn], window_days: int = 14, min_insiders: int = 2,
           as_of: dt.date | None = None) -> list[Cluster]:
    """Cluster purchases per issuer within the trailing window ending at as_of."""
    as_of = as_of or dt.date.today()
    cutoff = as_of - dt.timedelta(days=window_days)

    def in_window(t: Txn) -> bool:
        try:
            d = dt.date.fromisoformat(t.trade_date)
        except ValueError:
            return False
        return cutoff <= d <= as_of

    by_issuer: dict[str, Cluster] = {}
    for t in txns:
        if not in_window(t):
            continue
        c = by_issuer.setdefault(t.issuer_cik, Cluster(t.ticker, t.issuer_name, t.issuer_cik))
        c.txns.append(t)

    clusters = [c for c in by_issuer.values() if c.insiders >= min_insiders]
    return sorted(clusters, key=lambda c: c.score, reverse=True)
