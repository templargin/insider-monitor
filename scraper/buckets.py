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
