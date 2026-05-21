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
from .financials import _canonicalize, INCOME_CANONICAL, BALANCE_CANONICAL, CASHFLOW_CANONICAL


# ---- low-level fact extraction --------------------------------------------

def _period_days(fact):
    s, e = fact.get("start"), fact.get("end")
    if not s or not e:
        return None
    try:
        return (date.fromisoformat(e) - date.fromisoformat(s)).days
    except ValueError:
        return None


_DURATION_BANDS = ((60, 100), (150, 200), (240, 290), (350, 380))


def _duration_band(d):
    if d is None:
        return None
    for lo, hi in _DURATION_BANDS:
        if lo <= d <= hi:
            return (lo, hi)
    return None


def _series_one_tag_quarterly(usg, tag, unit="USD"):
    """Discrete-quarterly series for ONE tag, deriving Q4 / fills from YTD."""
    entries = usg.get(tag, {}).get("units", {}).get(unit, [])
    if not entries:
        return {}

    # Dedupe by (end, duration-band), newest accn wins. EDGAR companyfacts
    # holds every restatement under a new accession; without this step the
    # YTD walk below can subtract a fact from itself when the same FY/H1/Q
    # is re-emitted by a later filing — producing fake zero-value quarters.
    deduped = {}
    for f in entries:
        band = _duration_band(_period_days(f))
        if band is None:
            continue
        key = (f["end"], band)
        prev = deduped.get(key)
        if prev is None or f.get("accn", "") > prev.get("accn", ""):
            deduped[key] = f
    facts = list(deduped.values())

    # First pass: 60-100d facts ARE discrete quarters.
    discrete = {f["end"]: f["val"] for f in facts if _duration_band(_period_days(f)) == (60, 100)}

    # Second pass: YTD derivation for periods discrete didn't cover.
    # NOTE: groups by calendar year of end date — correct for the dominant
    # Dec-31 fiscal-year case. For non-calendar fiscal years (e.g., FY ending
    # June or September), this can mis-bucket the FY/Q1 transition. Acceptable
    # for our sub-$1B universe where Dec-FY is overwhelmingly common.
    by_year = defaultdict(list)
    for f in facts:
        try:
            yr = date.fromisoformat(f["end"]).year
        except ValueError:
            continue
        by_year[yr].append((_period_days(f), f))

    for yr, items in by_year.items():
        items.sort(key=lambda x: x[0])
        cum_val, cum_dur = None, None
        for dur, f in items:
            # Emit a derived quarter only when the implied duration
            # (current − cum) lands back in the Q band. Without this guard,
            # semi-annual filers (H1 + FY only) emit FY−H1 ≈ 185d as a
            # fake "Q4", silently labeling half-year H2 totals as quarterly.
            # The `end not in discrete` test already excludes discrete-Q
            # facts (first pass wrote them).
            end = f["end"]
            if (cum_val is not None and end not in discrete
                    and 60 <= dur - cum_dur <= 100):
                discrete[end] = f["val"] - cum_val
            cum_val, cum_dur = f["val"], dur
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
    ("Total Revenue", [
        # Banks/insurance: interest+dividend income + noninterest income.
        # Promoted to first place because banks like BWFG also report a
        # small `RevenueFromContractWithCustomer*` for contract-based fee
        # income that's NOT the bank's total revenue. The composite returns
        # empty for non-banks (those tags aren't present), so the standard
        # tags below take over.
        ("sum", ["InterestAndDividendIncomeOperating", "NoninterestIncome"]),
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ]),
    ("Cost of Revenue", [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        # Services-heavy filers (DLHC etc.) tag CoR ex-D&A separately:
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        # Media/entertainment (STRZ, etc.) — content licensing has no traditional COGS
        "DirectOperatingCosts",
        # Real estate developers / REITs (AXR, etc.)
        "CostOfRealEstateSales",
        # Restaurant operators (PTLO, etc.) split food + labor as separate tags
        ("sum", ["CostDirectMaterial", "CostDirectLabor"]),
    ]),
    ("Gross Profit", ["GrossProfit"]),
    ("SG&A", [
        "SellingGeneralAndAdministrativeExpense",
        ("sum", ["GeneralAndAdministrativeExpense", "SellingAndMarketingExpense"]),
        ("sum", ["GeneralAndAdministrativeExpense", "SellingExpense"]),
        "GeneralAndAdministrativeExpense",
        "NoninterestExpense",  # banks
    ]),
    ("R&D", ["ResearchAndDevelopmentExpense"]),
    # NOTE: "Operating Expense" intentionally has no XBRL candidates — it's
    # DERIVED in _derive_opex as (Rev - OpInc - CoR), so the page always
    # reconciles GP - OpEx = OpInc. The OperatingExpenses and CostsAndExpenses
    # tags are inconsistent across filers — some include COGS, some don't.
    ("Operating Expense", []),
    # `IncomeLossFromContinuingOperations` is sometimes used as a fallback
    # for OpInc, but for banks (CUBI etc.) that don't report OperatingIncomeLoss
    # at all, that fallback resolves to the post-tax bottom-of-IS line ≈ Net
    # Income. That conflates two different concepts. Strict tag only — banks
    # just won't show OpInc, which is honest given they don't report it.
    ("Operating Income", ["OperatingIncomeLoss"]),
    ("Interest Expense", ["InterestExpense", "InterestExpenseDebt",
                          "InterestExpenseNonoperating", "InterestIncomeExpenseNet"]),
    # Pretax is DERIVED in _derive_pretax as (NI + Tax). The XBRL Pretax tags
    # have filer-specific sign-convention bugs (e.g., LODE FY24/25 reported
    # loss as a positive value under the *MinorityInterest* variant).
    ("Pretax Income", []),
    ("Tax Provision", ["IncomeTaxExpenseBenefit"]),
    # ProfitLoss = total net income (includes NCI). NetIncomeLoss = parent's
    # share. Prefer total so the IS reconciles with Pretax - Tax. For most
    # small caps with no NCI they are identical, and the "Net Income to Common"
    # row below gets pruned in _drop_redundant_nci_row.
    ("Net Income", ["ProfitLoss", "NetIncomeLoss"]),
    # When NCI is meaningful (e.g., MKTW), Net Income to Common = parent's
    # share. Shown alongside total NI so that EPS = NI_to_Common / shares
    # is visibly consistent. Otherwise pruned.
    ("Net Income to Common", ["NetIncomeLoss"]),
]

LI_IS_PER_SHARE = [
    # EPS uses parent-attributable income / weighted shares. Diluted EPS
    # falling back to Basic would mislabel basic as diluted, so we keep
    # them strictly separate.
    ("Diluted EPS", ["EarningsPerShareDiluted"]),
    ("Basic EPS", ["EarningsPerShareBasic"]),
]

LI_IS_SHARES = [
    ("Diluted Avg Shares", ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    ("Basic Avg Shares", ["WeightedAverageNumberOfSharesOutstandingBasic"]),
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
    # Mezzanine equity: redeemable preferred stock and redeemable NCI sit
    # between liabilities and stockholders' equity in GAAP. Including it as
    # its own row makes Assets = Liabilities + Mezzanine + Total Equity
    # reconcile for SPACs and similar structures. Auto-pruned when zero.
    ("Mezzanine Equity", [
        "TemporaryEquityCarryingAmountAttributableToParent",
        "TemporaryEquityCarryingAmount",
        "TemporaryEquityRedemptionValue",
        "RedeemableNoncontrollingInterestEquityCarryingAmount",
    ]),
    ("Retained Earnings", ["RetainedEarningsAccumulatedDeficit"]),
    # "Total Equity" includes noncontrolling interest so that
    # Total Liabilities + Total Equity = Total Assets. The parent-only
    # StockholdersEquity tag is the fallback for filers that don't report
    # the including-NCI variant; on those, NCI is typically nil so the
    # two coincide.
    ("Total Equity", [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "StockholdersEquity",
    ]),
]

LI_CF_NEGATE = {"CapEx", "Stock Buyback", "Debt Repayment", "Dividends Paid"}

LI_CF = [
    ("Net Income", ["NetIncomeLoss", "ProfitLoss"]),
    ("D&A", ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "Depreciation"]),
    ("Stock-Based Comp", ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]),
    ("Operating Cash Flow", ["NetCashProvidedByUsedInOperatingActivities"]),
    ("CapEx", ["PaymentsToAcquirePropertyPlantAndEquipment",
               "PaymentsToAcquirePropertyPlantAndEquipmentNet",
               "PaymentsForCapitalImprovements",
               "PaymentsToAcquireProductiveAssets",
               "CapitalExpenditures"]),
    ("Investing Cash Flow", ["NetCashProvidedByUsedInInvestingActivities"]),
    ("Debt Issuance", ["ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromIssuanceOfDebt"]),
    ("Debt Repayment", ["RepaymentsOfLongTermDebt", "RepaymentsOfDebt"]),
    ("Stock Issuance", ["ProceedsFromIssuanceOfCommonStock"]),
    ("Stock Buyback", ["PaymentsForRepurchaseOfCommonStock",
                       "PaymentsForRepurchaseOfCommonStockForEmployeeTaxWithholdingObligations"]),
    ("Dividends Paid", ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"]),
    ("Financing Cash Flow", ["NetCashProvidedByUsedInFinancingActivities"]),
    # FX effect on cash held abroad. Closes the CF reconciliation gap for
    # foreign-operations companies (GTE etc.). Often zero / missing for
    # US-only filers, in which case _canonicalize prunes the row.
    ("Effect of FX on Cash", [
        "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "EffectOfExchangeRateOnCashAndCashEquivalents",
        "EffectOfExchangeRateOnCash",
    ]),
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
    """Fill GP = Rev - CoR for any period missing a reported GP. Filler runs
    per-period (not all-or-nothing) — some filers report GP only for older
    periods after a CoR-reporting change."""
    labels = stmt["labels"]
    if "Gross Profit" not in labels or "Total Revenue" not in labels or "Cost of Revenue" not in labels:
        return stmt
    gp_i, rev_i, cor_i = (labels.index(x) for x in ("Gross Profit", "Total Revenue", "Cost of Revenue"))
    gp_row = stmt["data"][gp_i]
    rev_row, cor_row = stmt["data"][rev_i], stmt["data"][cor_i]
    new_gp = []
    for g, r, c in zip(gp_row, rev_row, cor_row):
        if g is not None:
            new_gp.append(g)
        elif r is not None and c is not None:
            new_gp.append(r - c)
        else:
            new_gp.append(None)
    stmt["data"][gp_i] = new_gp
    return stmt


def _derive_opinc_from_costs_and_expenses(stmt, usg, freq):
    """Fill missing Operating Income as Revenue − `CostsAndExpenses` for filers
    that don't tag `OperatingIncomeLoss` (BH, STRZ). The XBRL `CostsAndExpenses`
    tag is defined as total operational costs (CoR + OpEx), so Rev − CostsAndExp
    = OpInc by construction for these filers.

    Runs only for periods where OpInc is None, so doesn't disturb filers that
    correctly report the tag."""
    labels = stmt["labels"]
    if "Operating Income" not in labels or "Total Revenue" not in labels:
        return stmt
    op_i = labels.index("Operating Income")
    rev_i = labels.index("Total Revenue")
    op_row = stmt["data"][op_i]
    if all(v is not None for v in op_row):
        return stmt  # nothing to derive
    rev_row = stmt["data"][rev_i]
    # Pull `CostsAndExpenses` directly at the same period ends
    ends = stmt.get("_ends") or [None] * len(rev_row)
    cae_series = _series_one_tag(usg, "CostsAndExpenses", freq)
    new_op = []
    for i, e in enumerate(ends):
        if op_row[i] is not None:
            new_op.append(op_row[i])
            continue
        r = rev_row[i]
        cae = cae_series.get(e) if e else None
        if r is not None and cae is not None:
            new_op.append(r - cae)
        else:
            new_op.append(None)
    stmt["data"][op_i] = new_op
    return stmt


def _derive_opex(stmt):
    """Operating Expense = Gross Profit - Operating Income. By definition this
    satisfies GP - OpEx = OpInc, so the IS always reconciles. Filers' XBRL
    OperatingExpenses tag is inconsistent (some include COGS, some don't), so
    we ignore it. Anchoring on GP (not Rev) is correct when CoR isn't reported
    for some periods — the company's reported GP already accounts for it."""
    labels = stmt["labels"]
    if not all(n in labels for n in ("Gross Profit", "Operating Income", "Operating Expense")):
        return stmt
    gp_row = stmt["data"][labels.index("Gross Profit")]
    op_row = stmt["data"][labels.index("Operating Income")]
    opex_i = labels.index("Operating Expense")
    stmt["data"][opex_i] = [
        (g - o) if (g is not None and o is not None) else None
        for g, o in zip(gp_row, op_row)
    ]
    return stmt


def _derive_pretax(stmt):
    """Pretax Income = Net Income + Tax Provision. Avoids filer-specific bugs
    in the XBRL Pretax tags (e.g., LODE's *MinorityInterest* variant reports
    losses as positive)."""
    labels = stmt["labels"]
    if not all(n in labels for n in ("Pretax Income", "Net Income", "Tax Provision")):
        return stmt
    ni_row = stmt["data"][labels.index("Net Income")]
    tax_row = stmt["data"][labels.index("Tax Provision")]
    pretax_i = labels.index("Pretax Income")
    stmt["data"][pretax_i] = [
        (n + t) if (n is not None and t is not None) else (n if n is not None else None)
        for n, t in zip(ni_row, tax_row)
    ]
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


def _add_ltm_column(annual_stmt, quarterly_stmt):
    """Inject an LTM (last twelve months) column as the leftmost period
    in annual_stmt.

    For each row:
      - flow items (Revenue, expenses, NI, all CF lines, EPS): sum of the
        last 4 quarterly values
      - point-in-time items (Diluted/Basic Avg Shares): latest quarterly value

    The LTM-end date is the most recent quarter end; we expose it in `_ends`
    so balance-sheet builders can pull the latest BS values at that date.

    Returns the augmented statement; periods[0] = "LTM".
    """
    if not quarterly_stmt:
        return annual_stmt
    q_labels = quarterly_stmt.get("labels", [])
    q_data = quarterly_stmt.get("data", [])
    q_ends = quarterly_stmt.get("_ends", [])
    if not q_labels or not q_ends:
        return annual_stmt

    point_in_time = {"Diluted Avg Shares", "Basic Avg Shares"}

    new_data = []
    for label, row in zip(annual_stmt["labels"], annual_stmt["data"]):
        ltm = None
        if label in q_labels:
            q_row = q_data[q_labels.index(label)]
            if q_row and len(q_row) >= 4:
                vals = q_row[:4]  # newest-first → last 4 quarters
                if all(v is not None for v in vals):
                    ltm = vals[0] if label in point_in_time else sum(vals)
        new_data.append([ltm] + list(row))

    annual_stmt["data"] = new_data
    annual_stmt["periods"] = ["LTM"] + list(annual_stmt["periods"])
    if "_ends" in annual_stmt:
        annual_stmt["_ends"] = [q_ends[0]] + list(annual_stmt["_ends"])
    return annual_stmt


def _drop_redundant_nci_row(stmt):
    """If 'Net Income to Common' equals 'Net Income' (no NCI exposure), drop
    the redundant row. Keep both when they differ so the EPS denominator is
    visibly tied to the right NI."""
    labels = stmt.get("labels", [])
    if "Net Income to Common" not in labels or "Net Income" not in labels:
        return stmt
    ni_row = stmt["data"][labels.index("Net Income")]
    common_row = stmt["data"][labels.index("Net Income to Common")]
    # Equal (within $1k) at every period → redundant.
    def near(a, b):
        if a is None and b is None: return True
        if a is None or b is None: return False
        return abs(a - b) <= 1000
    if all(near(a, b) for a, b in zip(ni_row, common_row)):
        idx = labels.index("Net Income to Common")
        del stmt["labels"][idx]
        del stmt["data"][idx]
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
    equity = bs_row("Total Equity")

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

    # Quarterly: pull 8 periods so _canonicalize can compute Q-vs-PYQ YoY (offset 4).
    # _canonicalize trims display to 4 periods after derived rows are computed.
    is_q = _build_grid(usg, LI_IS, "quarterly", n=8)
    is_q = _augment_eps_and_shares(is_q, usg, "quarterly")
    is_q = _derive_gross_profit(is_q)
    is_q = _derive_opinc_from_costs_and_expenses(is_q, usg, "quarterly")
    is_q = _derive_opex(is_q)
    is_q = _derive_pretax(is_q)

    is_a = _build_grid(usg, LI_IS, "annual", n=4)
    is_a = _augment_eps_and_shares(is_a, usg, "annual")
    is_a = _derive_gross_profit(is_a)
    is_a = _derive_opinc_from_costs_and_expenses(is_a, usg, "annual")
    is_a = _derive_opex(is_a)
    is_a = _derive_pretax(is_a)

    cf_q = _build_grid(usg, LI_CF, "quarterly", n=8)
    cf_q = _negate_outflows(cf_q)
    cf_q = _add_fcf(cf_q)

    cf_a = _build_grid(usg, LI_CF, "annual", n=4)
    cf_a = _negate_outflows(cf_a)
    cf_a = _add_fcf(cf_a)

    is_q = _add_ebitda(is_q, cf_q)
    is_a = _add_ebitda(is_a, cf_a)

    # LTM column: leftmost on the annual IS/CF tables. For BS we extend
    # _ends with the latest quarter end first so _build_bs_at_ends picks
    # up the most-recent BS values as the "LTM" column.
    is_a = _add_ltm_column(is_a, is_q)
    cf_a = _add_ltm_column(cf_a, cf_q)

    bs_q = _build_bs_at_ends(usg, is_q["_ends"])
    bs_a = _build_bs_at_ends(usg, is_a["_ends"])
    # Relabel the BS's leftmost period as "LTM" for visual consistency with
    # IS/CF — the underlying value is the latest quarter end (point-in-time).
    if bs_a["periods"] and is_a["periods"] and is_a["periods"][0] == "LTM":
        bs_a["periods"][0] = "LTM"

    # Run the canonical filter — adds margin rows, YoY rows, and spacers.
    # `display_n` trims to that many display periods AFTER derivations like
    # YoY (which need the wider buffer to compare against the prior year's
    # same quarter).
    is_q_c = _drop_redundant_nci_row(_canonicalize(_strip(is_q), INCOME_CANONICAL, "quarterly", display_n=4))
    is_a_c = _drop_redundant_nci_row(_canonicalize(_strip(is_a), INCOME_CANONICAL, "annual"))
    bs_q_c = _canonicalize(_strip(bs_q), BALANCE_CANONICAL, "quarterly", display_n=4)
    bs_a_c = _canonicalize(_strip(bs_a), BALANCE_CANONICAL, "annual")
    cf_q_c = _canonicalize(_strip(cf_q), CASHFLOW_CANONICAL, "quarterly", display_n=4)
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
