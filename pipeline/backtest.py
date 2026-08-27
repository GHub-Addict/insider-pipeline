"""Replay engine: run the screen as-of each historical date, join the top
clusters against forward 6/12-month returns, compare to VTI.

`cluster.detect()` already takes an `as_of` parameter for exactly this — the
mechanical filters (filters.py) are pure per-transaction functions that never
look past the transaction itself, so they can be applied once up front; only
the clustering step needs to be re-run per as-of date to respect the trailing
window.

THE ONE THING THIS MODULE REFUSES TO PAPER OVER: survivorship bias. A free
price feed (yfinance included) drops delisted tickers from its history.
Companies that get desperate insider buying ahead of a death spiral are
exactly the companies that sometimes die — so a survivorship-biased backtest
doesn't just have noisy numbers, it has a systematic upward bias baked in:
the worst outcomes silently vanish from the sample instead of counting as
losses. A backtest built on yfinance will look profitable right up until real
money is behind it. See README.md's Backtest section.

Every PriceProvider must declare `survivorship_bias_free` honestly.
`YFinancePriceProvider` refuses to run at all unless you explicitly
acknowledge that its numbers are not trustworthy for a real decision.
`summarize()` prints the warning in the report itself, not just here in a
docstring nobody reads before running the thing.
"""
from __future__ import annotations

import abc
import bisect
import csv
import datetime as dt
import statistics
from dataclasses import dataclass

from . import filters, store
from .cluster import detect

DEFAULT_HOLDOUT_START = "2022-01-01"  # per README: never tune thresholds on this


class PriceProvider(abc.ABC):
    """Interface for a price lookup used to compute forward returns.

    `survivorship_bias_free` must be set honestly by subclasses — it's the
    difference between a backtest and a story you're telling yourself.
    """

    survivorship_bias_free: bool = False

    @abc.abstractmethod
    def price_on_or_after(self, ticker: str, date: dt.date) -> tuple[dt.date, float] | None:
        """Nearest trading price on/after `date`, or None if unavailable."""

    def forward_return(self, ticker: str, start: dt.date, months: int) -> float | None:
        """Total return from `start` to `start + months`, or None if either
        endpoint is unavailable. Never fabricated, never zero-filled."""
        entry = self.price_on_or_after(ticker, start)
        if entry is None:
            return None
        target = start + dt.timedelta(days=30 * months)
        exit_ = self.price_on_or_after(ticker, target)
        if exit_ is None:
            return None
        return exit_[1] / entry[1] - 1.0

    def missing_kind(self, ticker: str, start: dt.date, months: int) -> str:
        """'ok' | 'never_listed' | 'stopped_mid_window'.

        `stopped_mid_window` is the tell for a likely delisting: the ticker
        had a price at `start` but the series runs out before the forward
        window completes. Callers must report this count, not silently drop
        it — dropping it is exactly the survivorship-bias failure mode.
        """
        entry = self.price_on_or_after(ticker, start)
        if entry is None:
            return "never_listed"
        target = start + dt.timedelta(days=30 * months)
        exit_ = self.price_on_or_after(ticker, target)
        if exit_ is None:
            return "stopped_mid_window"
        return "ok"


class CSVPriceProvider(PriceProvider):
    """Loads a long-format CSV: columns `ticker,date,price` (or
    `ticker,date,total_return_index`).

    Bring your own survivorship-bias-free source — a CRSP extract, Sharadar
    SEP+SFP (SFP carries delisted names), Norgate, or similar — that keeps
    delisted tickers in the series through their last trade. Only set
    survivorship_bias_free=True once you've verified that yourself; this
    class takes your word for it and has no way to check.
    """

    def __init__(self, path: str, survivorship_bias_free: bool = False):
        self.survivorship_bias_free = survivorship_bias_free
        self._series: dict[str, list[tuple[dt.date, float]]] = {}
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                t = row["ticker"].strip().upper()
                d = dt.date.fromisoformat(row["date"].strip()[:10])
                raw = row.get("total_return_index") or row.get("price")
                self._series.setdefault(t, []).append((d, float(raw)))
        for series in self._series.values():
            series.sort(key=lambda x: x[0])

    def price_on_or_after(self, ticker, date):
        series = self._series.get(ticker.upper())
        if not series:
            return None
        dates = [d for d, _ in series]
        i = bisect.bisect_left(dates, date)
        if i >= len(series):
            return None
        return series[i]


class YFinancePriceProvider(PriceProvider):
    """Convenience provider backed by yfinance (Yahoo Finance).

    Yahoo's free feed drops tickers once they delist, which is precisely the
    outcome an insider-cluster screen most needs to be graded on. This exists
    for a quick "does the recipe even point at real winners among names still
    trading" gut check — never for a number fed into a go/no-go decision.
    Refuses to instantiate without an explicit acknowledgment of that.
    """

    survivorship_bias_free = False

    def __init__(self, acknowledge_survivorship_bias: bool = False):
        if not acknowledge_survivorship_bias:
            raise RuntimeError(
                "YFinancePriceProvider drops delisted tickers from its price "
                "history. Insider clusters are disproportionately likely at "
                "companies that later got delisted (desperate buying ahead of "
                "distress looks identical to conviction buying ahead of a "
                "recovery until you check whether the company still exists) — "
                "so a backtest built on this will look profitable right up "
                "until real money is behind it. Pass "
                "acknowledge_survivorship_bias=True only for a quick sanity "
                "check you don't intend to act on; use CSVPriceProvider with a "
                "delisting-inclusive source (CRSP, Sharadar SEP+SFP, Norgate) "
                "for anything real. See README.md#backtest."
            )
        try:
            import yfinance as yf
        except ImportError as e:
            raise RuntimeError("pip install yfinance to use YFinancePriceProvider") from e
        self._yf = yf
        self._cache: dict[str, object] = {}

    def _history(self, ticker: str):
        if ticker not in self._cache:
            self._cache[ticker] = self._yf.Ticker(ticker).history(
                period="max", auto_adjust=True
            )
        return self._cache[ticker]

    def price_on_or_after(self, ticker, date):
        hist = self._history(ticker)
        if hist.empty:
            return None
        idx = hist.index[hist.index.date >= date]
        if len(idx) == 0:
            return None
        ts = idx[0]
        return ts.date(), float(hist.loc[ts, "Close"])


@dataclass
class Trade:
    as_of: str
    ticker: str
    score: float
    insiders: int
    fwd_6m: float | None
    fwd_12m: float | None
    vti_6m: float | None
    vti_12m: float | None
    missing_6m: str
    missing_12m: str


def replay(
    conn,
    start: dt.date,
    end: dt.date,
    price_provider: PriceProvider,
    window_days: int = 14,
    min_insiders: int = 2,
    top_n: int = 5,
    step_days: int = 7,
    benchmark: str = "VTI",
) -> list[Trade]:
    """Run detect() as-of every `step_days` between start and end, keep the
    top `top_n` clusters each time, and look up forward returns.

    A cluster that stays top-scored across several consecutive as-of dates
    (weekly steps, a 14-day window) is deduped to a single trade keyed on
    (ticker, cluster.first_date) — otherwise a single real cluster event
    would be counted, and its return double-counted, several times over.
    """
    survivors_all, _ = filters.apply(store.load_purchases(conn))
    survivors_all.sort(key=lambda t: t.trade_date)
    dates = [t.trade_date for t in survivors_all]

    seen: set[tuple[str, str]] = set()
    trades: list[Trade] = []
    d = start
    while d <= end:
        lo = (d - dt.timedelta(days=window_days)).isoformat()
        hi = d.isoformat()
        window_txns = survivors_all[bisect.bisect_left(dates, lo):bisect.bisect_right(dates, hi)]
        clusters = detect(window_txns, window_days=window_days, min_insiders=min_insiders, as_of=d)
        for c in clusters[:top_n]:
            key = (c.ticker, c.first_date)
            if key in seen:
                continue
            seen.add(key)
            trades.append(Trade(
                as_of=d.isoformat(),
                ticker=c.ticker,
                score=c.score,
                insiders=c.insiders,
                fwd_6m=price_provider.forward_return(c.ticker, d, 6),
                fwd_12m=price_provider.forward_return(c.ticker, d, 12),
                vti_6m=price_provider.forward_return(benchmark, d, 6),
                vti_12m=price_provider.forward_return(benchmark, d, 12),
                missing_6m=price_provider.missing_kind(c.ticker, d, 6),
                missing_12m=price_provider.missing_kind(c.ticker, d, 12),
            ))
        d += dt.timedelta(days=step_days)
    return trades


def _stats(trades: list[Trade], horizon: int) -> dict:
    fwd_attr, vti_attr, missing_attr = f"fwd_{horizon}m", f"vti_{horizon}m", f"missing_{horizon}m"
    n_total = len(trades)
    n_missing = sum(1 for t in trades if getattr(t, missing_attr) == "stopped_mid_window")
    pairs = [
        (getattr(t, fwd_attr), getattr(t, vti_attr)) for t in trades
        if getattr(t, fwd_attr) is not None and getattr(t, vti_attr) is not None
    ]
    out = {
        "n_trades": n_total,
        "n_priced": len(pairs),
        "n_likely_delisted_excluded": n_missing,
        "pct_likely_delisted_excluded": round(100 * n_missing / n_total, 1) if n_total else 0.0,
    }
    if not pairs:
        out.update(mean_return_pct=None, mean_vti_return_pct=None,
                    mean_excess_pct=None, hit_rate_vs_vti_pct=None)
        return out
    fwd = [f for f, _ in pairs]
    vti = [v for _, v in pairs]
    excess = [f - v for f, v in pairs]
    out.update(
        mean_return_pct=round(statistics.mean(fwd) * 100, 2),
        mean_vti_return_pct=round(statistics.mean(vti) * 100, 2),
        mean_excess_pct=round(statistics.mean(excess) * 100, 2),
        hit_rate_vs_vti_pct=round(100 * sum(1 for e in excess if e > 0) / len(excess), 1),
    )
    return out


def summarize(
    trades: list[Trade], price_provider: PriceProvider, holdout_start: str = DEFAULT_HOLDOUT_START
) -> dict:
    """Tuning-period stats (fair game for iterating on cluster.py's constants)
    kept strictly separate from holdout-period stats (2022-2024 by default —
    look at these only for a final validation, never to pick thresholds)."""
    tune = [t for t in trades if t.as_of < holdout_start]
    holdout = [t for t in trades if t.as_of >= holdout_start]
    result: dict = {"survivorship_bias_free": price_provider.survivorship_bias_free}
    if not price_provider.survivorship_bias_free:
        result["warning"] = (
            "This price source drops delisted tickers. Clusters at companies "
            "that later delisted are UNDER-represented here relative to "
            "reality, so every number below is optimistically biased. Do not "
            "treat this as a real backtest — see README.md#backtest."
        )
    result["tuning_period"] = {"6m": _stats(tune, 6), "12m": _stats(tune, 12)}
    result["holdout_period"] = {
        "note": "Held out per README — do not iterate on cluster.py/filters.py "
                "thresholds using these numbers; that defeats the point of holding it out.",
        "6m": _stats(holdout, 6), "12m": _stats(holdout, 12),
    }
    return result
