# insider-monitor

Daily small-cap insider purchase screener. Form 4 filings filtered to:

- Transaction code **P** (open-market purchase) only
- **One reporter (same CIK) ≥ $100k** in the bucket — two different insiders at $60k each do *not* qualify
- Enterprise value **< $1B** (basic shares)
- TTM revenue **> 0** — only revenue facts whose `end` date is within ~18 months of today count (otherwise a shell-co predecessor's ancient revenue, e.g. KLRS's 2019 $165k, leaks through after a reverse merger). Bank-style top-line tags (`InterestAndDividendIncomeOperating`, `NoninterestIncome`) are included in the screener's revenue ladder so community banks like BCML/CUBI/FMBM that don't tag `Revenues` aren't false-rejected.

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

- **LTM column** is the leftmost period on annual IS / BS / CF / Ratios. Flow items (Revenue, expenses, NI, EPS) sum the last 4 quarterly values; point-in-time items (Diluted/Basic Avg Shares) take the latest quarterly value; BS uses the most-recent quarter-end values. Margins are derived from the column directly so LTM Margin = LTM_NI / LTM_Revenue reconciles.
- **Cash-flow operating section foots by construction.** The XBRL extractor pulls only Net Income, D&A, and Stock-Based Comp explicitly, so a single derived row **Δ Working Cap & Other** = OCF − (NI + D&A + SBC) absorbs the change in working capital plus every other non-cash adjustment the filer discloses. Without it the operating section never reconciled from Net Income to Operating Cash Flow (gaps frequently exceeded OCF itself). `python -m scripts.audit_cashflow` now checks this bridge in addition to the bottom-line ΔCash identity.
- **Weighted-average share counts** use only directly-reported discrete quarters — the YTD-subtraction walk (FY − 9M = Q4) is disabled for them because share counts are not additive (it previously produced absurd values such as BKKT's −6.3M "Q4 diluted shares"). A separate EPS-consistency pass corrects clean order-of-magnitude unit errors (FMBM tagged 3,560M vs 3.55M actual) using the filer's own EPS as ground truth, while deliberately leaving reverse-split / minority-interest artifacts untouched.
- **Operating Expense** is derived as Gross Profit − Operating Income (avoids the inconsistent `OperatingExpenses` XBRL tag that double-counts COGS for many filers).
- **Operating Income** itself falls back to `Revenue − CostsAndExpenses` when the `OperatingIncomeLoss` tag is missing (filers like BH, STRZ that don't tag OpInc directly).
- **Pretax Income** is derived as Net Income + Tax (avoids the `*MinorityInterest*` Pretax variant that some filers sign-flip for losses).
- **Net Income** = `ProfitLoss` (includes NCI); a separate **Net Income to Common** row appears when NCI is meaningful (e.g., MKTW) so EPS × shares ≈ parent's NI is visibly consistent.
- **Total Equity** uses the including-NCI variant; a **Mezzanine Equity** row appears for redeemable preferred (e.g., BETR FY22) so Assets = Liabilities + Mezzanine + Total Equity holds.
- **Total Revenue** for banks/insurance uses `InterestAndDividendIncomeOperating + NoninterestIncome` as primary (otherwise BWFG-type filers would show just contract-customer fee income, wildly understating revenue).
- **Cost of Revenue** ladder covers the long tail of filer-specific tags: `CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization` (services, e.g., DLHC), `DirectOperatingCosts` (media/content licensing, e.g., STRZ), `CostOfRealEstateSales` (real estate, e.g., AXR), and a `CostDirectMaterial + CostDirectLabor` sum for restaurants (PTLO).
- **Gross Profit / Gross Margin % are mandatory and never blank.** A per-period derivation ladder fills GP for every filer, most-specific archetype first, then a universal floor:
  1. Reported `GrossProfit` tag, else Revenue − Cost of Revenue (CoR ladder below).
  2. **Services fallback** GP = OpInc + G&A (filers that report Operating Income and G&A but no CoR — PFHO, AXR, TOI, etc.). Gated *per period*: skipped when R&D ≥ 50% of revenue (genuine biotechs) or when the bank tag `NoninterestExpense` is present, and accepted only when the implied GP lands in [0, 1.05·Rev] so margin stays in a sane 0–100% band. The old gate was whole-statement `ResearchAndDevelopmentExpense in usg`, which wrongly blanked operators that merely tag a token R&D line (FONR — medical imaging, R&D = 1.5% of revenue, now correctly ~40%).
  3. **Bank Net Interest Income** GP = interest income − interest expense (`InterestAndDividendIncomeOperating` / `InterestIncomeOperating` / `InterestAndFeeIncomeLoansAndLeases`, minus `InterestExpense`). Excludes `InterestIncomeExpenseNet` so non-banks that carry a small net-interest line aren't mistaken for banks. Covers BCML, BWFG, CARE, CUBI, FMBM, NTB, PNBK, UBCP, AFCG, PFBX.
  4. **Insurance underwriting margin** GP = `PremiumsEarnedNet` − `PolicyholderBenefitsAndClaimsIncurredNet` (AII, KINS, UTGN).
  5. **Universal revenue floor** GP = Revenue (implied CoR = 0) for anything still missing — correct for pre-revenue clinical biotechs and early SaaS (ABSI, ZBIO, AUID, …), and the backstop that guarantees the row is never empty. A handful of large-revenue filers with non-standard cost tags (BKKT crypto, LEE media, PROP oil & gas annual) land here and show 100% — a known overstatement pending archetype-specific CoR synthesis.
- **Cost of Revenue reconciles to the gross block**: wherever a CoR value is shown it is rewritten to Rev − GP, so Rev − CoR = GP always holds on screen. Fixes filers (SLNH, GVH, MIMI) whose partial CoR tag disagreed with their authoritative reported GrossProfit subtotal. Banks/insurers/floor filers that report no CoR keep an empty row (no invented number).
- The bank interest-income tags are also in the **Total Revenue** ladder (incl. bare interest income as a last resort) so thrifts like PFBX that tag neither a standard revenue line nor `NoninterestIncome` still show a top line and therefore a margin.
- **Quarterly cadence guards**: the YTD walk (FY − 9M = Q4, etc.) dedupes restated XBRL facts by `(end, duration-band)` first and only emits a derived quarter when the implied subtraction span lands in the Q band (60–100d). Annual-only filers (SMWB, 20-F foreign) and semi-annual filers (UCAR, GVH — H1 + FY only) therefore collapse to "No quarterly data available" instead of fake year-end zeros or H2 totals mislabeled as Q4.
- **Quarterly YoY** compares each quarter to the same quarter one year prior (8-quarter internal buffer, 4 displayed).
- **Effect of FX on Cash** row in CF for foreign-operations filers.

Run `python -m scripts.audit_financials` for an end-to-end reconciliation pass (GP, OpInc, NI, A=L+M+E, share sanity). `python -m scripts.audit_cashflow` for CF reconciliation (ΔCash vs OCF+ICF+FCF+FX). `python -m scripts.probe_coverage` reports per-row coverage % across all stored tickers and ranks candidate XBRL tags that would unblock the most missing cells — re-run when a ticker's page looks empty to identify what tag to add to a ladder.

## Local dev

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m scripts.daily_run         # generate today's page
python -m scripts.backfill          # backfill from May 1, 2026
python -m scripts.refresh_financials # re-extract all companies' financials after extractor changes
python -m scripts.build_site        # render docs/ from data/
python -m scripts.probe_coverage    # coverage % per canonical row + candidate-tag backlog
```
