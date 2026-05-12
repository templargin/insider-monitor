"""Daily run: process today's URL date (in ET), regenerate the site."""
from datetime import datetime
from zoneinfo import ZoneInfo

from scraper import pipeline
from sitegen import generate

ET = ZoneInfo("America/New_York")


def main():
    today = datetime.now(ET).date()
    if today.weekday() >= 5:
        print(f"{today} is a weekend in ET — no page to generate.")
        return
    pipeline.process_bucket(today)
    summary = generate.generate()
    print(f"Site rebuilt: {summary['pages_built']} daily, {summary['companies_built']} companies.")


if __name__ == "__main__":
    main()
