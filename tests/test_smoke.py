"""Offline smoke test: parser -> filters -> cluster scoring. Run: python tests/test_smoke.py"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0].parent))

from pipeline import filters
from pipeline.cluster import detect
from pipeline.parser import Txn, parse_form4

FIXTURE = """<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>4</documentType>
  <aff10b5One>0</aff10b5One>
  <issuer>
    <issuerCik>0001234567</issuerCik>
    <issuerName>Team Inc</issuerName>
    <issuerTradingSymbol>TISI</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0007654321</rptOwnerCik>
      <rptOwnerName>Doe Jane</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>Chief Financial Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-08-21</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>21.05</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>40000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def mk(owner, ticker="TISI", cik="1", shares=10_000, price=21.05, owned_after=40_000,
       officer=True, director=False, ten_pct=False, title="CFO", date="2026-08-21",
       code="P", plan=False, footnotes=""):
    return Txn(accession="t", issuer_cik=cik, issuer_name=ticker + " Co", ticker=ticker,
               owner_cik=owner, owner_name=owner, is_director=director, is_officer=officer,
               is_ten_pct=ten_pct, officer_title=title, trade_date=date, code=code,
               shares=shares, price=price, acquired=True, owned_after=owned_after,
               direct=True, plan_10b5_1=plan, footnotes=footnotes)


def main():
    # 1. Parser
    txns = parse_form4(FIXTURE, accession="0001234567-26-000001")
    assert len(txns) == 1, "should parse one transaction"
    t = txns[0]
    assert t.ticker == "TISI" and t.code == "P" and t.acquired
    assert t.value == 210_500 and t.role_weight == 3.0
    assert t.delta_own_pct == 33.3, t.delta_own_pct
    print(f"parser        OK  TISI CFO buy ${t.value:,.0f}, ΔOwn {t.delta_own_pct}%")

    # 2. Filters: kills
    raw = [
        t,                                                            # good
        mk("plan_guy", plan=True),                                    # 10b5-1 -> kill
        mk("tiny", shares=200, price=20),                             # $4k -> kill
        mk("placement", footnotes="shares acquired in a private placement"),  # kill
        mk("spac", ticker="PNAQ", cik="2"),                           # SIC kill
        mk("seller", code="S"),                                       # not P -> kill
    ]
    survivors, killed = filters.apply(raw, sic_lookup={"2": "6770"})
    assert len(survivors) == 1 and len(killed) == 5, (len(survivors), len(killed))
    reasons = {r for _, r in killed}
    assert any("10b5-1" in r for r in reasons)
    assert any("SPAC" in r or "6770" in r for r in reasons)
    assert any("placement" in r for r in reasons)
    print(f"filters       OK  6 raw -> 1 survivor, kills: {len(killed)}")

    # 3. Soft flags
    round_buy = mk("rounder", shares=88_235, price=3.40, owned_after=89_008)
    s2, _ = filters.apply([round_buy])
    assert any("round-dollar" in f for f in s2[0].flags), s2[0].flags
    print(f"soft flags    OK  88,235 x $3.40 flagged as: {s2[0].flags[0]}")

    # 4. Clustering + score ordering
    as_of = dt.date(2026, 8, 26)
    strong = [mk(o, cik="10", ticker="NVRI", title=ti, officer=of, director=dr)
              for o, ti, of, dr in [("ceo", "CEO", True, False), ("cfo", "CFO", True, False),
                                    ("dir1", "", False, True), ("dir2", "", False, True)]]
    weak = [mk(o, cik="11", ticker="WEAK", shares=2000, price=15, owned_after=100_000,
               officer=False, director=True, title="") for o in ("a", "b")]
    clusters = detect(strong + weak, window_days=14, min_insiders=2, as_of=as_of)
    assert [c.ticker for c in clusters] == ["NVRI", "WEAK"]
    assert clusters[0].insiders == 4 and clusters[0].score > clusters[1].score
    print(f"clustering    OK  NVRI(4 insiders, score {clusters[0].score}) > "
          f"WEAK(2 insiders, score {clusters[1].score})")

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
