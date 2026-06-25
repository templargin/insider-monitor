"""One-time repair of the daily insider JSONs after the master.idx dedupe fix.

Before the fix, every Form 4 in a daily index was fetched, parsed, and
aggregated once per associated CIK (issuer + each reporting owner), so each
daily page's per-insider total_value, shares, and txn_count were inflated — 2x
in the common single-owner case, 3x for a joint filing. The PER-COMPANY JSONs
were never affected: they are built from the submissions walk, which lists each
accession exactly once, so they are the authoritative source for recomputing the
daily aggregates.

For each data/insiders/*.json this recomputes every ticker's insiders straight
from data/companies/<TICKER>.json — summing the code-P non-derivative purchases
whose date_filed falls in that page's bucket window, grouped by reporting owner —
then re-applies the $100k single-insider threshold (dropping insiders, and whole
tickers, that only cleared it because of the inflation). It deliberately does NOT
re-run the EV/revenue screener: those inputs were never doubled, and re-screening
with today's price would rewrite historical membership for reasons unrelated to
this bug. EV/MC and company name are preserved verbatim; tickers are re-sorted by
the corrected total.

Dry-run by default (prints a full report); pass --write to persist. Any ticker it
cannot recompute cleanly (missing company JSON, or no matching filings in the
bucket window) is reported and left exactly as-is.

    python -m scripts.dedupe_backfill           # dry run
    python -m scripts.dedupe_backfill --write    # apply
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

from scraper.filters import PURCHASE_THRESHOLD_USD

REPO = Path(__file__).resolve().parent.parent
INSIDERS_DIR = REPO / "data" / "insiders"
COMPANIES_DIR = REPO / "data" / "companies"


def _load_company(ticker):
    p = COMPANIES_DIR / f"{ticker.upper()}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def recompute_ticker(daily_ticker, filing_dates):
    """Recompute one ticker's insiders for a page's bucket window from the
    authoritative company JSON.

    Returns (status, new_insiders, new_total) where status is one of:
      "changed" — recomputed cleanly (new_insiders is the qualifying set)
      "dropped" — bucket filings found, but no reporter clears $100k de-doubled
      "anomaly:<why>" — could not recompute; caller must leave the ticker as-is
    """
    ticker = daily_ticker["ticker"]
    comp = _load_company(ticker)
    if comp is None:
        return f"anomaly:no company JSON", None, None

    fdset = set(filing_dates)
    # Mirror the pipeline's code-P filter exactly: non-derivative, code P,
    # positive signed shares (== acquired) and positive price. `shares` in the
    # company JSON is signed (negative for disposals), so shares > 0 is the same
    # gate as the pipeline's ad_code == "A".
    by_rep = defaultdict(lambda: {
        "reporter_name": "", "relationship": "",
        "total_value": 0.0, "shares": 0.0, "txn_count": 0,
    })
    for r in comp.get("form4_filings", []):
        if r.get("table") != "nonDerivative" or r.get("code") != "P":
            continue
        if r.get("date_filed") not in fdset:
            continue
        shares = r.get("shares") or 0.0
        price = r.get("price") or 0.0
        if shares <= 0 or price <= 0:
            continue
        name = r.get("reporter_name", "")
        rec = by_rep[name]
        rec["reporter_name"] = name
        rec["relationship"] = r.get("relationship", "") or rec["relationship"]
        rec["total_value"] += r.get("total_value") or 0.0
        rec["shares"] += shares
        rec["txn_count"] += 1

    if not by_rep:
        # No P-purchases for this issuer in the window — the company JSON can't
        # explain why the ticker is on the page. Leave it untouched for review
        # rather than silently dropping a possibly-real entry.
        return "anomaly:no bucket filings in company JSON", None, None

    qualifying = sorted(
        (r for r in by_rep.values() if r["total_value"] >= PURCHASE_THRESHOLD_USD),
        key=lambda r: r["total_value"], reverse=True,
    )
    if not qualifying:
        return "dropped", [], 0.0

    insiders = [{
        "reporter_name": r["reporter_name"],
        "relationship": r["relationship"],
        "total_value": r["total_value"],
        "shares": r["shares"],
        "txn_count": r["txn_count"],
    } for r in qualifying]
    return "changed", insiders, sum(r["total_value"] for r in qualifying)


def process_file(path, write=False):
    data = json.loads(path.read_text())
    tickers = data.get("tickers", [])
    if not tickers:
        return None  # empty/holiday page — nothing to do

    filing_dates = data.get("filing_dates", [])
    new_tickers = []
    notes = []           # (ticker, message)
    changed_any = False

    for t in tickers:
        old_total = t.get("total_value") or 0.0
        status, new_insiders, new_total = recompute_ticker(t, filing_dates)

        if status.startswith("anomaly"):
            why = status.split(":", 1)[1]
            notes.append((t["ticker"], f"ANOMALY ({why}) — left unchanged "
                                       f"(was {old_total:,.0f}, {len(t.get('insiders', []))} insiders)"))
            new_tickers.append(t)  # keep original verbatim
            continue

        if status == "dropped":
            changed_any = True
            notes.append((t["ticker"], f"DROPPED — real single-insider total < ${PURCHASE_THRESHOLD_USD:,} "
                                       f"after de-doubling (was {old_total:,.0f})"))
            continue

        # status == "changed"
        factor = (old_total / new_total) if new_total else float("nan")
        if abs(new_total - old_total) > 1.0:
            changed_any = True
        new_t = dict(t)
        new_t["total_value"] = new_total
        new_t["insiders"] = new_insiders
        new_tickers.append(new_t)
        notes.append((t["ticker"], f"{old_total:,.0f} -> {new_total:,.0f}  (x{factor:.2f}), "
                                   f"{len(t.get('insiders', []))} -> {len(new_insiders)} insiders"))

    new_tickers.sort(key=lambda x: x.get("total_value") or 0.0, reverse=True)

    if write and (changed_any or len(new_tickers) != len(tickers)):
        out = dict(data)
        out["tickers"] = new_tickers
        path.write_text(json.dumps(out, indent=2, default=str))

    return {
        "file": path.name,
        "n_old": len(tickers),
        "n_new": len(new_tickers),
        "notes": notes,
        "changed": changed_any or len(new_tickers) != len(tickers),
    }


def main():
    write = "--write" in sys.argv[1:]
    files = sorted(INSIDERS_DIR.glob("*.json"))
    n_changed = 0
    n_dropped = 0
    n_anom = 0
    anomalies = []
    print(f"{'DRY RUN' if not write else 'WRITING'} — {len(files)} daily files\n")
    for p in files:
        res = process_file(p, write=write)
        if res is None or not res["notes"]:
            continue
        if not res["changed"]:
            continue
        n_changed += 1
        print(f"== {res['file']}  ({res['n_old']} -> {res['n_new']} tickers)")
        for tk, msg in res["notes"]:
            print(f"     {tk:6s} {msg}")
            if "DROPPED" in msg:
                n_dropped += 1
            if "ANOMALY" in msg:
                n_anom += 1
                anomalies.append((res["file"], tk, msg))
        print()

    print("──────────────────────────────────────────────")
    print(f"files with changes : {n_changed}")
    print(f"tickers dropped    : {n_dropped}")
    print(f"anomalies (left as-is, REVIEW): {n_anom}")
    for f, tk, msg in anomalies:
        print(f"   {f}  {tk}: {msg}")
    if not write:
        print("\n(dry run — re-run with --write to apply)")


if __name__ == "__main__":
    main()
