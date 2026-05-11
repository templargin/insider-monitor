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


def _get(url, timeout=30):
    """Rate-limited HTTP GET. Lock ensures request-START spacing across threads;
    the actual network call happens outside the lock for concurrency."""
    global _last_request
    with _rate_lock:
        elapsed = time.time() - _last_request
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _last_request = time.time()
    resp = _session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


def quarter_for(d):
    return (d.month - 1) // 3 + 1


def daily_index_url(d):
    return f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{quarter_for(d)}/master.{d.strftime('%Y%m%d')}.idx"


def fetch_daily_index_form4s(d):
    """Return list of Form 4 / 4/A filings for date d. Empty list on 404 (weekend/holiday)."""
    url = daily_index_url(d)
    try:
        resp = _get(url)
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return []
        raise

    # Master idx uses Latin-1 for some special chars in company names
    text = resp.content.decode("latin-1")
    results = []
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


def fetch_form4_xml(cik, accession_nodash, xml_name=None):
    """Fetch the primary Form 4 XML. If xml_name provided (from search API), use it directly;
    otherwise look it up via index.json (slower)."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}"
    if xml_name is None:
        try:
            idx = _get(f"{base}/index.json").json()
        except requests.HTTPError:
            return None
        for item in idx.get("directory", {}).get("item", []):
            name = item.get("name", "")
            if name.endswith(".xml") and "index" not in name.lower() and "filing-summary" not in name.lower():
                xml_name = name
                break
        if not xml_name:
            return None
    try:
        return _get(f"{base}/{xml_name}").content
    except requests.HTTPError:
        return None


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
    relationship_parts = []
    title = ""
    if owner is not None:
        rpt_name = (owner.findtext("reportingOwnerId/rptOwnerName") or "").strip()
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
            try:
                shares = float(text_at(txn, "transactionAmounts/transactionShares") or "0")
            except ValueError:
                shares = 0.0
            try:
                price = float(text_at(txn, "transactionAmounts/transactionPricePerShare") or "0")
            except ValueError:
                price = 0.0
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
