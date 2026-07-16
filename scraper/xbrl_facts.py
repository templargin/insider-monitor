"""XBRL companyfacts helpers with fallback ladders for inconsistent small-cap tagging."""


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

    Considers the most recent fact across all candidate tags. NOTE: for
    point-in-time balance-sheet items prefer `instant_value_at` anchored to
    `balance_sheet_date` — `latest_value` has no date anchor, so a tag a filer
    abandoned years ago (e.g. REI/AVD still carrying a 2016 `LongTermDebt` fact)
    will mask the current balance sheet.
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


# Balance-sheet subtotal concepts that essentially every filer reports, used to
# pin down the current reporting date. `Assets` alone is near-universal; the rest
# are fallbacks for unusual sheets.
BS_ANCHOR_TAGS = ["Assets", "LiabilitiesAndStockholdersEquity", "Liabilities",
                  "StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]


def balance_sheet_date(companyfacts):
    """Period-end of the most recent balance sheet — the anchor date for every
    point-in-time (instant) read. Returns an ISO date string or None.

    Anchoring instant reads to this date is what prevents a tag a filer stopped
    using years ago from leaking into the current figures (the stale-fact bug
    that made REI read $0 debt off a 2016 `LongTermDebt` fact, and AVD read a
    2016 number while its real $266M sat in the current component tag).
    """
    best = None
    for tag in BS_ANCHOR_TAGS:
        for f in _facts_for_tag(companyfacts, tag):
            if "start" in f:                 # instant facts only (skip durations)
                continue
            end = f.get("end", "")
            if end and (best is None or end > best):
                best = end
    return best


def instant_value_at(companyfacts, tags, as_of, namespace="us-gaap", units="USD"):
    """Value of the first listed tag that has an instant fact dated exactly `as_of`.

    Tags are tried in priority order; returns (val, as_of) or (None, None). A tag
    with no fact on `as_of` is, by definition, not on the current balance sheet,
    so it is correctly ignored rather than substituted with a stale value. When a
    period-end carries more than one fact (an original + a later restatement),
    the latest-filed value wins.
    """
    if not as_of:
        return None, None
    for tag in tags:
        cands = [f for f in _facts_for_tag(companyfacts, tag, namespace, units)
                 if f.get("end") == as_of and "start" not in f]
        if cands:
            return max(cands, key=lambda f: f.get("filed", "")).get("val"), as_of
    return None, None


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

# Revenue is deliberately absent from this module. It lives in
# `xbrl_financials.ltm_revenue`, read off the canonical grid the company page
# renders. The ladder that used to live here picked the single freshest-ending tag
# and discarded the rest, reading CUBI's top line as $43M against its real $1.51B
# and FMBM's as $162k against $80M. Do not reintroduce a second one.
#
# Both the screen and the page read that one grid, but they do NOT apply the same
# rule to it: ltm_revenue sums whichever quarters exist (so a two-quarter IPO is
# not falsely rejected by `revenue > 0`), while sitegen's `_sum_ttm` is
# all-or-nothing (a partial sum is not a TTM to build a multiple on). So they
# differ for ~12 filers, and `valuation.ttm_revenue` holds a partial sum for
# those — not rendered anywhere, but not a trailing twelve months either.

OPTIONS_TAGS = [
    "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber",
    "EmployeeStockOptionsOutstanding",
]

WARRANT_TAGS = [
    "ClassOfWarrantOrRightOutstanding",
    "WarrantsAndRightsOutstanding",
]


def get_cash(facts, as_of=None):
    """Cash on the current balance sheet, anchored to the balance-sheet date.

    Falls back to the latest cash fact only when no anchor date is available
    (e.g. a filer with no `Assets` tag), never to a value off a different date.
    """
    if as_of is None:
        as_of = balance_sheet_date(facts)
    v, end = instant_value_at(facts, CASH_TAGS, as_of)
    if v is None:           # no cash fact on the anchor date — fall back to latest
        v, end = latest_value(facts, CASH_TAGS)
    return v, end


def get_total_debt(facts, as_of=None):
    """Total debt on the *current* balance sheet, anchored to the balance-sheet
    date so abandoned/stale tags can't leak in.

    Tries the aggregate tags first, then the sum of long- + short-term
    components — but only facts dated on the current balance sheet date count.
    Returns (total_debt_usd, as_of) or (None, None) when the filer reports none
    of these tags on the current sheet.

    This anchoring is move 1 of the structured-debt fix: it kills the stale-fact
    bug (a 2016 `LongTermDebt` no longer short-circuits a 2026 sheet). It does
    NOT by itself capture debt the filer reports under a tag outside these
    ladders (e.g. `LineOfCredit`, `SeniorNotes`) — see get_structured_debt.
    """
    if as_of is None:
        as_of = balance_sheet_date(facts)
    val, end = instant_value_at(facts, DEBT_AGGREGATE_TAGS, as_of)
    if val is not None:
        return val, end
    long_val, _ = instant_value_at(facts, DEBT_LONG_TAGS, as_of)
    short_val, _ = instant_value_at(facts, DEBT_SHORT_TAGS, as_of)
    if long_val is None and short_val is None:
        return None, None
    return (long_val or 0) + (short_val or 0), as_of


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


def get_description_from_facts(facts):
    """Try to extract entity description from companyfacts. (Usually not here, but cover-page tag exists.)"""
    if not facts:
        return ""
    return facts.get("entityName", "")
