# insider-monitor

Daily small-cap insider purchase screener. Form 4 filings filtered to:

- Transaction code **P** (open-market purchase) only
- Aggregate **≥ $100k** per issuer per filing bucket
- Enterprise value **< $1B** (basic shares)
- TTM revenue **> 0**

Static site published to GitHub Pages at <https://templargin.github.io/insider-monitor>.

## URLs

- `/insiders/YYYY/` — months
- `/insiders/YYYY/month/` — weekdays
- `/insiders/YYYY/month/DD/` — daily list (URL date = the day you read; content = previous weekday's filings; Monday rolls in Fri+Sat+Sun)
- `/companies/TICKER/` — canonical per-ticker page with insider activity, valuation, and financial statements

## How it runs

GitHub Action runs `0 11 * * 1-5` UTC (~7am ET, drifts ±1hr with DST). Pulls Form 4s from EDGAR, applies filters, regenerates affected pages, commits to `main`. Pages auto-deploys.

## Local dev

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m scripts.daily_run  # generate today's page
python -m scripts.backfill   # backfill from May 1, 2026
```
