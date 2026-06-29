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
import sys
from datetime import datetime, timezone
from pathlib import Path

from scraper import edgar, xbrl_facts, xbrl_statement
from sitegen import generate as G


def _render_company(c, env, now):
    """Re-render docs/companies/TICKER/index.html exactly as generate.generate()."""
    v = c.get("valuation", {})
    so = v.get("shares_basic") or 0
    opts = v.get("options") or 0
    wrnts = v.get("warrants") or 0
    sp = v.get("share_price") or 0
    cash = v.get("cash") or 0
    debt = v.get("debt") or 0
    fd_so = (so + opts + wrnts) if so else None
    fd_mc = (sp * fd_so) if (sp and fd_so) else None
    fd_ev = (fd_mc + debt - cash) if fd_mc is not None else None
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
        ev = (mc + (debt or 0) - (cash or 0)) if mc is not None else v.get("ev_basic")

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


if __name__ == "__main__":
    main()
