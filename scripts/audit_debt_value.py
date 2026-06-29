"""Adversarial check on the *positives*: for every stored company that
`get_structured_debt` reports a debt figure for, cross-check it against the
filer's own authoritative debt totals (the debt-footnote carrying amount and the
aggregate `LongTermDebt` tags). A large disagreement means the extractor is
double-counting overlapping tags or missing a component.

This is the counterpart to audit_debt_free.py — together they audit both sides:
that "—" companies are truly debt-free, and that companies with a number have
the *right* number. Found the ANGX over-count ($205M vs the filer's $102M/$106M
totals) that no within-extractor check could see.

    python -m scripts.audit_debt_value
"""
import json
import sys
from pathlib import Path

from scraper import edgar, xbrl_facts, xbrl_statement

REFS = ["DebtInstrumentCarryingAmount", "LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"]


def _at(us, tag, bsd):
    for f in us.get(tag, {}).get("units", {}).get("USD", []):
        if f.get("end") == bsd and "start" not in f and isinstance(f.get("val"), (int, float)):
            return f["val"]
    return None


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
        debt, _, flag = xbrl_statement.get_structured_debt(facts)
        if not debt:
            continue
        bsd = xbrl_facts.balance_sheet_date(facts)
        us = facts.get("facts", {}).get("us-gaap", {})
        refs = {t2: _at(us, t2, bsd) for t2 in REFS if _at(us, t2, bsd)}
        if not refs:
            continue
        # compare to the nearest authoritative total
        rt, rv = min(refs.items(), key=lambda kv: abs(kv[1] - debt))
        diff = abs(debt - rv)
        if diff > max(5_000_000, 0.2 * rv):
            suspects += 1
            fl = f" [{flag['reason']}]" if flag else ""
            print(f"  SUSPECT {t}: extractor ${debt/1e6:.0f}M vs {rt} ${rv/1e6:.0f}M "
                  f"(off ${diff/1e6:.0f}M){fl}")
    print(f"\n{suspects} debt figures disagree with the filer's own total — review each.")


if __name__ == "__main__":
    main()
