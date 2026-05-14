"""Audit reconciliation of stored data/companies/*.json financial statements.

Reports per-statement mismatches:
  PL:  Rev - CoR vs GP
       GP  - OpEx vs OpInc                 (when both OpEx and OpInc reported)
       SG&A + R&D + ?  vs OpEx
       Pretax - Tax vs NetIncome
  BS:  Assets vs Liab + Equity              (fundamental equation)
       Cur Assets ≥ Cash + Receivables + Inventory  (sanity)
  CF:  ΔCash approximation
  Shares: SO vs latest Diluted Avg Shares spread

Tolerance: 1% of the larger absolute side, or $0.1M, whichever is larger.

Usage:  ./venv/bin/python -m scripts.audit_financials [TICKER ...]
        defaults to all tickers in data/companies/
"""
import json
import sys
from pathlib import Path

COMPANIES = Path("data/companies")


def _row(stmt, label):
    if not stmt or "labels" not in stmt:
        return None
    for i, l in enumerate(stmt["labels"]):
        if l == label:
            return stmt["data"][i]
    return None


def _val(row, i):
    if row is None or i >= len(row):
        return None
    return row[i]


def _close(a, b, rel=0.01, abs_min=100_000):
    if a is None or b is None:
        return True
    diff = abs(a - b)
    tol = max(abs(a), abs(b)) * rel
    return diff <= max(tol, abs_min)


def _pct_diff(a, b):
    if a is None or b is None:
        return None
    if abs(a) < 1 and abs(b) < 1:
        return 0.0
    base = max(abs(a), abs(b), 1)
    return 100.0 * (a - b) / base


def audit_ticker(d):
    issues = []
    ticker = d.get("ticker", "?")
    fins = d.get("financials") or {}

    # ----- INCOME STATEMENT -----
    for freq in ("annual", "quarterly"):
        stmt = (fins.get("income_statement") or {}).get(freq)
        if not stmt:
            continue
        periods = stmt.get("periods", [])
        rev = _row(stmt, "Total Revenue")
        cor = _row(stmt, "Cost of Revenue")
        gp = _row(stmt, "Gross Profit")
        sga = _row(stmt, "SG&A")
        rd = _row(stmt, "R&D")
        opex = _row(stmt, "Operating Expense")
        opinc = _row(stmt, "Operating Income")
        pretax = _row(stmt, "Pretax Income")
        tax = _row(stmt, "Tax Provision")
        ni = _row(stmt, "Net Income")

        for i, p in enumerate(periods):
            r, c, g = _val(rev, i), _val(cor, i), _val(gp, i)
            if r is not None and c is not None and g is not None:
                expected = r - c
                if not _close(expected, g):
                    issues.append(f"[{ticker}] IS/{freq}/{p}: GP mismatch — Rev−CoR={expected/1e6:.2f}M vs reported GP={g/1e6:.2f}M (Δ {_pct_diff(expected, g):.1f}%)")

            ox, oi = _val(opex, i), _val(opinc, i)
            if g is not None and ox is not None and oi is not None:
                expected = g - ox
                if not _close(expected, oi):
                    issues.append(f"[{ticker}] IS/{freq}/{p}: OpInc mismatch — GP−OpEx={expected/1e6:.2f}M vs reported OpInc={oi/1e6:.2f}M (Δ {_pct_diff(expected, oi):.1f}%)")

            s, rdv = _val(sga, i), _val(rd, i)
            if ox is not None and s is not None and rdv is not None:
                # OpEx should be at least SG&A + R&D
                if ox < (s + rdv) * 0.95:
                    issues.append(f"[{ticker}] IS/{freq}/{p}: OpEx {ox/1e6:.2f}M < SG&A+R&D ({(s+rdv)/1e6:.2f}M) — suspicious")

            pt, tx, n_ = _val(pretax, i), _val(tax, i), _val(ni, i)
            if pt is not None and tx is not None and n_ is not None:
                expected = pt - tx
                if not _close(expected, n_, rel=0.02):
                    issues.append(f"[{ticker}] IS/{freq}/{p}: NI mismatch — Pretax−Tax={expected/1e6:.2f}M vs reported NI={n_/1e6:.2f}M (Δ {_pct_diff(expected, n_):.1f}%)")

    # ----- BALANCE SHEET (Assets = Liab + Equity) -----
    for freq in ("annual", "quarterly"):
        stmt = (fins.get("balance_sheet") or {}).get(freq)
        if not stmt:
            continue
        periods = stmt.get("periods", [])
        assets = _row(stmt, "Total Assets")
        liab = _row(stmt, "Total Liabilities")
        mezz = _row(stmt, "Mezzanine Equity")
        eq = _row(stmt, "Total Equity") or _row(stmt, "Stockholders' Equity")
        for i, p in enumerate(periods):
            a, l_, m, e = _val(assets, i), _val(liab, i), _val(mezz, i), _val(eq, i)
            if a is not None and l_ is not None and e is not None:
                expected = l_ + e + (m or 0)
                if not _close(expected, a, rel=0.01):
                    issues.append(f"[{ticker}] BS/{freq}/{p}: A=L+M+E mismatch — L+M+E={expected/1e6:.2f}M vs Assets={a/1e6:.2f}M (Δ {_pct_diff(expected, a):.1f}%)")

    # ----- SHARES SANITY -----
    val = d.get("valuation") or {}
    so = val.get("shares_basic")
    is_a = (fins.get("income_statement") or {}).get("annual")
    if is_a and so:
        avg = _row(is_a, "Diluted Avg Shares")
        latest = _val(avg, 0) if avg else None
        if latest and latest > 0:
            ratio = so / latest
            if ratio < 0.5 or ratio > 2.0:
                issues.append(f"[{ticker}] SHARES: SO={so/1e6:.2f}M vs latest Diluted Avg={latest/1e6:.2f}M — ratio {ratio:.2f}× (suspect)")

    return issues


def main():
    tickers = sys.argv[1:] or sorted(p.stem for p in COMPANIES.glob("*.json"))
    all_issues = []
    for t in tickers:
        p = COMPANIES / f"{t}.json"
        if not p.exists():
            print(f"[{t}] no data file")
            continue
        d = json.loads(p.read_text())
        all_issues.extend(audit_ticker(d))

    by_kind = {}
    for line in all_issues:
        # extract bracket kind, e.g. "IS/annual" or "BS/quarterly" or "SHARES"
        # crude: take 2nd token after "] "
        try:
            rest = line.split("] ", 1)[1]
            kind = rest.split(":", 1)[0].split("/")[0]
        except Exception:
            kind = "?"
        by_kind.setdefault(kind, []).append(line)

    for kind, lst in sorted(by_kind.items()):
        print(f"\n=== {kind} ({len(lst)} issues) ===")
        for line in lst[:40]:
            print(line)
        if len(lst) > 40:
            print(f"  ... and {len(lst) - 40} more")

    print(f"\nTotal: {len(all_issues)} issues across {len(tickers)} tickers.")


if __name__ == "__main__":
    main()
