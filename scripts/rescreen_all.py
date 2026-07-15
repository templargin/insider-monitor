"""Re-screen every stored company against the corrected screen.

Companies persist across days, so one admitted by a bug stays published until
something re-checks it — and nothing did. `refresh_debt` rewrote `ev_basic`
without re-applying the cap, which is how FBRT ($3,376M, 3.4x the ceiling) came
to be published by a site whose stated criterion is EV < $1B.

Runs each stored company through `pipeline.screener_pass` — the same boundary the
daily run uses — so the stored set can be reconciled with the criteria the site
claims.

    python -m scripts.rescreen_all              # report only (default)
    python -m scripts.rescreen_all --apply      # write verdicts + corrected valuations
    python -m scripts.rescreen_all --apply GRWG # specific tickers

`--apply` FLAGS rather than deletes. Daily pages link to company pages, so
removing one breaks the historical record referencing it — and that record isn't
wrong: FBRT genuinely was listed on the day its insider bought. Each company gets

    "screen": {"qualifies": true|false|null, "reason": str|None, "checked_at": iso}

`qualifies: null` means we could not evaluate it, which is not "it fails" — the
same three-state distinction the screener makes, carried onto the page.

A rejection can mean the company never qualified (a bug admitted it) or that it
has since outgrown the cap; the reason carries the measure so the two can be told
apart.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from scraper import pipeline


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_changes = "--apply" in sys.argv

    base = Path("data/companies")
    tickers = args or sorted(p.stem for p in base.glob("*.json"))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    passed, rejected, unevaluated = [], [], []
    revalued = 0

    for i, t in enumerate(tickers, 1):
        p = base / f"{t}.json"
        d = json.loads(p.read_text())
        cik = d.get("cik")
        snap = None

        if not cik:
            verdict = {"qualifies": None, "reason": "no CIK stored", "checked_at": now}
            unevaluated.append((t, verdict["reason"]))
        else:
            try:
                snap, why = pipeline.screener_pass(cik, t, {"ticker": t, "name": d.get("name", t)})
            except pipeline.DataUnavailable as e:
                snap, verdict = None, {"qualifies": None, "reason": str(e), "checked_at": now}
                unevaluated.append((t, str(e)))
            except Exception as e:              # noqa: BLE001 - report, don't abort the sweep
                snap, verdict = None, {"qualifies": None, "reason": f"{type(e).__name__}: {e}",
                                       "checked_at": now}
                unevaluated.append((t, verdict["reason"]))
            else:
                # `why` carries the verdict; `snap` carries the measurement and is
                # populated either way, so a rejected company's page still gets
                # corrected figures rather than the stale ones that predate it.
                if why is not None:
                    verdict = {"qualifies": False, "reason": why, "checked_at": now}
                    rejected.append((t, why, (d.get("valuation") or {}).get("ev_basic")))
                else:
                    verdict = {"qualifies": True, "reason": None, "checked_at": now}
                    passed.append(t)

        if apply_changes:
            d["screen"] = verdict
            if snap is not None:
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
                # Store the grid the revenue was read off, exactly as
                # update_company_data does. Refreshing the valuation but not the
                # statements leaves a fresh top line beside a stale income
                # statement on the same page — the self-contradiction this whole
                # change exists to remove.
                d["financials"] = snap["fins"]
                if before != (v["ev_basic"], v["ttm_revenue"]):
                    revalued += 1
            p.write_text(json.dumps(d, indent=2, default=str))

        if i % 25 == 0:
            print(f"  ...{i}/{len(tickers)}", flush=True)

    print(f"\n=== re-screened {len(tickers)} stored companies ===")
    print(f"  still qualify     : {len(passed)}")
    print(f"  NO LONGER qualify : {len(rejected)}")
    for t, why, ev in sorted(rejected, key=lambda x: -(x[2] or 0)):
        print(f"      {t:6s} {why}")
    print(f"  could not evaluate: {len(unevaluated)}")
    for t, why in unevaluated:
        print(f"      {t:6s} {why}")

    if apply_changes:
        print(f"\nWrote {len(tickers)} screen verdict(s); {revalued} valuation block(s) changed.")
        print("Run `python -m scripts.build_site` to render.")
    else:
        print("\nReport only. Re-run with --apply to write verdicts + corrected valuations.")


if __name__ == "__main__":
    main()
