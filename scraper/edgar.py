"""EDGAR client: Form 4 filings, XBRL companyfacts, submissions, ticker resolution.

SEC requires a descriptive User-Agent and politeness ≤ 10 req/sec.
"""
import re
import threading
import time
import requests
from lxml import etree

ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")

USER_AGENT = "insider-monitor templargin togayevadil@gmail.com"
_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

_session = requests.Session()
_session.headers.update(_HEADERS)
_last_request = 0.0
_MIN_INTERVAL = 0.10  # 10 req/sec SEC ceiling
_rate_lock = threading.Lock()

_MAX_RETRIES = 4
_RETRY_STATUS = {429, 500, 502, 503, 504}  # transient — gateway hiccup

# SEC answers a rate-limited request with **403**, not 429, and uses 403 for a
# genuinely absent index too (a weekend date). The status alone cannot tell them
# apart, so match the throttle body: retrying a weekend 403 four times would turn
# every non-trading day into a 15s stall and then a raise, while NOT retrying a
# throttle 403 drops the filing silently — which is how a real purchase vanishes
# with no counter and no log. `_MIN_INTERVAL` runs at SEC's 10/sec ceiling with
# `_MAX_WORKERS` threads, so this is a live path, not a hypothetical.
_THROTTLE_MARKERS = (
    "request rate threshold exceeded",
    "undeclared automated tool",
    "exceeded the rate limit",
)


def _is_missing(resp):
    """True when a response means 'this genuinely isn't here' — the only condition
    under which a fetch may be turned into None rather than raised.

    404, or a 403 that isn't a throttle (SEC serves 403 for a path that does not
    exist, e.g. a weekend daily-index). Everything else — 5xx that outlived its
    retries, a throttle, a transport error — is a failure to LOOK, not a finding
    of absence, and must reach the caller.
    """
    if resp is None:
        return False
    if resp.status_code == 404:
        return True
    return resp.status_code == 403 and not _is_throttle(resp)


def _is_throttle(resp):
    """True when a response is SEC saying 'slow down' rather than 'not found'."""
    if resp is None:
        return False
    if resp.status_code == 429:
        return True
    if resp.status_code != 403:
        return False
    try:
        body = (resp.text or "")[:2000].lower()
    except Exception:                      # noqa: BLE001 - body unreadable, assume not a throttle
        return False
    return any(m in body for m in _THROTTLE_MARKERS)


def _throttle():
    """Block until at least _MIN_INTERVAL has elapsed since the last request
    start. Lock spans only the bookkeeping so the network call stays concurrent."""
    global _last_request
    with _rate_lock:
        elapsed = time.time() - _last_request
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _last_request = time.time()


def _get(url, timeout=30):
    """Rate-limited HTTP GET with retry/backoff on transient failures.

    Retries on 429/5xx, on a throttle-flavoured 403 (see `_is_throttle`), and on
    connection/timeout errors with exponential backoff (respecting Retry-After),
    so a momentary SEC throttle doesn't surface as a hard failure. Other 4xx —
    including a 403 for an index that genuinely doesn't exist — are raised
    immediately for callers to handle."""
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            resp = _session.get(url, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            time.sleep(_backoff(attempt))
            continue
        if resp.status_code in _RETRY_STATUS or _is_throttle(resp):
            last_exc = requests.HTTPError(f"{resp.status_code} for {url}", response=resp)
            if attempt < _MAX_RETRIES - 1:
                ra = resp.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra else _backoff(attempt)
                except (TypeError, ValueError):
                    delay = _backoff(attempt)
                time.sleep(min(delay, 30))
            continue
        resp.raise_for_status()
        return resp
    # Exhausted retries on a transient error — surface it to the caller.
    raise last_exc


def _backoff(attempt):
    """Exponential backoff: 0.5s, 1s, 2s, 4s ... capped at 8s."""
    return min(0.5 * (2 ** attempt), 8.0)


def quarter_for(d):
    return (d.month - 1) // 3 + 1


def daily_index_url(d):
    return f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{quarter_for(d)}/master.{d.strftime('%Y%m%d')}.idx"


def _parse_master_idx(text):
    """Parse a daily ``master.idx`` body into Form 4 / 4-A filing rows — one row
    per accession.

    EDGAR's daily index lists a filing once for EACH CIK associated with it, and
    a Form 4 always has at least two (the issuer plus every reporting owner). So
    the same accession appears on multiple lines under different CIKs. A filing
    is uniquely identified by its accession number, so we key on that and keep
    the first occurrence — otherwise every Form 4 would be fetched, parsed, and
    aggregated once per associated CIK, inflating every insider's transaction
    count and dollar total (2x in the common single-owner case, 3x for a joint
    filing, etc.). The kept CIK is irrelevant downstream: the fetch URL resolves
    under any associated CIK, and aggregation reads the issuer CIK from the
    parsed XML, not from this row."""
    results = []
    seen = set()
    in_data = False
    for line in text.splitlines():
        if line.startswith("CIK|"):
            in_data = True
            continue
        if not in_data or line.startswith("---") or not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, company, form, date_filed, filename = parts
        if form not in ("4", "4/A"):
            continue
        m = ACCESSION_RE.search(filename)
        if not m:
            continue
        accession_dashed = m.group(0)
        if accession_dashed in seen:
            continue
        seen.add(accession_dashed)
        results.append({
            "cik": cik.lstrip("0") or "0",
            "company_name": company,
            "form": form,
            "date_filed": date_filed,
            "accession": accession_dashed,
            "accession_nodash": accession_dashed.replace("-", ""),
            "filename": filename,
        })
    return results


def fetch_daily_index_form4s(d):
    """Return list of Form 4 / 4/A filings for date d, one row per accession.

    Empty list on 404/403 (weekend, holiday, or future date — SEC returns 403 for
    non-existent indexes). A throttle 403 that survived the retries is NOT that:
    swallowing it would report a busy trading day as having no filings at all."""
    url = daily_index_url(d)
    try:
        resp = _get(url)
    except requests.HTTPError as e:
        if _is_missing(e.response):
            return []
        raise
    # Master idx uses Latin-1 for some special chars in company names
    return _parse_master_idx(resp.content.decode("latin-1"))


def fetch_form4_xml(cik, accession_nodash, xml_name=None):
    """Fetch the primary Form 4 XML. If xml_name provided (from search API), use it
    directly; otherwise look it up via index.json (slower).

    Returns None only when the filing genuinely isn't there (404, or no XML in the
    directory). ANY other transport failure is RAISED: a filing we could not fetch
    is not a filing that does not qualify. Returning None for it drops the
    accession before it reaches the screener, so the issuer never enters
    `threshold` and no outage guard can see it — the same silent-drop shape as the
    NaN price, one layer up.

    Note the guard is "is this a real 404", not "is this a throttle". Keying on the
    throttle let a 5xx that exhausted all four retries in `_get` fall through to
    `return None` and vanish — `_RETRY_STATUS` exists precisely because SEC 5xxs
    happen under load.
    """
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}"
    if xml_name is None:
        try:
            idx = _get(f"{base}/index.json").json()
        except requests.HTTPError as e:
            if _is_missing(e.response):
                return None
            raise
        for item in idx.get("directory", {}).get("item", []):
            name = item.get("name", "")
            if name.endswith(".xml") and "index" not in name.lower() and "filing-summary" not in name.lower():
                xml_name = name
                break
        if not xml_name:
            return None
    try:
        return _get(f"{base}/{xml_name}").content
    except requests.HTTPError as e:
        if _is_missing(e.response):
            return None
        raise


def fetch_form4_index_via_search(start_date, end_date=None):
    """Use EDGAR full-text search to get Form 4 / 4-A filings in a date range,
    pre-resolving the primary XML filename per filing.

    Returns list of dicts: {accession, accession_nodash, xml_name, file_date}.
    Note: this returns the filer (reporting person) CIK, not the issuer — we need
    the XML for the issuer.
    """
    end_date = end_date or start_date
    out = []
    page_from = 0
    seen = set()
    while True:
        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?forms=4,4%2FA"
            f"&dateRange=custom&startdt={start_date}&enddt={end_date}"
            f"&from={page_from}&hits=100"
        )
        resp = _get(url).json()
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            break
        for h in hits:
            _id = h.get("_id", "")
            if ":" not in _id:
                continue
            acc_dashed, xml_name = _id.split(":", 1)
            if acc_dashed in seen:
                continue
            seen.add(acc_dashed)
            ciks = h.get("_source", {}).get("ciks", [])
            file_date = h.get("_source", {}).get("file_date", "")
            out.append({
                "accession": acc_dashed,
                "accession_nodash": acc_dashed.replace("-", ""),
                "xml_name": xml_name,
                "file_date": file_date,
                "filer_cik": ciks[0].lstrip("0") if ciks else "",
            })
        total = resp.get("hits", {}).get("total", {}).get("value", 0)
        page_from += len(hits)
        if page_from >= total:
            break
    return out


def _amount(txn, path, text_at, code, ticker):
    """Numeric transaction amount; 0.0 when absent, 0.0 + a shout when malformed.

    Absent is ordinary (a gift or award carries no price). Malformed is not, and
    it is indistinguishable downstream: both become 0.0 and both get dropped by
    the `price > 0` filter. Only the malformed case is a lost purchase.
    """
    raw = text_at(txn, path)
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        print(f"  ! unparseable {path.rsplit('/', 1)[-1]}={raw!r} on a code-{code} "
              f"transaction for {ticker or '?'} — dropped from the screen", flush=True)
        return 0.0


def parse_form4(xml_bytes):
    """Parse Form 4 XML. Returns dict or None on parse failure."""
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return None

    def text_at(elem, path):
        if elem is None:
            return ""
        node = elem.find(path)
        if node is None:
            return ""
        val = node.find("value")
        if val is not None:
            return (val.text or "").strip()
        return (node.text or "").strip()

    issuer = root.find("issuer")
    if issuer is None:
        return None
    issuer_cik = (issuer.findtext("issuerCik") or "").strip().lstrip("0") or "0"
    issuer_name = (issuer.findtext("issuerName") or "").strip()
    issuer_ticker = (issuer.findtext("issuerTradingSymbol") or "").strip()

    owner = root.find("reportingOwner")
    rpt_name = ""
    rpt_cik = ""
    relationship_parts = []
    title = ""
    if owner is not None:
        rpt_name = (owner.findtext("reportingOwnerId/rptOwnerName") or "").strip()
        rpt_cik = (owner.findtext("reportingOwnerId/rptOwnerCik") or "").strip().lstrip("0") or "0"
        rel = owner.find("reportingOwnerRelationship")
        if rel is not None:
            def is_true(tag):
                v = (rel.findtext(tag) or "").strip().lower()
                return v in ("1", "true")
            if is_true("isDirector"):
                relationship_parts.append("Director")
            if is_true("isOfficer"):
                title = (rel.findtext("officerTitle") or "").strip()
                relationship_parts.append(f"Officer ({title})" if title else "Officer")
            if is_true("isTenPercentOwner"):
                relationship_parts.append("10% Owner")
            if is_true("isOther"):
                other_text = (rel.findtext("otherText") or "").strip()
                relationship_parts.append(f"Other ({other_text})" if other_text else "Other")
    relationship = ", ".join(relationship_parts)

    transactions = []
    for table_name, xpath in [("nonDerivative", ".//nonDerivativeTransaction"),
                              ("derivative", ".//derivativeTransaction")]:
        for txn in root.findall(xpath):
            code = text_at(txn, "transactionCoding/transactionCode")
            if not code:
                continue
            # An unparseable amount becomes 0.0, and filters.aggregate_p_purchases
            # drops a code-P row on `shares > 0 and price > 0` — so a real
            # open-market purchase would vanish, taking its reporter below the
            # $100k line and the issuer out of the screen entirely, with nothing
            # logged. Measured against the 2026-07-14 bucket: 0 of 83 code-P
            # transactions were unparseable, so this is a latent door rather than
            # an active leak, and a shout is the whole fix. If it ever fires it
            # must not be silent.
            shares = _amount(txn, "transactionAmounts/transactionShares",
                             text_at, code, issuer_ticker)
            price = _amount(txn, "transactionAmounts/transactionPricePerShare",
                            text_at, code, issuer_ticker)
            ad = text_at(txn, "transactionAmounts/transactionAcquiredDisposedCode")
            signed_shares = shares if ad == "A" else -shares if ad == "D" else shares
            transactions.append({
                "table": table_name,
                "security_title": text_at(txn, "securityTitle"),
                "transaction_date": text_at(txn, "transactionDate"),
                "code": code,
                "shares": signed_shares,
                "price": price,
                "total_value": signed_shares * price,
                "ownership": text_at(txn, "ownershipNature/directOrIndirectOwnership"),
                "ad_code": ad,
            })

    return {
        "issuer_cik": issuer_cik,
        "issuer_name": issuer_name,
        "issuer_ticker": issuer_ticker,
        "reporter_name": rpt_name,
        "reporter_cik": rpt_cik,
        "relationship": relationship,
        "period_of_report": (root.findtext("periodOfReport") or "").strip(),
        "transactions": transactions,
    }


def fetch_companyfacts(cik):
    """Fetch companyfacts XBRL JSON for a CIK. Returns dict or None on 404."""
    padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{padded}.json"
    try:
        return _get(url).json()
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return None
        raise


def fetch_submissions(cik):
    """Fetch submissions JSON for a CIK (filing index)."""
    padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    return _get(url).json()


_ticker_to_cik_cache = None


def ticker_to_cik(ticker):
    """Resolve ticker → CIK using SEC's company_tickers.json. Cached in-process."""
    global _ticker_to_cik_cache
    if _ticker_to_cik_cache is None:
        data = _get("https://www.sec.gov/files/company_tickers.json").json()
        _ticker_to_cik_cache = {
            entry["ticker"].upper(): str(entry["cik_str"])
            for entry in data.values()
        }
    return _ticker_to_cik_cache.get(ticker.upper())


def cik_to_ticker(cik):
    """Reverse lookup: CIK → ticker via SEC's company_tickers.json. Returns None if not listed."""
    global _ticker_to_cik_cache
    if _ticker_to_cik_cache is None:
        ticker_to_cik("AAPL")  # warm cache
    target = str(cik).lstrip("0")
    for tk, ck in _ticker_to_cik_cache.items():
        if ck.lstrip("0") == target:
            return tk
    return None
