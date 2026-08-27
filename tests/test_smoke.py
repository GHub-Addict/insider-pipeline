"""Offline smoke test: parser -> filters -> cluster scoring. Run: python tests/test_smoke.py"""
import datetime as dt
import io
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0].parent))

from pipeline import backtest as bt
from pipeline import bulk_loader, filters, store
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

    # 5. Bulk loader: quarterly TSV zip -> same Txn shape as the live XML parser
    test_bulk_loader()

    # 6. Backtest replay: as-of clustering -> forward returns, with delisting tracked, not dropped
    test_backtest()

    print("\nALL SMOKE TESTS PASSED")


def _mk_dataset_zip() -> str:
    """Fabricate a minimal quarterly zip matching SEC's documented TSV schema."""
    files = {
        "SUBMISSION.tsv": (
            "ACCESSION_NUMBER\tFILING_DATE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL\n"
            "0001234567-26-000001\t2026-08-24\t0001234567\tTeam Inc\tTISI\n"
        ),
        "REPORTINGOWNER.tsv": (
            "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tISDIRECTOR\tISOFFICER\t"
            "ISTENPERCENTOWNER\tOFFICERTITLE\n"
            "0001234567-26-000001\t0007654321\tDoe Jane\t0\t1\t0\tChief Financial Officer\n"
        ),
        "NONDERIV_TRANS.tsv": (
            "ACCESSION_NUMBER\tNONDERIV_TRANS_SK\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\t"
            "TRANS_PRICEPERSHARE\tTRANS_ACQUIRED_DISP_CD\tSHRS_OWND_FOLWNG_TRANS\t"
            "DIRECT_INDIRECT_OWNERSHIP\n"
            "0001234567-26-000001\t1\t2026-08-21\tP\t10000\t21.05\tA\t40000\tD\n"
        ),
        "FOOTNOTES.tsv": "ACCESSION_NUMBER\tFOOTNOTE_ID\tFOOTNOTE_TEXT\n",
    }
    fd, path = tempfile.mkstemp(suffix=".zip")
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def test_bulk_loader():
    zip_path = _mk_dataset_zip()
    txns = bulk_loader.load_zip(zip_path, resolve_tickers=False)
    assert len(txns) == 1, "should parse one transaction"
    t = txns[0]
    assert t.ticker == "TISI" and t.code == "P" and t.acquired
    assert t.issuer_cik == "0001234567" and t.owner_cik == "0007654321"
    assert t.officer_title == "Chief Financial Officer" and t.is_officer and not t.is_director
    assert t.value == 210_500 and t.role_weight == 3.0
    assert t.direct is True
    survivors, killed = filters.apply(txns)
    assert len(survivors) == 1 and not killed, "should pass filters unchanged, same as live-parsed Txns"
    print(f"bulk_loader   OK  TSV zip -> Txn matches live parser shape, ${t.value:,.0f} CFO buy")


class _FakePriceProvider(bt.PriceProvider):
    """In-memory PriceProvider for testing replay()/summarize() without network."""

    survivorship_bias_free = True

    def __init__(self, series: dict[str, list[tuple[dt.date, float]]]):
        self._series = series

    def price_on_or_after(self, ticker, date):
        for d, p in self._series.get(ticker, []):
            if d >= date:
                return (d, p)
        return None


def test_backtest():
    conn = store.connect(":memory:")
    as_of = dt.date(2015, 1, 8)

    def mk(owner, ticker, cik, title, officer, director):
        return Txn(accession="t", issuer_cik=cik, issuer_name=ticker + " Co", ticker=ticker,
                   owner_cik=owner, owner_name=owner, is_director=director, is_officer=officer,
                   is_ten_pct=False, officer_title=title, trade_date="2015-01-02", code="P",
                   shares=5000, price=10.0, acquired=True, owned_after=20000, direct=True,
                   plan_10b5_1=False)

    # NVRI: 2-insider cluster with priced forward returns; DELI: 2-insider cluster
    # that stops trading before the forward window completes (simulated delisting).
    txns = [
        mk("ceo", "NVRI", "10", "CEO", True, False),
        mk("cfo", "NVRI", "10", "CFO", True, False),
        mk("dir1", "DELI", "11", "", False, True),
        mk("dir2", "DELI", "11", "", False, True),
    ]
    store.save(conn, txns)

    provider = _FakePriceProvider({
        "NVRI": [(as_of, 100.0), (dt.date(2015, 7, 7), 130.0), (dt.date(2016, 1, 3), 150.0)],
        "VTI": [(as_of, 100.0), (dt.date(2015, 7, 7), 105.0), (dt.date(2016, 1, 3), 110.0)],
        "DELI": [(as_of, 50.0)],  # no further prices -- delisted before either forward window
    })

    trades = bt.replay(conn, as_of, as_of, provider, window_days=14, min_insiders=2,
                        top_n=5, step_days=7)
    assert len(trades) == 2, "both clusters should qualify (2 insiders each)"

    report = bt.summarize(trades, provider, holdout_start="2022-01-01")
    assert report["survivorship_bias_free"] is True and "warning" not in report

    six_m = report["tuning_period"]["6m"]
    assert six_m["n_trades"] == 2 and six_m["n_priced"] == 1
    assert six_m["n_likely_delisted_excluded"] == 1, "DELI must be tracked, not silently dropped"
    assert six_m["mean_excess_pct"] == 25.0, six_m  # NVRI +30% vs VTI +5%

    assert report["holdout_period"]["6m"]["n_trades"] == 0, "2015 trade must not leak into holdout"
    print(f"backtest      OK  2 clusters replayed, 1 priced (excess {six_m['mean_excess_pct']}%), "
          f"1 flagged as likely-delisted rather than dropped")


if __name__ == "__main__":
    main()
