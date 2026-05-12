"""XBRL-primary financial statements extractor.

Reads SEC XBRL companyfacts and produces canonical {labels, periods, data}
structures matching what `_canonicalize` in financials.py expects.

Tag candidates can be:
  - a string (single us-gaap tag)
  - ("sum", [tag1, tag2, ...]) — sum the per-period values of those tags
Candidates are tried in priority order; earlier candidates fill in first,
later ones fill in periods the earlier didn't cover.
"""
from collections import defaultdict
from datetime import date

from . import edgar


# ---- low-level fact extraction --------------------------------------------

def _period_days(fact):
    s, e = fact.get("start"), fact.get("end")
    if not s or not e:
        return None
    try:
        return (date.fromisoformat(e) - date.fromisoformat(s)).days
    except ValueError:
        return None


def _series_one_tag_quarterly(usg, tag, unit="USD"):
    """Discrete-quarterly series for ONE tag, deriving Q4 / fills from YTD."""
    entries = usg.get(tag, {}).get("units", {}).get(unit, [])
    if not entries:
        return {}

    derived = {}
    # First pass: pick discrete (60-100d) by end-date, newest accn wins
    for f in entries:
        d = _period_days(f)
        if d is None or not (60 <= d <= 100):
            continue
        end = f["end"]
        if end not in derived or f.get("accn", "") > derived[end][1]:
            derived[end] = (f["val"], f.get("accn", ""))

    discrete = {end: v for end, (v, _) in derived.items()}

    # Second pass: YTD derivation for periods discrete didn't cover
    by_year = defaultdict(list)
    for f in entries:
        d = _period_days(f)
        if d is None:
            continue
        try:
            yr = date.fromisoformat(f["end"]).year
        except ValueError:
            continue
        by_year[yr].append((d, f))

    for yr, items in by_year.items():
        items.sort(key=lambda x: x[0])
        cum_val = None
        for dur, f in items:
            end = f["end"]
            if 60 <= dur <= 100:
                cum_val = f["val"]
            elif 150 <= dur <= 200:
                if end not in discrete and cum_val is not None:
                    discrete[end] = f["val"] - cum_val
                cum_val = f["val"]
            elif 240 <= dur <= 290:
                if end not in discrete and cum_val is not None:
                    discrete[end] = f["val"] - cum_val
                cum_val = f["val"]
            elif 350 <= dur <= 380:
                if end not in discrete and cum_val is not None:
                    discrete[end] = f["val"] - cum_val
                cum_val = f["val"]
    return discrete


def _series_one_tag_annual(usg, tag, unit="USD"):
    entries = usg.get(tag, {}).get("units", {}).get(unit, [])
    by_end = {}
    for f in entries:
        d = _period_days(f)
        if d is None or not (350 <= d <= 380):
            continue
        end = f["end"]
        if end not in by_end or f.get("accn", "") > by_end[end][1]:
            by_end[end] = (f["val"], f.get("accn", ""))
    return {end: v for end, (v, _) in by_end.items()}


def _series_one_tag_balance(usg, tag, unit="USD"):
    """Point-in-time facts."""
    entries = usg.get(tag, {}).get("units", {}).get(unit, [])
    by_end = {}
    for f in entries:
        s, e = f.get("start"), f.get("end")
        if not e:
            continue
        if s and s != e:
            d = _period_days(f) or 0
            if d > 1:
                continue
        if e not in by_end or f.get("accn", "") > by_end[e][1]:
            by_end[e] = (f["val"], f.get("accn", ""))
    return {end: v for end, (v, _) in by_end.items()}


def _series_one_tag(usg, tag, freq, unit="USD"):
    if freq == "quarterly":
        return _series_one_tag_quarterly(usg, tag, unit)
    if freq == "annual":
        return _series_one_tag_annual(usg, tag, unit)
    if freq == "balance":
        return _series_one_tag_balance(usg, tag, unit)
    return {}


def _sum_dicts(dicts):
    """Per-end sum across multiple {end: val} dicts. Only ends present in ALL dicts are included."""
    if not dicts or not all(dicts):
        return {}
    common = set(dicts[0].keys())
    for d in dicts[1:]:
        common &= set(d.keys())
    return {e: sum(d[e] for d in dicts) for e in common}


def _series(usg, candidates, freq, unit="USD"):
    """Try each candidate in priority order. Merge results so earlier candidates
    win at the same period end; later candidates fill in periods earlier missed."""
    accumulated = {}
    for cand in candidates:
        if isinstance(cand, str):
            sub = _series_one_tag(usg, cand, freq, unit)
        elif isinstance(cand, tuple) and cand[0] == "sum":
            sub_dicts = [_series_one_tag(usg, t, freq, unit) for t in cand[1]]
            sub = _sum_dicts(sub_dicts)
        else:
            continue
        for end, val in sub.items():
            if end not in accumulated:
                accumulated[end] = val
    return accumulated


def _fmt_period(end_str):
    try:
        d = date.fromisoformat(end_str)
        return f"{d.month}/{d.day}/{d.year % 100:02d}"
    except ValueError:
        return end_str


# ---- canonical line items -------------------------------------------------

LI_IS = [
    ("Total Revenue", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                       "RevenueFromContractWithCustomerIncludingAssessedTax",
                       "SalesRevenueNet", "SalesRevenueGoodsNet"]),
    ("Cost of Revenue", ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]),
    ("Gross Profit", ["GrossProfit"]),
    ("SG&A", [
        "SellingGeneralAndAdministrativeExpense",
        ("sum", ["GeneralAndAdministrativeExpense", "SellingAndMarketingExpense"]),
        ("sum", ["GeneralAndAdministrativeExpense", "SellingExpense"]),
        "GeneralAndAdministrativeExpense",
    ]),
    ("R&D", ["ResearchAndDevelopmentExpense"]),
    ("Operating Expense", ["OperatingExpenses", "CostsAndExpenses"]),
    ("Operating Income", ["OperatingIncomeLoss", "IncomeLossFromContinuingOperations"]),
    ("Interest Expense", ["InterestExpense", "InterestExpenseDebt",
                          "InterestExpenseNonoperating", "InterestIncomeExpenseNet"]),
    ("Pretax Income", [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesAndIncomeTaxExpenseBenefit",
    ]),
    ("Tax Provision", ["IncomeTaxExpenseBenefit"]),
    ("Net Income", ["NetIncomeLoss", "ProfitLoss"]),
]

LI_IS_PER_SHARE = [
    ("Diluted EPS", ["EarningsPerShareDiluted", "EarningsPerShareBasic"]),
    ("Basic EPS", ["EarningsPerShareBasic"]),
]

LI_IS_SHARES = [
    ("Diluted Avg Shares", ["WeightedAverageNumberOfDilutedSharesOutstanding",
                            "WeightedAverageNumberOfSharesOutstandingBasic"]),
]

LI_BS = [
    ("Cash & Equivalents", ["CashAndCashEquivalentsAtCarryingValue",
                            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                            "Cash"]),
    ("Receivables", ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"]),
    ("Inventory", ["InventoryNet"]),
    ("Total Current Assets", ["AssetsCurrent"]),
    ("Net PPE", ["PropertyPlantAndEquipmentNet"]),
    ("Goodwill", ["Goodwill"]),
    ("Total Assets", ["Assets"]),
    ("Accounts Payable", ["AccountsPayableCurrent"]),
    ("Total Current Liabilities", ["LiabilitiesCurrent"]),
    ("Long-term Debt", ["LongTermDebtNoncurrent", "LongTermDebt"]),
    ("Total Liabilities", ["Liabilities"]),
    ("Retained Earnings", ["RetainedEarningsAccumulatedDeficit"]),
    ("Stockholders' Equity", ["StockholdersEquity"]),
]

LI_CF_NEGATE = {"CapEx", "Stock Buyback", "Debt Repayment", "Dividends Paid"}

LI_CF = [
    ("Net Income", ["NetIncomeLoss", "ProfitLoss"]),
    ("D&A", ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "Depreciation"]),
    ("Stock-Based Comp", ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]),
    ("Operating Cash Flow", ["NetCashProvidedByUsedInOperatingActivities"]),
    ("CapEx", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    ("Investing Cash Flow", ["NetCashProvidedByUsedInInvestingActivities"]),
    ("Debt Issuance", ["ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromIssuanceOfDebt"]),
    ("Debt Repayment", ["RepaymentsOfLongTermDebt", "RepaymentsOfDebt"]),
    ("Stock Issuance", ["ProceedsFromIssuanceOfCommonStock"]),
    ("Stock Buyback", ["PaymentsForRepurchaseOfCommonStock",
                       "PaymentsForRepurchaseOfCommonStockForEmployeeTaxWithholdingObligations"]),
    ("Dividends Paid", ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"]),
    ("Financing Cash Flow", ["NetCashProvidedByUsedInFinancingActivities"]),
]


# ---- builders -------------------------------------------------------------

def _build_grid(usg, line_items, freq, n=4, unit_overrides=None):
    unit_overrides = unit_overrides or {}
    item_series = {}
    for label, cands in line_items:
        unit = unit_overrides.get(label, "USD")
        item_series[label] = _series(usg, cands, freq, unit=unit)

    # Period ends: primary item, else union
    primary = item_series[line_items[0][0]]
    if primary:
        period_ends = sorted(primary.keys(), reverse=True)[:n]
    else:
        all_ends = set()
        for d in item_series.values():
            all_ends.update(d.keys())
        period_ends = sorted(all_ends, reverse=True)[:n]

    labels = [label for label, _ in line_items]
    data = [[item_series[label].get(e) for e in period_ends] for label, _ in line_items]
    periods = [_fmt_period(e) for e in period_ends]
    return {"labels": labels, "periods": periods, "data": data, "_ends": period_ends}


def _augment_eps_and_shares(stmt, usg, freq):
    period_ends = stmt["_ends"]
    if not period_ends:
        return stmt
    for label, cands in LI_IS_PER_SHARE:
        ser = _series(usg, cands, freq, unit="USD/shares")
        stmt["labels"].append(label)
        stmt["data"].append([ser.get(e) for e in period_ends])
    for label, cands in LI_IS_SHARES:
        ser = _series(usg, cands, freq, unit="shares")
        stmt["labels"].append(label)
        stmt["data"].append([ser.get(e) for e in period_ends])
    return stmt


def _derive_gross_profit(stmt):
    labels = stmt["labels"]
    if "Gross Profit" not in labels or "Total Revenue" not in labels or "Cost of Revenue" not in labels:
        return stmt
    gp_i, rev_i, cor_i = (labels.index(x) for x in ("Gross Profit", "Total Revenue", "Cost of Revenue"))
    gp_row = stmt["data"][gp_i]
    if any(v is not None for v in gp_row):
        return stmt
    rev_row, cor_row = stmt["data"][rev_i], stmt["data"][cor_i]
    stmt["data"][gp_i] = [(r - c) if (r is not None and c is not None) else None
                          for r, c in zip(rev_row, cor_row)]
    return stmt


def _negate_outflows(stmt):
    labels = stmt["labels"]
    for label in LI_CF_NEGATE:
        if label in labels:
            i = labels.index(label)
            stmt["data"][i] = [(-v if v is not None else None) for v in stmt["data"][i]]
    return stmt


def _add_ebitda(is_stmt, cf_stmt):
    is_labels = is_stmt["labels"]
    cf_labels = cf_stmt["labels"]
    if "Operating Income" not in is_labels or "D&A" not in cf_labels:
        return is_stmt

    is_ends = is_stmt["_ends"]
    cf_ends = cf_stmt["_ends"]
    op_row = is_stmt["data"][is_labels.index("Operating Income")]
    da_row_by_end = {end: cf_stmt["data"][cf_labels.index("D&A")][i] for i, end in enumerate(cf_ends)}
    ebitda = []
    for i, end in enumerate(is_ends):
        op = op_row[i]
        da = da_row_by_end.get(end)
        ebitda.append((op + da) if (op is not None and da is not None) else None)
    is_stmt["labels"].append("EBITDA")
    is_stmt["data"].append(ebitda)
    return is_stmt


def _add_fcf(cf_stmt):
    labels = cf_stmt["labels"]
    if "Operating Cash Flow" not in labels or "CapEx" not in labels:
        return cf_stmt
    ocf = cf_stmt["data"][labels.index("Operating Cash Flow")]
    cap = cf_stmt["data"][labels.index("CapEx")]
    cf_stmt["labels"].append("Free Cash Flow")
    cf_stmt["data"].append([(o + c) if (o is not None and c is not None) else None
                            for o, c in zip(ocf, cap)])
    return cf_stmt


def _build_bs_at_ends(usg, target_ends):
    """Build a balance-sheet grid using the canonical line items, looking up each
    label's value at exactly the given period ends. Used so BS annual can hit older
    fiscal year-ends that may not be in the top-N most recent BS ends."""
    full = {label: _series(usg, cands, "balance") for label, cands in LI_BS}
    data = []
    chosen = []
    for tgt in target_ends:
        chosen.append(tgt)  # we'll just use the target end as-is; missing → None
    for label, _ in LI_BS:
        ser = full[label]
        row = [ser.get(e) for e in chosen]
        data.append(row)
    periods = [_fmt_period(e) if e else "—" for e in chosen]
    return {
        "labels": [l for l, _ in LI_BS],
        "periods": periods,
        "data": data,
        "_ends": chosen,
    }


def _strip(stmt):
    stmt.pop("_ends", None)
    return stmt


def _build_ratios(is_stmt, bs_stmt):
    """Build a ratios grid from already-extracted IS + BS data, aligned by period end.

    Rows: Current Ratio, Quick Ratio, Debt / Equity, Net Debt, Working Capital, ROE %, ROA %.
    """
    periods = is_stmt.get("periods", [])
    if not periods:
        return None

    is_labels = is_stmt["labels"]
    bs_labels = bs_stmt["labels"]
    is_data = is_stmt["data"]
    bs_data = bs_stmt["data"]

    def is_row(name):
        return is_data[is_labels.index(name)] if name in is_labels else [None] * len(periods)

    def bs_row(name):
        return bs_data[bs_labels.index(name)] if name in bs_labels else [None] * len(periods)

    n = len(periods)
    net_income = is_row("Net Income")
    revenue = is_row("Total Revenue")
    ebitda = is_row("EBITDA")
    cash = bs_row("Cash & Equivalents")
    receivables = bs_row("Receivables")
    inventory = bs_row("Inventory")
    ca = bs_row("Total Current Assets")
    assets = bs_row("Total Assets")
    cl = bs_row("Total Current Liabilities")
    lt_debt = bs_row("Long-term Debt")
    total_liab = bs_row("Total Liabilities")
    equity = bs_row("Stockholders' Equity")

    def safe_div(a, b):
        return [(x / y) if (x is not None and y is not None and y != 0) else None for x, y in zip(a, b)]

    def safe_sub(a, b):
        return [(x - y) if (x is not None and y is not None) else None for x, y in zip(a, b)]

    def safe_add(a, b):
        return [(x + y) if (x is not None and y is not None) else None for x, y in zip(a, b)]

    def safe_pct(a, b):
        return [(100 * x / y) if (x is not None and y is not None and y != 0) else None for x, y in zip(a, b)]

    quick_assets = safe_sub(ca, inventory)
    net_debt = safe_sub(lt_debt, cash)
    working_cap = safe_sub(ca, cl)
    ebitda_margin = safe_pct(ebitda, revenue)

    labels = [
        "Current Ratio",
        "Quick Ratio",
        "Debt / Equity",
        "EBITDA Margin %",
        "Working Capital",
        "Net Debt",
        "ROE %",
        "ROA %",
    ]
    data = [
        safe_div(ca, cl),
        safe_div(quick_assets, cl),
        safe_div(total_liab, equity),
        ebitda_margin,
        working_cap,
        net_debt,
        safe_pct(net_income, equity),
        safe_pct(net_income, assets),
    ]
    return {"labels": labels, "periods": periods, "data": data}


def fetch_xbrl_financials(cik):
    facts = edgar.fetch_companyfacts(cik)
    if facts is None:
        return None
    usg = facts.get("facts", {}).get("us-gaap", {})

    is_q = _build_grid(usg, LI_IS, "quarterly", n=4)
    is_q = _augment_eps_and_shares(is_q, usg, "quarterly")
    is_q = _derive_gross_profit(is_q)

    is_a = _build_grid(usg, LI_IS, "annual", n=4)
    is_a = _augment_eps_and_shares(is_a, usg, "annual")
    is_a = _derive_gross_profit(is_a)

    cf_q = _build_grid(usg, LI_CF, "quarterly", n=4)
    cf_q = _negate_outflows(cf_q)
    cf_q = _add_fcf(cf_q)

    cf_a = _build_grid(usg, LI_CF, "annual", n=4)
    cf_a = _negate_outflows(cf_a)
    cf_a = _add_fcf(cf_a)

    is_q = _add_ebitda(is_q, cf_q)
    is_a = _add_ebitda(is_a, cf_a)

    bs_q = _build_bs_at_ends(usg, is_q["_ends"])
    bs_a = _build_bs_at_ends(usg, is_a["_ends"])

    # Run the canonical filter — this adds margin rows, YoY rows, and spacers
    # (and drops yfinance-style noise that doesn't apply to XBRL output, but the
    # canonical entries are designed around these exact display labels).
    from .financials import _canonicalize, INCOME_CANONICAL, BALANCE_CANONICAL, CASHFLOW_CANONICAL
    is_q_c = _canonicalize(_strip(is_q), INCOME_CANONICAL, "quarterly")
    is_a_c = _canonicalize(_strip(is_a), INCOME_CANONICAL, "annual")
    bs_q_c = _canonicalize(_strip(bs_q), BALANCE_CANONICAL, "quarterly")
    bs_a_c = _canonicalize(_strip(bs_a), BALANCE_CANONICAL, "annual")
    cf_q_c = _canonicalize(_strip(cf_q), CASHFLOW_CANONICAL, "quarterly")
    cf_a_c = _canonicalize(_strip(cf_a), CASHFLOW_CANONICAL, "annual")

    # Build Ratios using the canonicalized IS + BS so the periods are aligned.
    ratios_q = _build_ratios(is_q_c, bs_q_c)
    ratios_a = _build_ratios(is_a_c, bs_a_c)

    return {
        "income_statement": {"quarterly": is_q_c, "annual": is_a_c},
        "balance_sheet": {"quarterly": bs_q_c, "annual": bs_a_c},
        "cash_flow": {"quarterly": cf_q_c, "annual": cf_a_c},
        "ratios": {"quarterly": ratios_q, "annual": ratios_a},
    }
