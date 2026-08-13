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


# ---- filer scope ---------------------------------------------------------------

# Which reporting regime an issuer files under. Only the domestic one produces the
# us-gaap financial statements this screener measures; the other three are not
# failures to extract but issuers the screen does not apply to.
DOMESTIC_FORMS = {"10-K", "10-Q", "10-KT", "10-QT"}
FOREIGN_FORMS = {"20-F", "40-F", "6-K"}

SCOPE_DOMESTIC = "domestic"
SCOPE_FOREIGN = "foreign"
SCOPE_FUND = "fund"
SCOPE_PRE_REPORT = "pre_report"


def filer_scope(cik):
    """`domestic` | `foreign` | `fund` | `pre_report` — the reporting regime an
    issuer files under, from its EDGAR filing history.

    Consulted only when an issuer could not be screened, to tell *the screen does
    not apply here* apart from *we failed to read something we should have read*.
    A registered fund (N-CSR/N-CEN/N-2) publishes no XBRL financial statements at
    all, and a company whose first 10-Q has not posted yet has none either — both
    return the same answer every day, forever. Asking this question of an issuer
    that screened fine would be wasted requests, so nothing does.

    Order matters: a listed BDC files BOTH 10-K and N-2, and it IS screenable, so
    the domestic test wins. Amendments (`10-K/A`) count as their base form."""
    forms = {f.split("/")[0].upper()
             for f in fetch_submissions(cik).get("filings", {}).get("recent", {}).get("form", [])}
    if forms & DOMESTIC_FORMS:
        return SCOPE_DOMESTIC
    if forms & FOREIGN_FORMS:
        return SCOPE_FOREIGN
    if any(f.startswith("N-") for f in forms):
        return SCOPE_FUND
    return SCOPE_PRE_REPORT


# ---- cover-page share count ----------------------------------------------------

_PERIODIC_FORMS = ("10-Q", "10-K", "10-KT", "10-QT")
_NOT_INSTANCE = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", "_ref.xml")
_COVER_SHARES_TAG = "EntityCommonStockSharesOutstanding"


def _local(tag):
    """Local name of a possibly namespaced lxml tag."""
    return str(tag).rsplit("}", 1)[-1]


def latest_periodic_filing(cik, forms=_PERIODIC_FORMS):
    """(form, accession_nodash, filing_date) of the newest 10-K/10-Q, or None.

    Amendments count — an amended cover page is still the current one. A CIK with
    no submissions record at all returns None: that is an absence SEC is telling
    us about, not a fetch we failed to make."""
    try:
        sub = fetch_submissions(cik)
    except requests.HTTPError as e:
        if _is_missing(e.response):
            return None
        raise
    recent = sub.get("filings", {}).get("recent", {})
    for form, acc, filed in zip(recent.get("form", []),
                                recent.get("accessionNumber", []),
                                recent.get("filingDate", [])):
        if form.split("/")[0].upper() in forms:
            return form, acc.replace("-", ""), filed
    return None


def _instance_name(names):
    """The XBRL instance document among a filing's files, or None.

    Inline filings (everything since 2021) carry a SEC-generated `*_htm.xml`
    extraction alongside the human-readable document; older ones carry a bare
    `ticker-YYYYMMDD.xml`. Both hold the facts with their dimensions intact and
    their values UNSCALED — which is the whole point of reading the instance
    rather than the rendered cover report `R1.htm`, where a filer reporting "in
    millions" renders LILA's 156,500,000 shares as the string `156.5`."""
    for n in names:
        if n.endswith("_htm.xml"):
            return n
    for n in names:
        if (n.endswith(".xml") and not n.endswith(_NOT_INSTANCE)
                and "index" not in n.lower() and "filingsummary" not in n.lower()
                and re.match(r"^[a-z0-9\-]+-\d{8}\.xml$", n)):
            return n
    return None


def cover_page_shares(cik):
    """Total common shares outstanding from the latest periodic report's cover page.

    Returns `(total_shares, as_of_iso)`, or `(None, None)` when the filing carries
    no cover-page count this function can stand behind.

    This exists because `companyfacts` — the API every other share read goes
    through — silently DROPS every fact that carries a dimension. A multi-class
    filer tags its cover-page count once per class, each dimensioned by class of
    stock, so all of them vanish: SUJA's companyfacts has no `dei` namespace at
    all, and OPFI's newest surviving count is the undimensioned one it stopped
    tagging in 2025. That is not a share count that is missing from SEC, it is a
    share count missing from one API, and 21 issuers (7 of them inside the EV cap,
    with a qualifying insider purchase) were dropped over it between 2026-07-15
    and 2026-08-13.

    Three properties keep the sum honest:

    - **One value per class.** Facts are grouped by their context's dimension
      signature and the newest instant wins within each, so a concept tagged twice
      in one document (SUJA tags its weighted-average shares under two ids in the
      same context) is counted once.
    - **The issuer only.** Contexts are filtered to the issuer's own CIK, so a
      co-registrant subsidiary's cover count is not added to its parent's.
    - **No scaling.** Values come from the instance, where they are absolute.

    Summing classes is the right total for these structures: Up-C Class V/B shares
    pair 1:1 with exchangeable LLC units, and Liberty-style A/B/C series are
    economically identical — which is why the sum reconciles to OPFI's own diluted
    weighted average (85.2M vs 86.1M) rather than to its Class A alone."""
    filing = latest_periodic_filing(cik)
    if filing is None:
        return None, None
    _, accn, _ = filing
    base = f"https://www.sec.gov/Archives/edgar/data/{str(cik).lstrip('0')}/{accn}"
    try:
        idx = _get(f"{base}/index.json").json()
    except requests.HTTPError as e:
        if _is_missing(e.response):
            return None, None
        raise
    name = _instance_name([i.get("name", "") for i in idx.get("directory", {}).get("item", [])])
    if not name:
        return None, None
    try:
        xml = _get(f"{base}/{name}").content
    except requests.HTTPError as e:
        if _is_missing(e.response):
            return None, None
        raise
    return parse_cover_page_shares(xml, cik)


def parse_cover_page_shares(xml, cik):
    """Sum the cover-page share counts in one XBRL instance. See `cover_page_shares`.

    Split out from the fetch so the parsing rules are testable without network."""
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError:
        return None, None

    contexts = {}
    for el in root.iter():
        if _local(el.tag) != "context":
            continue
        ident, instant, dims = None, None, []
        for sub in el.iter():
            ln = _local(sub.tag)
            if ln == "identifier":
                ident = (sub.text or "").strip()
            elif ln == "instant":
                instant = (sub.text or "").strip()
            elif ln == "explicitMember":
                dims.append((sub.get("dimension"), (sub.text or "").strip()))
        contexts[el.get("id")] = (ident, instant, tuple(sorted(dims)))

    target = str(cik).lstrip("0")
    by_class = {}          # dimension signature → (instant, value)
    for el in root.iter():
        if _local(el.tag) != _COVER_SHARES_TAG:
            continue
        ctx = contexts.get(el.get("contextRef"))
        if ctx is None:
            continue
        ident, instant, dims = ctx
        if ident is not None and ident.lstrip("0") != target:
            # A co-registrant's own cover count (subsidiary guarantors file under
            # the same accession). Adding it to the parent's would invent shares.
            continue
        try:
            val = float((el.text or "").replace(",", "").strip())
        except ValueError:
            continue
        if not val > 0:
            continue
        prior = by_class.get(dims)
        if prior is None or (instant or "") > (prior[0] or ""):
            by_class[dims] = (instant, val)

    if not by_class:
        return None, None
    total = sum(v for _, v in by_class.values())
    as_of = max((i for i, _ in by_class.values() if i), default=None)
    return total, as_of
