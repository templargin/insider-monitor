"""Daily run: back-fill any recently missed URL dates, process today (in ET),
regenerate the site.

Catch-up exists because a skipped write used to be permanent. `process_bucket`
returns None whenever it refuses to publish — an upstream outage, an index SEC
has not posted yet — and this script only ever asked it about today, so nothing
revisited the gap. 2026-07-29 was lost exactly that way: both runs that morning
hit a mass companyfacts 429, the guard correctly published nothing, and the page
stayed missing until someone noticed by eye eleven days of pages later.

A missed day is cheap to retry and the retry is idempotent (the same guards that
refuse a bad write today refuse it again), so sweep the recent window every
morning before doing today's work.
"""
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scraper import pipeline
from sitegen import generate

ET = ZoneInfo("America/New_York")

# How far back to look for holes. Long enough to cover a long weekend plus a
# couple of bad mornings, short enough that a run never re-screens the archive.
CATCHUP_DAYS = 10


def missing_url_dates(today, window=CATCHUP_DAYS):
    """Weekday URL dates in [today-window, today) with no daily JSON on disk."""
    out = []
    for back in range(window, 0, -1):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        if not (pipeline.INSIDERS_DIR / f"{d.isoformat()}.json").exists():
            out.append(d)
    return out


def main():
    today = datetime.now(ET).date()

    for d in missing_url_dates(today):
        print(f"--- catch-up: {d} has no page, retrying ---")
        if pipeline.process_bucket(d) is None:
            print(f"--- catch-up: {d} still unwritable, leaving it ---")

    # Issuers that reached no verdict on an earlier run get asked again here,
    # before today's work, so a recovery lands on its own day's page and the site
    # rebuild below republishes it. This is what earns the right to publish ONLY
    # the companies that met the criteria: the ones we could not judge are not
    # dropped from the page, they are still in flight.
    retries = pipeline.retry_unresolved()
    if retries["retried"]:
        print(f"Unresolved queue: {retries['retried']} retried — "
              f"{retries['recovered']} recovered, {retries['rejected']} rejected on merits, "
              f"{retries['still_open']} still open, {retries['abandoned']} flagged for review.")

    if today.weekday() >= 5:
        print(f"{today} is a weekend in ET — no page to generate.")
    else:
        pipeline.process_bucket(today)

    summary = generate.generate()
    print(f"Site rebuilt: {summary['pages_built']} daily, {summary['companies_built']} companies.")

    # Exit code is the monitoring signal. The runner script pings Healthchecks
    # (and thus Telegram) off the workflow's conclusion, so a run that completes
    # cleanly while writing nothing used to look exactly like a healthy one —
    # which is why 7/29 and 7/30 went dark with no alert. Report the miss.
    #
    # Deliberately AFTER the site rebuild, and the workflow runs this check after
    # its commit step, so a failing day still publishes whatever it did produce.
    if today.weekday() < 5 and not (pipeline.INSIDERS_DIR / f"{today.isoformat()}.json").exists():
        print(f"MISSING PAGE: no data/insiders/{today.isoformat()}.json was written.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
