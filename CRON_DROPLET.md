# insider-monitor on the droplet

This file documents how **insider-monitor specifically** runs on the shared `aspancrm-claude` droplet. For host-level operations — authentication, env file conventions, monitoring, adding/removing workloads, setup-from-scratch, troubleshooting — see the canonical droplet docs at **[templargin/droplet](https://github.com/templargin/droplet)**.

---

## TL;DR — what happens every weekday morning

```
06:30 ET — cron fires /home/cron-runner/run-insider.sh
06:30 — git-pulls /opt/insider-monitor, dispatches daily.yml on GH Actions
06:30 → ~06:42 — polls gh run view every 30s until workflow completes
            (daily.yml scrapes EDGAR, applies filters, refreshes XBRL data,
             fetches 10-Q/10-K footnote text for new survivors, commits + pushes)
06:42 — git-pulls again to retrieve the workflow's commits
06:42 → ~06:48 — invokes `claude -p "/extract-options-warrants"` — Claude reads
            each new ticker's footnote text and fills in valuation.options /
            valuation.warrants by reasoning over filing language
06:48 — if the skill changed anything: rebuild site, commit + push
06:48 — HC ping fires
~07:00 ET — Pages deploys; you read a complete page
```

If the workflow fails, the skill is skipped but yesterday's page stays live. If the skill fails, the daily list still updates and only options/warrants for new survivors show `—`. If the whole script doesn't finish, the HC ping doesn't arrive within the 30-min grace and Telegram alerts you.

---

## What runs

Single crontab entry (as `cron-runner`):

```
30 6 * * 1-5 /home/cron-runner/run-insider.sh >> /home/cron-runner/logs/cron.log 2>&1
```

---

## The runner script

`/home/cron-runner/run-insider.sh` — full source:

```bash
#!/usr/bin/env bash
set -uo pipefail
ulimit -n 4096

REPO=/opt/insider-monitor
LOGDIR=/home/cron-runner/logs
mkdir -p "$LOGDIR"
TS=$(date +%Y%m%d-%H%M)
LOG="$LOGDIR/insider-$TS.log"

# Auto-export env so claude inherits CLAUDE_CODE_OAUTH_TOKEN + HC_PING_URL
set -a
source /home/cron-runner/.shared.env
source /home/cron-runner/.insider-monitor.env
set +a

cd "$REPO"

{
  echo "=== run-insider $TS ==="

  # 1. Sync repo
  git fetch -q origin
  git reset -q --hard origin/main

  # 2. Dispatch daily.yml + wait
  gh workflow run "Daily insider monitor" -R templargin/insider-monitor
  sleep 5
  RUN_ID=$(gh run list -R templargin/insider-monitor --workflow="Daily insider monitor" --limit 1 --json databaseId --jq '.[0].databaseId')
  for i in $(seq 1 60); do
    sleep 30
    STATUS=$(gh run view "$RUN_ID" -R templargin/insider-monitor --json status,conclusion --jq '"\(.status) \(.conclusion // "—")"')
    [[ "$STATUS" == completed* ]] && break
  done

  # 3. Re-pull (workflow committed new data + footnotes)
  git fetch -q origin
  git reset -q --hard origin/main

  # 4. Run extraction skill — Claude reasons over footnote text, fills in JSONs
  claude --dangerously-skip-permissions -p "/extract-options-warrants" 2>&1 || echo "(skill exited non-zero)"

  # 5. If skill modified data: rebuild + commit + push
  if ! git diff --quiet --exit-code data/companies/ 2>/dev/null; then
    ./venv/bin/python -m scripts.build_site
    git add data/companies/ docs/
    if ! git diff --staged --quiet; then
      git commit -m "Backfill options/warrants from footnotes ($TS)"
      git push origin main
    fi
  fi

  # 6. Heartbeat to Healthchecks.io. Success ping = healthy; /fail ping fires
  # the Telegram webhook immediately. CONCLUSION is anything but "success" on
  # workflow failure, cancellation, or wait-loop timeout.
  if [[ "$CONCLUSION" == "success" ]]; then
    curl -fsS -m 10 --retry 3 "$HC_PING_URL" >/dev/null && echo "HC ping ok" || echo "HC ping failed"
  else
    curl -fsS -m 10 --retry 3 "$HC_PING_URL/fail" >/dev/null && echo "HC fail ping sent (workflow: $CONCLUSION)" || echo "HC fail ping failed"
  fi
  echo "=== done $(date +%Y-%m-%dT%H:%M:%S) ==="
} >>"$LOG" 2>&1

# Prune logs older than 30 days
find "$LOGDIR" -name 'insider-*.log' -mtime +30 -delete 2>/dev/null
```

Key design choices:

- **`git reset --hard origin/main`** (not `git pull`) — guarantees a clean tree even if a previous run left artifacts.
- **30s poll interval** with a 30-minute cap. Workflow normally takes 8–12 min.
- **Skill failure tolerated** — daily list update is more important than dilution data.
- **Site rebuild gated on data change** — saves the deploy round-trip when there's nothing to push.
- **HC ping at the very end, conditional on workflow conclusion** (since 2026-07-09) — a plain ping only when the GH workflow succeeded; otherwise a `/fail` ping so the Telegram alert fires immediately. Previously the ping was unconditional, which made a failed workflow invisible to monitoring (discovered when the 2026-07-09 run failed silently during a GitHub hosted-runner outage). A missed ping still covers "script crashed or hung."

---

## The /extract-options-warrants skill

Lives in the repo at `.claude/skills/extract-options-warrants/SKILL.md`. Invoked by the runner script.

For every `data/companies/TICKER.json` where `valuation.options is null` OR `valuation.warrants is null`:

1. Read the corresponding `data/footnotes/TICKER.txt` (pre-fetched by the daily workflow via `scraper/footnotes.py`).
2. Reason over the footnote text to find total OUTSTANDING options + warrants as of the report date.
3. Decision rules:
   - Explicit count → use it.
   - RSU-only plan / no options granted → `options = 0`.
   - No warrants section anywhere → `warrants = 0`.
   - Ambiguous, truncated, or partial figure → leave `null`.
   - Never guess a specific number.
4. Update the JSON in place. The wrapper script handles commit + push.

Sample reasoning from a real run:

| Ticker | Decision | Why |
|---|---|---|
| AERA | options=0, warrants=5,260,943 | 2026 Equity Plan adopted Mar 1 after period end; no prior options. Warrants count read from disclosure table. |
| LODE | options=0, warrants=2,756,970 | EPS note explicitly says dilutive equivalents are "limited to outstanding warrants" — confirms no options exist. |
| ONT | options=2,405,102 | Summed 2017 Plan (2,134,182) + 2013 Plan (270,920) from explicit activity rollforward tables. |
| MKTW | options=0, warrants=0 | Equity awards are RSUs and SARs only; EPS exclusion lists only RSUs + SARs. |
| NSPR | options=null, warrants=25,828,164 | Options bundled with warrants/RSUs/preferred in a 39.3M aggregate anti-dilutive count; can't isolate options. Warrants total stated explicitly elsewhere. |

---

## Env files used

- `/home/cron-runner/.shared.env` — `CLAUDE_CODE_OAUTH_TOKEN`
- `/home/cron-runner/.insider-monitor.env` — `HC_PING_URL`

See the [canonical droplet doc](https://github.com/templargin/droplet) for the per-project env split convention.

---

## What lives where in the repo

| Path | Purpose |
|---|---|
| `.github/workflows/daily.yml` | The scrape/filter/build workflow. Dispatched by the droplet. Has a `schedule:` cron as a fallback but the droplet is the primary trigger. |
| `.github/workflows/apply-extracts.yml` | `workflow_dispatch` endpoint kept around as a manual fixup channel. |
| `.claude/skills/extract-options-warrants/SKILL.md` | The Claude skill the droplet invokes. |
| `scraper/footnotes.py` | Fetches latest 10-Q/10-K from EDGAR, strips HTML, extracts keyword-anchored sections to `data/footnotes/TICKER.txt`. Run during `daily.yml`. |
| `scraper/pipeline.py::update_company_data` | Preserves existing `valuation.options` / `valuation.warrants` if XBRL returns null — prevents the skill's extracted values from being clobbered by the next daily refresh. |
| `data/companies/TICKER.json` | Per-ticker data the skill reads and writes. |
| `data/footnotes/TICKER.txt` | Pre-fetched footnote text from the latest 10-Q/10-K. Source of truth for the skill. |

---

## What's been retired

History worth keeping for context:

- **Claude routine "insider-monitor daily trigger"** — disabled. Tried to dispatch `daily.yml` from a Claude routine; failed because the routine sandbox lacks `gh` and a viable direct-API auth path.
- **Claude routine "insider-monitor options/warrants extraction"** — disabled. Worked but slowly (file-by-file commits via GitHub MCP). Replaced by the droplet skill which uses normal `git push`.
- **GH Actions `schedule:` cron on `daily.yml`** — still active as a fallback. Fires `30 10 * * 1-5` UTC (= 6:30am EDT / 5:30am EST). The droplet's idempotent `git diff` guard makes the second daily fire a no-op when the droplet already ran. Belt and suspenders. **Caveat:** the fallback re-runs the *full* screen, so its result can differ from the droplet's — and a transient SEC/price-fetch outage on the fallback used to overwrite the droplet's good page with an empty one (this is what blanked 2026-06-19's PRTA + GOTU). `process_bucket` now refuses to write on a mass `DataUnavailable` outage and never downgrades a non-empty page to empty (see *Page-write safety* in the README), so the fallback can no longer regress a good page.

---

## Project-specific operational commands

```bash
# Manual full pipeline run (~13 min)
ssh root@142.93.227.10 'sudo -u cron-runner -H /home/cron-runner/run-insider.sh'

# Skill-only test (no workflow re-run; ~5 min)
ssh root@142.93.227.10 'sudo -u cron-runner -H bash -c "set -a; source /home/cron-runner/.shared.env; source /home/cron-runner/.insider-monitor.env; set +a; cd /opt/insider-monitor && git pull -q && claude --dangerously-skip-permissions -p \"/extract-options-warrants\""'

# Latest pipeline log
ssh root@142.93.227.10 'ls -t /home/cron-runner/logs/insider-*.log | head -1 | xargs cat'
```

For host-level ops (gh auth rotation, env file management, adding new workloads, etc.), see [templargin/droplet](https://github.com/templargin/droplet).

---

## Project-specific failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Daily list page is from yesterday | Workflow never finished, OR HC ping didn't fire | Check latest `insider-*.log`; manually re-trigger via `gh workflow run` |
| Daily page suddenly empty / lost its tickers | A run hit a transient SEC/price-fetch outage (the cloud-IP fallback run is most prone) | Guarded since 2026-06: the writer skips on a mass outage and won't downgrade a non-empty page. Grep the run log for `upstream outage; skipping write` or `data unavailable`. To rebuild a page that was lost before the guard existed, restore the good `data/insiders/YYYY-MM-DD.json` (from git history) or re-run `process_bucket` for that date, then `build_site` + push |
| Monday-after-holiday page is empty (HTTP 200) | Bucket is entirely weekend + federal holiday (e.g. the Monday after Juneteenth — Fri+Sat+Sun) | Expected — explicit empty page by design (`buckets.is_trading_day`) |
| Survivor's page shows `—` for options/warrants | Skill was conservative (ambiguous footnote) OR footnote file is missing | Inspect `data/footnotes/TICKER.txt`; manually edit JSON + push if you can determine the value |
| Workflow fires but skill output is empty | All tickers already have non-null options/warrants — expected |
| Skill commits but page doesn't refresh | GH Pages deploy lag (1–5 min) | Wait and refresh |

For host-level troubleshooting (gh broken, claude not authed, env files, etc.), see [templargin/droplet](https://github.com/templargin/droplet#troubleshooting).
