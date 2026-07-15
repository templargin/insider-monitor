"""Re-screen every stored company against the corrected screen.

Companies persist across days, so a company admitted by a bug stays published
until something re-checks it — and nothing did. `refresh_debt` rewrote `ev_basic`
without re-applying the cap, which is how FBRT ($3,376M), CUBI, XRN, BETR and
GSHD came to be published by a site whose stated criterion is EV < $1B.

This reports each stored company as PASS / REJECT / UNEVALUATED using
`pipeline.screener_pass` — the same boundary the daily run uses — so the stored
set can be reconciled with the criteria the site claims.

    python -m scripts.rescreen_all              # report only (default)
    python -m scripts.rescreen_all --apply      # also rewrite corrected valuations
    python -m scripts.rescreen_all --apply GRWG # specific tickers

REJECT means the company does not meet the criteria on today's data. That can mean
it never qualified (a bug admitted it) or that it has since outgrown the cap —
this prints the measure so the two can be told apart. Delisting is deliberately
NOT automatic: daily pages link to company pages, so removing one breaks the
historical record that references it.
"""
import json
import sys
from pathlib import Path

from scraper import pipeline


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_changes = "--apply" in sys.argv

    base = Path("data/companies")
    tickers = args or sorted(p.stem for p in base.glob("*.json"))

    passed, rejected, unevaluated, updated = [], [], [], 0

    for i, t in enumerate(tickers, 1):
        p = base / f"{t}.json"
        d = json.loads(p.read_text())
        cik = d.get("cik")
        if not cik:
            unevaluated.append((t, "no CIK stored"))
            continue

        try:
            snap = pipeline.screener_pass(cik, t, {"ticker": t, "name": d.get("name", t)})
        except pipeline.DataUnavailable as e:
            unevaluated.append((t, str(e)))
            continue
        except Exception as e:                      # noqa: BLE001 - report, don't abort the sweep
            unevaluated.append((t, f"{type(e).__name__}: {e}"))
            continue

        if snap is None:
            v = d.get("valuation") or {}
            rejected.append((t, v.get("ev_basic"), v.get("mc_basic")))
            continue

        passed.append(t)
        if apply_changes:
            v = d.get("valuation") or {}
            before = (v.get("ev_basic"), v.get("ttm_revenue"))
            v.update({
                "shares_basic": snap["shares"],
                "shares_basic_as_of": snap["shares_as_of"],
                "cash": snap["cash"],
                "debt": snap["debt"],
                "debt_flag": snap["debt_flag"],
                "ttm_revenue": snap["ttm_revenue"],
                "share_price": snap["share_price"],
                "mc_basic": snap["mc_basic"],
                "ev_basic": snap["ev_basic"],
            })
            d["valuation"] = v
            if before != (v["ev_basic"], v["ttm_revenue"]):
                updated += 1
            p.write_text(json.dumps(d, indent=2, default=str))

        if i % 25 == 0:
            print(f"  ...{i}/{len(tickers)}", flush=True)

    print(f"\n=== re-screened {len(tickers)} stored companies ===")
    print(f"  still qualify : {len(passed)}")
    print(f"  NO LONGER qualify: {len(rejected)}")
    for t, ev, mc in sorted(rejected, key=lambda x: -(x[1] or 0)):
        ev_s = "—" if ev is None else f"${ev/1e6:,.1f}M"
        print(f"      {t:6s} stored EV={ev_s}")
    print(f"  could not evaluate: {len(unevaluated)}")
    for t, why in unevaluated:
        print(f"      {t:6s} {why}")

    if apply_changes:
        print(f"\nRewrote {updated} valuation block(s). Run `python -m scripts.build_site` to render.")
    else:
        print("\nReport only. Re-run with --apply to rewrite corrected valuations.")
    print("Delisting is manual: daily pages link to company pages.")


if __name__ == "__main__":
    main()
