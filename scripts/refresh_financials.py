"""Re-extract financial statements for every company JSON using the current
xbrl_financials code. Preserves everything else (Form 4 history, valuation,
description). Used after schema changes to the financials extractor."""
import json
import sys
from pathlib import Path

from scraper import xbrl_financials


def main():
    base = Path("data/companies")
    tickers = sys.argv[1:] or sorted(p.stem for p in base.glob("*.json"))
    updated = 0
    for t in tickers:
        p = base / f"{t}.json"
        d = json.loads(p.read_text())
        cik = d.get("cik")
        if not cik:
            continue
        fin = xbrl_financials.fetch_xbrl_financials(cik)
        if fin is None:
            print(f"[{t}] no XBRL facts (CIK {cik}) — skipped")
            continue
        d["financials"] = fin
        p.write_text(json.dumps(d, indent=2, default=str))
        updated += 1
        print(f"[{t}] refreshed")
    print(f"\nUpdated {updated}/{len(tickers)} company files.")


if __name__ == "__main__":
    main()
