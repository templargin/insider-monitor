"""Audit cash flow reconciliation: OCF + ICF + FCF ≈ ΔCash (annual).
Also checks: CapEx displayed as negative (outflow); FCF = OCF + CapEx_negated."""
import json
import sys
from pathlib import Path


def _row(stmt, label):
    if not stmt: return None
    for i, l in enumerate(stmt["labels"]):
        if l == label:
            return stmt["data"][i]
    return None


def _val(row, i):
    if row is None or i >= len(row): return None
    return row[i]


def audit(d):
    issues = []
    t = d.get("ticker", "?")
    fins = d.get("financials") or {}
    cf = (fins.get("cash_flow") or {}).get("annual")
    bs = (fins.get("balance_sheet") or {}).get("annual")
    if not cf or not bs:
        return issues
    periods = cf.get("periods", [])

    ocf = _row(cf, "Operating Cash Flow")
    icf = _row(cf, "Investing Cash Flow")
    fcf = _row(cf, "Financing Cash Flow")
    capex = _row(cf, "CapEx")
    fcf_calc = _row(cf, "Free Cash Flow")
    cash = _row(bs, "Cash & Equivalents")

    # CapEx sign — should be negative (outflow) on display
    if capex:
        for i, p in enumerate(periods):
            v = capex[i]
            if v is not None and v > 0:
                issues.append(f"[{t}] CF/{p}: CapEx={v/1e6:.2f}M is POSITIVE (should be negative outflow)")

    # FCF = OCF + CapEx (capex already negated)
    if ocf and capex and fcf_calc:
        for i, p in enumerate(periods):
            o, c, f = _val(ocf, i), _val(capex, i), _val(fcf_calc, i)
            if o is not None and c is not None and f is not None:
                exp = o + c
                if abs(exp - f) > 100_000:
                    issues.append(f"[{t}] CF/{p}: FCF mismatch — OCF+CapEx={exp/1e6:.2f}M vs FCF={f/1e6:.2f}M")

    # ΔCash check: this year's cash - last year's cash ≈ OCF + ICF + FCF
    # Periods are newest-left, so ΔCash[j] = cash[j] - cash[j+1]
    if ocf and icf and fcf and cash:
        for j in range(len(periods) - 1):
            cn, cp = _val(cash, j), _val(cash, j + 1)
            o, i_, f = _val(ocf, j), _val(icf, j), _val(fcf, j)
            if all(v is not None for v in (cn, cp, o, i_, f)):
                d_cash = cn - cp
                flow = o + i_ + f
                gap = abs(d_cash - flow)
                rel = gap / max(abs(d_cash), abs(flow), 1)
                if rel > 0.05 and gap > 1_000_000:
                    issues.append(f"[{t}] CF/{periods[j]}: ΔCash={d_cash/1e6:.2f}M vs OCF+ICF+FCF={flow/1e6:.2f}M (Δ={gap/1e6:.2f}M, rel={rel*100:.1f}%)")

    return issues


def main():
    base = Path("data/companies")
    tickers = sys.argv[1:] or sorted(p.stem for p in base.glob("*.json"))
    issues = []
    for t in tickers:
        p = base / f"{t}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        issues.extend(audit(d))
    for i in issues:
        print(i)
    print(f"\nTotal: {len(issues)} CF issues across {len(tickers)} tickers.")


if __name__ == "__main__":
    main()
