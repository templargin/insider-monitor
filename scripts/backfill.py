"""One-shot backfill: process every weekday in a date range, then regenerate site."""
import argparse
import sys
from datetime import date, datetime

from scraper import pipeline, buckets
from sitegen import generate


def parse_d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=parse_d, default=date(2026, 5, 4))
    p.add_argument("--end", type=parse_d, default=date(2026, 5, 11))
    p.add_argument("--no-build", action="store_true",
                   help="Skip the final site build (just produce JSON)")
    args = p.parse_args()

    for d in buckets.weekdays_in_range(args.start, args.end):
        try:
            pipeline.process_bucket(d)
        except Exception as e:
            print(f"!! error processing {d}: {e}", file=sys.stderr)

    if not args.no_build:
        summary = generate.generate()
        print(f"\nSite built: {summary['pages_built']} daily, {summary['companies_built']} companies.")


if __name__ == "__main__":
    main()
