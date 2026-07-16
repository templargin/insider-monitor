"""Recompute the valuation debt block for every company JSON using the current
structured-debt extractor, then re-render only the pages that changed.

Counterpart to refresh_financials.py — run after a change to the debt/EV
extraction (scraper/xbrl_statement.py or the anchoring in scraper/xbrl_facts.py)
so stored companies that haven't had recent insider activity (and so weren't
re-screened) pick up the corrected figure. Updates valuation.debt,
valuation.debt_flag, valuation.cash and the derived valuation.ev_basic; leaves
share count, price, options/warrants, revenue and the financial statements
untouched. Daily-page docs are not touched.

    python -m scripts.refresh_debt              # all companies
    python -m scripts.refresh_debt REI AVD      # specific tickers
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from scraper import edgar, filters, xbrl_facts, xbrl_statement
from sitegen import generate as G


def _render_company(c, env, now):
    """Re-render docs/companies/TICKER/index.html exactly as generate.generate()."""
    fd_so, fd_mc, fd_ev = G.fd_figures(c.get("valuation"))
    rendered = env.get_template("company.html").render(
        data=c, fd_so=fd_so, fd_mc=fd_mc, fd_ev=fd_ev,
        multiples=G._compute_multiples(c, fd_mc, fd_ev),
        root=G.root_path_from(2), generated_at=now,
    )
    G.write_html(G.DOCS_DIR / f"companies/{c['ticker']}/index.html", rendered)


def main():
    base = Path("data/companies")
    tickers = sys.argv[1:] or sorted(p.stem for p in base.glob("*.json"))
    env = G.get_env()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed = 0
    over = []   # refreshed past the screen's own ceiling
    for t in tickers:
        p = base / f"{t}.json"
        d = json.loads(p.read_text())
        cik = d.get("cik")
        if not cik:
            continue
        try:
            facts = edgar.fetch_companyfacts(cik)
        except Exception as e:
            print(f"[{t}] companyfacts fetch failed ({e}) — skipped")
            continue
        if facts is None:
            print(f"[{t}] no companyfacts — skipped")
            continue

        debt, _, debt_flag = xbrl_statement.get_structured_debt(facts)
        cash, _ = xbrl_facts.get_cash(facts)
        v = d.get("valuation", {})
        mc = v.get("mc_basic")

        # Preserve-on-failure FIRST. Never let a momentary gap in the debt tags turn
        # a known borrowing into `null`, which the EV maths then reads as zero debt.
        # Mirrors the options/warrants rule in pipeline.update_company_data. This has
        # to come before the cap check: checking first meant reporting on an EV built
        # with `(debt or 0)` = 0 — understated by exactly the debt we then kept, so
        # the figure reported never matched what was on disk.
        if debt is None and v.get("debt") is not None:
            print(f"[{t}] debt now unreadable — keeping stored ${v['debt']/1e6:,.0f}M")
            continue

        ev = (mc + (debt or 0) - (cash or 0)) if mc is not None else v.get("ev_basic")

        # A refreshed debt figure can move EV across the screen's own ceiling. This
        # used to be written back unchecked, which is how FBRT ($3,376M), CUBI, XRN
        # and GSHD came to be published by a site whose stated criterion is EV < $1B.
        # (BETR was on that list too, but it is a flagged financial institution and
        # is now sized on market cap, where it clears.) Report rather than delete —
        # `scripts.rescreen_all` owns delisting.
        is_bank = bool(debt_flag) and debt_flag.get("reason") == "financial_institution"
        size = mc if is_bank else ev
        if size is not None and math.isfinite(size) and not filters.passes_ev_cap(size):
            over.append((t, size, "MC" if is_bank else "EV"))

        old = (v.get("debt"), v.get("debt_flag"), v.get("cash"), v.get("ev_basic"))
        new = (debt, debt_flag, cash, ev)
        if old == new:
            continue

        v["debt"], v["debt_flag"], v["cash"], v["ev_basic"] = debt, debt_flag, cash, ev
        d["valuation"] = v
        p.write_text(json.dumps(d, indent=2, default=str))
        _render_company(d, env, now)
        changed += 1
        od = "—" if old[0] is None else f"${old[0]/1e6:,.0f}M"
        nd = "—" if debt is None else f"${debt/1e6:,.0f}M"
        fl = f" ⚠{debt_flag['reason']}" if debt_flag else ""
        print(f"[{t}] debt {od} -> {nd}{fl}")

    print(f"\nUpdated {changed}/{len(tickers)} companies.")
    if over:
        print(f"\n{len(over)} company(ies) no longer meet the EV < "
              f"${filters.EV_CAP_USD/1e6:,.0f}M criterion after refresh:")
        for t, size, measure in sorted(over, key=lambda x: -x[1]):
            print(f"  {t:6s} {measure}=${size/1e6:,.1f}M")
        print("Run `python -m scripts.rescreen_all` to delist them.")


if __name__ == "__main__":
    main()
