"""End-to-end pipeline: scrape Form 4s for a URL bucket, apply filters,
update per-ticker data, write daily JSON. Heavy lifting orchestrator.
"""
import json
import math
import sys
import time
from collections import Counter
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
    empty one.

    `transient` says WHICH of the two this instance is, and only the transient
    ones arm the outage guard. An unreachable upstream (a 429 from companyfacts,
    a throttled price source) is transient: retrying tomorrow gets a different
    answer. A closed-end fund with no companyfacts at all, an IFRS filer with no
    us-gaap balance sheet, a Form 4 whose "ticker" is the literal string NONE —
    those return the same answer every day, forever.

    Conflating them is what blanked 2026-07-30: four candidates, all four
    permanently unevaluable, read as "upstream is down" and skipped the write.
    The correct page there was an empty one. Defaults to False so a new raise
    site has to opt IN to suppressing a day's page."""

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


# Some Form 4s carry a placeholder where the issuer's trading symbol goes —
# non-traded BDCs and interval funds file with "NONE" or "N/A". That is not a
# ticker, so it can never be priced; treating its price failure as an outage
# armed the guard on an issuer that was never evaluable to begin with.
#
# Bracketed forms are here because "[NONE]" is not "NONE": it slipped the set,
# reached the screener as if it were a symbol, and came back as a share-count
# failure — a placeholder wearing the costume of a data problem.
_NON_TICKERS = {"NONE", "N/A", "NA", "N.A.", "NULL", "NIL", "", "-", "--"}


def _is_real_ticker(ticker):
    return bool(ticker) and (ticker or "").strip().upper().strip("[](){}<>.") not in _NON_TICKERS


# Why an issuer could not be screened, when the answer is "the screen does not
# apply to it" rather than "we failed to read something". These are silent: they
# are not on the daily page, because a reader cannot act on them and their number
# is the same tomorrow. The counts still reach the run log.
EXCLUSION_REASONS = {
    edgar.SCOPE_FUND: "registered fund / non-traded vehicle — files no XBRL financial statements",
    edgar.SCOPE_FOREIGN: "foreign private issuer (20-F/IFRS) — no us-gaap statements to screen",
    edgar.SCOPE_PRE_REPORT: "no periodic report filed yet (recent IPO or SPAC)",
    "not_listed": "no trading symbol — private or non-traded issuer",
}


def _share_count_unusable(shares, sh_end, as_of):
    """True when companyfacts' share count cannot carry a market cap: absent,
    non-positive, undated, or from a reporting cycle before the balance sheet."""
    if shares is None or shares <= 0:
        return True
    return sh_end is None or _days_before(sh_end, as_of) > _SHARES_STALE_DAYS


def classify_unscreenable(cik, exc):
    """Scope category for an issuer `screener_pass` could not evaluate, or None
    when it is a genuine unknown that deserves a retry.

    The split this whole module turns on. A closed-end fund publishing no XBRL, a
    20-F filer with no us-gaap statements, a company whose first 10-Q has not
    posted — the screen does not apply to any of them, and reporting them as
    "could not be evaluated" told the reader to go check 122 issuers by hand over
    22 days, 60% of which were never checkable by anyone.

    A transient failure is never a scope answer: an upstream we could not reach
    says nothing about what the issuer files. Nor is a failure to answer the scope
    question itself — that returns None too, so the issuer is retried rather than
    quietly written off."""
    if getattr(exc, "transient", False):
        return None
    try:
        scope = edgar.filer_scope(cik)
    except requests.RequestException:
        return None
    return None if scope == edgar.SCOPE_DOMESTIC else scope

_MAX_WORKERS = 6

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
INSIDERS_DIR = DATA_DIR / "insiders"
COMPANIES_DIR = DATA_DIR / "companies"
FOOTNOTES_DIR = DATA_DIR / "footnotes"
# Issuers that reached no verdict, waiting to be asked again. Not a log — a work
# queue: `retry_unresolved` empties it, and what it cannot empty it flags.
UNRESOLVED_PATH = DATA_DIR / "unresolved.json"
# Days an issuer gets retried before it stops being a retry and starts being a
# question for a person. Counted in days, not runs — see retry_unresolved.
MAX_RETRY_ATTEMPTS = 3
INSIDERS_DIR.mkdir(parents=True, exist_ok=True)
COMPANIES_DIR.mkdir(parents=True, exist_ok=True)
FOOTNOTES_DIR.mkdir(parents=True, exist_ok=True)


# A share count this much older than the balance sheet is from a prior reporting
# cycle and cannot describe the same company (BETA carried a pre-IPO count 194
# days stale; FONR one 2,775 days stale). Inside one cycle the drift is immaterial
# to a $1B size test, and rejecting it would be a false negative.
_SHARES_STALE_DAYS = 90


def _log(*a):
    print(*a, flush=True)


def _days_before(earlier, later):
    """How many days `earlier` precedes `later`; 0 when it is the same or newer.
    Both are ISO date strings; an unparseable one counts as maximally stale."""
    try:
        d = (date.fromisoformat(later) - date.fromisoformat(earlier)).days
    except (ValueError, TypeError):
        return 10**6
    return max(d, 0)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_iso():
    return datetime.now(timezone.utc).date().isoformat()


def _empty_daily(url_date):
    """Daily-page payload with no tickers.

    `excluded` and `unresolved` record what happened to every candidate that did
    not reach a verdict, so an empty `tickers` list is never mistaken for "no
    issuer qualified". Neither is rendered — the page is the answer, not the
    working — but both are kept on disk, and `unresolved` is what the retry queue
    is built from. (They replace the old single `unevaluated` list, which put both
    populations on the page and made the daily list unreadable: 122 entries against
    56 issuers shown over the 22 days it ran.)
    """
    return {
        "url_date": url_date.isoformat(),
        "weekday": url_date.strftime("%A"),
        "filing_dates": [fd.isoformat() for fd in buckets.filing_dates_for_url(url_date)],
        "generated_at": _now_iso(),
        "tickers": [],
        "excluded": [],
        "unresolved": [],
    }


def _daily_row(snap, bucket_data):
    """One daily-page row for a passing issuer.

    Per-insider threshold: only insiders who individually crossed $100k show up,
    and the headline `total_value` is the sum across those qualifying insiders
    (NOT the company-wide raw total — that would mix in sub-threshold buys from
    other filers and inflate the number).

    Shared with the retry queue so a recovered issuer lands on its page in exactly
    the shape the original run would have written."""
    qualifying = filters.qualifying_reporters(bucket_data)
    return {
        "ticker": snap["ticker"],
        "name": snap["name"],
        "total_value": sum(r["total_value"] for r in qualifying),
        "ev_basic": snap["ev_basic"],
        "mc_basic": snap["mc_basic"],
        "insiders": [
            {
                "reporter_name": r["reporter_name"],
                "relationship": r["relationship"],
                "total_value": r["total_value"],
                "shares": r["shares"],
                "txn_count": r["txn_count"],
            }
            for r in qualifying
        ],
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
    """Screen one issuer.

    Returns `(measurement, reason)`. `reason` is None on a PASS and carries the
    merits rejection otherwise (size ≥ cap, or no revenue) — so `reason is None`,
    not `measurement is None`, is the test for a pass. The measurement is returned
    either way: a rejected company still has a page, and it should show correct
    figures rather than whatever stale ones predate the rejection.

    The reason is returned rather than logged so each caller reports it its own
    way — the daily run to the console, the re-screen onto the company page — and
    so this stays a pure function of its inputs.

    Raises DataUnavailable when any required input cannot be established. Callers
    must NOT treat that as a screen-out — an unknown allowed to masquerade as a
    merits rejection is exactly how the 2026-07-15 page silently lost BUKS.

    This is the validation boundary for the inputs the screen turns on — the
    anchor, the share count and its freshness, the price, and the finiteness of
    the resulting EV. It is NOT a proof that every field is sound: `debt` and
    `cash` are checked only through `basic_ev`'s finiteness test, and `cash` of
    None is known to be weaker than it looks (see filters.basic_ev).
    """
    try:
        facts = edgar.fetch_companyfacts(cik)
    except requests.RequestException as e:
        raise DataUnavailable(f"companyfacts fetch failed for CIK {cik}: {e}",
                              transient=True)
    if facts is None:
        raise DataUnavailable(f"no companyfacts for CIK {cik}")

    # Anchor first. Without a us-gaap balance sheet we cannot read debt or cash at
    # a known date — an IFRS/20-F filer (GLBS), a non-USD reporter, or a filer
    # tagging no balance-sheet subtotal. Screening those on EV would silently read
    # unknown debt as zero and admit a leveraged company.
    as_of = xbrl_facts.balance_sheet_date(facts)
    if as_of is None:
        raise DataUnavailable(f"no us-gaap balance sheet for {ticker}")

    shares, sh_end = xbrl_facts.get_basic_shares(facts)
    if _share_count_unusable(shares, sh_end, as_of):
        # Not "this company has no share count" — "this API does not carry it".
        # companyfacts drops every dimensional fact, and a multi-class filer tags
        # its cover-page count once per class, so what survives is nothing at all
        # (SUJA) or a stale leftover from before the classes split (OPFI's 2025
        # count, PLNT's from 2015). Read the cover page itself before concluding
        # that we do not know. See edgar.cover_page_shares.
        try:
            cover, cover_end = edgar.cover_page_shares(cik)
        except requests.RequestException as e:
            raise DataUnavailable(
                f"cover page unreadable for {ticker} "
                f"(companyfacts carries no usable share count): {e}", transient=True)
        if cover is not None:
            had = "none" if shares is None else f"{shares:,.0f} as of {sh_end}"
            _log(f"    share count from cover page: {cover:,.0f} as of {cover_end} "
                 f"(companyfacts had {had})")
            shares, sh_end = cover, cover_end
    if shares is None or shares <= 0:
        raise DataUnavailable(f"no basic share count for {ticker}")
    if sh_end is None or _days_before(sh_end, as_of) > _SHARES_STALE_DAYS:
        # A cover-page count is legitimately FRESHER than the balance sheet; one
        # that is OLDER predates it and cannot describe the same company (BETA
        # carried a pre-IPO count, FONR one from 2018). But refusing a count merely
        # days older would be a false negative of exactly the kind this boundary
        # exists to prevent — CTNT's lagged by 12 days, which cannot move a $1B
        # test. One filing cycle is the tolerance.
        raise DataUnavailable(
            f"share count for {ticker} predates its balance sheet ({sh_end} < {as_of})")

    cash, _ = xbrl_facts.get_cash(facts, as_of)
    # Structured debt: date-anchored, classified by the us-gaap debt hierarchy,
    # bounded by reported liabilities, with a move-3 uncertainty flag.
    debt, _, debt_flag = xbrl_statement.get_structured_debt(facts)

    price = financials.fetch_share_price(ticker)
    if price is None or not math.isfinite(price) or price <= 0:
        # NaN arrives whenever Yahoo serves a null bar for a thin name. It is
        # neither None nor <= 0, so it has to be rejected explicitly or it poisons
        # EV and reads as "too big".
        # Transient only for a real symbol — that failure mode is Yahoo throttling
        # a cloud IP, which is exactly what the guard exists to catch. A
        # placeholder symbol fails permanently and must not arm it.
        raise DataUnavailable(f"no share price for {ticker}",
                              transient=_is_real_ticker(ticker))

    mc_basic = price * shares
    ev = filters.basic_ev(mc_basic, debt, cash)

    # Revenue off the canonical grid — the same builder the company page renders,
    # so the screen and the page can never quote different numbers for one filer.
    fins = xbrl_financials.fetch_xbrl_financials(cik, facts=facts)
    if fins is None:
        raise DataUnavailable(f"no financial statements for {ticker}")
    ttm_rev, rev_end = xbrl_financials.ltm_revenue(fins)

    # Measure first, judge second. Short-circuiting the cap test before reading
    # revenue would save one fetch on the handful of over-cap issuers a day, at
    # the cost of returning a half-measured company — which is how a rejected
    # CUBI kept publishing a $43M revenue beside its own $1.51B income statement.
    # The caller gets the full measurement whichever way the verdict goes.
    snap = {
        "facts": facts,
        "fins": fins,
        "shares": shares,
        "shares_as_of": sh_end,
        "cash": cash,
        "debt": debt,
        "ttm_revenue": ttm_rev,
        "ttm_revenue_as_of": rev_end,
        "share_price": price,
        "mc_basic": mc_basic,
        "ev_basic": ev,
        "debt_flag": debt_flag,
    }

    # A deposit-funded bank's liabilities are its customers' deposits, not
    # borrowings, so EV is not a size measure for it — get_structured_debt says so
    # in as many words when it raises `financial_institution`. Market cap is.
    is_bank = bool(debt_flag) and debt_flag.get("reason") == "financial_institution"
    size = mc_basic if is_bank else ev
    if not filters.passes_ev_cap(size):
        return snap, (f"{'MC' if is_bank else 'EV'}=${size/1e6:,.1f}M "
                      f"≥ ${filters.EV_CAP_USD/1e6:,.0f}M cap")

    # get_structured_debt reconciles liabilities it cannot classify and reports the
    # residual rather than plugging it — "could be debt under the filer's custom
    # namespace". Where that residual would carry EV over the ceiling we cannot
    # confirm the criterion, so we must not assert it: STRZ published at EV $869M
    # with $361M unexplained, which is $1,230M if those liabilities are borrowings.
    # Only bites when the uncertainty actually spans the cap; a flagged filer
    # comfortably below it is unaffected.
    # Key on the REASON, not on `amount` being truthy. `debt_tags_overlap_clamped`
    # also carries an amount — but that one is debt the extractor REMOVED as
    # double-counted, so adding it back as possible hidden debt asserts the exact
    # opposite of what the clamp established (and can exceed reported liabilities,
    # which the clamp guarantees it cannot). Only `unexplained_liabilities` means
    # "we could not classify this, and it might be debt".
    if not is_bank and debt_flag and debt_flag.get("reason") == "unexplained_liabilities" \
            and debt_flag.get("amount"):
        upper = ev + debt_flag["amount"]
        if not filters.passes_ev_cap(upper):
            raise DataUnavailable(
                f"cannot confirm EV < ${filters.EV_CAP_USD/1e6:,.0f}M for {ticker}: "
                f"EV=${ev/1e6:,.1f}M with ${debt_flag['amount']/1e6:,.1f}M of "
                f"{debt_flag['reason']} (up to ${upper/1e6:,.1f}M)")
    if not filters.passes_revenue(ttm_rev):
        return snap, f"TTM revenue = ${ttm_rev:,.0f}"

    return snap, None


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

    profile = financials.fetch_profile(ticker)
    description = profile["description"]
    ownership = profile["ownership"]
    # XBRL-primary: skip the yfinance financial-statement scrape entirely. The
    # screener already built this grid to read revenue off it, so reuse it rather
    # than refetch companyfacts and rebuild.
    fins = screener_snapshot.get("fins") or xbrl_financials.fetch_xbrl_financials(cik, facts=facts)

    # CRITICAL: preserve existing options/warrants if XBRL returns None.
    # Those fields are populated by the LLM-extraction routine from filing
    # footnotes — rewriting them as None on every daily refresh would clobber
    # the routine's work for the common case where XBRL doesn't tag them.
    # Same preserve-on-failure rule for the yfinance-sourced description and
    # ownership block: a Yahoo throttle (common on cloud-IP runs) must never
    # blank out data a previous run fetched successfully.
    path = COMPANIES_DIR / f"{ticker.upper()}.json"
    existing_options = None
    existing_warrants = None
    existing_description = ""
    existing_ownership = None
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            ev = existing.get("valuation", {}) or {}
            existing_options = ev.get("options")
            existing_warrants = ev.get("warrants")
            existing_description = existing.get("description") or ""
            existing_ownership = existing.get("ownership")
        except Exception:
            pass
    final_options = options if options is not None else existing_options
    final_warrants = warrants if warrants is not None else existing_warrants
    final_description = description or existing_description
    final_ownership = ownership if ownership is not None else existing_ownership

    payload = {
        "ticker": ticker.upper(),
        "cik": str(cik),
        "name": screener_snapshot.get("name") or subs.get("name", ""),
        "description": final_description,
        "ownership": final_ownership,
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


def _queue_load():
    if not UNRESOLVED_PATH.exists():
        return []
    try:
        return json.loads(UNRESOLVED_PATH.read_text()).get("entries", [])
    except (ValueError, OSError) as e:
        _log(f"  unresolved queue unreadable ({e}) — starting a new one")
        return []


def _queue_save(entries):
    UNRESOLVED_PATH.write_text(json.dumps(
        {"generated_at": _now_iso(), "entries": entries}, indent=2, default=str))


def queue_unresolved(url_date, unresolved):
    """Record issuers that reached no verdict, for tomorrow's run to retry.

    Keyed by (url_date, cik) so a re-run of the same day updates its entry rather
    than queueing it twice, and `attempts` survives that update — a re-run is not
    a fresh start for an issuer that has already spent two of its three days."""
    entries = {f"{e['url_date']}:{e['cik']}": e for e in _queue_load()}
    for u in unresolved:
        key = f"{url_date.isoformat()}:{u['cik']}"
        prior = entries.get(key, {})
        entries[key] = {
            "url_date": url_date.isoformat(),
            "cik": u["cik"],
            "ticker": u["ticker"],
            "name": u["name"],
            "reason": u["reason"],
            "transient": u["transient"],
            "bucket_data": u["bucket_data"],
            "first_seen": prior.get("first_seen", _now_iso()),
            "attempts": prior.get("attempts", 0),
            "abandoned": prior.get("abandoned", False),
        }
    _queue_save(list(entries.values()))
    _log(f"  queued {len(unresolved)} unresolved issuer(s) → {UNRESOLVED_PATH}")


def _add_to_daily(url_date_iso, snap, bucket_data):
    """Put a recovered issuer onto the page it belongs to.

    `added` | `present` | `missing` — the three are not the same thing, and the
    caller has to tell them apart: a page that does not exist has not carried this
    issuer, so the recovery would be silently lost if that read as success."""
    path = INSIDERS_DIR / f"{url_date_iso}.json"
    if not path.exists():
        return "missing"
    daily = json.loads(path.read_text())
    if any(t["ticker"] == snap["ticker"] for t in daily.get("tickers", [])):
        return "present"
    daily["tickers"].append(_daily_row(snap, bucket_data))
    daily["tickers"].sort(key=lambda t: t["total_value"], reverse=True)
    daily["unresolved"] = [u for u in daily.get("unresolved", [])
                           if u.get("cik") != snap["cik"]]
    path.write_text(json.dumps(daily, indent=2, default=str))
    return "added"


def retry_unresolved(max_attempts=MAX_RETRY_ATTEMPTS):
    """Re-screen every queued unknown; put the ones that now resolve onto their page.

    The half of the promise that removing the unevaluated block would otherwise
    break. Nothing is dropped silently: an issuer that now passes is written back
    into its own day's page (the site rebuild that follows republishes it), one
    that now fails on merits leaves the queue as a decided question, and one that
    is still unreadable after `max_attempts` mornings is flagged for review rather
    than retried forever.

    Returns counts for the run summary."""
    entries = _queue_load()
    if not entries:
        return {"retried": 0, "recovered": 0, "rejected": 0, "still_open": 0, "abandoned": 0}

    keep, counts = [], {"retried": 0, "recovered": 0, "rejected": 0,
                        "still_open": 0, "abandoned": 0}
    _log(f"=== Retrying {len([e for e in entries if not e.get('abandoned')])} unresolved issuer(s) ===")
    for entry in entries:
        if entry.get("abandoned"):
            keep.append(entry)
            continue
        counts["retried"] += 1
        ticker, cik = entry.get("ticker"), entry.get("cik")
        bucket_data = entry.get("bucket_data") or {}
        label = (f"{entry.get('name', '?')} ({ticker or 'no symbol'}) "
                 f"from {entry.get('url_date', 'an unrecorded day')}")
        if not ticker and cik:
            # Queued because the ticker lookup itself failed — so retrying means
            # asking that question again, not giving up for want of an answer.
            try:
                ticker = entry["ticker"] = edgar.cik_to_ticker(cik)
            except requests.RequestException:
                ticker = None
        # A queue entry missing its inputs is a bug in whatever wrote it. Flag it
        # rather than raise: one malformed record must not cost the whole sweep,
        # and behind it may be a real qualifying purchase.
        if not ticker or not cik or not entry.get("url_date") or not bucket_data.get("by_reporter"):
            # Nothing to re-screen with. Keep it visible rather than pretend.
            entry["abandoned"] = True
            entry["last_reason"] = "queue entry is missing what it takes to re-screen"
            counts["abandoned"] += 1
            _log(f"  REVIEW NEEDED: {label} — {entry['last_reason']}")
            keep.append(entry)
            continue
        try:
            snap, reason = screener_pass(cik, ticker, bucket_data)
        except Exception as e:                # noqa: BLE001
            # Attempts count DAYS, not runs. Two runs cover each weekday (the
            # droplet's and the GH Actions fallback, hours apart) and both should
            # get a shot at a source that was throttled earlier — but an issuer
            # should not burn its whole allowance before lunch.
            if entry.get("last_tried_date") != _today_iso():
                entry["attempts"] += 1
            entry["last_tried_date"] = _today_iso()
            entry["last_reason"] = str(e)
            entry["last_tried"] = _now_iso()
            if entry["attempts"] >= max_attempts:
                entry["abandoned"] = True
                counts["abandoned"] += 1
                _log(f"  REVIEW NEEDED: {label} — still unreadable after "
                     f"{entry['attempts']} days: {e}")
            else:
                counts["still_open"] += 1
                _log(f"  still unresolved (day {entry['attempts']}/{max_attempts}): {label} — {e}")
            keep.append(entry)
            continue
        if reason is not None:
            counts["rejected"] += 1
            _log(f"  resolved: {label} — screened out on merits ({reason})")
            continue
        snap.update({"name": entry["name"], "ticker": ticker, "cik": cik})
        where = _add_to_daily(entry["url_date"], snap, bucket_data)
        if where == "missing":
            # The day has no page yet (its write was skipped). Nothing was learned
            # about the issuer, so this does not spend one of its days — the
            # catch-up sweep rebuilds that page first, and this lands next run.
            _log(f"  resolved but its page is not written yet: {label} — staying queued")
            counts["still_open"] += 1
            keep.append(entry)
            continue
        counts["recovered"] += 1
        _log(f"  RECOVERED: {label} — EV=${snap['ev_basic']/1e6:,.1f}M, "
             f"TTM rev=${snap['ttm_revenue']/1e6:,.1f}M"
             + ("" if where == "added" else " (page already carried it)"))
        try:
            update_company_data(ticker, cik, snap)
        except Exception as e:                # noqa: BLE001
            _log(f"    company refresh failed for {ticker}: {e}")
    _queue_save(keep)
    return counts


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
    index_incomplete = False
    for fd in bucket_fds:
        try:
            total_index_rows += len(edgar.fetch_daily_index_form4s(fd))
        except Exception as e:
            # A blanket except here swallowed the very throttle edgar.py raises,
            # counting a busy trading day as zero rows. Remember it: an index we
            # failed to READ is not an index with nothing in it, and the row count
            # it produces is not comparable to what the second pass parses.
            index_incomplete = True
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

    try:
        parsed = fetch_all_form4s_for_bucket(url_date)
    except requests.RequestException as e:
        # A filing we could not fetch is not a filing that does not qualify. Half a
        # bucket cannot be screened honestly — the missing accession may be the one
        # qualifying purchase, and it would never reach `threshold` for any guard to
        # notice. Publish nothing rather than a page that looks complete.
        _log(f"  bucket fetch failed ({e}) — skipping write rather than screen a partial bucket.")
        return None

    # Both numbers were always printed; nothing ever compared them. A gap is
    # filings fetched but not parseable — a permanent per-filing data problem
    # rather than an outage, but it must be visible, not left for someone to spot
    # by eye across two log lines.
    #
    # Only comparable when the pre-flight actually read every index: the count
    # comes from that first pass and `parsed` from a second, independent one, so a
    # failed pre-flight made this negative ("-456 unreadable").
    if index_incomplete:
        _log(f"  Parsed {len(parsed)} Form 4 filings (index count unavailable — "
             f"a daily-index fetch failed, so the totals aren't comparable)")
    else:
        unparsed = total_index_rows - len(parsed)
        _log(f"  Parsed {len(parsed)} of {total_index_rows} Form 4 filings"
             + (f"  ({unparsed} unreadable)" if unparsed > 0 else ""))
    aggregated = filters.aggregate_p_purchases(parsed)
    threshold = [(cik, b) for cik, b in aggregated.items() if filters.passes_threshold(b)]
    _log(f"  {len(threshold)} issuers with ≥1 insider ≥${filters.PURCHASE_THRESHOLD_USD:,}")

    survivors = []
    excluded = []      # issuers the screen does not apply to — silent, counted
    unresolved = []    # genuine unknowns — off the page, into the retry queue
    screened = 0   # issuers we fully evaluated (passed OR merit-failed)
    errored = 0    # issuers we could not evaluate (data unavailable)
    transient = 0  # ...of which look like an upstream outage, not a permanent gap

    def _exclude(ticker, name, category):
        _log(f"  - out of scope: {name} ({ticker or 'no symbol'}) — {EXCLUSION_REASONS[category]}")
        excluded.append({"ticker": ticker, "name": name, "category": category,
                         "reason": EXCLUSION_REASONS[category]})

    def _defer(ticker, name, cik, bucket_data, reason, is_transient):
        nonlocal errored, transient
        errored += 1
        transient += 1 if is_transient else 0
        _log(f"    unresolved{' (transient)' if is_transient else ''}: {reason} — queued for retry")
        unresolved.append({"ticker": ticker, "name": name, "cik": cik,
                           "reason": reason, "transient": bool(is_transient),
                           "bucket_data": bucket_data})

    for cik, bucket_data in threshold:
        ticker = bucket_data["ticker"]
        name = bucket_data["name"]
        if not _is_real_ticker(ticker):
            # The symbol field is the filer's to fill in and plenty leave it blank
            # or write "NONE" — but SEC's own ticker file knows the answer from the
            # CIK, and Wilson Bank Holding (a real, listed, screenable bank) was
            # dropped for exactly this. Ask before writing the issuer off.
            shown = f" (ticker field: {ticker!r})" if ticker else ""
            try:
                resolved = edgar.cik_to_ticker(cik)
            except requests.RequestException as e:
                _defer(None, name, cik, bucket_data,
                       f"ticker lookup failed for CIK {cik}: {e}", True)
                continue
            if not resolved:
                _log(f"  - skip {name} (no ticker on Form 4){shown}")
                _exclude(None, name, "not_listed")
                continue
            _log(f"  ~ {name}: no ticker on Form 4{shown} — resolved CIK {cik} → {resolved}")
            ticker = resolved
        _log(f"  ? probing {name} ({ticker})...")
        try:
            snap, reason = screener_pass(cik, ticker, bucket_data)
        except DataUnavailable as e:
            category = classify_unscreenable(cik, e)
            if category is not None:
                _exclude(ticker, name, category)
                continue
            _defer(ticker, name, cik, bucket_data, str(e), e.transient)
            continue
        except Exception as e:              # noqa: BLE001
            # The filters raise ValueError when handed an unknown, by design. If
            # one ever escapes screener_pass it means the boundary has a hole —
            # but that should cost this issuer, not the whole day's page. Count it
            # as unresolved (which keeps the outage guards armed) and carry on.
            _defer(ticker, name, cik, bucket_data,
                   f"screening error: {type(e).__name__}: {e}",
                   True)   # a hole in the boundary, not a fact about the issuer
            continue
        screened += 1
        if reason is not None:
            _log(f"    screened out: {reason}")
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
    #
    # Gated on `transient`, not on `errored`: a day whose whole candidate pool is
    # out of scope (closed-end funds, IFRS filers, placeholder tickers) is a quiet
    # day, and its page is a real empty page. Only an upstream we could not reach
    # justifies publishing nothing.
    if threshold and screened == 0 and transient:
        _log(f"  Could not evaluate any of {len(threshold)} candidate issuers "
             f"({errored} unresolved, {transient} transient) — "
             f"upstream outage; skipping write.")
        return None
    if threshold and screened == 0:
        _log(f"  None of {len(threshold)} candidate issuers reached a verdict "
             f"({len(excluded)} out of scope, {errored} unresolved) — writing empty page.")

    by_category = Counter(e["category"] for e in excluded)
    _log(f"  screened {screened} of {len(threshold)} candidates: "
         f"{len(survivors)} passed, {screened - len(survivors)} rejected on merits, "
         f"{len(excluded)} out of scope"
         + (f" ({', '.join(f'{c} {n}' for c, n in sorted(by_category.items()))})" if by_category else "")
         + f", {len(unresolved)} unresolved")

    # Persist daily JSON
    daily = _empty_daily(url_date)
    daily["excluded"] = excluded
    daily["unresolved"] = [{k: v for k, v in u.items() if k != "bucket_data"}
                           for u in unresolved]
    for s in survivors:
        daily["tickers"].append(_daily_row(s, s["bucket_data"]))
    daily["tickers"].sort(key=lambda t: t["total_value"], reverse=True)

    # Belt-and-suspenders against a partial outage: never downgrade an existing
    # non-empty page to empty on a run where some fetches errored — the empties
    # are far more likely transient than a real same-day reversal.
    if not daily["tickers"] and transient and _existing_ticker_count() > 0:
        _log(f"  0 survivors with {transient} transient data-unavailable issuer(s), but the "
             f"existing page has {_existing_ticker_count()} ticker(s) — keeping it, skipping write.")
        return None

    daily_path.write_text(json.dumps(daily, indent=2, default=str))
    _log(f"  wrote {daily_path}")

    # An unresolved issuer is not a published outcome — it is a question still
    # open, and the queue is what makes tomorrow's run ask it again.
    if unresolved:
        queue_unresolved(url_date, unresolved)

    # Always refresh company JSON when a ticker survives the screener — guarantees
    # the company page reflects any new Form 4s referenced from the daily page.
    for s in survivors:
        _log(f"    refreshing {s['ticker']} company data...")
        try:
            update_company_data(s["ticker"], s["cik"], s)
        except Exception as e:
            _log(f"    company refresh failed for {s['ticker']}: {e}")

    return daily
