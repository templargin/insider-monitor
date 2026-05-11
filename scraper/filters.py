"""Screener filters: code-P aggregation, EV<$1B, TTM revenue>0."""
from collections import defaultdict

PURCHASE_THRESHOLD_USD = 100_000
EV_CAP_USD = 1_000_000_000


def aggregate_p_purchases(form4s):
    """Group parsed Form 4s by issuer_cik. Return dict of cik → {ticker, name, total_value, filings}.

    Only counts non-derivative transactions with code=P and ad_code=A (acquired).
    Negative shares (disposals) are ignored even if code=P (extremely rare).
    """
    by_issuer = defaultdict(lambda: {
        "ticker": "",
        "name": "",
        "total_value": 0.0,
        "filings": [],
    })
    for f in form4s:
        if not f:
            continue
        cik = f["issuer_cik"]
        if not cik:
            continue
        bucket = by_issuer[cik]
        if not bucket["ticker"] and f["issuer_ticker"]:
            bucket["ticker"] = f["issuer_ticker"]
        if not bucket["name"] and f["issuer_name"]:
            bucket["name"] = f["issuer_name"]
        p_txns = [t for t in f["transactions"]
                  if t["table"] == "nonDerivative"
                  and t["code"] == "P"
                  and t["ad_code"] == "A"
                  and t["shares"] > 0
                  and t["price"] > 0]
        if not p_txns:
            continue
        value = sum(t["total_value"] for t in p_txns)
        bucket["total_value"] += value
        bucket["filings"].append({
            "form_meta": {
                "reporter_name": f["reporter_name"],
                "relationship": f["relationship"],
                "issuer_cik": cik,
                "issuer_ticker": f["issuer_ticker"],
                "issuer_name": f["issuer_name"],
            },
            "purchase_txns": p_txns,
            "value": value,
        })
    return dict(by_issuer)


def passes_threshold(bucket, threshold_usd=PURCHASE_THRESHOLD_USD):
    return bucket["total_value"] >= threshold_usd


def basic_ev(market_cap_basic, total_debt, cash):
    """EV = MC + Debt - Cash. Returns None if MC unknown."""
    if market_cap_basic is None:
        return None
    return market_cap_basic + (total_debt or 0) - (cash or 0)


def passes_ev_cap(ev, cap=EV_CAP_USD):
    if ev is None:
        return False  # unknown → drop from screener
    return ev < cap


def passes_revenue(ttm_revenue):
    if ttm_revenue is None:
        return False  # no revenue evidence → drop
    return ttm_revenue > 0
