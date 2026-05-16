"""Coverage probe across all stored tickers.

For every canonical IS row (Revenue, CoR, GP, SG&A, R&D, OpInc, Pretax,
Tax, NI), report:
  1. How many tickers populate the row vs total.
  2. For tickers missing it, scan their raw XBRL for candidate tags that
     plausibly fit the concept (e.g., any FY-period tag containing "Cost"
     for CoR), and rank candidates by how many missing tickers report them.

Output: a per-row backlog showing the top candidate tags ranked by how
many tickers they'd unblock. Use this to prioritize ladder additions.

Usage: ./venv/bin/python -m scripts.probe_coverage [TICKER ...]
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from scraper import edgar

COMPANIES = Path("data/companies")


# Canonical row → keyword patterns to identify candidate tags in raw XBRL.
# Each keyword pattern is a list of substrings; a tag matches if ALL substrings
# appear in the tag name (case-sensitive XBRL tag style).
CANDIDATE_PATTERNS = {
    "Total Revenue": [
        ["Revenue"], ["Sales"], ["InterestAndDividend"], ["NoninterestIncome"],
    ],
    "Cost of Revenue": [
        ["Cost", "Revenue"], ["Cost", "Goods"], ["Cost", "Services"],
        ["Cost", "Sales"], ["Direct", "Cost"], ["Direct", "Operating"],
    ],
    "Gross Profit": [["GrossProfit"]],
    "SG&A": [
        ["Selling"], ["GeneralAndAdministrative"], ["Marketing"],
        ["NoninterestExpense"],
    ],
    "R&D": [["ResearchAndDevelopment"]],
    "Operating Income": [
        ["OperatingIncome"], ["IncomeFromContinuingOperations"],
    ],
    "Pretax Income": [
        ["BeforeIncomeTax"], ["BeforeTax"], ["Pretax"],
    ],
    "Tax Provision": [
        ["IncomeTax", "Expense"], ["TaxProvision"], ["IncomeTax", "Benefit"],
    ],
    "Net Income": [
        ["NetIncome"], ["ProfitLoss"], ["NetEarnings"],
    ],
}


# Tags my current code already uses — don't suggest these as "new candidates"
CURRENT_LADDERS = {
    "Total Revenue": {
        "InterestAndDividendIncomeOperating", "NoninterestIncome",
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet", "SalesRevenueGoodsNet",
    },
    "Cost of Revenue": {
        "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    },
    "Gross Profit": {"GrossProfit"},
    "SG&A": {
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
        "SellingAndMarketingExpense", "SellingExpense",
        "NoninterestExpense",
    },
    "R&D": {"ResearchAndDevelopmentExpense"},
    "Operating Income": {"OperatingIncomeLoss"},
    "Pretax Income": set(),  # derived
    "Tax Provision": {"IncomeTaxExpenseBenefit"},
    "Net Income": {"ProfitLoss", "NetIncomeLoss"},
}


def fy_value(usg, tag):
    """Return the latest FY USD value for a tag, or None."""
    units = usg.get(tag, {}).get("units", {}).get("USD", [])
    fy = []
    for e in units:
        s, en = e.get("start"), e.get("end")
        if not s or not en:
            continue
        try:
            d = (date.fromisoformat(en) - date.fromisoformat(s)).days
        except ValueError:
            continue
        if 350 <= d <= 380:
            fy.append(e)
    if not fy:
        return None
    e = max(fy, key=lambda x: (x["end"], x.get("accn", "")))
    return e["val"]


def is_canonical_present(d, row):
    """Check if the stored financials JSON populates `row` in annual IS (latest period)."""
    fins = d.get("financials") or {}
    isa = (fins.get("income_statement") or {}).get("annual") or {}
    labels = isa.get("labels", [])
    if row not in labels:
        return False
    i = labels.index(row)
    data = isa.get("data", [])
    if i >= len(data):
        return False
    row_data = data[i] or []
    # The leftmost is now LTM. Look at index 1 (FY25) — that's the canonical "latest" annual value.
    target_idx = 1 if isa.get("periods", [None])[0] == "LTM" and len(row_data) > 1 else 0
    return target_idx < len(row_data) and row_data[target_idx] is not None


def candidate_tags(usg, row, exclude_existing=True):
    """Return all tags in usg that could plausibly fit `row` (based on
    CANDIDATE_PATTERNS) AND that have a non-zero FY value AND aren't already
    in our current ladder."""
    patterns = CANDIDATE_PATTERNS.get(row, [])
    existing = CURRENT_LADDERS.get(row, set())
    hits = []
    for tag in usg.keys():
        if exclude_existing and tag in existing:
            continue
        # All substrings in at least one pattern must be in the tag
        if not any(all(sub in tag for sub in pat) for pat in patterns):
            continue
        val = fy_value(usg, tag)
        if val is None or val == 0:
            continue
        hits.append((tag, val))
    return hits


def main():
    tickers = sys.argv[1:] or sorted(p.stem for p in COMPANIES.glob("*.json"))
    print(f"Probing {len(tickers)} tickers...\n")

    # Per-row stats and per-row backlog
    coverage = {row: 0 for row in CANDIDATE_PATTERNS}
    missing_by_row = defaultdict(list)  # row -> [(ticker, cik, usg)]
    total = 0

    for t in tickers:
        p = COMPANIES / f"{t}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        cik = d.get("cik")
        if not cik:
            continue
        total += 1
        for row in CANDIDATE_PATTERNS:
            if is_canonical_present(d, row):
                coverage[row] += 1
            else:
                missing_by_row[row].append((t, cik))

    print("=== Coverage (latest annual period) ===")
    for row, n in coverage.items():
        pct = 100 * n / total if total else 0
        print(f"  {row:18}  {n:>3}/{total}  ({pct:5.1f}%)")

    # For each row missing in any ticker, fetch XBRL once and aggregate
    print("\n=== Candidate tags for under-covered rows ===")
    # Cache fetched XBRL by cik so we don't re-fetch
    facts_cache = {}
    for row, missers in missing_by_row.items():
        if not missers:
            continue
        if coverage[row] == total:
            continue
        candidates = Counter()
        ticker_examples = defaultdict(list)
        for t, cik in missers:
            facts = facts_cache.get(cik)
            if facts is None:
                facts = edgar.fetch_companyfacts(cik)
                facts_cache[cik] = facts
            if not facts:
                continue
            usg = facts.get("facts", {}).get("us-gaap", {})
            for tag, val in candidate_tags(usg, row):
                candidates[tag] += 1
                ticker_examples[tag].append((t, val))
        if not candidates:
            print(f"\n  {row}: {len(missers)} tickers missing — NO candidate tags found")
            print(f"     Missing: {', '.join(t for t, _ in missers[:10])}{'...' if len(missers) > 10 else ''}")
            continue
        print(f"\n  {row}: {len(missers)} tickers missing — top candidate tags:")
        for tag, count in candidates.most_common(8):
            sample_t, sample_v = ticker_examples[tag][0]
            extra = f" + {len(ticker_examples[tag]) - 1} more" if len(ticker_examples[tag]) > 1 else ""
            print(f"     {count:>3}× {tag}")
            print(f"          e.g. {sample_t}: {sample_v:,.0f}{extra}")


if __name__ == "__main__":
    main()
