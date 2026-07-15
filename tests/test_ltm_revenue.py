"""Contract tests for the single revenue implementation.

`xbrl_financials.ltm_revenue` replaced `xbrl_facts.get_ttm_revenue`, which read
CUBI's top line as $43M against its real $1.51B and FMBM's as $162k against $80M
by picking the single freshest-ending tag and discarding the rest.

These exercise the real function against grid shapes taken from live filers, not
a stub — the first cut of ltm_revenue read only the quarterly grid and reported
every 20-F filer (NTB, SMWB, GMEX) as zero revenue, which the stubbed
screener tests could not see.
"""
from datetime import date

from scraper.xbrl_financials import ltm_revenue


def grid(labels, data, periods):
    return {"labels": labels, "data": data, "periods": periods}


def fins(quarterly=None, annual=None):
    return {"income_statement": {
        "quarterly": quarterly or grid([], [], []),
        "annual": annual or grid([], [], []),
    }}


def test_sums_four_quarters():
    """CUBI: the number get_ttm_revenue read as $43,396,000."""
    q = grid(["Total Revenue"],
             [[370_628_000, 387_714_000, 391_670_000, 357_607_000]],
             ["3/31/26", "12/31/25", "9/30/25", "6/30/25"])
    assert ltm_revenue(fins(quarterly=q)) == (1_507_619_000, "3/31/26")


def test_annual_fallback_for_filers_with_no_quarterly_xbrl():
    """NTB/SMWB/GMEX file 20-F: no quarterly XBRL exists, so the quarterly grid is
    empty while the annual grid holds the real top line. Reading only quarterly
    reports $0 and delists a company with $607M of revenue."""
    a = grid(["Total Revenue"],
             [[606_792_000, 579_933_000, 578_597_000, 549_297_000]],
             ["12/31/25", "12/31/24", "12/31/23", "12/31/22"])
    assert ltm_revenue(fins(annual=a)) == (606_792_000, "12/31/25")


def test_quarterly_wins_over_annual():
    q = grid(["Total Revenue"], [[10, 10, 10, 10]], ["3/31/26", "12/31/25", "9/30/25", "6/30/25"])
    a = grid(["Total Revenue"], [[999]], ["12/31/25"])
    assert ltm_revenue(fins(quarterly=q, annual=a))[0] == 40


def test_annual_skips_an_empty_leading_column():
    a = grid(["Total Revenue"], [[None, 250_000]], ["LTM", "12/31/25"])
    assert ltm_revenue(fins(annual=a)) == (250_000, "12/31/25")


def test_stale_annual_revenue_does_not_count():
    """STEX last reported $40k for FY2024 and nothing since. Reaching back to it
    answers 'when did this filer last have revenue', not 'what does it earn now' —
    the hole the removed get_ttm_revenue's 540-day cutoff existed to close."""
    a = grid(["Total Revenue"], [[None, None, 40_000, 18_000]],
             ["LTM", "12/31/25", "12/31/24", "12/31/23"])
    assert ltm_revenue(fins(annual=a), today=date(2026, 7, 15)) == (0.0, None)


def test_recent_annual_still_counts():
    a = grid(["Total Revenue"], [[606_792_000]], ["12/31/25"])
    assert ltm_revenue(fins(annual=a), today=date(2026, 7, 15))[0] == 606_792_000


def test_stops_at_the_newest_populated_annual_column():
    """Newest-first: if the latest real column is stale, older ones cannot save it."""
    a = grid(["Total Revenue"], [[None, 5_000, 900_000]], ["LTM", "12/31/24", "12/31/20"])
    assert ltm_revenue(fins(annual=a), today=date(2026, 7, 15)) == (0.0, None)


def test_no_revenue_row_anywhere_is_a_real_zero():
    """ARTV is clinical-stage and tags no revenue concept at all."""
    q = grid(["SG&A", "R&D"], [[1, 1, 1, 1], [2, 2, 2, 2]], ["3/31/26"])
    assert ltm_revenue(fins(quarterly=q)) == (0.0, None)


def test_partial_quarters_sum_what_exists():
    """A recent IPO with two quarters still has revenue > 0; it must not read as 0."""
    q = grid(["Total Revenue"], [[5, 5, None, None]], ["3/31/26", "12/31/25"])
    assert ltm_revenue(fins(quarterly=q))[0] == 10


def test_missing_statements_are_the_callers_problem():
    assert ltm_revenue(None) == (0.0, None)
