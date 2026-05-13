---
name: extract-options-warrants
description: Scan data/companies/*.json for tickers missing options or warrants. Read pre-extracted footnote text from data/footnotes/TICKER.txt and fill in the numbers. Used by the insider-monitor cron pipeline on the aspancrm-claude droplet.
---

# extract-options-warrants

Working directory is the `insider-monitor` repo root (`/opt/insider-monitor` on the droplet).

## Task

For every `data/companies/TICKER.json` where `valuation.options` is `null` OR `valuation.warrants` is `null`:

1. Read the corresponding `data/footnotes/TICKER.txt` file. If it doesn't exist, skip this ticker.
2. The footnote file contains keyword-anchored sections from the company's latest 10-Q or 10-K. The first line is a `# Source:` header noting the filing URL and period.
3. Find the latest end-of-period counts:
   - **Stock options outstanding** — total OUTSTANDING (not just exercisable, not just vested).
   - **Warrants outstanding** — total OUTSTANDING.
4. Update the JSON in place (preserve all other fields).

## Decision rules — be decisive, not paranoid

- Explicit number ("the Company had X options outstanding") → use that integer.
- Equity-plan footnote describes ONLY RSUs / restricted stock / PSUs / time-vesting awards with no options granted → `options = 0`.
- No warrants section anywhere in the file AND no other mention of warrants outstanding → `warrants = 0`. (Most non-SPAC small caps genuinely have zero warrants.)
- Section is truncated or you only see a partial figure (e.g., one investor's holding) → leave as `null`.
- Genuinely unclear → leave as `null`. **Never guess a specific number.**

## How to update the JSON

Read the file, parse with `json.loads`, set `data["valuation"]["options"]` and `data["valuation"]["warrants"]`, write back with `json.dumps(d, indent=2, default=str)`. Preserve every other field exactly.

## Output

Print one line per ticker: `[TICKER] options=X warrants=Y` (use `null` for unset, `0` for explicit zero).

End with a one-line summary:
```
Processed: N tickers — applied options for X, applied warrants for Y, left null for Z.
```

That's it. The wrapping cron script handles git pull/push, site rebuild, and logging.
