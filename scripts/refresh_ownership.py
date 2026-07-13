"""Fetch/refresh the institutional-ownership + analyst-coverage block for every
stored company JSON via yfinance. A failed fetch preserves the existing block
(a transient Yahoo throttle must never clobber good data). Used for the initial
backfill and after any ownership-schema change; safe to re-run anytime.

Run from a residential/droplet IP where possible — Yahoo throttles cloud IPs
(GitHub Actions) hardest.

Usage: python -m scripts.refresh_ownership [TICKER ...]
"""
import json
import sys
import time
from pathlib import Path

from scraper import financials


def main():
    base = Path("data/companies")
    tickers = sys.argv[1:] or sorted(p.stem for p in base.glob("*.json"))
    updated = failed = 0
    for i, t in enumerate(tickers, 1):
        p = base / f"{t}.json"
        d = json.loads(p.read_text())
        own = financials.fetch_ownership(t)
        if own is None:
            failed += 1
            print(f"[{t}] fetch failed — existing block preserved ({i}/{len(tickers)})")
        else:
            d["ownership"] = own
            p.write_text(json.dumps(d, indent=2, default=str))
            updated += 1
            inst = own.get("inst_pct")
            inst_s = f"{inst*100:.1f}%" if inst is not None else "—"
            print(f"[{t}] inst={inst_s} analysts={own.get('analyst_count') or 0} ({i}/{len(tickers)})")
        time.sleep(0.4)  # be gentle with Yahoo
    print(f"\nUpdated {updated}/{len(tickers)} company files ({failed} fetch failures).")


if __name__ == "__main__":
    main()
