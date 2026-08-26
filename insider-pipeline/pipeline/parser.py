"""Parse SEC Form 4 ownershipDocument XML into flat transaction records."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class Txn:
    accession: str
    issuer_cik: str
    issuer_name: str
    ticker: str
    owner_cik: str
    owner_name: str
    is_director: bool
    is_officer: bool
    is_ten_pct: bool
    officer_title: str
    trade_date: str          # YYYY-MM-DD
    code: str                # transaction code (P, S, A, ...)
    shares: float
    price: float
    acquired: bool           # A vs D
    owned_after: float
    direct: bool
    plan_10b5_1: bool        # aff10b5One checkbox (post-2023 filings)
    footnotes: str = ""
    flags: list[str] = field(default_factory=list)

    @property
    def value(self) -> float:
        return self.shares * self.price

    @property
    def delta_own_pct(self) -> float:
        prior = self.owned_after - self.shares if self.acquired else self.owned_after + self.shares
        if prior <= 0:
            return 999.0
        return round(100.0 * self.shares / prior, 1)

    @property
    def role_weight(self) -> float:
        title = self.officer_title.upper()
        if self.is_officer and any(k in title for k in ("CEO", "CHIEF EXEC", "CFO", "CHIEF FIN", "PRES")):
            return 3.0
        if self.is_officer:
            return 2.0
        if self.is_director:
            return 1.0
        if self.is_ten_pct:
            return 0.5
        return 0.5


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _text(el: ET.Element | None, path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.find(path)
    return (found.text or default).strip() if found is not None and found.text else default


def _val(el: ET.Element | None, path: str) -> str:
    """Form 4 wraps most fields as <field><value>x</value></field>."""
    return _text(el, path + "/value") or _text(el, path)


def _num(s: str) -> float:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def _bool(s: str) -> bool:
    return s.strip() in ("1", "true", "True")


def parse_form4(xml_text: str, accession: str = "") -> list[Txn]:
    """One Form 4 -> list of non-derivative transactions (all codes; filtering later)."""
    xml_text = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text).strip()
    root = ET.fromstring(xml_text)
    _strip_ns(root)

    issuer = root.find("issuer")
    issuer_cik = _text(issuer, "issuerCik")
    issuer_name = _text(issuer, "issuerName")
    ticker = _text(issuer, "issuerTradingSymbol").upper()

    plan_flag = _bool(_text(root, "aff10b5One"))

    footnotes = " ".join(
        (fn.text or "").strip() for fn in root.findall(".//footnotes/footnote")
    ).lower()

    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        owners.append({
            "cik": _text(ro, "reportingOwnerId/rptOwnerCik"),
            "name": _text(ro, "reportingOwnerId/rptOwnerName"),
            "is_director": _bool(_text(rel, "isDirector")),
            "is_officer": _bool(_text(rel, "isOfficer")),
            "is_ten_pct": _bool(_text(rel, "isTenPercentOwner")),
            "title": _text(rel, "officerTitle"),
        })
    if not owners:
        return []
    o = owners[0]  # joint filings are rare; first owner is primary

    txns: list[Txn] = []
    for t in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        txns.append(Txn(
            accession=accession,
            issuer_cik=issuer_cik,
            issuer_name=issuer_name,
            ticker=ticker,
            owner_cik=o["cik"],
            owner_name=o["name"],
            is_director=o["is_director"],
            is_officer=o["is_officer"],
            is_ten_pct=o["is_ten_pct"],
            officer_title=o["title"],
            trade_date=_val(t, "transactionDate"),
            code=_val(t, "transactionCoding/transactionCode") or _text(t, "transactionCoding/transactionCode"),
            shares=_num(_val(t, "transactionAmounts/transactionShares")),
            price=_num(_val(t, "transactionAmounts/transactionPricePerShare")),
            acquired=_val(t, "transactionAmounts/transactionAcquiredDisposedCode") == "A",
            owned_after=_num(_val(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
            direct=_val(t, "ownershipNature/directOrIndirectOwnership") == "D",
            plan_10b5_1=plan_flag,
            footnotes=footnotes,
        ))
    return txns
