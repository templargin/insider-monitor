"""Contract tests for the screener's validation boundary.

Every test here encodes a way the screener previously turned "I don't know" into
a confident merits rejection. The canonical case is 2026-07-15: yfinance returned
a NaN close for BUKS (a thin name with no bar that day), NaN passed both the
`is None` and `<= 0` guards, poisoned EV, and `nan < 1e9` evaluated False — so a
$131k director buy at a $284M-EV company was published as "no issuers met the
criteria today".

Fixture-based: no network.
"""
import math

import pytest

from scraper import filters, pipeline

CIK = "0000000001"
TICKER = "TEST"
ANCHOR = "2026-03-31"


def install(monkeypatch, *, anchor=ANCHOR, shares=10_000_000, sh_end="2026-05-01",
            cash=0, debt=0, flag=None, price=10.0, revenue=(1e6, 1e6, 1e6, 1e6),
            fins_none=False, facts=None):
    """Stub every input screener_pass depends on, so only its boundary logic runs."""
    facts = {"facts": {"us-gaap": {}}} if facts is None else facts
    monkeypatch.setattr(pipeline.edgar, "fetch_companyfacts", lambda cik: facts)
    monkeypatch.setattr(pipeline.xbrl_facts, "balance_sheet_date", lambda f: anchor)
    monkeypatch.setattr(pipeline.xbrl_facts, "get_basic_shares", lambda f: (shares, sh_end))
    monkeypatch.setattr(pipeline.xbrl_facts, "get_cash", lambda f, a=None: (cash, anchor))
    monkeypatch.setattr(pipeline.xbrl_statement, "get_structured_debt",
                        lambda f: (debt, anchor, flag))
    monkeypatch.setattr(pipeline.financials, "fetch_share_price", lambda t: price)

    built = None if fins_none else {
        "income_statement": {"quarterly": {
            "labels": ["Total Revenue"],
            "data": [list(revenue)] if revenue is not None else [],
            "periods": ["3/31/26", "12/31/25", "9/30/25", "6/30/25"],
        }}
    }
    if revenue is None and not fins_none:      # filer tags no revenue concept at all
        built = {"income_statement": {"quarterly": {
            "labels": ["SG&A"], "data": [[1, 1, 1, 1]], "periods": ["3/31/26"]}}}
    monkeypatch.setattr(pipeline.xbrl_financials, "fetch_xbrl_financials",
                        lambda cik, facts=None: built)


def run():
    """(snapshot, reason) — snapshot is None on a merits rejection."""
    return pipeline.screener_pass(CIK, TICKER, {"ticker": TICKER, "name": "Test Co"})


def passes():
    snap, reason = run()
    assert reason is None, f"expected a pass, got rejection: {reason}"
    return snap


def rejected():
    """`reason`, not `snap`, is the verdict — the measurement comes back either way."""
    snap, reason = run()
    assert reason is not None, "expected a merits rejection, got a pass"
    return reason


# --- price: the BUKS regression -------------------------------------------------

def test_nan_price_is_unavailable_not_a_rejection(monkeypatch):
    """The 2026-07-15 BUKS bug. NaN must not read as 'EV >= cap'."""
    install(monkeypatch, price=float("nan"))
    with pytest.raises(pipeline.DataUnavailable, match="no share price"):
        run()


@pytest.mark.parametrize("price", [None, 0, -1.0, float("inf")])
def test_unusable_prices_are_unavailable(monkeypatch, price):
    install(monkeypatch, price=price)
    with pytest.raises(pipeline.DataUnavailable, match="no share price"):
        run()


# --- anchor: the GLBS / IFRS case -----------------------------------------------

def test_missing_balance_sheet_anchor_is_unavailable(monkeypatch):
    """An IFRS/20-F filer (GLBS) has no us-gaap anchor, so debt and cash cannot be
    read at a known date. Screening it anyway would treat unknown debt as zero."""
    install(monkeypatch, anchor=None)
    with pytest.raises(pipeline.DataUnavailable, match="no us-gaap balance sheet"):
        run()


# --- shares ---------------------------------------------------------------------

def test_share_count_older_than_balance_sheet_is_unavailable(monkeypatch):
    """BETA screened on a pre-IPO count; FONR on one from 2018."""
    install(monkeypatch, sh_end="2025-09-30", anchor="2026-03-31")
    with pytest.raises(pipeline.DataUnavailable, match="predates its balance sheet"):
        run()


def test_share_count_fresher_than_balance_sheet_is_fine(monkeypatch):
    """Cover-page counts are legitimately fresher than the balance sheet (BUKS:
    shares as of 2026-06-26 against a 2026-04-30 sheet). Must not be rejected."""
    install(monkeypatch, sh_end="2026-06-26", anchor="2026-04-30")
    assert passes() is not None


@pytest.mark.parametrize("shares", [None, 0])
def test_missing_share_count_is_unavailable(monkeypatch, shares):
    install(monkeypatch, shares=shares)
    with pytest.raises(pipeline.DataUnavailable, match="no basic share count"):
        run()


# --- sizing ---------------------------------------------------------------------

def test_negative_ev_passes(monkeypatch):
    """A company below net cash (GVH: MC $7.1M vs cash $7.5M) is emphatically
    small. Negative EV must not be rejected."""
    install(monkeypatch, shares=1_000_000, price=7.10, cash=7_500_000, debt=0)
    assert passes()["ev_basic"] < 0


def test_over_cap_is_a_merits_rejection_not_an_error(monkeypatch):
    install(monkeypatch, shares=1_000_000_000, price=10.0)   # MC $10B
    assert rejected() is not None


def test_bank_is_sized_on_market_cap_not_ev(monkeypatch):
    """FGBI published at EV -$352M because deposits aren't in the debt ladder while
    its cash is fully netted — and negative EV always passed `ev < cap`. A bank
    must be sized on market cap."""
    flag = {"reason": "financial_institution", "amount": None, "concept": None}
    # EV would be hugely negative (deposit cash), but MC is over the cap.
    install(monkeypatch, shares=200_000_000, price=10.0,      # MC $2B
            cash=5_000_000_000, debt=0, flag=flag)
    assert "MC=" in rejected(), "a bank must be rejected on market cap, not EV"


def test_small_bank_still_passes_on_market_cap(monkeypatch):
    flag = {"reason": "financial_institution", "amount": None, "concept": None}
    install(monkeypatch, shares=10_000_000, price=10.0,       # MC $100M
            cash=800_000_000, debt=0, flag=flag)
    assert passes() is not None


def test_non_bank_flag_does_not_switch_to_market_cap(monkeypatch):
    """Only `financial_institution` switches the measure; other flags must not."""
    flag = {"reason": "unexplained_liabilities", "amount": 361e6, "concept": None}
    install(monkeypatch, shares=1_000_000_000, price=10.0, flag=flag)   # MC/EV $10B
    assert rejected() is not None


# --- revenue --------------------------------------------------------------------

def test_no_revenue_row_is_a_merits_rejection(monkeypatch):
    """ARTV is clinical-stage and tags no revenue concept at all — a real zero, not
    missing data, so it belongs in the rejected pile, not the unevaluated one."""
    install(monkeypatch, revenue=None)
    assert rejected() is not None


def test_zero_revenue_is_a_merits_rejection(monkeypatch):
    install(monkeypatch, revenue=(0, 0, 0, 0))
    assert rejected() is not None


def test_missing_statements_are_unavailable(monkeypatch):
    install(monkeypatch, fins_none=True)
    with pytest.raises(pipeline.DataUnavailable, match="no financial statements"):
        run()


def test_revenue_comes_from_the_canonical_grid(monkeypatch):
    """The screener must quote the same number the page renders."""
    install(monkeypatch, revenue=(370_628_000, 387_714_000, 391_670_000, 357_607_000))
    assert passes()["ttm_revenue"] == 1_507_619_000   # CUBI's real LTM, not $43M


def test_a_rejected_company_still_returns_its_measurement(monkeypatch):
    """CUBI is rejected on size, but its page persists and must show correct
    figures. Short-circuiting the cap test before reading revenue left it
    publishing $43M beside its own $1.51B income statement."""
    install(monkeypatch, shares=1_000_000_000, price=10.0,        # MC $10B — over cap
            revenue=(370_628_000, 387_714_000, 391_670_000, 357_607_000))
    snap, reason = run()
    assert reason is not None, "expected a size rejection"
    assert snap is not None, "a rejected company must still be measured"
    assert snap["ttm_revenue"] == 1_507_619_000


# --- filters are total functions ------------------------------------------------

@pytest.mark.parametrize("bad", [None, float("nan"), float("inf")])
def test_passes_ev_cap_refuses_unknown(bad):
    """A predicate that answers 'no' to a question it couldn't ask is the bug."""
    with pytest.raises(ValueError):
        filters.passes_ev_cap(bad)


@pytest.mark.parametrize("bad", [None, float("nan")])
def test_passes_revenue_refuses_unknown(bad):
    with pytest.raises(ValueError):
        filters.passes_revenue(bad)


def test_passes_ev_cap_allows_negative():
    assert filters.passes_ev_cap(-352_000_000) is True


def test_basic_ev_refuses_non_finite_market_cap():
    with pytest.raises(ValueError):
        filters.basic_ev(float("nan"), 0, 0)


def test_basic_ev_reads_absent_debt_as_zero():
    """Sound only because the anchor check upstream guarantees we could look."""
    assert filters.basic_ev(100.0, None, None) == 100.0
