"""Screener filters: code-P aggregation, EV<$1B, TTM revenue>0.

Threshold semantics: a single insider (reporter, identified by CIK) must
have ≥$100k of P-buys in the bucket for the company to qualify. Two
different insiders at $60k each do NOT make the cut, because no single
person crossed the line.
"""
from collections import defaultdict

PURCHASE_THRESHOLD_USD = 100_000
EV_CAP_USD = 1_000_000_000


def aggregate_p_purchases(form4s):
    """Group parsed Form 4s by issuer, with per-reporter sub-aggregation.

    Returns dict of issuer_cik → {
        ticker, name,
        by_reporter: {reporter_cik: {reporter_name, relationship, total_value, shares, txn_count, txns}},
        filings: [...],  # raw filing-level rows kept for downstream use
    }

    Only counts non-derivative transactions with code=P and ad_code=A
    and positive shares + price.
    """
    by_issuer = defaultdict(lambda: {
        "ticker": "",
        "name": "",
        "by_reporter": {},
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
        # Reporter key — prefer CIK, fall back to name when missing/zero.
        # (rpt_cik can be "0" on malformed filings; treat that as no-CIK.)
        rcik = f.get("reporter_cik") or ""
        rkey = rcik if rcik and rcik != "0" else f"name:{f['reporter_name']}"
        rec = bucket["by_reporter"].setdefault(rkey, {
            "reporter_cik": rcik,
            "reporter_name": f["reporter_name"],
            "relationship": f["relationship"],
            "total_value": 0.0,
            "shares": 0.0,
            "txn_count": 0,
        })
        for t in p_txns:
            rec["total_value"] += t["total_value"]
            rec["shares"] += t["shares"]
            rec["txn_count"] += 1
        value = sum(t["total_value"] for t in p_txns)
        bucket["filings"].append({
            "form_meta": {
                "reporter_name": f["reporter_name"],
                "reporter_cik": rcik,
                "relationship": f["relationship"],
                "issuer_cik": cik,
                "issuer_ticker": f["issuer_ticker"],
                "issuer_name": f["issuer_name"],
            },
            "purchase_txns": p_txns,
            "value": value,
        })
    return dict(by_issuer)


def qualifying_reporters(bucket, threshold_usd=PURCHASE_THRESHOLD_USD):
    """Return list of reporter records (sorted desc by total_value) that
    individually cleared the threshold."""
    return sorted(
        (r for r in bucket["by_reporter"].values() if r["total_value"] >= threshold_usd),
        key=lambda r: r["total_value"],
        reverse=True,
    )


def passes_threshold(bucket, threshold_usd=PURCHASE_THRESHOLD_USD):
    """At least one reporter must have crossed the per-insider threshold."""
    return any(r["total_value"] >= threshold_usd for r in bucket["by_reporter"].values())


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
