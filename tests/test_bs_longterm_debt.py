"""Long-term debt classification prefers a noncurrent measure over the
current-inclusive `LongTermDebt` total.

ImmuCell (ICCC) abandoned `LongTermDebtNoncurrent` in FY22/FY23: FY23 tagged only
`LongTermDebtAndCapitalLeaseObligations` (so the debt fell into the derived
"Other Liabilities" plug), and FY22 tagged the bare `LongTermDebt` total, whose
current maturity is also counted in "Current Debt" — double-counting it and
pushing the plug negative. The ladder now sits `LongTermDebtAndCapitalLeaseObligations`
(a noncurrent figure) ahead of `LongTermDebt`.
"""
from scraper.xbrl_financials import LI_IS, LI_BS, _series  # noqa: F401


def _ladder(label):
    return dict(LI_BS)[label]


def _bal(**tag_vals):
    return {tag: {"units": {"USD": [{"end": "2022-12-31", "val": v, "accn": "a1"}]}}
            for tag, v in tag_vals.items()}


def test_ladder_order():
    assert _ladder("Long-term Debt") == [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebt",
    ]


def test_prefers_noncurrent_and_lease_total_over_bare_total():
    # FY22 shape: both present — take the noncurrent 9.19M, not the 10.23M total.
    usg = _bal(LongTermDebt=10_230_000, LongTermDebtAndCapitalLeaseObligations=9_190_000)
    assert _series(usg, _ladder("Long-term Debt"), "balance") == {"2022-12-31": 9_190_000}


def test_captures_debt_when_only_the_lease_total_is_tagged():
    # FY23 shape: only LongTermDebtAndCapitalLeaseObligations — previously "—".
    usg = _bal(LongTermDebtAndCapitalLeaseObligations=10_540_000)
    assert _series(usg, _ladder("Long-term Debt"), "balance") == {"2022-12-31": 10_540_000}


def test_noncurrent_tag_still_wins_when_present():
    # FY24/FY25 shape: LongTermDebtNoncurrent present — unchanged behavior.
    usg = _bal(LongTermDebtNoncurrent=7_490_000,
               LongTermDebtAndCapitalLeaseObligations=9_100_000,
               LongTermDebt=9_100_000)
    assert _series(usg, _ladder("Long-term Debt"), "balance") == {"2022-12-31": 7_490_000}
