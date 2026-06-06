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


# Canonical line items. Each entry: (display_name, [yfinance_label_candidates_in_priority]).
# "__spacer__" inserts an empty divider row. Only rows in this list survive — duplicates,
# normalized variants, and yfinance noise are dropped.
SPACER = ("__spacer__", None)

INCOME_CANONICAL = [
    ("Total Revenue", ["Total Revenue", "Operating Revenue"]),
    ("Revenue YoY %", "__derived_yoy__:Total Revenue"),
    ("Cost of Revenue", ["Cost Of Revenue", "Reconciled Cost Of Revenue"]),
    ("Gross Profit", ["Gross Profit"]),
    ("Gross Profit YoY %", "__derived_yoy__:Gross Profit"),
    ("Gross Margin %", "__derived_margin__:Gross Profit:Total Revenue"),
    SPACER,
    ("SG&A", ["Selling General And Administration"]),
    ("R&D", ["Research And Development"]),
    ("Operating Expense", ["Operating Expense"]),
    SPACER,
    ("Operating Income", ["Operating Income", "EBIT"]),
    ("Operating Income YoY %", "__derived_yoy__:Operating Income"),
    ("Operating Margin %", "__derived_margin__:Operating Income:Total Revenue"),
    ("EBITDA", ["EBITDA", "Normalized EBITDA"]),
    ("EBITDA Margin %", "__derived_margin__:EBITDA:Total Revenue"),
    SPACER,
    ("Interest Expense", ["Interest Expense", "Net Interest Income"]),
    ("Other Income / (Expense)", ["Other Income / (Expense)"]),
    ("Pretax Income", ["Pretax Income"]),
    ("Tax Provision", ["Tax Provision", "Tax Effect Of Unusual Items"]),
    ("Net Income", ["Net Income Common Stockholders", "Net Income", "Net Income Continuous Operations"]),
    ("Net Income to Common", ["Net Income to Common"]),
    ("Net Margin %", "__derived_margin__:Net Income:Total Revenue"),
    SPACER,
    ("Diluted EPS", ["Diluted EPS"]),
    ("Basic EPS", ["Basic EPS"]),
    ("Diluted Avg Shares", ["Diluted Average Shares", "Diluted Avg Shares"]),
    ("Basic Avg Shares", ["Basic Avg Shares"]),
]

BALANCE_CANONICAL = [
    ("Cash & Equivalents", ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]),
    ("Short-term Investments", ["Other Short Term Investments", "Short Term Investments"]),
    ("Receivables", ["Accounts Receivable", "Net Receivables", "Receivables"]),
    ("Inventory", ["Inventory"]),
    ("Other Current Assets", ["Other Current Assets"]),
    ("Total Current Assets", ["Current Assets"]),
    SPACER,
    ("Net PPE", ["Net PPE", "Property Plant And Equipment Net"]),
    ("Operating Lease ROU", ["Operating Lease ROU"]),
    ("Goodwill", ["Goodwill", "Goodwill And Other Intangible Assets"]),
    ("Intangible Assets", ["Intangible Assets"]),
    ("Long-term Investments", ["Long-term Investments"]),
    ("Other Assets", ["Other Assets"]),
    ("Total Assets", ["Total Assets"]),
    SPACER,
    ("Accounts Payable", ["Payables", "Accounts Payable"]),
    ("Current Debt", ["Current Debt And Capital Lease Obligation", "Current Debt"]),
    ("Current Lease Liabilities", ["Current Lease Liabilities"]),
    ("Other Current Liabilities", ["Other Current Liabilities"]),
    ("Total Current Liabilities", ["Current Liabilities"]),
    ("Long-term Debt", ["Long Term Debt And Capital Lease Obligation", "Long Term Debt"]),
    ("Long-term Lease Liabilities", ["Long-term Lease Liabilities"]),
    ("Other Liabilities", ["Other Liabilities"]),
    ("Total Liabilities", ["Total Liabilities Net Minority Interest", "Total Liabilities"]),
    ("Mezzanine Equity", ["Mezzanine Equity"]),
    SPACER,
    ("Retained Earnings", ["Retained Earnings"]),
    ("Total Equity", ["Total Equity", "Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"]),
]

CASHFLOW_CANONICAL = [
    # Operating
    ("Net Income", ["Net Income From Continuing Operations", "Net Income"]),
    ("D&A", ["Depreciation Amortization Depletion", "Depreciation And Amortization", "Reconciled Depreciation"]),
    ("Stock-Based Comp", ["Stock Based Compensation"]),
    # Working-capital change + all other non-cash operating adjustments, rolled
    # into one plug so the operating section foots NI → OCF by construction.
    # The XBRL path derives this in _derive_cf_other_operating; the legacy
    # yfinance label is kept as a fallback source.
    ("Δ Working Cap & Other", ["Δ Working Cap & Other", "Change In Working Capital"]),
    ("Operating Cash Flow", ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"]),
    SPACER,
    # Investing
    ("CapEx", ["Capital Expenditure"]),
    ("Acquisitions", ["Acquisitions"]),
    ("Other Investing", ["Other Investing"]),
    ("Investing Cash Flow", ["Investing Cash Flow", "Cash Flow From Continuing Investing Activities"]),
    SPACER,
    # Financing
    ("Debt Issuance", ["Issuance Of Debt"]),
    ("Debt Repayment", ["Repayment Of Debt"]),
    ("Stock Issuance", ["Issuance Of Capital Stock"]),
    ("Stock Buyback", ["Repurchase Of Capital Stock"]),
    ("Dividends Paid", ["Common Stock Dividend Paid", "Cash Dividends Paid"]),
    ("Other Financing", ["Other Financing"]),
    ("Financing Cash Flow", ["Financing Cash Flow", "Cash Flow From Continuing Financing Activities"]),
    SPACER,
    # FX effect — auto-prunes when zero/missing.
    ("Effect of FX on Cash", ["Effect of FX on Cash"]),
    SPACER,
    # Reconciliation
    ("End Cash Position", ["End Cash Position"]),
    SPACER,
    # Derived / non-GAAP
    ("Free Cash Flow", ["Free Cash Flow"]),
]


def _canonicalize(stmt_dict, canonical, freq="annual", display_n=None):
    """Keep only canonical rows, in canonical order, with spacer rows interleaved.
    Drops everything else (duplicates, normalized variants, EBIT vs Operating Income, etc.).

    Supports derived rows:
      - "__derived_margin__:<num>:<den>" — pct of num/den
      - "__derived_yoy__:<source>"      — pct change vs prior period (offset 1 for
        annual, offset 4 for quarterly so each quarter is compared to the same
        quarter one year prior).

    `display_n` (optional) — after derivations are computed, trim the period
    columns to the first N (newest). Use for quarterly so we can pull 8 quarters
    internally for YoY but display only the most recent 4.
    """
    if not stmt_dict or not stmt_dict.get("labels"):
        return stmt_dict
    labels = stmt_dict["labels"]
    data = stmt_dict["data"]
    periods = stmt_dict["periods"]
    label_to_idx = {l: i for i, l in enumerate(labels)}
    nulls = [None] * len(periods)

    new_labels, new_data = [], []

    def find_row(name):
        for i, l in enumerate(new_labels):
            if l == name:
                return new_data[i]
        return None

    # YoY offset: 1 step for annual, 4 steps for quarterly (same quarter PY).
    yoy_offset = 1 if freq == "annual" else 4

    for display_name, options in canonical:
        if display_name == "__spacer__":
            new_labels.append("")
            new_data.append(nulls[:])
            continue
        if isinstance(options, str):
            if options.startswith("__derived_margin__:"):
                _, num_name, den_name = options.split(":", 2)
                num_row, den_row = find_row(num_name), find_row(den_name)
                if num_row is None or den_row is None:
                    continue
                row = [
                    (100.0 * n / d) if (n is not None and d not in (None, 0)) else None
                    for n, d in zip(num_row, den_row)
                ]
                new_labels.append(display_name)
                new_data.append(row)
                continue
            if options.startswith("__derived_yoy__:"):
                _, source_name = options.split(":", 1)
                src_row = find_row(source_name)
                if src_row is None:
                    continue
                # Periods are newest-left. Quarterly: row[j] vs row[j+4] (same
                # quarter one year prior). Annual: row[j] vs row[j+1].
                row = []
                for j in range(len(src_row)):
                    cur = src_row[j]
                    k = j + yoy_offset
                    prev = src_row[k] if k < len(src_row) else None
                    if cur is None or prev is None or prev == 0:
                        row.append(None)
                    else:
                        row.append(100.0 * (cur - prev) / abs(prev))
                new_labels.append(display_name)
                new_data.append(row)
                continue
        # Plain source-label lookup; display name is a fallback for re-canonicalization.
        matched_idx = None
        for opt in list(options) + [display_name]:
            if opt in label_to_idx:
                matched_idx = label_to_idx[opt]
                break
        if matched_idx is None:
            continue
        new_labels.append(display_name)
        new_data.append(data[matched_idx])

    # Optional period-trim after derivations are computed.
    if display_n is not None and display_n < len(periods):
        periods = periods[:display_n]
        new_data = [row[:display_n] for row in new_data]

    # Drop content rows that came back all-None (keep spacers for later collapse).
    pruned_labels, pruned_data = [], []
    for l, row in zip(new_labels, new_data):
        if l != "" and all(v is None for v in row):
            continue
        pruned_labels.append(l)
        pruned_data.append(row)

    # Collapse consecutive spacers and trim leading/trailing.
    final_labels, final_data = [], []
    for l, row in zip(pruned_labels, pruned_data):
        if l == "" and (not final_labels or final_labels[-1] == ""):
            continue
        final_labels.append(l)
        final_data.append(row)
    while final_labels and final_labels[-1] == "":
        final_labels.pop()
        final_data.pop()

    return {"labels": final_labels, "periods": periods, "data": final_data}


# Back-compat shim for callers expecting `_reorder` — now does canonical filtering
def _reorder(stmt_dict, canonical):
    return _canonicalize(stmt_dict, canonical)


INCOME_STMT_ORDER = INCOME_CANONICAL
BALANCE_SHEET_ORDER = BALANCE_CANONICAL
CASH_FLOW_ORDER = CASHFLOW_CANONICAL


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

    # Columns are timestamps; keep the most recent N in newest-left order.
    cols = list(df.columns)
    cols_sorted = sorted(cols, reverse=True)[:max_cols]
    # Already newest → oldest; that's the display order we want.

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
            "quarterly": _canonicalize(_df_to_dict(getattr(t, "quarterly_income_stmt", None)), INCOME_STMT_ORDER, "quarterly"),
            "annual": _canonicalize(_df_to_dict(getattr(t, "income_stmt", None)), INCOME_STMT_ORDER, "annual"),
        }
    except Exception:
        out["income_statement"] = {"quarterly": None, "annual": None}

    try:
        out["balance_sheet"] = {
            "quarterly": _canonicalize(_df_to_dict(getattr(t, "quarterly_balance_sheet", None)), BALANCE_SHEET_ORDER, "quarterly"),
            "annual": _canonicalize(_df_to_dict(getattr(t, "balance_sheet", None)), BALANCE_SHEET_ORDER, "annual"),
        }
    except Exception:
        out["balance_sheet"] = {"quarterly": None, "annual": None}

    try:
        out["cash_flow"] = {
            "quarterly": _canonicalize(_df_to_dict(getattr(t, "quarterly_cashflow", None)), CASH_FLOW_ORDER, "quarterly"),
            "annual": _canonicalize(_df_to_dict(getattr(t, "cashflow", None)), CASH_FLOW_ORDER, "annual"),
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
