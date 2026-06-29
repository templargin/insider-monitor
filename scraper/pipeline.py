"""End-to-end pipeline: scrape Form 4s for a URL bucket, apply filters,
update per-ticker data, write daily JSON. Heavy lifting orchestrator.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from . import edgar, filters, xbrl_facts, xbrl_statement, financials, xbrl_financials, footnotes, buckets


class DataUnavailable(Exception):
    """A required input for an issuer (companyfacts, share count, or price) could
    not be fetched — distinct from the issuer being screened OUT on its
    EV/revenue merits. Lets process_bucket tell a transient upstream outage apart
    from a genuinely quiet day, so an outage never overwrites a good page with an
    empty one."""

_MAX_WORKERS = 6

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
INSIDERS_DIR = DATA_DIR / "insiders"
COMPANIES_DIR = DATA_DIR / "companies"
FOOTNOTES_DIR = DATA_DIR / "footnotes"
INSIDERS_DIR.mkdir(parents=True, exist_ok=True)
COMPANIES_DIR.mkdir(parents=True, exist_ok=True)
FOOTNOTES_DIR.mkdir(parents=True, exist_ok=True)


def _log(*a):
    print(*a, flush=True)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_daily(url_date):
    """Daily-page payload with no tickers."""
    return {
        "url_date": url_date.isoformat(),
        "weekday": url_date.strftime("%A"),
        "filing_dates": [fd.isoformat() for fd in buckets.filing_dates_for_url(url_date)],
        "generated_at": _now_iso(),
        "tickers": [],
    }


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
    """Return a valuation dict if the issuer PASSES EV/revenue screening, or None
    if it is screened OUT on the merits (EV ≥ cap, or no TTM revenue).

    Raises DataUnavailable when a required input could not be obtained
    (companyfacts/share-price fetch failed, or the filer tags no share count) —
    callers must NOT treat that as a screen-out."""
    try:
        facts = edgar.fetch_companyfacts(cik)
    except requests.RequestException as e:
        raise DataUnavailable(f"companyfacts fetch failed for CIK {cik}: {e}")
    if facts is None:
        raise DataUnavailable(f"no companyfacts for CIK {cik}")
    shares, sh_end = xbrl_facts.get_basic_shares(facts)
    cash, _ = xbrl_facts.get_cash(facts)
    # Structured debt: date-anchored, classified by the us-gaap debt hierarchy,
    # bounded by reported liabilities, with a move-3 uncertainty flag.
    debt, _, debt_flag = xbrl_statement.get_structured_debt(facts)
    ttm_rev, _ = xbrl_facts.get_ttm_revenue(facts)

    if shares is None or shares <= 0:
        # Facts fetched fine but this filer tags no basic share count — a
        # permanent per-filer data gap, not a transient outage. We can't confirm
        # EV < $1B, so screen it out conservatively (counts as evaluated).
        return None
    price = financials.fetch_share_price(ticker)
    if price is None or price <= 0:
        raise DataUnavailable(f"no share price for {ticker}")
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
        "debt_flag": debt_flag,
    }


def update_company_data(ticker, cik, screener_snapshot):
    """Refresh `data/companies/TICKER.json` with full 2y Form 4 history,
    valuation table inputs, and financial statements. screener_snapshot supplies
    pre-fetched facts/price to avoid re-fetching.
    """
    facts = screener_snapshot["facts"]
    options, _ = xbrl_facts.get_options_outstanding(facts)
    warrants, _ = xbrl_facts.get_warrants_outstanding(facts)

    debt = screener_snapshot["debt"]
    debt_flag = screener_snapshot.get("debt_flag")

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

    # CRITICAL: preserve existing options/warrants if XBRL returns None.
    # Those fields are populated by the LLM-extraction routine from filing
    # footnotes — rewriting them as None on every daily refresh would clobber
    # the routine's work for the common case where XBRL doesn't tag them.
    path = COMPANIES_DIR / f"{ticker.upper()}.json"
    existing_options = None
    existing_warrants = None
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            ev = existing.get("valuation", {}) or {}
            existing_options = ev.get("options")
            existing_warrants = ev.get("warrants")
        except Exception:
            pass
    final_options = options if options is not None else existing_options
    final_warrants = warrants if warrants is not None else existing_warrants

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
            "options": final_options,
            "warrants": final_warrants,
            "cash": screener_snapshot["cash"],
            "debt": debt,
            "debt_flag": debt_flag,
            "ttm_revenue": screener_snapshot["ttm_revenue"],
            "mc_basic": screener_snapshot["mc_basic"],
            "ev_basic": screener_snapshot["ev_basic"],
        },
        "financials": fins,
        "last_updated": _now_iso(),
    }
    path.write_text(json.dumps(payload, indent=2, default=str))

    # Pre-fetch footnote text for the LLM-extraction routine to consume.
    # The routine is sandboxed away from sec.gov; we do the network fetch here.
    # Only when the merged value is still null (XBRL didn't have it AND prior
    # extraction routine hasn't filled it yet).
    if final_options is None or final_warrants is None:
        try:
            fn = footnotes.fetch_footnotes(cik, ticker)
            if fn:
                (FOOTNOTES_DIR / f"{ticker.upper()}.txt").write_text(fn)
        except Exception as e:
            _log(f"    footnote fetch failed for {ticker}: {e}")
    return path


def process_bucket(url_date):
    """Process one URL date end-to-end: scrape, filter, write daily + company JSONs.

    Safety on an empty index:
    - If every bucket date is a non-trading day (weekend/federal holiday) there
      will never be filings — write an explicit empty page so the URL 200s
      (e.g. the Monday after a Friday holiday) instead of 404ing.
    - Otherwise a real trading day returned nothing, which means SEC hasn't
      published the daily-index yet (late-evening / early run) — skip writing so
      we don't clobber an existing good page with an empty one.
    """
    _log(f"=== Processing /insiders/{url_date.year}/{buckets.MONTH_NAMES[url_date.month-1]}/{url_date.day} (read on {url_date.strftime('%A')}) ===")
    daily_path = INSIDERS_DIR / f"{url_date.isoformat()}.json"

    def _existing_ticker_count():
        if not daily_path.exists():
            return 0
        try:
            return len(json.loads(daily_path.read_text()).get("tickers", []))
        except Exception:
            return 0

    # Pre-flight: count daily-index rows across all bucket dates
    bucket_fds = buckets.filing_dates_for_url(url_date)
    total_index_rows = 0
    for fd in bucket_fds:
        try:
            total_index_rows += len(edgar.fetch_daily_index_form4s(fd))
        except Exception as e:
            _log(f"  daily-index fetch failed for {fd}: {e}")
    if total_index_rows == 0:
        all_nontrading = all(not buckets.is_trading_day(fd) for fd in bucket_fds)
        if all_nontrading and _existing_ticker_count() == 0:
            _log("  0 index rows; every bucket date is a weekend/holiday — writing explicit empty page.")
            daily = _empty_daily(url_date)
            daily_path.write_text(json.dumps(daily, indent=2, default=str))
            _log(f"  wrote empty {daily_path}")
            return daily
        _log("  EDGAR returned 0 Form 4 index rows but a trading day is pending (or a good page already exists) — skipping write.")
        return None

    parsed = fetch_all_form4s_for_bucket(url_date)
    _log(f"  Parsed {len(parsed)} Form 4 filings total")
    aggregated = filters.aggregate_p_purchases(parsed)
    threshold = [(cik, b) for cik, b in aggregated.items() if filters.passes_threshold(b)]
    _log(f"  {len(threshold)} issuers with ≥1 insider ≥${filters.PURCHASE_THRESHOLD_USD:,}")

    survivors = []
    screened = 0   # issuers we fully evaluated (passed OR merit-failed)
    errored = 0    # issuers we could not evaluate (data unavailable)
    for cik, bucket_data in threshold:
        ticker = bucket_data["ticker"]
        name = bucket_data["name"]
        if not ticker:
            _log(f"  - skip {name} (no ticker on Form 4)")
            continue
        _log(f"  ? probing {name} ({ticker})...")
        try:
            snap = screener_pass(cik, ticker, bucket_data)
        except DataUnavailable as e:
            errored += 1
            _log(f"    data unavailable: {e}")
            continue
        screened += 1
        if snap is None:
            _log(f"    screened out (EV ≥ cap or no TTM revenue)")
            continue
        _log(f"    PASS: EV=${snap['ev_basic']/1e6:,.1f}M  TTM rev=${snap['ttm_revenue']/1e6:,.1f}M")
        snap["name"] = name
        snap["ticker"] = ticker
        snap["cik"] = cik
        snap["bucket_data"] = bucket_data
        survivors.append(snap)

    # Outage guard: candidates existed but we couldn't evaluate a single one.
    # That's an upstream data outage (SEC companyfacts or the share-price source
    # throttling a cloud IP), not a quiet day — bail rather than write an empty
    # page that clobbers a good one. (June 2026: a delayed fallback run hit a
    # mass price-fetch failure and overwrote PRTA + GOTU with an empty list.)
    if threshold and screened == 0:
        _log(f"  Could not evaluate any of {len(threshold)} candidate issuers "
             f"({errored} data-unavailable) — upstream outage; skipping write.")
        return None

    # Persist daily JSON
    daily = _empty_daily(url_date)
    for s in survivors:
        # Per-insider threshold: only insiders who individually crossed
        # $100k show up on the daily page, and the headline `total_value`
        # is the sum across those qualifying insiders (NOT the company-wide
        # raw total — that would mix in sub-threshold buys from other
        # filers and inflate the number).
        qualifying = filters.qualifying_reporters(s["bucket_data"])
        insiders = [
            {
                "reporter_name": r["reporter_name"],
                "relationship": r["relationship"],
                "total_value": r["total_value"],
                "shares": r["shares"],
                "txn_count": r["txn_count"],
            }
            for r in qualifying
        ]
        headline_total = sum(r["total_value"] for r in qualifying)
        daily["tickers"].append({
            "ticker": s["ticker"],
            "name": s["name"],
            "total_value": headline_total,
            "ev_basic": s["ev_basic"],
            "mc_basic": s["mc_basic"],
            "insiders": insiders,
        })
    daily["tickers"].sort(key=lambda t: t["total_value"], reverse=True)

    # Belt-and-suspenders against a partial outage: never downgrade an existing
    # non-empty page to empty on a run where some fetches errored — the empties
    # are far more likely transient than a real same-day reversal.
    if not daily["tickers"] and errored and _existing_ticker_count() > 0:
        _log(f"  0 survivors with {errored} data-unavailable issuer(s), but the "
             f"existing page has {_existing_ticker_count()} ticker(s) — keeping it, skipping write.")
        return None

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
