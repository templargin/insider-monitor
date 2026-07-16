"""Static site generator. Reads data/ JSON files, renders templates to docs/."""
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
INSIDERS_DIR = DATA_DIR / "insiders"
COMPANIES_DIR = DATA_DIR / "companies"
DOCS_DIR = REPO_ROOT / "docs"
TEMPLATES = REPO_ROOT / "sitegen" / "templates"
STATIC_SRC = REPO_ROOT / "sitegen" / "static"

MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december"]


def _finite(v):
    """float(v) when it is a number we can render, else None.

    The em dash is this layer's DataUnavailable: whatever we cannot state, we do
    not state. NaN and inf are not renderable numbers — `money` alone knew that,
    so the others turned a NaN into the literal string "$nanM" on the page. A
    non-numeric value must not escape either: `price_or_dash` called float()
    OUTSIDE its try, so one bad value raised and took down the whole site build.
    """
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    return f if math.isfinite(f) else None


def money(v):
    f = _finite(v)
    return "—" if f is None else f"${f:,.0f}"


def money_signed(v):
    f = _finite(v)
    if f is None:
        return "—"
    return f"-${abs(f):,.0f}" if f < 0 else f"${f:,.0f}"


def money_m(v):
    f = _finite(v)
    if f is None:
        return "—"
    m = f / 1_000_000
    return f"${m/1000:,.2f}B" if abs(m) >= 1000 else f"${m:,.1f}M"


def number_int(v):
    f = _finite(v)
    return "—" if f is None else f"{int(f):,}"


def number_int_signed(v):
    f = _finite(v)
    if f is None:
        return "—"
    n = int(f)
    return f"({abs(n):,})" if n < 0 else f"{n:,}"


def number_int_or_dash(v):
    return number_int(v)


def number_2(v):
    f = _finite(v)
    return "—" if f is None else f"{f:,.2f}"


def price_or_dash(v):
    f = _finite(v)
    return "—" if f is None or f == 0 else f"${f:,.2f}"


_RATIO_LABELS = {"current ratio", "quick ratio", "debt / equity"}
_SHARE_COUNT_LABELS = {"diluted avg shares", "basic avg shares",
                       "diluted average shares", "basic average shares"}
_EPS_LABELS = {"diluted eps", "basic eps"}


def fin_cell(v, label=""):
    if v is None:
        return "—"
    try:
        f = float(v)
    except (ValueError, TypeError):
        return "—"
    if math.isnan(f):
        return "—"
    label_l = (label or "").lower().strip()
    # Percentages / margins — negative in parens, matching standard convention
    if "%" in label_l or "margin" in label_l:
        if f < 0:
            return f"({abs(f):.1f}%)"
        return f"{f:.1f}%"
    # Specific ratios (not substring "ratio" — catches "administRATION")
    if label_l in _RATIO_LABELS:
        return f"{f:.2f}"
    # Per-share values shown raw
    if label_l in _EPS_LABELS:
        sign = "" if f >= 0 else "-"
        return f"{sign}${abs(f):.2f}"
    # Share counts shown in millions, no $
    if label_l in _SHARE_COUNT_LABELS:
        m = f / 1_000_000
        return f"{m:,.1f}"
    # Money in millions
    m = f / 1_000_000
    if f < 0:
        return f"({abs(m):,.2f})"
    return f"{m:,.2f}"


def shares_m(v):
    """Format share count in millions, no $ sign. '—' if None/NaN."""
    f = _finite(v)
    return "—" if f is None else f"{f/1_000_000:,.1f}M"


def pct(v):
    """Format a 0–1 fraction as a percent. '—' if None/NaN/non-numeric.

    yfinance routinely puts NaN in `info` (heldPercentInstitutions,
    shortPercentOfFloat, targetMeanPrice), which fetch_profile passes straight
    into the ownership block — this filter is where they surface.
    """
    f = _finite(v)
    return "—" if f is None else f"{f * 100:,.1f}%"


def analyst_count(v):
    """Analyst count. Yahoo omits the field entirely for uncovered names, so a
    None inside a successfully-fetched ownership block means 0 analysts —
    itself the signal on this site — not missing data."""
    if v is None or v == "":
        return "0"
    try:
        return f"{int(float(v)):,}"
    except (ValueError, TypeError):
        return "0"


_REC_LABELS = {
    "strong_buy": "Strong Buy",
    "buy": "Buy",
    "hold": "Hold",
    "underperform": "Underperform",
    "sell": "Sell",
}


def rec_label(v):
    """Human-readable consensus label from Yahoo's recommendationKey."""
    if not v:
        return "—"
    return _REC_LABELS.get(v, str(v).replace("_", " ").title())


def multiple(v):
    """Format a valuation multiple. None / negative / non-numeric → '—'."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (ValueError, TypeError):
        return "—"
    if math.isnan(f) or f <= 0:
        return "—"
    return f"{f:.1f}x"


def _sum_ttm(stmt_dict, label):
    """Sum the last four quarters of a row, or None when all four aren't there.

    All-or-nothing on purpose. Summing whichever quarters happen to be populated
    and calling the result TTM is how BKKT came to publish an EV/Revenue built
    from a SINGLE quarter — roughly 4x overstated — under a table headed "TTM
    Multiples" and a footnote reading "sum of last 4 quarters". A partial sum is
    not a trailing twelve months, and a multiple the reader cannot distinguish
    from a real one is worse than an em dash.

    `xbrl_financials._add_ltm_column` already takes this line (`if all(v is not
    None for v in vals)`); this is the same rule for the display layer.
    """
    if not stmt_dict or not stmt_dict.get("labels"):
        return None
    for i, l in enumerate(stmt_dict["labels"]):
        if l == label:
            vals = stmt_dict["data"][i][:4]
            if len(vals) < 4 or any(v is None for v in vals):
                return None
            return sum(vals)
    return None


def fd_figures(valuation):
    """(fd_so, fd_mc, fd_ev) from a company's valuation block.

    THE single fully-diluted computation — it was duplicated in generate.generate
    and scripts.refresh_debt, so a change had two places to be wrong in.

    A missing option/warrant count is counted as zero, which UNDERSTATES dilution.
    That is a deliberate choice, not an oversight: XBRL gives None both for "this
    filer has no warrants" and for "this filer tags no warrant concept", and for
    warrants the first is the overwhelmingly common case. Treating None as unknown
    and blanking the figures cost 31 of 209 companies their entire cap-structure
    table and every multiple — including BUKS, whose company page then showed no EV
    while the daily page showed $302.0M. Understating dilution on a few filers is
    the smaller error, and company.html says so next to the number.

    Contrast `_sum_ttm`, which is strict: a multiple is a derived claim and a
    partial sum is simply not a TTM, whereas EV here is a real figure the screen
    itself used.
    """
    v = valuation or {}
    so = v.get("shares_basic") or 0
    opts = v.get("options") or 0
    wrnts = v.get("warrants") or 0
    sp = v.get("share_price") or 0
    cash = v.get("cash") or 0
    debt = v.get("debt") or 0
    fd_so = (so + opts + wrnts) if so else None
    fd_mc = (sp * fd_so) if (sp and fd_so) else None
    fd_ev = (fd_mc + debt - cash) if fd_mc is not None else None
    return fd_so, fd_mc, fd_ev


def _compute_multiples(c, fd_mc, fd_ev):
    """Return dict of TTM valuation multiples for a company JSON."""
    fins = c.get("financials") or {}
    isq = (fins.get("income_statement") or {}).get("quarterly")
    cfq = (fins.get("cash_flow") or {}).get("quarterly")
    rev = _sum_ttm(isq, "Total Revenue")
    ebitda = _sum_ttm(isq, "EBITDA")
    ni = _sum_ttm(isq, "Net Income")
    fcf = _sum_ttm(cfq, "Free Cash Flow")

    def div(num, den, positive_only=True):
        if num is None or den is None or den == 0:
            return None
        if positive_only and den <= 0:
            return None
        return num / den

    return {
        "ev_revenue": div(fd_ev, rev, positive_only=True),
        "ev_ebitda": div(fd_ev, ebitda, positive_only=True),
        "mc_net_income": div(fd_mc, ni, positive_only=True),
        "ev_fcf": div(fd_ev, fcf, positive_only=True),
        "mc_fcf": div(fd_mc, fcf, positive_only=True),
        # raw TTM dollar values for the tooltip-style subtle hints
        "ttm_revenue": rev,
        "ttm_ebitda": ebitda,
        "ttm_net_income": ni,
        "ttm_fcf": fcf,
    }


def debt_flag_text(flag):
    """Human-readable move-3 debt-uncertainty note from the structured-debt flag."""
    if not flag:
        return ""
    reason = flag.get("reason")
    amt = flag.get("amount")
    amt_s = money_m(amt) if amt else None
    if reason == "debt_tags_overlap_clamped":
        return ("overlapping debt tags exceeded total liabilities; clamped to a"
                + (f" defensible bound (≈{amt_s} dropped)" if amt_s else " defensible bound"))
    if reason == "unexplained_liabilities":
        return (f"{amt_s} of liabilities are neither recognized debt nor a named non-debt item"
                if amt_s else "some liabilities are unclassified")
    if reason == "financial_institution":
        return "deposit-funded financial institution — debt/EV not a meaningful metric"
    if reason == "debt_from_footnote_total":
        return "taken from the debt-footnote total (filer tags no balance-sheet debt line)"
    return "see filing"


_env = None


def get_env():
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATES)),
            autoescape=select_autoescape(["html", "xml"]),
        )
        _env.filters["money"] = money
        _env.filters["money_m"] = money_m
        _env.filters["money_signed"] = money_signed
        _env.filters["number_int"] = number_int
        _env.filters["number_int_signed"] = number_int_signed
        _env.filters["number_int_or_dash"] = number_int_or_dash
        _env.filters["number_2"] = number_2
        _env.filters["price_or_dash"] = price_or_dash
        _env.filters["fin_cell"] = fin_cell
        _env.filters["shares_m"] = shares_m
        _env.filters["multiple"] = multiple
        _env.filters["debt_flag_text"] = debt_flag_text
        _env.filters["pct"] = pct
        _env.filters["analyst_count"] = analyst_count
        _env.filters["rec_label"] = rec_label
    return _env


def write_html(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def parse_url_date(s):
    return date.fromisoformat(s)


def list_daily_pages():
    """Return list of (url_date, ticker_count) for all daily JSON files."""
    out = []
    for p in sorted(INSIDERS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            d = parse_url_date(data["url_date"])
            out.append((d, len(data.get("tickers", [])), data))
        except Exception:
            continue
    return out


def list_companies():
    out = []
    for p in sorted(COMPANIES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            out.append(data)
        except Exception:
            continue
    return out


def root_path_from(depth):
    """Return relative path to site root from a page at given depth (0 = root)."""
    return "../" * depth


def generate():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    env = get_env()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pages = list_daily_pages()
    companies = list_companies()

    # Copy static assets
    static_dest = DOCS_DIR / "static"
    static_dest.mkdir(parents=True, exist_ok=True)
    for f in STATIC_SRC.iterdir():
        (static_dest / f.name).write_bytes(f.read_bytes())

    # Per-day pages
    years_set = set()
    months_by_year = {}  # year → set of months
    days_by_month = {}   # (year, month) → list of (day, ticker_count)
    for d, count, data in pages:
        url_date = d
        years_set.add(url_date.year)
        months_by_year.setdefault(url_date.year, set()).add(MONTH_NAMES[url_date.month - 1])
        days_by_month.setdefault((url_date.year, MONTH_NAMES[url_date.month - 1]), []).append(
            (url_date.day, count, data)
        )
        # Render the daily page
        rel = f"insiders/{url_date.year}/{MONTH_NAMES[url_date.month - 1]}/{url_date.day}/"
        out_path = DOCS_DIR / rel / "index.html"
        depth = 4
        filing_dates = data.get("filing_dates", [])
        rendered = env.get_template("daily.html").render(
            data=data,
            display_date=url_date.strftime("%A, %B %-d, %Y"),
            filing_dates_pretty=", ".join(filing_dates),
            root=root_path_from(depth),
            generated_at=now,
        )
        write_html(out_path, rendered)

    # Month index pages
    for (year, month), day_list in days_by_month.items():
        day_list_sorted = sorted(day_list, key=lambda t: t[0])
        rel = f"insiders/{year}/{month}/"
        out_path = DOCS_DIR / rel / "index.html"
        depth = 3
        rendered = env.get_template("month_index.html").render(
            year=year, month=month,
            days=[(d, True, c) for (d, c, _) in day_list_sorted],
            root=root_path_from(depth),
            generated_at=now,
        )
        write_html(out_path, rendered)

    # Year index pages
    for year, months in months_by_year.items():
        rel = f"insiders/{year}/"
        out_path = DOCS_DIR / rel / "index.html"
        depth = 2
        all_months = [(m, m in months) for m in MONTH_NAMES]
        rendered = env.get_template("year_index.html").render(
            year=year, months=all_months,
            root=root_path_from(depth),
            generated_at=now,
        )
        write_html(out_path, rendered)

    # Years index (top of /insiders/)
    years_rendered = env.get_template("years_index.html").render(
        years=sorted(years_set),
        root=root_path_from(1),
        generated_at=now,
    )
    write_html(DOCS_DIR / "insiders" / "index.html", years_rendered)

    # Company pages
    for c in companies:
        ticker = c["ticker"]
        rel = f"companies/{ticker}/"
        out_path = DOCS_DIR / rel / "index.html"
        depth = 2
        fd_so, fd_mc, fd_ev = fd_figures(c.get("valuation"))
        multiples = _compute_multiples(c, fd_mc, fd_ev)
        rendered = env.get_template("company.html").render(
            data=c,
            fd_so=fd_so,
            fd_mc=fd_mc,
            fd_ev=fd_ev,
            multiples=multiples,
            root=root_path_from(depth),
            generated_at=now,
        )
        write_html(out_path, rendered)

    # Companies index. A company whose last re-screen said it no longer meets the
    # criteria is listed separately rather than dropped: the daily pages link here,
    # and the record of what was filed on the day isn't wrong just because the
    # company has since outgrown the cap (or was admitted by a bug). `qualifies`
    # of None means we could not evaluate it — not that it failed — so it stays in
    # the main table. Companies never re-screened have no `screen` block and are
    # treated as qualifying.
    company_list = sorted(
        [{
            "ticker": c["ticker"],
            "name": c.get("name", ""),
            "ev_basic": c.get("valuation", {}).get("ev_basic"),
            "last_updated": (c.get("last_updated") or "")[:10],
            "qualifies": (c.get("screen") or {}).get("qualifies", True),
            "screen_reason": (c.get("screen") or {}).get("reason"),
        } for c in companies],
        key=lambda c: c["ticker"],
    )
    rendered = env.get_template("companies_index.html").render(
        tickers=[c for c in company_list if c["qualifies"] is not False],
        disqualified=[c for c in company_list if c["qualifies"] is False],
        root=root_path_from(1),
        generated_at=now,
    )
    write_html(DOCS_DIR / "companies" / "index.html", rendered)

    # Home (root)
    if pages:
        latest_d = max(d for d, _, _ in pages)
        latest_url = f"insiders/{latest_d.year}/{MONTH_NAMES[latest_d.month - 1]}/{latest_d.day}/"
        latest_label = latest_d.strftime("%A, %B %-d, %Y")
    else:
        latest_url = None
        latest_label = None
    rendered = env.get_template("home.html").render(
        latest_url=latest_url, latest_label=latest_label,
        root="",
        generated_at=now,
    )
    write_html(DOCS_DIR / "index.html", rendered)

    # About
    rendered = env.get_template("about.html").render(
        root="../",
        generated_at=now,
    )
    write_html(DOCS_DIR / "about" / "index.html", rendered)

    # .nojekyll so GH Pages doesn't try to Jekyll-process the docs/ folder
    (DOCS_DIR / ".nojekyll").write_text("")

    return {
        "pages_built": len(pages),
        "companies_built": len(companies),
    }


if __name__ == "__main__":
    summary = generate()
    print(f"Built {summary['pages_built']} daily pages, {summary['companies_built']} company pages.")
