# insider-monitor

Daily small-cap insider purchase screener. Form 4 filings filtered to:

- Transaction code **P** (open-market purchase) only
- **One reporter (same CIK) ≥ $100k** in the bucket — two different insiders at $60k each do *not* qualify
- Enterprise value **< $1B** (basic shares)
- TTM revenue **> 0**

Static site published to GitHub Pages at <https://templargin.github.io/insider-monitor>.

## URLs

- `/insiders/YYYY/` — months
- `/insiders/YYYY/month/` — weekdays
- `/insiders/YYYY/month/DD/` — daily list (URL date = the day you read; content = previous weekday's filings; Monday rolls in Fri+Sat+Sun)
- `/companies/TICKER/` — canonical per-ticker page with insider activity, valuation, and financial statements

## How it runs

Primary trigger is a cron on the `aspancrm-claude` droplet at `30 6 * * 1-5` ET (6:30am ET). It pulls main, dispatches GH Actions `daily.yml`, waits for completion, runs the LLM options/warrants extraction, then rebuilds + pushes. GH Actions also has its own `30 10 * * 1-5` UTC schedule as a fallback. See [CRON_DROPLET.md](CRON_DROPLET.md).

## Financial statements

Per-ticker pages pull IS / BS / CF directly from SEC XBRL `companyfacts`. Rows are designed to reconcile by construction:

- **Operating Expense** is derived as Gross Profit − Operating Income (avoids the inconsistent `OperatingExpenses` XBRL tag that double-counts COGS for many filers).
- **Pretax Income** is derived as Net Income + Tax (avoids the `*MinorityInterest*` Pretax variant that some filers sign-flip for losses).
- **Net Income** = `ProfitLoss` (includes NCI); a separate **Net Income to Common** row appears when NCI is meaningful (e.g., MKTW) so EPS × shares ≈ parent's NI is visibly consistent.
- **Total Equity** uses the including-NCI variant; a **Mezzanine Equity** row appears for redeemable preferred (e.g., BETR FY22) so Assets = Liabilities + Mezzanine + Total Equity holds.
- **Quarterly YoY** compares each quarter to the same quarter one year prior (8-quarter internal buffer, 4 displayed).
- **Effect of FX on Cash** row in CF for foreign-operations filers.

Run `python -m scripts.audit_financials` for an end-to-end reconciliation pass (GP, OpInc, NI, A=L+M+E, share sanity). `python -m scripts.audit_cashflow` for CF reconciliation (ΔCash vs OCF+ICF+FCF+FX).

## Local dev

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m scripts.daily_run         # generate today's page
python -m scripts.backfill          # backfill from May 1, 2026
python -m scripts.refresh_financials # re-extract all companies' financials after extractor changes
python -m scripts.build_site        # render docs/ from data/
```
