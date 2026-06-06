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


def _series_one_tag_quarterly(usg, tag, unit="USD", derive_ytd=True):
    """Discrete-quarterly series for ONE tag, deriving Q4 / fills from YTD.

    `derive_ytd=False` disables the YTD-subtraction walk and returns only the
    directly-reported discrete quarters. Required for weighted-average share
    counts and similar non-additive metrics: subtracting a 9-month average from
    a full-year average (FY − 9M) is meaningless for shares and produced absurd
    values (e.g. BKKT's −6.3M "Q4 diluted shares")."""
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

    if not derive_ytd:
        return discrete

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


def _series_one_tag(usg, tag, freq, unit="USD", derive_ytd=True):
    if freq == "quarterly":
        return _series_one_tag_quarterly(usg, tag, unit, derive_ytd=derive_ytd)
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


def _series(usg, candidates, freq, unit="USD", derive_ytd=True):
    """Try each candidate in priority order. Merge results so earlier candidates
    win at the same period end; later candidates fill in periods earlier missed."""
    accumulated = {}
    for cand in candidates:
        if isinstance(cand, str):
            sub = _series_one_tag(usg, cand, freq, unit, derive_ytd=derive_ytd)
        elif isinstance(cand, tuple) and cand[0] == "sum":
            sub_dicts = [_series_one_tag(usg, t, freq, unit, derive_ytd=derive_ytd) for t in cand[1]]
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
        ("sum", ["InterestAndFeeIncomeLoansAndLeases", "NoninterestIncome"]),
        ("sum", ["InterestIncomeOperating", "NoninterestIncome"]),
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        # Bare bank interest income — last-resort top line for thrifts (PFBX)
        # that tag neither a standard revenue line nor NoninterestIncome. Sits
        # last so it only fires when no broader revenue tag matched.
        "InterestAndDividendIncomeOperating",
        "InterestAndFeeIncomeLoansAndLeases",
        "InterestIncomeOperating",
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
    ("Short-term Investments", ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                                "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]),
    ("Receivables", ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"]),
    ("Inventory", ["InventoryNet"]),
    # Derived plug = Total Current Assets − (the current items above). Filled in
    # _reconcile_bs_subtotals so the current-asset section foots by construction.
    ("Other Current Assets", []),
    ("Total Current Assets", ["AssetsCurrent"]),
    # Net PPE ladder covers oil & gas properties (VTS) and PP&E-incl-finance-lease
    # (OPAL). The specialized productive-asset tags come FIRST because such filers
    # also carry a tiny generic PropertyPlantAndEquipmentNet (office gear) that
    # would otherwise win and bury the real $800M asset base in the Other plug.
    ("Net PPE", ["OilAndGasPropertySuccessfulEffortMethodNet",
                 "OilAndGasPropertyFullCostMethodNet",
                 "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
                 "PropertyPlantAndEquipmentNet"]),
    ("Operating Lease ROU", ["OperatingLeaseRightOfUseAsset"]),
    ("Goodwill", ["Goodwill"]),
    ("Intangible Assets", ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"]),
    ("Long-term Investments", ["LongTermInvestments", "EquityMethodInvestments",
                               "MarketableSecuritiesNoncurrent"]),
    ("Other Assets", []),  # derived: Total Assets − accounted assets
    ("Total Assets", ["Assets"]),
    ("Accounts Payable", ["AccountsPayableCurrent"]),
    ("Current Debt", ["LongTermDebtCurrent", "DebtCurrent",
                      "LongTermDebtAndCapitalLeaseObligationsCurrent"]),
    ("Current Lease Liabilities", ["OperatingLeaseLiabilityCurrent"]),
    ("Other Current Liabilities", []),  # derived: Total Current Liab − items above
    ("Total Current Liabilities", ["LiabilitiesCurrent"]),
    ("Long-term Debt", ["LongTermDebtNoncurrent", "LongTermDebt"]),
    ("Long-term Lease Liabilities", ["OperatingLeaseLiabilityNoncurrent"]),
    ("Other Liabilities", []),  # derived: Total Liab − accounted liabilities
    ("Total Liabilities", ["Liabilities", ("sum", ["LiabilitiesCurrent", "LiabilitiesNoncurrent"])]),
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
        # Partnerships / MLPs (TXO) report partners' capital, not stockholders'
        # equity; LLCs report members' equity. Without these the equity row is
        # blank and the balance sheet can't reconcile.
        "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
        "PartnersCapital",
        "MembersEquity",
    ]),
]

LI_CF_NEGATE = {"CapEx", "Stock Buyback", "Debt Repayment", "Dividends Paid"}

LI_CF = [
    # Same tag priority as the income statement's Net Income (ProfitLoss first,
    # i.e. total incl. NCI) so the CF and IS tabs show the SAME net income. The
    # reversed order here previously made the two tabs disagree for NCI filers —
    # and outright sign-flip for filers like ACCS where ProfitLoss and
    # NetIncomeLoss carry opposite signs. The Δ Working Cap & Other plug
    # re-absorbs any difference, so the operating section still foots.
    ("Net Income", ["ProfitLoss", "NetIncomeLoss"]),
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

    # Period ends: anchor on the union of the primary row (Revenue for the IS,
    # Net Income for the CF) AND Net Income. Anchoring on the primary alone went
    # stale when a filer stopped tagging revenue while still reporting earnings
    # (STEX: revenue last tagged 2024, net income current to 2026 — the whole
    # quarterly IS was frozen at 2024 and its LTM window no longer matched the
    # cash-flow LTM). Net income is near-universally tagged and current, so the
    # union keeps the grid on the most recent periods. Falls back to the union
    # of all rows if neither anchor has data.
    anchor_ends = set(item_series.get(line_items[0][0], {}).keys())
    anchor_ends |= set(item_series.get("Net Income", {}).keys())
    if anchor_ends:
        period_ends = sorted(anchor_ends, reverse=True)[:n]
    else:
        all_ends = set()
        for d in item_series.values():
            all_ends.update(d.keys())
        period_ends = sorted(all_ends, reverse=True)[:n]
    # Note: a genuinely pre-revenue filer whose only revenue facts predate this
    # window (CVM's last revenue was 2014; KLRS's was a pre-reverse-merger 2019)
    # will have no Revenue in the displayed periods and therefore no Gross
    # Margin — correct, since margin is undefined with no current revenue.
    # Anchoring on that stale revenue instead would show decade-old data and
    # desync the IS from the (current) cash-flow statement.

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
        # Weighted-average share counts are not additive across quarters, so the
        # YTD-subtraction walk must not run for them (it yields nonsense Q4s).
        ser = _series(usg, cands, freq, unit="shares", derive_ytd=False)
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
        # Reject an implausible reported GrossProfit tag (GP > 1.05·Revenue → a
        # margin above 100%, impossible). Some filers emit a partial/segment
        # GrossProfit fact for an interim period that dwarfs that period's
        # revenue (e.g. BETR's quarterly tag → 679% LTM). Drop it and let the
        # Rev−CoR / archetype / floor ladder fill the period instead.
        if g is not None and r is not None and r > 0 and g > 1.05 * r:
            g = None
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


def _derive_gross_profit_from_opinc_plus_gna(stmt, usg):
    """Last-resort GP fill for services filers that report Operating Income and
    G&A but no Cost of Revenue / Cost of Goods Sold. Assumes G&A is the ONLY
    non-COGS operating expense — so implied CoR = Rev - OpInc - G&A, which
    gives GP = OpInc + G&A.

    Mathematically GP - OpEx = OpInc still holds (OpEx ≡ G&A). The decomposition
    just isn't disclosed — the filer rolls everything into a single
    OperatingExpenses total. PFHO is the canonical case (workers' comp services:
    Labor + Other Op Cost + G&A = OperatingExpenses, no CoR tagged).

    Gating (per-period, not whole-statement):
      - Skip if `NoninterestExpense` is tagged. That's the bank fallback in the
        SG&A ladder — banks have no GP concept; NII handles them downstream.
      - Skip a period when R&D is large relative to revenue (≥ 50% of revenue).
        That's the genuine-biotech signal — there CoR doesn't exist as a concept
        and OpInc+G&A would be massively negative. But filers that merely *tag*
        a small R&D line on top of a real revenue business (e.g. FONR: R&D = 1.5%
        of revenue, a medical-imaging operator) are NOT excluded — the old
        whole-statement `ResearchAndDevelopmentExpense in usg` early-return
        wrongly blanked them. Genuine biotechs fall through to the revenue floor.
      - Accept only when the implied GP lands in [0, 1.05·Revenue] so gross
        margin stays in a sane 0–100% band; out-of-band periods fall through.

    Tracks filled periods in `_gp_fallback_indices` so `_derive_opex` can skip
    them (otherwise OpEx would render as a duplicate of the SG&A row, since
    OpEx = GP - OpInc = G&A by construction here, which is uninformative)."""
    labels = stmt["labels"]
    if not all(n in labels for n in ("Gross Profit", "Operating Income", "SG&A", "Total Revenue")):
        return stmt
    if "NoninterestExpense" in usg:
        return stmt
    gp_i = labels.index("Gross Profit")
    op_row = stmt["data"][labels.index("Operating Income")]
    sga_row = stmt["data"][labels.index("SG&A")]
    rev_row = stmt["data"][labels.index("Total Revenue")]
    rd_row = stmt["data"][labels.index("R&D")] if "R&D" in labels else [None] * len(op_row)
    gp_row = stmt["data"][gp_i]
    fallback_idx = stmt.setdefault("_gp_fallback_indices", set())
    new_gp = []
    for i, (g, op, sga, rev) in enumerate(zip(gp_row, op_row, sga_row, rev_row)):
        if g is not None:
            new_gp.append(g)
            continue
        rd = rd_row[i] if i < len(rd_row) else None
        rd_heavy = rd is not None and rev not in (None, 0) and abs(rd) >= 0.5 * rev
        if (op is not None and sga is not None and rev is not None
                and rev >= 1_000_000 and not rd_heavy):
            cand = op + sga
            # Trade-off: a negative `cand` (OpInc+G&A < 0) is rejected rather
            # than shown as a negative gross margin. It would only arise when the
            # operating loss exceeds G&A, where the "G&A is the only non-COGS
            # opex" assumption is already breaking down — so we'd rather let the
            # period fall to the revenue floor than publish a shaky negative GP.
            # Net effect: such a filer reads 100% (optimistic) instead of, say,
            # −40%. Acceptable for the small-rev tail; revisit if a real
            # negative-gross-margin operator surfaces here.
            if 0 <= cand <= rev * 1.05:
                new_gp.append(cand)
                fallback_idx.add(i)
                continue
        new_gp.append(None)
    stmt["data"][gp_i] = new_gp
    return stmt


def _derive_opex(stmt):
    """Operating Expense = Gross Profit - Operating Income. By definition this
    satisfies GP - OpEx = OpInc, so the IS always reconciles. Filers' XBRL
    OperatingExpenses tag is inconsistent (some include COGS, some don't), so
    we ignore it. Anchoring on GP (not Rev) is correct when CoR isn't reported
    for some periods — the company's reported GP already accounts for it.

    Periods where GP came from the OpInc+G&A fallback (see
    `_derive_gross_profit_from_opinc_plus_gna`) are skipped — there OpEx would
    just equal SG&A and double up the row uselessly."""
    labels = stmt["labels"]
    if not all(n in labels for n in ("Gross Profit", "Operating Income", "Operating Expense")):
        return stmt
    gp_row = stmt["data"][labels.index("Gross Profit")]
    op_row = stmt["data"][labels.index("Operating Income")]
    opex_i = labels.index("Operating Expense")
    skip = stmt.get("_gp_fallback_indices") or set()
    stmt["data"][opex_i] = [
        None if i in skip
        else ((g - o) if (g is not None and o is not None) else None)
        for i, (g, o) in enumerate(zip(gp_row, op_row))
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


# Interest-income tags that signify a BANK / lender top line. Deliberately
# EXCLUDES `InterestIncomeExpenseNet` — non-banks (CDLX, BKKT, PROP …) tag that
# for a small non-operating net-interest line, and treating it as a bank top
# line would invent a bogus gross-profit row for them.
_BANK_INTEREST_INCOME = [
    "InterestAndDividendIncomeOperating",
    "InterestIncomeOperating",
    "InterestAndFeeIncomeLoansAndLeases",
    "InterestAndFeeIncomeLoansAndLeasesHeldInPortfolio",
]
_BANK_INTEREST_EXPENSE = ["InterestExpense", "InterestExpenseDeposits"]

_INS_PREMIUMS = ["PremiumsEarnedNet"]
_INS_CLAIMS = ["PolicyholderBenefitsAndClaimsIncurredNet",
               "IncurredClaimsPropertyCasualtyAndLiability"]


def _fill_missing_gp(stmt, value_at):
    """Shared helper: fill `Gross Profit` for periods still None using
    value_at(end, index) -> number|None. Records filled indices in
    `_gp_fallback_indices` (so _derive_opex skips them). Needs `_ends`."""
    labels = stmt["labels"]
    if "Gross Profit" not in labels:
        return stmt
    # `_ends` is always set by _build_grid (these fills run before _strip); the
    # `or []` is just belt-and-suspenders — the `i < len(ends)` guard below
    # tolerates a short/empty list.
    ends = stmt.get("_ends") or []
    gp_i = labels.index("Gross Profit")
    gp_row = stmt["data"][gp_i]
    fb = stmt.setdefault("_gp_fallback_indices", set())
    new = []
    for i, g in enumerate(gp_row):
        if g is not None:
            new.append(g)
            continue
        v = value_at(ends[i] if i < len(ends) else None, i)
        if v is not None:
            new.append(v)
            fb.add(i)
        else:
            new.append(None)
    stmt["data"][gp_i] = new
    return stmt


def _derive_gross_profit_bank(stmt, usg, freq):
    """Banks have no cost-of-revenue concept; their gross-profit analog is Net
    Interest Income = interest income − interest expense. Fills missing GP for
    filers that tag a bank-style interest-income line. Gross Margin % then reads
    NII / Total Revenue (interest + noninterest income)."""
    if "Total Revenue" not in stmt["labels"]:
        return stmt
    inc = _series(usg, _BANK_INTEREST_INCOME, freq)
    if not inc:
        return stmt
    exp = _series(usg, _BANK_INTEREST_EXPENSE, freq)

    def val(end, i):
        if end is None:
            return None
        ii = inc.get(end)
        if ii is None:
            return None
        return ii - (exp.get(end) or 0)

    return _fill_missing_gp(stmt, val)


def _derive_gross_profit_insurance(stmt, usg, freq):
    """Insurance underwriting margin = net premiums earned − incurred losses &
    claims. The closest gross-profit analog for carriers (AII, KINS, UTGN).

    Caveat: when a period has premiums but no claims fact (`claims.get(end)` is
    None → treated as 0), GP collapses to premiums, i.e. a 100% underwriting
    margin for that period. That's optimistic but rare — claims are tagged
    alongside premiums for these carriers — so we accept it rather than blank a
    period that does have a premium top line."""
    prem = _series(usg, _INS_PREMIUMS, freq)
    if not prem:
        return stmt
    claims = _series(usg, _INS_CLAIMS, freq)

    def val(end, i):
        if end is None:
            return None
        p = prem.get(end)
        if p is None:
            return None
        return p - (claims.get(end) or 0)

    return _fill_missing_gp(stmt, val)


def _derive_gross_profit_revenue_floor(stmt):
    """Universal last-resort: any period still missing GP after every archetype
    derivation gets GP = Revenue (implied CoR = 0). Correct for pre-revenue /
    pre-commercial filers that genuinely have no cost-of-revenue concept (clinical
    biotechs, early SaaS), and guarantees the mandated Gross Profit / Gross Margin
    rows are never blank. Filled periods are tracked so OpEx skips them."""
    labels = stmt["labels"]
    if "Gross Profit" not in labels or "Total Revenue" not in labels:
        return stmt
    rev_row = stmt["data"][labels.index("Total Revenue")]

    def val(end, i):
        return rev_row[i] if i < len(rev_row) else None

    return _fill_missing_gp(stmt, val)


def _clamp_gp_to_revenue(stmt):
    """Enforce the accounting invariant Gross Profit ≤ Revenue in every column.
    A reported/derived GP can never exceed revenue (cost of revenue ≥ 0). This
    is also what bounds the LTM column: LTM GP = Σ quarterly GP, so clamping each
    quarter to ≤ its revenue keeps the summed LTM margin ≤ 100% even when a
    filer emits a volatile interim GP fact larger than that quarter's (sometimes
    negative) revenue — e.g. BETR's mortgage quarters that produced a 679% LTM."""
    labels = stmt["labels"]
    if "Gross Profit" not in labels or "Total Revenue" not in labels:
        return stmt
    gp_i = labels.index("Gross Profit")
    gp = stmt["data"][gp_i]
    rev = stmt["data"][labels.index("Total Revenue")]

    def clamp(g, r):
        if g is None or r is None:
            return g
        if r <= 0:
            return None  # gross margin is meaningless on zero/negative revenue
        return min(g, r)

    stmt["data"][gp_i] = [clamp(g, r) for g, r in zip(gp, rev)]
    return stmt


def _reconcile_cor(stmt):
    """Make the displayed Cost of Revenue foot to Rev − GP wherever a CoR value
    is shown, so the gross block reconciles by construction (Rev − CoR = GP).
    Only rewrites periods that already HAVE a CoR value — never invents a CoR
    row for banks/insurers/floor-filled filers that don't report one. Fixes
    filers (SLNH, GVH, MIMI) whose CoR tag is partial and disagrees with their
    authoritative reported GrossProfit subtotal."""
    labels = stmt["labels"]
    if not all(n in labels for n in ("Total Revenue", "Cost of Revenue", "Gross Profit")):
        return stmt
    rev = stmt["data"][labels.index("Total Revenue")]
    cor_i = labels.index("Cost of Revenue")
    cor = stmt["data"][cor_i]
    gp = stmt["data"][labels.index("Gross Profit")]
    stmt["data"][cor_i] = [
        (rev[i] - gp[i]) if (cor[i] is not None and rev[i] is not None and gp[i] is not None) else cor[i]
        for i in range(len(cor))
    ]
    return stmt


def _reconcile_shares(stmt):
    """Correct order-of-magnitude unit errors in weighted-average share counts.
    Some filers tag `WeightedAverageNumberOf…SharesOutstanding` in the wrong unit
    (e.g. FMBM: 3,560M reported vs 3.55M actual — a clean 1000× error). The
    filer's own EPS is ground truth: implied shares = |NetIncome / EPS|. When the
    reported share count differs from that by a near-exact power of ten ≥ 1000×,
    rescale it. Conservative by design — it does NOT touch reverse-split or
    minority-interest artifacts (GPUS, MKTW), whose discrepancies are not clean
    powers of ten, because mangling those would introduce wrong numbers.

    Also corrects a wrong EPS *sign*: some filers tag EPS with the opposite sign
    of net income (e.g. AWRE: NI = +$5.9M but EPS = −$0.28). When EPS×shares
    matches net income in magnitude but not sign, flip the EPS sign. Gated tight
    (magnitude within 10%) so it never touches a legitimate loss-per-share."""
    labels = stmt["labels"]
    if "Net Income" not in labels:
        return stmt
    ni = stmt["data"][labels.index("Net Income")]
    for eps_l, sh_l in (("Basic EPS", "Basic Avg Shares"), ("Diluted EPS", "Diluted Avg Shares")):
        if eps_l not in labels or sh_l not in labels:
            continue
        eps = stmt["data"][labels.index(eps_l)]
        sh_i = labels.index(sh_l)
        sh = stmt["data"][sh_i]
        for i in range(len(sh)):
            e, s, n = (eps[i] if i < len(eps) else None), sh[i], (ni[i] if i < len(ni) else None)
            if e is None or s is None or n is None or abs(e) < 0.01 or s <= 0:
                continue
            implied = abs(n / e)
            if implied <= 0:
                continue
            ratio = s / implied
            for factor in (1000.0, 1_000_000.0, 0.001, 0.000001):
                if 0.95 <= ratio / factor <= 1.05:
                    sh[i] = s / factor
                    break
            # EPS sign sanity: |EPS×shares| ≈ |NI| but opposite sign → flip EPS.
            if e * n < 0 and abs(n) > 1_000_000 and abs(abs(e * sh[i]) - abs(n)) <= 0.10 * abs(n):
                eps[i] = -e
    return stmt


def _derive_cf_other_operating(stmt):
    """Insert the operating-section bridge row `Δ Working Cap & Other` =
    OCF − (Net Income + D&A + Stock-Based Comp), per period. This is the change
    in working capital plus every other non-cash operating adjustment the filer
    discloses (deferred tax, gains/losses, impairments, provisions) rolled into
    one line. Computing it as a plug guarantees the operating section foots from
    Net Income to Operating Cash Flow by construction — the XBRL extractor pulls
    only NI/D&A/SBC explicitly, so without this row the section never reconciled.
    Missing addends are treated as 0; the row is blank when OCF itself is absent."""
    labels = stmt["labels"]
    if "Operating Cash Flow" not in labels or "Net Income" not in labels:
        return stmt
    ni = stmt["data"][labels.index("Net Income")]
    ocf = stmt["data"][labels.index("Operating Cash Flow")]
    da = stmt["data"][labels.index("D&A")] if "D&A" in labels else None
    sbc = stmt["data"][labels.index("Stock-Based Comp")] if "Stock-Based Comp" in labels else None
    row = []
    for i, o in enumerate(ocf):
        if o is None:
            row.append(None)
            continue
        adj = (ni[i] or 0)
        if da is not None:
            adj += (da[i] or 0)
        if sbc is not None:
            adj += (sbc[i] or 0)
        row.append(o - adj)
    if "Δ Working Cap & Other" in labels:
        stmt["data"][labels.index("Δ Working Cap & Other")] = row
    else:
        stmt["labels"].append("Δ Working Cap & Other")
        stmt["data"].append(row)
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


def _reconcile_balance_sheet(stmt):
    """Make Assets = Liabilities + Mezzanine + Total Equity hold per column,
    using the most reliable anchor (Total Assets, which equals the always-tagged
    `LiabilitiesAndStockholdersEquity` bottom line). Two distinct fixes, both
    keyed off whether a real Total Liabilities value is present for the period:

      • Missing Total Liabilities (filers like AMST/ANIK/FOSL that tag only
        `LiabilitiesCurrent` + `LiabilitiesAndStockholdersEquity`, never
        `Liabilities`): derive Liabilities = Assets − Equity − Mezzanine.
      • Liabilities present but the sheet still doesn't balance: the equity tag
        we picked is parent-only and omits noncontrolling interest (Up-C filers
        like LOGC where the `…IncludingNoncontrollingInterest` variant isn't
        tagged at that period-end). Override Total Equity = Assets − Liab − Mezz
        so NCI is captured and the sheet foots.

    Normal filers that already balance are untouched (the override only fires
    outside tolerance)."""
    labels = stmt["labels"]
    if not all(n in labels for n in ("Total Assets", "Total Liabilities", "Total Equity")):
        return stmt
    ta = stmt["data"][labels.index("Total Assets")]
    tl_i = labels.index("Total Liabilities")
    tl = stmt["data"][tl_i]
    te_i = labels.index("Total Equity")
    te = stmt["data"][te_i]
    mez = stmt["data"][labels.index("Mezzanine Equity")] if "Mezzanine Equity" in labels else None
    n = len(ta)
    for i in range(n):
        a = ta[i]
        if a is None:
            continue
        l = tl[i] if i < len(tl) else None
        e = te[i] if i < len(te) else None
        m = mez[i] if (mez and i < len(mez) and mez[i] is not None) else 0
        tol = max(abs(a) * 0.01, 500_000)
        if l is not None and e is not None:
            if abs(a - (l + e + m)) > tol:
                te[i] = a - l - m            # capture NCI / restatement gap in equity
        elif l is None and e is not None:
            tl[i] = a - e - m                # derive missing liabilities
        elif e is None and l is not None:
            te[i] = a - l - m                # derive missing equity
    return stmt


def _reconcile_bs_subtotals(stmt):
    """Fill the derived 'Other …' plug rows so each balance-sheet subtotal foots
    from its line items by construction:

      Other Current Assets   = Total Current Assets − (Cash + ST Inv + Recv + Inv)
      Other Assets           = Total Assets − Total Current Assets − (non-current items)
      Other Current Liab.    = Total Current Liab − (AP + Current Debt + Current Lease)
      Other Liabilities      = Total Liab − Total Current Liab − (LT Debt + LT Lease)

    For unclassified sheets (banks, REITs — no AssetsCurrent/LiabilitiesCurrent),
    the 'Other Assets'/'Other Liabilities' plug instead absorbs everything between
    the named items and the total. A plug within rounding of zero is left None so
    the row is pruned (no noise when the items already explain the subtotal)."""
    labels = stmt["labels"]
    idx = {l: i for i, l in enumerate(labels)}

    def row(name):
        return stmt["data"][idx[name]] if name in idx else None

    cash, stinv, recv, inv = row("Cash & Equivalents"), row("Short-term Investments"), row("Receivables"), row("Inventory")
    tca = row("Total Current Assets")
    ppe, rou, gw, intang, ltinv = row("Net PPE"), row("Operating Lease ROU"), row("Goodwill"), row("Intangible Assets"), row("Long-term Investments")
    ta = row("Total Assets")
    ap, cd, cll = row("Accounts Payable"), row("Current Debt"), row("Current Lease Liabilities")
    tcl = row("Total Current Liabilities")
    ltd, ltl = row("Long-term Debt"), row("Long-term Lease Liabilities")
    tl = row("Total Liabilities")
    oca, oa = row("Other Current Assets"), row("Other Assets")
    ocl, ol = row("Other Current Liabilities"), row("Other Liabilities")
    n = len(stmt["periods"])

    def v(r, i):
        return r[i] if (r is not None and i < len(r) and r[i] is not None) else 0

    def has(r, i):
        return r is not None and i < len(r) and r[i] is not None

    def trivial(plug, total_i):
        return abs(plug) < max(500_000.0, abs(total_i) * 0.005)

    def cap(items, subtotal, i):
        """A line item can't exceed the subtotal that contains it; if it does it
        was mistagged for a different concept (a debt-maturity schedule, a
        non-current security tagged 'current', a total-debt tag used for the
        non-current row). Drop it (→ None) so it flows into the plug instead of
        producing a large negative plug. Guards STRZ/LOGC/GPUS double-counts."""
        if subtotal is None:
            return
        for r in items:
            if r is not None and i < len(r) and r[i] is not None and r[i] > abs(subtotal) * 1.02 + 500_000:
                r[i] = None

    for i in range(n):
        # Sanity-cap mistagged items before computing the plugs.
        cap([cash, stinv, recv, inv], (tca[i] if has(tca, i) else None), i)
        cap([ap, cd, cll], (tcl[i] if has(tcl, i) else None), i)
        nca_pool = (ta[i] - tca[i]) if (has(ta, i) and has(tca, i)) else (ta[i] if has(ta, i) else None)
        cap([ppe, rou, gw, intang, ltinv], nca_pool, i)
        ncl_pool = (tl[i] - tcl[i]) if (has(tl, i) and has(tcl, i)) else (tl[i] if has(tl, i) else None)
        cap([ltd, ltl], ncl_pool, i)
        # Current assets
        if oca is not None and has(tca, i):
            p = tca[i] - sum(v(r, i) for r in (cash, stinv, recv, inv))
            oca[i] = None if trivial(p, tca[i]) else p
        # Total assets
        if oa is not None and has(ta, i):
            if has(tca, i):
                p = ta[i] - tca[i] - sum(v(r, i) for r in (ppe, rou, gw, intang, ltinv))
            else:
                p = ta[i] - sum(v(r, i) for r in (cash, stinv, recv, inv, ppe, rou, gw, intang, ltinv))
            oa[i] = None if trivial(p, ta[i]) else p
        # Current liabilities
        if ocl is not None and has(tcl, i):
            p = tcl[i] - sum(v(r, i) for r in (ap, cd, cll))
            ocl[i] = None if trivial(p, tcl[i]) else p
        # Total liabilities
        if ol is not None and has(tl, i):
            if has(tcl, i):
                p = tl[i] - tcl[i] - sum(v(r, i) for r in (ltd, ltl))
            else:
                p = tl[i] - sum(v(r, i) for r in (ap, cd, cll, ltd, ltl))
            ol[i] = None if trivial(p, tl[i]) else p
    return stmt


def _strip(stmt):
    stmt.pop("_ends", None)
    stmt.pop("_gp_fallback_indices", None)
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
    # GP derivation ladder (per-period, most-specific first; each only fills
    # periods still missing GP): tag/Rev−CoR → services OpInc+G&A → bank Net
    # Interest Income → insurance underwriting margin → universal revenue floor.
    # All run before _derive_opex so fallback-filled periods are skipped there,
    # and before _reconcile_cor / LTM so the LTM column sums derived quarterly GP.
    is_q = _build_grid(usg, LI_IS, "quarterly", n=8)
    is_q = _augment_eps_and_shares(is_q, usg, "quarterly")
    is_q = _derive_gross_profit(is_q)
    is_q = _derive_opinc_from_costs_and_expenses(is_q, usg, "quarterly")
    is_q = _derive_gross_profit_from_opinc_plus_gna(is_q, usg)
    is_q = _derive_gross_profit_bank(is_q, usg, "quarterly")
    is_q = _derive_gross_profit_insurance(is_q, usg, "quarterly")
    is_q = _derive_gross_profit_revenue_floor(is_q)
    is_q = _clamp_gp_to_revenue(is_q)
    is_q = _reconcile_cor(is_q)
    is_q = _derive_opex(is_q)
    is_q = _derive_pretax(is_q)
    is_q = _reconcile_shares(is_q)

    is_a = _build_grid(usg, LI_IS, "annual", n=4)
    is_a = _augment_eps_and_shares(is_a, usg, "annual")
    is_a = _derive_gross_profit(is_a)
    is_a = _derive_opinc_from_costs_and_expenses(is_a, usg, "annual")
    is_a = _derive_gross_profit_from_opinc_plus_gna(is_a, usg)
    is_a = _derive_gross_profit_bank(is_a, usg, "annual")
    is_a = _derive_gross_profit_insurance(is_a, usg, "annual")
    is_a = _derive_gross_profit_revenue_floor(is_a)
    is_a = _clamp_gp_to_revenue(is_a)
    is_a = _reconcile_cor(is_a)
    is_a = _derive_opex(is_a)
    is_a = _derive_pretax(is_a)
    is_a = _reconcile_shares(is_a)

    cf_q = _build_grid(usg, LI_CF, "quarterly", n=8)
    cf_q = _negate_outflows(cf_q)
    cf_q = _derive_cf_other_operating(cf_q)
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
    is_a = _clamp_gp_to_revenue(is_a)  # bound the summed LTM GP column too
    cf_a = _add_ltm_column(cf_a, cf_q)
    # Operating-section plug computed AFTER the LTM column so the LTM period
    # foots directly: LTM ΔWC&Other = LTM OCF − (LTM NI + LTM D&A + LTM SBC).
    cf_a = _derive_cf_other_operating(cf_a)

    bs_q = _reconcile_bs_subtotals(_reconcile_balance_sheet(_build_bs_at_ends(usg, is_q["_ends"])))
    bs_a = _reconcile_bs_subtotals(_reconcile_balance_sheet(_build_bs_at_ends(usg, is_a["_ends"])))
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
