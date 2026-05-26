"""XBRL companyfacts helpers with fallback ladders for inconsistent small-cap tagging."""
from datetime import date, timedelta


def _facts_for_tag(companyfacts, tag, namespace="us-gaap", units="USD"):
    """Return list of fact entries (each with 'val', 'end', 'fp', 'form', etc.) for a tag, or []."""
    if not companyfacts:
        return []
    ns = companyfacts.get("facts", {}).get(namespace, {})
    tag_data = ns.get(tag)
    if not tag_data:
        return []
    return tag_data.get("units", {}).get(units, [])


def latest_value(companyfacts, tags, namespace="us-gaap", units="USD"):
    """Return latest (val, end_date_str) across all listed tags, or (None, None).

    Considers the most recent fact across all candidate tags.
    """
    best_val, best_end = None, None
    for tag in tags:
        for f in _facts_for_tag(companyfacts, tag, namespace, units):
            end = f.get("end", "")
            if not end:
                continue
            if best_end is None or end > best_end:
                best_val, best_end = f.get("val"), end
    return best_val, best_end


# ---- specific extractors ----

CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "Cash",
]

# For debt we sum components — many small caps split long/short
DEBT_AGGREGATE_TAGS = [
    "LongTermDebt",
    "DebtLongtermAndShorttermCombinedAmount",
]
DEBT_LONG_TAGS = ["LongTermDebtNoncurrent"]
DEBT_SHORT_TAGS = ["LongTermDebtCurrent", "ShortTermBorrowings", "DebtCurrent",
                   "NotesPayableCurrent"]

# Shares outstanding (basic, point-in-time)
SHARES_TAGS_DEI = ["EntityCommonStockSharesOutstanding"]
SHARES_TAGS_USGAAP = ["CommonStockSharesOutstanding"]

# Revenue.
# Banks/insurance tag top-line as InterestAndDividendIncomeOperating (and
# NoninterestIncome) rather than Revenues — included here so community banks
# like BCML/CUBI/FMBM aren't false-rejected by the `TTM revenue > 0` screener
# filter. For the screener's >0 check the bank tag alone is enough; the
# financials module separately sums Interest + Noninterest income for the
# displayed Total Revenue.
REVENUE_TAGS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "InterestAndDividendIncomeOperating",
    "NoninterestIncome",
]

OPTIONS_TAGS = [
    "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber",
    "EmployeeStockOptionsOutstanding",
]

WARRANT_TAGS = [
    "ClassOfWarrantOrRightOutstanding",
    "WarrantsAndRightsOutstanding",
]


def get_cash(facts):
    v, end = latest_value(facts, CASH_TAGS)
    return v, end


def get_total_debt(facts):
    """Return (total_debt_usd, as_of_date). Tries aggregate tags first, then sum of components.

    Returns (None, None) if no debt tags found (which we treat as 0 for screener since
    no-debt cos exist; but the page should flag uncertainty).
    """
    val, end = latest_value(facts, DEBT_AGGREGATE_TAGS)
    if val is not None:
        return val, end
    # Sum components
    long_val, long_end = latest_value(facts, DEBT_LONG_TAGS)
    short_val, short_end = latest_value(facts, DEBT_SHORT_TAGS)
    if long_val is None and short_val is None:
        return None, None
    total = (long_val or 0) + (short_val or 0)
    end_date = max(filter(None, [long_end, short_end]), default=None)
    return total, end_date


def get_basic_shares(facts):
    """Latest basic shares outstanding from DEI cover-page tag (preferred — most recent)
    falling back to us-gaap balance-sheet tag.
    """
    v, end = latest_value(facts, SHARES_TAGS_DEI, namespace="dei", units="shares")
    if v is not None:
        return v, end
    v, end = latest_value(facts, SHARES_TAGS_USGAAP, units="shares")
    return v, end


def get_options_outstanding(facts):
    v, end = latest_value(facts, OPTIONS_TAGS, units="shares")
    return v, end


def get_warrants_outstanding(facts):
    v, end = latest_value(facts, WARRANT_TAGS, units="shares")
    return v, end


def get_latest_revenue_any(facts):
    """Most recent revenue value (any period). For the simple > 0 filter."""
    v, end = latest_value(facts, REVENUE_TAGS, units="USD")
    return v, end


def get_ttm_revenue(facts):
    """Sum the 4 most recent quarterly revenue values for a rough TTM.

    Falls back to most recent annual (FY) value if quarterly data unavailable.
    Returns (ttm_value, as_of_end_date) or (None, None) if no revenue tags found.

    Stale-data guard: ignore revenue entries whose `end` predates today by more
    than ~18 months. Shell-co reverse mergers (e.g. KLRS = Kalaris Therapeutics,
    which inherited a 2019 $165k revenue fact from its predecessor and has been
    a clinical-stage biotech with zero revenue since) would otherwise leak past
    the `TTM revenue > 0` screener filter.
    """
    stale_cutoff = (date.today() - timedelta(days=540)).isoformat()

    # Pick the single revenue tag with the freshest data (fresh-only)
    best_tag, best_end = None, None
    for tag in REVENUE_TAGS:
        for f in _facts_for_tag(facts, tag):
            end = f.get("end", "")
            if not end or end < stale_cutoff:
                continue
            if best_end is None or end > best_end:
                best_end, best_tag = end, tag
    if best_tag is None:
        return None, None

    entries = [e for e in _facts_for_tag(facts, best_tag)
               if e.get("end", "") >= stale_cutoff]
    if not entries:
        return None, None

    # Classify by period length (end - start days)
    def days(e):
        s = e.get("start", "")
        en = e.get("end", "")
        if not s or not en:
            return 0
        return (date.fromisoformat(en) - date.fromisoformat(s)).days

    # Sort by end descending
    entries.sort(key=lambda e: e.get("end", ""), reverse=True)

    # Try to assemble TTM from quarterly (60-100 day periods)
    quarterly = [e for e in entries if 60 <= days(e) <= 100]
    if len(quarterly) >= 4:
        latest_four = quarterly[:4]
        # Dedupe by end date
        seen_ends = set()
        unique = []
        for e in latest_four:
            if e["end"] not in seen_ends:
                seen_ends.add(e["end"])
                unique.append(e)
        if len(unique) >= 4:
            total = sum(e["val"] for e in unique[:4])
            return total, unique[0]["end"]

    # Fallback: most recent annual (~365 day period)
    annual = [e for e in entries if 350 <= days(e) <= 380]
    if annual:
        return annual[0]["val"], annual[0]["end"]

    # Last resort: latest value
    return entries[0]["val"], entries[0]["end"]


def get_description_from_facts(facts):
    """Try to extract entity description from companyfacts. (Usually not here, but cover-page tag exists.)"""
    if not facts:
        return ""
    return facts.get("entityName", "")
