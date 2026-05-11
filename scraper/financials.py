"""Per-company financial statements (4 tabs): IS, BS, CF, Ratios.

yfinance primary, XBRL backup for missing fields. Returns plain dicts of
{statement_name: {"quarterly": {labels: [...], periods: [...], data: [[...]]},
                  "annual":    {labels: [...], periods: [...], data: [[...]]}}}
"""
import math
from datetime import date, timedelta


def _safe_yf_ticker(ticker):
    """Return yfinance.Ticker or None if yfinance import fails."""
    try:
        import yfinance as yf
        return yf.Ticker(ticker)
    except Exception:
        return None


def _df_to_dict(df, max_cols=4):
    """Convert a yfinance DataFrame (rows = line items, cols = period end dates) to
    {labels: [...], periods: [...], data: [[...]]} keeping the most recent max_cols periods.
    Returns None for empty/missing dataframes.
    """
    if df is None:
        return None
    try:
        if df.empty:
            return None
    except Exception:
        return None

    # Columns are timestamps; sort descending and limit
    cols = list(df.columns)
    cols_sorted = sorted(cols, reverse=True)[:max_cols]
    cols_sorted = sorted(cols_sorted)  # display oldest to newest

    labels = [str(idx) for idx in df.index]
    periods = []
    for c in cols_sorted:
        try:
            periods.append(c.strftime("%-m/%-d/%y"))
        except Exception:
            periods.append(str(c)[:10])

    data = []
    for label in df.index:
        row = []
        for c in cols_sorted:
            try:
                v = df.at[label, c]
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    row.append(None)
                else:
                    row.append(float(v))
            except Exception:
                row.append(None)
        data.append(row)

    # Drop rows that are all None or all zero
    kept_labels, kept_data = [], []
    for label, row in zip(labels, data):
        if any(v is not None and v != 0 for v in row):
            kept_labels.append(label)
            kept_data.append(row)

    return {"labels": kept_labels, "periods": periods, "data": kept_data}


def fetch_financials(ticker):
    """Return all 4 statements (income/balance/cashflow/ratios) for a ticker, both quarterly and annual.

    Each statement: {"quarterly": {...} or None, "annual": {...} or None}
    Returns None if yfinance is unavailable.
    """
    t = _safe_yf_ticker(ticker)
    if t is None:
        return None

    out = {}
    try:
        out["income_statement"] = {
            "quarterly": _df_to_dict(getattr(t, "quarterly_income_stmt", None)),
            "annual": _df_to_dict(getattr(t, "income_stmt", None)),
        }
    except Exception:
        out["income_statement"] = {"quarterly": None, "annual": None}

    try:
        out["balance_sheet"] = {
            "quarterly": _df_to_dict(getattr(t, "quarterly_balance_sheet", None)),
            "annual": _df_to_dict(getattr(t, "balance_sheet", None)),
        }
    except Exception:
        out["balance_sheet"] = {"quarterly": None, "annual": None}

    try:
        out["cash_flow"] = {
            "quarterly": _df_to_dict(getattr(t, "quarterly_cashflow", None)),
            "annual": _df_to_dict(getattr(t, "cashflow", None)),
        }
    except Exception:
        out["cash_flow"] = {"quarterly": None, "annual": None}

    # Ratios — computed from IS + BS
    out["ratios"] = _compute_ratios(out)
    return out


def _safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def _row(stmt, label):
    """Pull a row's values from a statement dict by label substring match (case-insensitive)."""
    if not stmt or not stmt.get("labels"):
        return None
    lower = label.lower()
    for i, l in enumerate(stmt["labels"]):
        if lower in l.lower():
            return stmt["data"][i]
    return None


def _row_exact(stmt, label):
    if not stmt or not stmt.get("labels"):
        return None
    for i, l in enumerate(stmt["labels"]):
        if l == label:
            return stmt["data"][i]
    return None


def _compute_ratios(financials):
    """Build a 'ratios' statement: gross/operating/net margins, D/E, current ratio.
    Returns a dict shaped like other statements: {quarterly:{labels,periods,data}, annual:{...}}.
    """
    out = {}
    for freq in ("quarterly", "annual"):
        is_ = (financials.get("income_statement") or {}).get(freq)
        bs_ = (financials.get("balance_sheet") or {}).get(freq)
        if not is_:
            out[freq] = None
            continue
        periods = is_.get("periods", [])
        n = len(periods)

        def pct_row(num, den):
            if not num or not den:
                return [None] * n
            return [
                (100.0 * num[i] / den[i]) if (num[i] is not None and den[i] not in (None, 0)) else None
                for i in range(n)
            ]

        revenue = _row(is_, "Total Revenue") or _row(is_, "Revenue") or _row_exact(is_, "Revenues")
        gross = _row(is_, "Gross Profit")
        op_inc = _row(is_, "Operating Income")
        net_inc = _row(is_, "Net Income")
        ebitda = _row(is_, "EBITDA")

        labels, data = [], []
        if revenue and gross:
            labels.append("Gross Margin %"); data.append(pct_row(gross, revenue))
        if revenue and op_inc:
            labels.append("Operating Margin %"); data.append(pct_row(op_inc, revenue))
        if revenue and net_inc:
            labels.append("Net Margin %"); data.append(pct_row(net_inc, revenue))
        if revenue and ebitda:
            labels.append("EBITDA Margin %"); data.append(pct_row(ebitda, revenue))

        # Balance sheet ratios
        if bs_:
            curr_assets = _row(bs_, "Current Assets")
            curr_liab = _row(bs_, "Current Liabilities")
            total_debt = _row(bs_, "Total Debt")
            total_equity = _row(bs_, "Stockholders Equity") or _row(bs_, "Total Equity")
            cash = _row(bs_, "Cash And Cash Equivalents") or _row(bs_, "Cash")

            if curr_assets and curr_liab:
                labels.append("Current Ratio")
                data.append([_safe_div(curr_assets[i], curr_liab[i]) for i in range(n)])
            if total_debt and total_equity:
                labels.append("Debt / Equity")
                data.append([_safe_div(total_debt[i], total_equity[i]) for i in range(n)])
            if total_debt and cash:
                labels.append("Net Debt (Debt - Cash)")
                data.append([
                    (total_debt[i] - cash[i]) if (total_debt[i] is not None and cash[i] is not None) else None
                    for i in range(n)
                ])

        out[freq] = {"labels": labels, "periods": periods, "data": data} if labels else None

    return out


def fetch_description(ticker):
    """Try yfinance longBusinessSummary; return string or empty."""
    t = _safe_yf_ticker(ticker)
    if t is None:
        return ""
    try:
        info = t.info  # may be slow / throttled
        return (info.get("longBusinessSummary") or "").strip()
    except Exception:
        return ""


def fetch_share_price(ticker):
    """Most recent close price via yfinance. None on failure."""
    t = _safe_yf_ticker(ticker)
    if t is None:
        return None
    try:
        hist = t.history(period="5d")
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None
