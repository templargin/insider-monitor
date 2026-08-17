"""The Ratios table's debt rows measure debt, and blank when debt is unknown.

Proficient Auto Logistics (PAL, 2026-08-17) published `Debt / Equity 0.52`
against $63.7M of debt and $303.2M of equity — the row was dividing *Total
Liabilities* by equity. `Net Debt 36.45` on the same page netted cash against
the long-term leg alone, so it disagreed with the Net Debt implied by the
cap-structure block's EV, $19.2M of current debt lower.

Both rows now take debt as the legs the filer actually reports. A leg never
tagged is nil, so the other leg alone is the debt; a leg tagged but blank for a
period is unknown. Reporting neither leg is unknown too, never debt-free: the
two-tag ladder cannot see whole families of borrowing — CUBI $3.46B, FBRT
$2.85B, XRN's $663M revolver — and those filers must read blank, not low-geared.
"""
from scraper.xbrl_financials import _build_ratios

PERIODS = ["6/30/26"]


def _ratios(**bs_rows):
    """Ratios grid for a one-period filer. bs_rows in millions; omit a row to
    leave it untagged, pass None to tag it but leave the period empty."""
    is_stmt = {
        "labels": ["Total Revenue", "EBITDA", "Net Income"],
        "periods": PERIODS,
        "data": [[109.4], [6.35], [-3.9]],
    }
    bs_stmt = {
        "labels": list(bs_rows),
        "periods": PERIODS,
        "data": [[v] for v in bs_rows.values()],
    }
    out = _build_ratios(is_stmt, bs_stmt)
    return dict(zip(out["labels"], (row[0] for row in out["data"])))


PAL = {
    "Cash & Equivalents": 8.13,
    "Current Debt": 19.16,
    "Long-term Debt": 44.58,
    "Total Liabilities": 157.32,
    "Total Equity": 303.19,
}


def test_debt_to_equity_divides_debt_not_total_liabilities():
    assert _ratios(**PAL)["Debt / Equity"] == (19.16 + 44.58) / 303.19


def test_net_debt_includes_the_current_leg():
    assert _ratios(**PAL)["Net Debt"] == 19.16 + 44.58 - 8.13


def test_a_leg_never_tagged_leaves_the_other_leg_as_the_debt():
    # AMPY/PAR/TXO/CDLX shape: no current maturities, so no Current Debt row was
    # ever tagged. The long-term leg alone is the debt, not a data gap.
    only_long_term = {k: v for k, v in PAL.items() if k != "Current Debt"}
    r = _ratios(**only_long_term)
    assert r["Debt / Equity"] == 44.58 / 303.19
    assert r["Net Debt"] == 44.58 - 8.13


def test_a_tagged_leg_with_no_value_for_the_period_is_still_unknown():
    r = _ratios(**{**PAL, "Current Debt": None})
    assert r["Debt / Equity"] is None
    assert r["Net Debt"] is None


def test_a_filer_whose_borrowings_we_cannot_classify_never_reads_debt_free():
    # Bank/REIT shape: billions of liabilities, no debt row this ladder matches.
    bank = {"Cash & Equivalents": 900.0, "Total Liabilities": 23736.5, "Total Equity": 2100.0}
    r = _ratios(**bank)
    assert r["Debt / Equity"] is None
    assert r["Net Debt"] is None
