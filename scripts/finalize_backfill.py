"""After the initial multi-day backfill, fix anything missed and rebuild + push.

Specifically:
- Re-run May 4 (Mon) which failed due to the 403-on-weekend bug now fixed
- Run May 11 (Mon) for the most recent weekend rollup
- Regenerate the site
"""
from datetime import date
from scraper import pipeline
from sitegen import generate


def main():
    for d in [date(2026, 5, 4), date(2026, 5, 11)]:
        json_path = f"data/insiders/{d.isoformat()}.json"
        from pathlib import Path
        if Path(json_path).exists():
            print(f"Skipping {d} — JSON already exists")
            continue
        pipeline.process_bucket(d)

    summary = generate.generate()
    print(f"\nSite built: {summary['pages_built']} daily pages, {summary['companies_built']} company pages.")


if __name__ == "__main__":
    main()
