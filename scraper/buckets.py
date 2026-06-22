"""Bucket math for the read-day URL model.

URL date = day the user reads the page.
Content = filings posted since the previous weekday's read.
  - Tue-Fri page (URL date D): content = filings on D-1 (calendar)
  - Mon page (URL date D): content = filings on D-3, D-2, D-1 (Fri + Sat + Sun)
  - Sat/Sun pages don't exist.
"""
from datetime import date, timedelta

MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december"]


def is_weekday(d):
    return d.weekday() < 5  # 0=Mon ... 4=Fri


def _nth_weekday(year, month, weekday, n):
    """Date of the nth <weekday> (Mon=0 ... Sun=6) in a month (n >= 1)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    """Date of the last <weekday> (Mon=0 ... Sun=6) in a month."""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d):
    """US federal observance shift: a holiday on Saturday is observed the Friday
    before; on Sunday, the Monday after."""
    if d.weekday() == 5:      # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:      # Sunday
        return d + timedelta(days=1)
    return d


def us_federal_holidays(year):
    """Observed US federal holidays for `year` — the days EDGAR does not publish
    a daily index (so Form 4s are never filed on them). Computed rather than
    hardcoded so this keeps working in future years. Good Friday is intentionally
    excluded: NYSE closes but the SEC/EDGAR does accept filings that day."""
    return {
        _observed(date(year, 1, 1)),    # New Year's Day
        _nth_weekday(year, 1, 0, 3),    # MLK Jr. Day        (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),    # Washington's Bday  (3rd Mon Feb)
        _last_weekday(year, 5, 0),      # Memorial Day       (last Mon May)
        _observed(date(year, 6, 19)),   # Juneteenth
        _observed(date(year, 7, 4)),    # Independence Day
        _nth_weekday(year, 9, 0, 1),    # Labor Day          (1st Mon Sep)
        _nth_weekday(year, 10, 0, 2),   # Columbus Day       (2nd Mon Oct)
        _observed(date(year, 11, 11)),  # Veterans Day
        _nth_weekday(year, 11, 3, 4),   # Thanksgiving       (4th Thu Nov)
        _observed(date(year, 12, 25)),  # Christmas
    }


def is_trading_day(d):
    """True if EDGAR publishes filings on `d`: a weekday that isn't a federal
    holiday. Used to tell a genuinely empty bucket (Monday after a Friday
    holiday) apart from a trading day whose index hasn't published yet."""
    return is_weekday(d) and d not in us_federal_holidays(d.year)


def filing_dates_for_url(url_date):
    """Return the list of calendar dates whose filings live on this URL page.

    Raises ValueError if url_date is Sat/Sun (no page exists).
    """
    if url_date.weekday() == 5 or url_date.weekday() == 6:
        raise ValueError(f"{url_date} is a weekend — no page exists for Sat/Sun")
    if url_date.weekday() == 0:  # Monday
        # Mon page contains Fri + Sat + Sun filings
        return [url_date - timedelta(days=3),
                url_date - timedelta(days=2),
                url_date - timedelta(days=1)]
    # Tue-Fri: yesterday
    return [url_date - timedelta(days=1)]


def url_path_for_date(url_date):
    """Return the URL path segments for a page date, e.g. (2026, 'may', 11)."""
    return (url_date.year, MONTH_NAMES[url_date.month - 1], url_date.day)


def weekdays_in_range(start, end):
    """Yield weekdays from start to end inclusive."""
    d = start
    while d <= end:
        if is_weekday(d):
            yield d
        d += timedelta(days=1)
