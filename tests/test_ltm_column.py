"""The LTM column fills what the annual facts actually determine.

Two regressions, both surfaced on ICCC (ImmuCell):

  * A point-in-time figure (weighted-average share count) is the latest quarter's
    value and does not depend on the other three. It was being blanked whenever a
    sibling quarter was None — and a *derived* Q4 share count is deliberately None
    (_reconcile_shares), so LTM shares showed "—" while every quarter had a count.

  * A flow item whose trailing four quarters have one underivable hole (ICCC tags
    Q1, a discrete Q3, 9M, and FY for interest, but never an H1, so Q2 can't be
    split out) blanked the whole LTM cell — even though FY − prior-stub + current-
    stub determines it exactly. The roll-forward recovers it and, by construction,
    equals the 4-quarter sum whenever both are computable.
"""
from scraper.xbrl_financials import _add_ltm_column


def _q(labels, data, ends):
    return {"labels": labels, "data": data, "_ends": ends}


def _a(labels, data, periods, ends):
    return {"labels": labels, "data": data, "periods": periods, "_ends": ends}


# 8 quarterly ends, newest-first, and 4 annual ends.
QENDS = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30",
         "2025-03-31", "2024-12-31", "2024-09-30", "2024-06-30"]
AENDS = ["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"]
APERIODS = ["12/31/25", "12/31/24", "12/31/23", "12/31/22"]


def _ltm_cell(label, q_row, a_row):
    q = _q([label], [q_row], QENDS)
    a = _a([label], [a_row], APERIODS, AENDS)
    out = _add_ltm_column(a, q)
    return out["data"][0][0]  # LTM is the new leftmost cell


def test_point_in_time_takes_latest_quarter_despite_none_sibling():
    # Q4'25 share count deliberately None; LTM = latest quarter (Q1'26).
    shares = [9_046_000, None, 9_030_000, 9_020_000, 9_010_000, 8_200_000, 8_200_000, 8_200_000]
    assert _ltm_cell("Basic Avg Shares", shares, [9_026_000, 8_167_000, 7_748_000, 7_745_000]) == 9_046_000


def test_point_in_time_blank_only_when_latest_quarter_missing():
    shares = [None, 9_030_000, 9_020_000, 9_010_000, 9_010_000, 8_200_000, 8_200_000, 8_200_000]
    assert _ltm_cell("Diluted Avg Shares", shares, [9_026_000, 8_167_000, 7_748_000, 7_745_000]) is None


def test_flow_rollforward_fills_hole_from_annual_facts():
    # ICCC interest: Q2'25 underivable. LTM = FY25 - Q1'25 + Q1'26.
    interest = [99_675, 106_145, 134_516, None, 127_928, 140_000, 144_141, 142_386]
    fy = [493_384, 568_725, 476_000, 349_000]
    assert _ltm_cell("Interest Expense", interest, fy) == 493_384 + 99_675 - 127_928


def test_flow_rollforward_equals_four_quarter_sum_when_both_computable():
    # Q1'26 100, Q4'25 110, Q3'25 120, Q2'25 130, Q1'25 90; FY25 = 90+130+120+110 = 450.
    full = [100, 110, 120, 130, 90, 80, 80, 80]
    holed = [100, 110, 120, None, 90, 80, 80, 80]
    fy = [450, 400, 400, 400]
    via_sum = _ltm_cell("Interest Expense", full, fy)      # direct 4-quarter sum path
    via_roll = _ltm_cell("Interest Expense", holed, fy)    # roll-forward fallback path
    assert via_sum == 100 + 110 + 120 + 130 == 460
    assert via_roll == via_sum


def test_flow_stays_blank_when_annual_missing_too():
    interest = [99_675, 106_145, 134_516, None, 127_928, 140_000, 144_141, 142_386]
    fy = [None, 568_725, 476_000, 349_000]                 # no FY25 anchor
    assert _ltm_cell("Interest Expense", interest, fy) is None
