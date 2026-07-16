"""Screener filters: code-P aggregation, EV<$1B, TTM revenue>0.

Threshold semantics: a single insider (reporter, identified by CIK) must
have ≥$100k of P-buys in the bucket for the company to qualify. Two
different insiders at $60k each do NOT make the cut, because no single
person crossed the line.
"""
import math
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
    """EV = MC + Debt - Cash.

    `total_debt` of None means the filer reports no debt-shaped liability at all —
    a real zero — and that reading is sound because `pipeline.screener_pass`
    refuses to evaluate a filer whose us-gaap balance sheet can't be anchored.

    `cash` of None is weaker and worth naming honestly: `xbrl_facts.get_cash`
    falls back to an unanchored `latest_value`, so None means "this filer tags no
    cash concept on any date", not "no cash on the current sheet". Coercing that
    to 0 OVERSTATES EV, which risks a false rejection at the cap rather than a
    false admission — the safer direction, but not the one the old comment here
    claimed to be guarding.
    """
    if market_cap_basic is None or not math.isfinite(market_cap_basic):
        raise ValueError(f"basic_ev needs a finite market cap, got {market_cap_basic!r}")
    ev = market_cap_basic + (total_debt or 0) - (cash or 0)
    if not math.isfinite(ev):
        # debt and cash come from companyfacts, and json.loads accepts bare NaN /
        # Infinity literals. Validating only the market cap let a non-finite debt
        # reach passes_ev_cap, which RAISES ValueError — and process_bucket catches
        # only DataUnavailable, so it would escape and kill the whole daily run:
        # exactly the failure this change exists to prevent. Fail here, where the
        # caller can turn it into a per-issuer DataUnavailable instead of losing
        # the day.
        raise ValueError(f"basic_ev got non-finite inputs: mc={market_cap_basic!r} "
                         f"debt={total_debt!r} cash={cash!r}")
    return ev


def passes_ev_cap(ev, cap=EV_CAP_USD):
    """True when the size measure clears the small-cap ceiling.

    The caller must resolve "unknown" *before* asking. A predicate that answers
    "no" to a question it was unable to ask is indistinguishable from a real
    rejection — that is precisely how a NaN price (NaN is not None and not <= 0,
    and every NaN comparison is False) silently deleted BUKS from the 2026-07-15
    page while logging a confident "EV >= cap".

    Negative EV is a legitimate PASS: a company trading below net cash is
    emphatically under the cap (GVH, INM).
    """
    if ev is None or not math.isfinite(ev):
        raise ValueError(f"passes_ev_cap needs a finite EV, got {ev!r}; caller must validate")
    return ev < cap


def passes_revenue(ttm_revenue):
    """True when the filer has any revenue. Zero is a real answer (clinical-stage
    biotechs tag no revenue concept at all); None/NaN is not, and is a caller bug."""
    if ttm_revenue is None or not math.isfinite(ttm_revenue):
        raise ValueError(f"passes_revenue needs a finite revenue, got {ttm_revenue!r}")
    return ttm_revenue > 0
