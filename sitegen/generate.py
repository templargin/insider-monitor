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


def money(v):
    if v is None or v == "" or (isinstance(v, float) and math.isnan(v)):
        return "—"
    try:
        return f"${float(v):,.0f}"
    except (ValueError, TypeError):
        return "—"


def money_signed(v):
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
        if f < 0:
            return f"-${abs(f):,.0f}"
        return f"${f:,.0f}"
    except (ValueError, TypeError):
        return "—"


def money_m(v):
    if v is None or v == "":
        return "—"
    try:
        f = float(v) / 1_000_000
        if abs(f) >= 1000:
            return f"${f/1000:,.2f}B"
        return f"${f:,.1f}M"
    except (ValueError, TypeError):
        return "—"


def number_int(v):
    if v is None or v == "":
        return "—"
    try:
        return f"{int(float(v)):,}"
    except (ValueError, TypeError):
        return "—"


def number_int_signed(v):
    if v is None or v == "":
        return "—"
    try:
        n = int(float(v))
        if n < 0:
            return f"({abs(n):,})"
        return f"{n:,}"
    except (ValueError, TypeError):
        return "—"


def number_int_or_dash(v):
    return number_int(v)


def number_2(v):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f}"
    except (ValueError, TypeError):
        return "—"


def price_or_dash(v):
    if v is None or v == "" or float(v) == 0:
        return "—"
    try:
        return f"${float(v):,.2f}"
    except (ValueError, TypeError):
        return "—"


def fin_cell(v, label=""):
    if v is None:
        return "—"
    try:
        f = float(v)
    except (ValueError, TypeError):
        return "—"
    if math.isnan(f):
        return "—"
    # Ratios that look like percentages or multiples
    label_l = (label or "").lower()
    if "%" in label_l or "margin" in label_l:
        return f"{f:.1f}%"
    if "ratio" in label_l or label_l in ("debt / equity",):
        return f"{f:.2f}"
    # Money in millions
    m = f / 1_000_000
    if f < 0:
        return f"({abs(m):,.2f})"
    return f"{m:,.2f}"


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
        v = c.get("valuation", {})
        so = v.get("shares_basic") or 0
        opts = v.get("options") or 0
        wrnts = v.get("warrants") or 0
        fd_so = so + opts + wrnts if so else None
        rendered = env.get_template("company.html").render(
            data=c,
            fd_so=fd_so,
            root=root_path_from(depth),
            generated_at=now,
        )
        write_html(out_path, rendered)

    # Companies index
    company_list = sorted(
        [{
            "ticker": c["ticker"],
            "name": c.get("name", ""),
            "ev_basic": c.get("valuation", {}).get("ev_basic"),
            "last_updated": (c.get("last_updated") or "")[:10],
        } for c in companies],
        key=lambda c: c["ticker"],
    )
    rendered = env.get_template("companies_index.html").render(
        tickers=company_list,
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
