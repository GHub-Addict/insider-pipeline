"""Mechanical kill filters. Cheap, deterministic, run before any AI spend.

Each filter returns a reason string if the transaction should be killed,
or adds a soft flag (score penalty, not removal). Tune thresholds at top.
"""
from __future__ import annotations

from .parser import Txn

MIN_TRADE_VALUE = 25_000          # ignore token buys
KILL_SICS = {"6770", "6726", "6722"}  # blank checks / SPACs, investment offices, closed-end mgmt
PLACEMENT_FOOTNOTE_HINTS = (
    "private placement", "securities purchase agreement", "subscription agreement",
    "registered direct", "underwritten offering", "pursuant to the offering",
)


def hard_kill(t: Txn, sic: str = "") -> str | None:
    """Return kill reason or None. Killed txns never reach clustering."""
    if t.code != "P" or not t.acquired:
        return "not an open-market purchase"
    if t.price <= 0 or t.shares <= 0:
        return "zero price/shares"
    if t.value < MIN_TRADE_VALUE:
        return f"below ${MIN_TRADE_VALUE:,} minimum"
    if t.plan_10b5_1 or "10b5-1" in t.footnotes or "10b5-l" in t.footnotes:
        return "10b5-1 plan trade (pre-scheduled, no conviction signal)"
    if sic in KILL_SICS:
        return f"SIC {sic}: fund/SPAC/blank-check structure"
    if not t.ticker or t.ticker in ("NONE", "N/A"):
        return "no listed ticker"
    if any(h in t.footnotes for h in PLACEMENT_FOOTNOTE_HINTS):
        return "footnote indicates placement/offering, not open-market buy"
    return None


def soft_flags(t: Txn) -> list[str]:
    """Non-fatal warnings that reduce cluster score."""
    flags = []
    # Suspiciously exact dollar target (e.g. 88,235 sh x $3.40 = $299,999) -> placement smell
    v = t.value
    nearest_k = round(v / 1000) * 1000
    if v >= 100_000 and abs(v - nearest_k) <= 2:
        flags.append("round-dollar buy (placement pattern)")
    # First-ever purchase by a director: often an obligation/optics buy
    if t.is_director and not t.is_officer and t.owned_after == t.shares and v < 100_000:
        flags.append("initial director buy (obligation pattern)")
    # Indirect ownership (trusts/LLCs) is fine but weaker as a personal-conviction read
    if not t.direct:
        flags.append("indirect ownership")
    return flags


def apply(txns: list[Txn], sic_lookup: dict[str, str] | None = None) -> tuple[list[Txn], list[tuple[Txn, str]]]:
    """Split into (survivors_with_flags, killed_with_reasons)."""
    sic_lookup = sic_lookup or {}
    survivors, killed = [], []
    for t in txns:
        reason = hard_kill(t, sic_lookup.get(t.issuer_cik, ""))
        if reason:
            killed.append((t, reason))
            continue
        t.flags = soft_flags(t)
        survivors.append(t)
    return survivors, killed
