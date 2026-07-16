"""The display layer's rule: whatever we cannot state, we render as an em dash.

The screener's rule is "unknown -> DataUnavailable". This layer had no equivalent
— it had `or 0` and "skip the Nones" — so it published a number wherever it had
partial data. Same bug, one layer out.
"""
import math

import pytest

from sitegen.generate import (_finite, _sum_ttm, fd_figures, money, money_m,
                              money_signed, number_2, number_int, pct,
                              price_or_dash, shares_m)


def grid(labels, data):
    return {"labels": labels, "data": data, "periods": ["3/31/26", "12/31/25", "9/30/25", "6/30/25"]}


# --- _sum_ttm: all-or-nothing ---------------------------------------------------

def test_four_quarters_sum():
    assert _sum_ttm(grid(["Total Revenue"], [[10, 20, 30, 40]]), "Total Revenue") == 100


def test_a_single_quarter_is_not_a_ttm():
    """BKKT published an EV/Revenue off ONE quarter — ~4x overstated — under a
    table headed 'TTM Multiples'."""
    assert _sum_ttm(grid(["Total Revenue"], [[10, None, None, None]]), "Total Revenue") is None


@pytest.mark.parametrize("row", [
    [10, 20, None, 40],       # a hole in the middle
    [10, 20, 30],             # only three periods exist
    [None, None, None, None],
])
def test_partial_quarters_are_not_a_ttm(row):
    assert _sum_ttm(grid(["Total Revenue"], [row]), "Total Revenue") is None


def test_missing_row_is_none():
    assert _sum_ttm(grid(["SG&A"], [[1, 1, 1, 1]]), "Total Revenue") is None
    assert _sum_ttm(None, "Total Revenue") is None


# --- fd_figures: unknown dilution is unknown, not zero ---------------------------

BASE = {"shares_basic": 1_000_000, "options": 100_000, "warrants": 50_000,
        "share_price": 10.0, "cash": 2_000_000, "debt": 1_000_000}


def test_full_dilution_computes():
    fd_so, fd_mc, fd_ev = fd_figures(BASE)
    assert fd_so == 1_150_000
    assert fd_mc == 11_500_000
    assert fd_ev == 10_500_000       # + debt - cash


@pytest.mark.parametrize("missing,expected_fd_so", [
    ("options", 1_050_000),     # 1,000,000 basic + 0 options + 50,000 warrants
    ("warrants", 1_100_000),    # 1,000,000 basic + 100,000 options + 0 warrants
])
def test_untagged_dilution_counts_as_zero_and_understates(missing, expected_fd_so):
    """Deliberate. XBRL reports None both for "has no warrants" and for "tags no
    warrant concept", and for warrants the first is overwhelmingly the common case.
    Treating None as unknown blanked the whole cap table for 31 of 209 companies —
    BUKS (warrants=None) then showed no EV on its page while the daily page showed
    $302.0M. company.html warns that FD SO is a floor."""
    fd_so, fd_mc, fd_ev = fd_figures(dict(BASE, **{missing: None}))
    assert fd_so == expected_fd_so
    assert fd_mc == expected_fd_so * 10.0
    assert fd_ev is not None


def test_buks_shape_still_gets_an_ev():
    """The regression this pins: options tagged, warrants not — BUKS's exact shape.
    Its page must not go blank while the daily page quotes a number."""
    fd_so, fd_mc, fd_ev = fd_figures(
        {"shares_basic": 63_932_907, "options": 6_057_843, "warrants": None,
         "share_price": 4.71, "cash": 35_124_000, "debt": 33_445_000})
    assert fd_so == 69_990_750
    assert fd_ev is not None and fd_ev > 0


def test_absent_cash_and_debt_are_real_zeroes():
    """Past the screener's anchor check, absent means the filer reports no such line."""
    fd_so, fd_mc, fd_ev = fd_figures(dict(BASE, cash=None, debt=None))
    assert fd_ev == fd_mc


def test_no_price_leaves_fd_so_but_no_market_cap():
    fd_so, fd_mc, fd_ev = fd_figures(dict(BASE, share_price=None))
    assert (fd_so, fd_mc, fd_ev) == (1_150_000, None, None)


def test_no_share_count_is_unknown():
    assert fd_figures({}) == (None, None, None)
    assert fd_figures(None) == (None, None, None)
    assert fd_figures(dict(BASE, shares_basic=None)) == (None, None, None)


# --- formatters: NaN must never reach the page ----------------------------------

@pytest.mark.parametrize("fmt", [money, money_m, money_signed, shares_m, pct,
                                 price_or_dash, number_int, number_2])
def test_nan_renders_as_a_dash_not_the_word_nan(fmt):
    """Only money() had this guard, so a NaN share price reached the page as the
    literal string '$nanM'. yfinance routinely puts NaN in `info`."""
    assert fmt(float("nan")) == "—"


@pytest.mark.parametrize("fmt", [money, money_m, money_signed, shares_m, pct,
                                 price_or_dash, number_int, number_2])
def test_infinity_renders_as_a_dash(fmt):
    assert fmt(float("inf")) == "—"


@pytest.mark.parametrize("fmt", [money, money_m, money_signed, shares_m, pct,
                                 price_or_dash, number_int, number_2])
def test_non_numeric_never_raises(fmt):
    """price_or_dash called float() outside its try — one bad value took down the
    entire site build."""
    assert fmt("not a number") == "—"
    assert fmt(None) == "—"


def test_price_or_dash_does_not_explode_on_a_string():
    assert price_or_dash("abc") == "—"


def test_formatters_still_format():
    assert money(1234) == "$1,234"
    assert money_m(2_500_000) == "$2.5M"
    assert money_m(2_500_000_000) == "$2.50B"
    assert money_signed(-1234) == "-$1,234"
    assert shares_m(1_500_000) == "1.5M"
    assert pct(0.1234) == "12.3%"
    assert price_or_dash(4.47) == "$4.47"
    assert price_or_dash(0) == "—"


def test_finite_helper():
    assert _finite(1.5) == 1.5
    assert _finite("2") == 2.0
    assert _finite(float("nan")) is None
    assert _finite(float("-inf")) is None
    assert _finite("") is None
    assert _finite(None) is None
