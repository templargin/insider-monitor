"""Refresh financials for all stored companies via the new pipeline, then audit.
Reuses the existing JSON's ticker/cik to skip Form-4 fetches."""
import json
import sys
from pathlib import Path

from scraper import xbrl_financials

# Inline audit to avoid loading old data
sys.path.insert(0, str(Path(__file__).parent))
from audit_financials import audit_ticker  # noqa


def refresh_one(ticker, cik):
    fin = xbrl_financials.fetch_xbrl_financials(cik)
    return {"ticker": ticker, "cik": cik, "financials": fin, "valuation": {}}


if __name__ == "__main__":
    base = Path("data/companies")
    tickers = sys.argv[1:] or sorted(p.stem for p in base.glob("*.json"))
    all_issues = []
    for t in tickers:
        p = base / f"{t}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        cik = d.get("cik")
        # Preserve SO for the shares audit
        val = d.get("valuation") or {}
        refreshed = refresh_one(t, cik)
        refreshed["valuation"] = {"shares_basic": val.get("shares_basic")}
        all_issues.extend(audit_ticker(refreshed))

    by_kind = {}
    for line in all_issues:
        try:
            rest = line.split("] ", 1)[1]
            kind = rest.split(":", 1)[0].split("/")[0]
        except Exception:
            kind = "?"
        by_kind.setdefault(kind, []).append(line)

    for kind, lst in sorted(by_kind.items()):
        print(f"\n=== {kind} ({len(lst)} issues) ===")
        for line in lst[:80]:
            print(line)
        if len(lst) > 80:
            print(f"  ... and {len(lst) - 80} more")

    print(f"\nTotal: {len(all_issues)} issues across {len(tickers)} tickers.")
