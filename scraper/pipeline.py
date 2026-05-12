"""End-to-end pipeline: scrape Form 4s for a URL bucket, apply filters,
update per-ticker data, write daily JSON. Heavy lifting orchestrator.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import edgar, filters, xbrl_facts, financials, xbrl_financials, buckets

_MAX_WORKERS = 6

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
INSIDERS_DIR = DATA_DIR / "insiders"
COMPANIES_DIR = DATA_DIR / "companies"
INSIDERS_DIR.mkdir(parents=True, exist_ok=True)
COMPANIES_DIR.mkdir(parents=True, exist_ok=True)


def _log(*a):
    print(*a, flush=True)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_and_parse(row):
    """Fetch + parse one Form 4 filing. Returns parsed dict or None."""
    xml = edgar.fetch_form4_xml(row["cik"], row["accession_nodash"])
    if xml is None:
        return None
    parsed = edgar.parse_form4(xml)
    if parsed is None:
        return None
    parsed["date_filed"] = row["date_filed"]
    parsed["accession"] = row["accession"]
    parsed["form"] = row["form"]
    return parsed


def fetch_all_form4s_for_bucket(url_date):
    """Fetch + parse every Form 4 in the bucket via daily-index, threaded."""
    out = []
    for fd in buckets.filing_dates_for_url(url_date):
        rows = edgar.fetch_daily_index_form4s(fd)
        _log(f"  [{fd}] {len(rows)} Form 4 / 4-A index rows")
        if not rows:
            continue
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            for i, parsed in enumerate(ex.map(_fetch_and_parse, rows), 1):
                if parsed is not None:
                    out.append(parsed)
                if i % 250 == 0:
                    rate = i / max(time.time() - t0, 0.001)
                    _log(f"    {i}/{len(rows)} ({rate:.1f} filings/s)")
        _log(f"  [{fd}] done in {time.time()-t0:.1f}s")
    return out


def screener_pass(cik, ticker, bucket_data):
    """Returns dict with valuation + filter outcome, or None if data unavailable."""
    facts = edgar.fetch_companyfacts(cik)
    if facts is None:
        return None
    shares, sh_end = xbrl_facts.get_basic_shares(facts)
    cash, _ = xbrl_facts.get_cash(facts)
    debt, _ = xbrl_facts.get_total_debt(facts)
    ttm_rev, _ = xbrl_facts.get_ttm_revenue(facts)

    if shares is None or shares <= 0:
        return None
    price = financials.fetch_share_price(ticker)
    if price is None or price <= 0:
        return None
    mc_basic = price * shares
    ev = filters.basic_ev(mc_basic, debt, cash)
    if not filters.passes_ev_cap(ev):
        return None
    if not filters.passes_revenue(ttm_rev):
        return None
    return {
        "facts": facts,
        "shares": shares,
        "shares_as_of": sh_end,
        "cash": cash,
        "debt": debt,
        "ttm_revenue": ttm_rev,
        "share_price": price,
        "mc_basic": mc_basic,
        "ev_basic": ev,
    }


def update_company_data(ticker, cik, screener_snapshot):
    """Refresh `data/companies/TICKER.json` with full 2y Form 4 history,
    valuation table inputs, and financial statements. screener_snapshot supplies
    pre-fetched facts/price to avoid re-fetching.
    """
    facts = screener_snapshot["facts"]
    options, _ = xbrl_facts.get_options_outstanding(facts)
    warrants, _ = xbrl_facts.get_warrants_outstanding(facts)

    # Pull 2y of Form 4 filings via the submissions JSON
    cutoff = (date.today() - timedelta(days=730)).isoformat()
    subs = edgar.fetch_submissions(cik)
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])

    form4_filings = []
    for i, f in enumerate(forms):
        if f not in ("4", "4/A"):
            continue
        if dates[i] < cutoff:
            continue
        acc = accs[i]
        acc_nodash = acc.replace("-", "")
        xml = edgar.fetch_form4_xml(cik, acc_nodash)
        if xml is None:
            continue
        parsed = edgar.parse_form4(xml)
        if parsed is None:
            continue
        # We trust transactions; convert to thin display rows
        for txn in parsed["transactions"]:
            form4_filings.append({
                "date_filed": dates[i],
                "reporter_name": parsed["reporter_name"],
                "relationship": parsed["relationship"],
                "transaction_date": txn["transaction_date"],
                "code": txn["code"],
                "shares": txn["shares"],
                "price": txn["price"],
                "total_value": txn["total_value"],
                "security_title": txn["security_title"],
                "table": txn["table"],
                "ownership": txn["ownership"],
                "accession": acc,
            })

    # Sort filings most recent first
    form4_filings.sort(key=lambda r: (r["date_filed"], r["transaction_date"]), reverse=True)

    description = financials.fetch_description(ticker)
    # XBRL-primary: skip the yfinance financial-statement scrape entirely.
    fins = xbrl_financials.fetch_xbrl_financials(cik)

    payload = {
        "ticker": ticker.upper(),
        "cik": str(cik),
        "name": screener_snapshot.get("name") or subs.get("name", ""),
        "description": description,
        "form4_filings": form4_filings,
        "valuation": {
            "share_price": screener_snapshot["share_price"],
            "shares_basic": screener_snapshot["shares"],
            "shares_basic_as_of": screener_snapshot["shares_as_of"],
            "options": options,
            "warrants": warrants,
            "cash": screener_snapshot["cash"],
            "debt": screener_snapshot["debt"],
            "ttm_revenue": screener_snapshot["ttm_revenue"],
            "mc_basic": screener_snapshot["mc_basic"],
            "ev_basic": screener_snapshot["ev_basic"],
        },
        "financials": fins,
        "last_updated": _now_iso(),
    }

    path = COMPANIES_DIR / f"{ticker.upper()}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def process_bucket(url_date):
    """Process one URL date end-to-end: scrape, filter, write daily + company JSONs.

    Safety: if EDGAR returned ZERO filings for every filing-date in this bucket
    (typical when SEC hasn't published the daily-index yet for late-evening runs),
    skip writing — don't clobber an existing good page with an empty one.
    """
    _log(f"=== Processing /insiders/{url_date.year}/{buckets.MONTH_NAMES[url_date.month-1]}/{url_date.day} (read on {url_date.strftime('%A')}) ===")
    # Pre-flight: count daily-index rows across all bucket dates
    total_index_rows = 0
    for fd in buckets.filing_dates_for_url(url_date):
        try:
            total_index_rows += len(edgar.fetch_daily_index_form4s(fd))
        except Exception as e:
            _log(f"  daily-index fetch failed for {fd}: {e}")
    if total_index_rows == 0:
        _log("  EDGAR returned 0 Form 4 index rows for every bucket date — skipping write (likely too early for SEC daily-index).")
        return None

    parsed = fetch_all_form4s_for_bucket(url_date)
    _log(f"  Parsed {len(parsed)} Form 4 filings total")
    aggregated = filters.aggregate_p_purchases(parsed)
    threshold = [(cik, b) for cik, b in aggregated.items() if filters.passes_threshold(b)]
    _log(f"  {len(threshold)} issuers ≥${filters.PURCHASE_THRESHOLD_USD:,} aggregate")

    survivors = []
    for cik, bucket_data in threshold:
        ticker = bucket_data["ticker"]
        name = bucket_data["name"]
        if not ticker:
            _log(f"  - skip {name} (no ticker on Form 4)")
            continue
        _log(f"  ? probing {name} ({ticker})...")
        snap = screener_pass(cik, ticker, bucket_data)
        if snap is None:
            _log(f"    fail (EV/revenue/data unavailable)")
            continue
        _log(f"    PASS: EV=${snap['ev_basic']/1e6:,.1f}M  TTM rev=${snap['ttm_revenue']/1e6:,.1f}M")
        snap["name"] = name
        snap["ticker"] = ticker
        snap["cik"] = cik
        snap["bucket_data"] = bucket_data
        survivors.append(snap)

    # Persist daily JSON
    daily = {
        "url_date": url_date.isoformat(),
        "weekday": url_date.strftime("%A"),
        "filing_dates": [fd.isoformat() for fd in buckets.filing_dates_for_url(url_date)],
        "generated_at": _now_iso(),
        "tickers": [],
    }
    for s in survivors:
        # Aggregate per (reporter, relationship) for clean display
        by_insider = {}
        for filing in s["bucket_data"]["filings"]:
            meta = filing["form_meta"]
            key = (meta["reporter_name"], meta["relationship"])
            if key not in by_insider:
                by_insider[key] = {
                    "reporter_name": meta["reporter_name"],
                    "relationship": meta["relationship"],
                    "total_value": 0.0,
                    "shares": 0.0,
                    "txn_count": 0,
                }
            for txn in filing["purchase_txns"]:
                by_insider[key]["total_value"] += txn["total_value"]
                by_insider[key]["shares"] += txn["shares"]
                by_insider[key]["txn_count"] += 1
        insiders = sorted(by_insider.values(), key=lambda r: r["total_value"], reverse=True)
        daily["tickers"].append({
            "ticker": s["ticker"],
            "name": s["name"],
            "total_value": s["bucket_data"]["total_value"],
            "ev_basic": s["ev_basic"],
            "mc_basic": s["mc_basic"],
            "insiders": insiders,
        })
    daily["tickers"].sort(key=lambda t: t["total_value"], reverse=True)

    daily_path = INSIDERS_DIR / f"{url_date.isoformat()}.json"
    daily_path.write_text(json.dumps(daily, indent=2, default=str))
    _log(f"  wrote {daily_path}")

    # Always refresh company JSON when a ticker survives the screener — guarantees
    # the company page reflects any new Form 4s referenced from the daily page.
    for s in survivors:
        _log(f"    refreshing {s['ticker']} company data...")
        try:
            update_company_data(s["ticker"], s["cik"], s)
        except Exception as e:
            _log(f"    company refresh failed for {s['ticker']}: {e}")

    return daily
