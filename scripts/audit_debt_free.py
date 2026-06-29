"""Adversarial check on the *negatives*: for every stored company that
`get_structured_debt` reports as debt-free (—), scan its SEC companyfacts at the
balance-sheet date for any borrowing-shaped liability the extractor missed.

This is the check that was absent when EPSN — which tags its $45.5M only under
the debt-footnote total — slipped through showing "—". Verifying that captured
debt is correct is not enough; the debt-free set must be audited too. Run after
any change to the debt extractor.

    python -m scripts.audit_debt_free
"""
import json
import re
import sys
from pathlib import Path

from scraper import edgar, xbrl_facts, xbrl_statement

# Broad borrowing-liability detector — deliberately wider than the extractor's,
# so it surfaces what the extractor's narrower families miss.
_BORROW = re.compile(
    r"(debt|borrow|lineofcredit|notespayable|notesandloans|loanspayable|loanpayable"
    r"|seniornote|secured(note|debt)|unsecured(note|debt)|subordinat|debenture"
    r"|convertible|commercialpaper|termloan|mediumtermnote|financelease"
    r"|capitalleaseobligation|federalhomeloanbankadvances|repurchase|warehouse)", re.I)
# Exclude assets, disclosures (capacity/available), and explicit non-debt tags.
_NOT = re.compile(
    r"(availableforsale|heldtomaturity|debtsecur|tradingsecur|marketablesecur|investment"
    r"|maturit|proceed|repayment|issuance|amortiz|interestexpense|interestpaid"
    r"|accruedinterest|discount|premium|faceamount|conversionprice|converted|covenant"
    r"|rate|percentage|warrant|paymentsdue|capacity|available|unused|maximum|rightofuse"
    r"|asset|stock|receivable|allowance|heldforsale|duefrom|gross|fairvalue|expense"
    r"|guarantee|liabilitiesotherthan)", re.I)
MATERIAL = 2_000_000


def main():
    base = Path("data/companies")
    tickers = sys.argv[1:] or sorted(p.stem for p in base.glob("*.json"))
    suspects = 0
    for t in tickers:
        cik = json.loads((base / f"{t}.json").read_text()).get("cik")
        if not cik:
            continue
        try:
            facts = edgar.fetch_companyfacts(cik)
        except Exception:
            continue
        if facts is None:
            continue
        debt, _, _ = xbrl_statement.get_structured_debt(facts)
        if debt is not None:                       # only audit the debt-free
            continue
        bsd = xbrl_facts.balance_sheet_date(facts)
        hits = []
        for tag, obj in facts.get("facts", {}).get("us-gaap", {}).items():
            if not _BORROW.search(tag) or _NOT.search(tag):
                continue
            for f in obj.get("units", {}).get("USD", []):
                if f.get("end") == bsd and "start" not in f \
                        and isinstance(f.get("val"), (int, float)) and f["val"] > MATERIAL:
                    hits.append((tag, f["val"]))
                    break
        if hits:
            suspects += 1
            top = sorted(set(hits), key=lambda z: -z[1])[:3]
            print(f"  SUSPECT {t} ({bsd}): " + ", ".join(f"{tag}=${v/1e6:.1f}M" for tag, v in top))
    print(f"\n{suspects} debt-free companies have a possible missed borrowing — review each.")


if __name__ == "__main__":
    main()
