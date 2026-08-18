"""The quarterly walk groups by fiscal year, and a cash-flow panel with no cash
flows in it is not published.

Both were found from one page — SUJA's, whose entire XBRL is a single 10-Q —
but neither is confined to it:

  * The YTD walk keyed its buckets on the calendar year of a fact's END date, so
    it split every fiscal year that does not end in December. An August-year
    filer's Q1 (ends 11/30) and its H1 (ends 2/28) fell either side of the split
    and the H1 quarter could never be derived. Eleven filers were publishing four
    columns that skipped a quarter, with an LTM above them summing a 15-month
    window; twenty-two more had the hole in one cash-flow column.

  * Net Income, D&A and Stock-Based Comp come from income-statement concepts,
    which filers tag per quarter even when the cash flow statement is presented
    only cumulatively — so the cash-flow tab could render those three rows alone,
    with no section that foots. A filer that reports cash flows year-to-date and
    never twice within one year (a fresh IPO, a 6-K semi-annual issuer) gets its
    cumulative columns published instead, labelled by span and marked so the
    display layer will not sum them into a TTM.
"""
from scraper.xbrl_financials import (_has_cashflow, _series_one_tag_quarterly,
                                     _ytd_span_labels)
from sitegen.generate import _sum_ttm


def usg(tag, facts):
    """facts: (start, end, value, accession) -> companyfacts-shaped us-gaap."""
    return {tag: {"units": {"USD": [
        {"start": s, "end": e, "val": v, "accn": accn}
        for s, e, v, accn in facts
    ]}}}


A = "0001-25-000001"


# --- fiscal-year bucketing ------------------------------------------------------

def test_august_fiscal_year_derives_every_quarter():
    """FY ends 8/31: Q1 11/30, H1 2/28, 9M 5/31, FY 8/31. The 2/28 quarter is the
    one calendar-year bucketing lost — its predecessor sits in the prior year."""
    facts = usg("X", [
        ("2025-09-01", "2025-11-30", 100, A),
        ("2025-09-01", "2026-02-28", 250, A),
        ("2025-09-01", "2026-05-31", 400, A),
        ("2025-09-01", "2026-08-31", 600, A),
    ])
    out = _series_one_tag_quarterly(facts, "X")
    assert out["2025-11-30"] == 100          # discrete, first pass
    assert out["2026-02-28"] == 150          # H1 − Q1  (was missing)
    assert out["2026-05-31"] == 150          # 9M − H1
    assert out["2026-08-31"] == 200          # FY − 9M


def test_fifty_three_week_year_derives_its_q4():
    """EML/FOSL close FY2025 on 1/3/26. The FY fact's end is in the NEXT calendar
    year, so calendar bucketing left it alone and Q4 never existed."""
    facts = usg("X", [
        ("2024-12-29", "2025-03-29", 10, A),
        ("2024-12-29", "2025-06-28", 25, A),
        ("2024-12-29", "2025-09-27", 45, A),
        ("2024-12-29", "2026-01-03", 70, A),
    ])
    out = _series_one_tag_quarterly(facts, "X")
    assert out["2026-01-03"] == 25


def test_fiscal_year_start_quoted_a_day_apart_still_one_bucket():
    """BDL's 10-K opens FY2025 on 2024-09-30, its 10-Qs on 2024-09-29. Exact-match
    grouping puts the annual fact alone and loses Q4 = FY − 9M."""
    facts = usg("X", [
        ("2024-09-29", "2024-12-28", 10, A),
        ("2024-09-29", "2025-03-29", 25, A),
        ("2024-09-29", "2025-06-28", 45, A),
        ("2024-09-30", "2025-09-27", 70, A),
    ])
    assert _series_one_tag_quarterly(facts, "X")["2025-09-27"] == 25


def test_a_discrete_quarter_is_never_mixed_into_the_cumulative_walk():
    """GPUS tags a discrete Q4 (start 10/1) alongside its YTD ladder. Sorted by
    duration inside one calendar bucket, 91d fell between Q1 and H1 and the walk
    computed H1 − Q4 — a negative stock-comp add-back."""
    facts = usg("X", [
        ("2025-01-01", "2025-03-31", 67, A),
        ("2025-01-01", "2025-06-30", 135, A),
        ("2025-10-01", "2025-12-31", 500, A),
        ("2025-01-01", "2025-12-31", 709, A),
    ])
    out = _series_one_tag_quarterly(facts, "X")
    assert out["2025-06-30"] == 68           # H1 − Q1, not H1 − Q4
    assert out["2025-12-31"] == 500          # the filer's own discrete Q4 wins


def test_semiannual_filer_still_yields_no_fake_quarter():
    """H1 + FY only: FY − H1 is a half-year, not a quarter. It must not be
    published as one (the guard that predates this change, re-checked here)."""
    facts = usg("X", [
        ("2025-01-01", "2025-06-30", 40, A),
        ("2025-01-01", "2025-12-31", 90, A),
    ])
    assert _series_one_tag_quarterly(facts, "X") == {}


# --- a cash-flow panel with no cash flows ---------------------------------------

def cf_grid(rows):
    return {"labels": list(rows), "data": [rows[k] for k in rows],
            "periods": ["6/29/26", "6/30/25"]}


def test_three_orphan_rows_are_not_a_cash_flow_statement():
    assert not _has_cashflow(cf_grid({
        "Net Income": [-27798000, -5658000],
        "D&A": [1900000, 1300000],
        "Stock-Based Comp": [13023000, None],
    }))


def test_one_section_subtotal_is_enough():
    assert _has_cashflow(cf_grid({
        "Net Income": [-27798000, -5658000],
        "Financing Cash Flow": [8641000, None],
    }))


def test_a_subtotal_row_that_is_empty_everywhere_does_not_count():
    assert not _has_cashflow(cf_grid({
        "Net Income": [-27798000, -5658000],
        "Operating Cash Flow": [None, None],
    }))


# --- cumulative columns are labelled, and refused as a TTM ----------------------

def test_ytd_columns_carry_their_span():
    facts = usg("NetCashProvidedByUsedInOperatingActivities", [
        ("2025-12-30", "2026-06-29", -5150000, A),
        ("2024-12-31", "2025-06-30", -3041000, A),
    ])
    labels = _ytd_span_labels(
        facts, [("Operating Cash Flow", ["NetCashProvidedByUsedInOperatingActivities"])],
        ["2026-06-29", "2025-06-30"])
    assert labels == ["6M 6/29/26", "6M 6/30/25"]


def test_four_cumulative_half_years_are_not_a_ttm():
    """SLGL reports half-yearly and has four H1 columns. Summing them spans two
    years; the multiple must be an em dash, not a number the reader cannot tell
    apart from a real one."""
    g = {"labels": ["Free Cash Flow"], "data": [[1, 2, 3, 4]],
         "periods": ["6M 6/30/25", "6M 6/30/24", "6M 6/30/23", "6M 6/30/22"],
         "basis": "ytd"}
    assert _sum_ttm(g, "Free Cash Flow") is None
