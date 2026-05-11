"""Daily run: process today's URL date, regenerate the site."""
from datetime import date
from scraper import pipeline, buckets
from sitegen import generate


def main():
    today = date.today()
    if today.weekday() >= 5:
        print(f"{today} is a weekend — no page to generate.")
        return
    pipeline.process_bucket(today)
    summary = generate.generate()
    print(f"Site rebuilt: {summary['pages_built']} daily, {summary['companies_built']} companies.")


if __name__ == "__main__":
    main()
