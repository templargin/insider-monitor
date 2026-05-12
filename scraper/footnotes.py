"""Extract relevant footnote sections from a company's latest 10-Q/10-K.

Used so the daily workflow (which has full internet) does the SEC fetching,
and the LLM-extraction routine (which is sandboxed away from sec.gov) can
just read the pre-extracted text.
"""
import re
from . import edgar


# Heuristics: footnote phrases that signal the dilution-data sections
KEYWORDS = [
    "stock-based compensation",
    "share-based compensation",
    "stock option",
    "options outstanding",
    "outstanding options",
    "warrants outstanding",
    "outstanding warrants",
    "common stock warrants",
    "class of warrant",
    "warrant activity",
]

CONTEXT_BEFORE = 600
CONTEXT_AFTER = 4000
MAX_SECTIONS = 8
MAX_BYTES = 60_000  # cap final output


def _pick_primary_doc(index_json):
    """Choose the primary 10-Q/10-K HTML document from the filing's index.json.

    Heuristic: largest .htm that isn't an index or financial-report summary.
    """
    items = index_json.get("directory", {}).get("item", [])
    candidates = []
    for it in items:
        name = (it.get("name") or "").lower()
        if not (name.endswith(".htm") or name.endswith(".html")):
            continue
        if "index" in name or "summary" in name or "filingsummary" in name:
            continue
        try:
            size = int(it.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        candidates.append((size, it["name"]))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _strip_html(html):
    """Convert HTML to plain text. Cheap and cheerful — not a full parser."""
    # Drop scripts/styles entirely
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    # Replace common block tags with newlines so layout is preserved-ish
    html = re.sub(r"<(br|p|tr|li|h[1-6]|div)[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode numeric entities (covers things like &#8203; zero-width space)
    def _ent(match):
        try:
            return chr(int(match.group(1)))
        except (ValueError, OverflowError):
            return " "
    html = re.sub(r"&#(\d+);", _ent, html)
    html = (html.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&apos;", "'")
                .replace("​", "")  # zero-width space
                .replace(" ", " ")  # nbsp
                .replace("—", "-")  # em dash
                .replace("–", "-")  # en dash
                .replace("‘", "'").replace("’", "'")
                .replace("“", '"').replace("”", '"'))
    # Collapse whitespace
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r" *\n+ *", "\n", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _extract_sections(text):
    """Find keyword-anchored sections, dedup overlapping ones."""
    lower = text.lower()
    matches = []
    for kw in KEYWORDS:
        i = 0
        while True:
            j = lower.find(kw, i)
            if j == -1:
                break
            matches.append(j)
            i = j + len(kw)
    if not matches:
        return ""
    matches.sort()

    # Merge overlapping windows
    sections = []
    cur_start = None
    cur_end = None
    for m in matches:
        s, e = max(0, m - CONTEXT_BEFORE), min(len(text), m + CONTEXT_AFTER)
        if cur_start is None:
            cur_start, cur_end = s, e
        elif s <= cur_end + 200:
            cur_end = max(cur_end, e)
        else:
            sections.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    if cur_start is not None:
        sections.append((cur_start, cur_end))

    # Cap section count and total bytes
    out_parts = []
    total = 0
    for s, e in sections[:MAX_SECTIONS]:
        chunk = text[s:e].strip()
        if total + len(chunk) > MAX_BYTES:
            chunk = chunk[: MAX_BYTES - total]
        out_parts.append(chunk)
        total += len(chunk)
        if total >= MAX_BYTES:
            break
    return "\n\n----\n\n".join(out_parts)


def fetch_footnotes(cik, ticker):
    """Fetch the most recent 10-Q or 10-K for this CIK, extract dilution-related
    footnote sections. Returns plain text (or None if nothing useful)."""
    subs = edgar.fetch_submissions(cik)
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    period_of_report = recent.get("reportDate", [])

    target_idx = None
    for i, f in enumerate(forms):
        if f in ("10-Q", "10-K"):
            target_idx = i
            break
    if target_idx is None:
        return None

    acc = accs[target_idx]
    acc_nodash = acc.replace("-", "")
    primary_hint = docs[target_idx] if target_idx < len(docs) else None
    period = period_of_report[target_idx] if target_idx < len(period_of_report) else ""

    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}"

    # Pick primary doc — prefer the submissions JSON's `primaryDocument` hint,
    # falling back to the largest .htm in the filing index.
    primary = primary_hint
    if not primary or not (primary.endswith(".htm") or primary.endswith(".html")):
        try:
            idx = edgar._get(f"{base}/index.json").json()
            primary = _pick_primary_doc(idx)
        except Exception:
            return None
    if not primary:
        return None

    try:
        html = edgar._get(f"{base}/{primary}").content.decode("utf-8", errors="replace")
    except Exception:
        return None

    text = _strip_html(html)
    sections = _extract_sections(text)
    if not sections:
        return None

    header = (
        f"# Footnote extracts for {ticker.upper()} (CIK {cik})\n"
        f"# Source: {forms[target_idx]} filed {dates[target_idx]}, period {period}\n"
        f"# Filing URL: {base}/{primary}\n\n"
    )
    return header + sections
