# insider-monitor Cron Droplet

The daily insider-monitor pipeline runs on the shared `aspancrm-claude` Digital Ocean droplet — the same host that runs the CRM `/health` check. Triggered by Linux cron at **6:30am ET Mon–Fri**.

This doc covers everything: what runs, where it lives, how it's authenticated, how to operate it, how to debug it, and the history of why we ended up here.

---

## TL;DR — what happens every weekday morning

```
06:30 ET — cron fires /home/cron-runner/run-insider.sh
06:30 — script git-pulls /opt/insider-monitor, dispatches daily.yml on GH Actions
06:30 → ~06:42 — script polls gh run view every 30s until workflow completes
            (daily.yml scrapes Form 4s, applies filters, refreshes XBRL data,
             fetches 10-Q/10-K footnote text for new survivors, commits + pushes)
06:42 — script git-pulls again to retrieve the workflow's commits
06:42 → ~06:48 — invokes `claude -p "/extract-options-warrants"` — Claude reads
            each new ticker's footnote text and fills in valuation.options /
            valuation.warrants by reasoning over filing language
06:48 — if the skill changed anything: rebuild site, commit + push
06:49 — log written to /home/cron-runner/logs/insider-YYYYMMDD-HHMM.log
~07:00 ET — Pages deploys; you read a complete page
```

Failure isolation: if the workflow fails, the skill is skipped but yesterday's page stays live. If the skill fails, the daily list still updates and only options/warrants for new survivors show `—`.

---

## Why a droplet, not GH Actions or Claude routines

We tried both before landing here:

- **GitHub Actions `schedule:` cron** fires "best effort" — typically on time, but in practice 1–2 hours late, occasionally skipped entirely after a workflow file change. Not acceptable when the requirement is "ready before I wake up at 7am ET."
- **Claude routines** run in a hardened sandbox: no `gh` CLI, no direct GitHub write access (`mcp__github__push_files` is gated behind a Copilot subscription we don't have), no access to `sec.gov`, no env vars / secrets. Workarounds break in subtle ways — one attempt base64-corrupted `daily.yml` while trying to dispatch a workflow.

A regular Linux droplet has none of these constraints. Cron fires within seconds of the scheduled time, `gh` works, `claude` runs non-interactively with an OAuth token, and ordinary `git push` over HTTPS Just Works.

The droplet costs $6/month and is shared with the CRM `/health` check, so the marginal cost of adding insider-monitor is effectively zero.

---

## Identifiers

| Field | Value |
|---|---|
| Droplet name | `aspancrm-claude` (shared with CRM `/health`) |
| Droplet ID | `567650856` |
| Public IP | `142.93.227.10` |
| Region / size | `ams3`, `s-1vcpu-1gb` ($6/mo, shared) |
| OS | Ubuntu 22.04 (kernel 5.15) |
| Timezone | `America/New_York` (cron uses local wall clock; DST handled automatically) |
| User running our work | `cron-runner` (non-root; Claude refuses `--dangerously-skip-permissions` as root) |

SSH access is via `root@142.93.227.10` for admin, then `sudo -u cron-runner -H ...` for everything else.

---

## What runs

The crontab entry as user `cron-runner`:

```
30 6 * * 1-5 /home/cron-runner/run-insider.sh >> /home/cron-runner/logs/cron.log 2>&1
```

That's it. One script, one cron entry. The droplet also runs `run-health.sh` (CRM, 08:00) and `run-clearsessions.sh` (CRM, 03:30) — see CRM `docs/CRON_DROPLET.md` for those.

Schedules are staggered so two Claude invocations never overlap (they share the same Max quota).

---

## Layout

| Path | Purpose |
|---|---|
| `/opt/insider-monitor` | Cloned repo, owned by `cron-runner` |
| `/opt/insider-monitor/venv` | Python 3.12 venv with `requirements.txt` installed |
| `/home/cron-runner/run-insider.sh` | The runner script (the only file unique to insider-monitor outside the cloned repo) |
| `/home/cron-runner/.shared.env` | `CLAUDE_CODE_OAUTH_TOKEN` only — sourced by every Claude-using runner on the droplet (mode 0600) |
| `/home/cron-runner/.insider-monitor.env` | insider-monitor-specific: `HC_PING_URL` for Healthchecks.io ping (mode 0600) |
| `/home/cron-runner/.config/gh/hosts.yml` | gh CLI auth state (fine-grained PAT scoped to `templargin/insider-monitor`) |
| `/home/cron-runner/logs/insider-YYYYMMDD-HHMM.log` | One log per pipeline run, 30-day retention |
| `/home/cron-runner/logs/cron.log` | All cron stdout/stderr (every job appends) |

CRM jobs on the same droplet use `.crm.env` + `.crm-prod.env` + `.shared.env`; see CRM `docs/CRON_DROPLET.md`. The old combined `.aspan-cron.env` and `.aspan-prod.env` were split into per-project files in May 2026.

---

## Authentication

### gh CLI

- Authed via a **fine-grained Personal Access Token** dedicated to this repo (not your personal session token).
- Scope: `templargin/insider-monitor` only.
- Permissions: Actions `read+write` (for `gh workflow run`), Contents `read+write` (for `git push`), Metadata `read`.
- Expiry: 1 year (calendar reminder for ~11 months out).
- The same token configures git's credential helper, so `git pull/push` over HTTPS works without prompts.
- File: `/home/cron-runner/.config/gh/hosts.yml` (chmod 0600).

To rotate / re-auth:

```bash
# 1. Generate a new fine-grained PAT at:
#    https://github.com/settings/personal-access-tokens
#    Same scope and permissions as above.

# 2. Pipe it to the droplet:
echo "<new_pat>" | ssh root@142.93.227.10 'cat > /tmp/.new_pat && chmod 600 /tmp/.new_pat'

# 3. Re-auth gh:
ssh root@142.93.227.10 'cat /tmp/.new_pat | sudo -u cron-runner -H gh auth login --with-token && sudo -u cron-runner -H gh auth setup-git && rm /tmp/.new_pat'

# 4. Verify:
ssh root@142.93.227.10 'sudo -u cron-runner -H gh auth status'
```

### Claude Code

- Long-lived OAuth token in `/home/cron-runner/.shared.env` as `CLAUDE_CODE_OAUTH_TOKEN`.
- Token TTL: ~1 year. Same token used by CRM `/health`. Next renewal: see CRM `docs/CRON_DROPLET.md`.
- Renewal command: `claude setup-token` on the Mac → approve in browser → copy the `sk-ant-oat...` value → `sed` it into the env file on the droplet.
- The runner script auto-exports it with `set -a; source ...; set +a` so the `claude` subprocess inherits it.

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

  # 6. Heartbeat to Healthchecks.io — confirms the pipeline reached the end.
  #    If this ping doesn't arrive within the check's grace, HC fires the
  #    Telegram webhook and you get an alert.
  curl -fsS -m 10 --retry 3 "$HC_PING_URL" >/dev/null && echo "HC ping ok" || echo "HC ping failed"
  echo "=== done $(date +%Y-%m-%dT%H:%M:%S) ==="
} >>"$LOG" 2>&1

# Prune logs older than 30 days
find "$LOGDIR" -name 'insider-*.log' -mtime +30 -delete 2>/dev/null
```

Key design choices:

- **`git reset --hard origin/main`** (not `git pull`) — guarantees a clean tree even if a previous run left uncommitted artifacts.
- **30s poll interval** with a 30-minute cap (60 × 30s). Workflow normally takes 8–12 min; the cap protects against an infinite wait if GH Actions hangs.
- **Skill failure tolerated** (`|| echo "(skill exited non-zero)"`) — daily list update is more important than dilution data; we don't fail the pipeline if one part errors.
- **Site rebuild gated on data change** — saves the deploy round-trip when there's nothing to push.
- **Single log file per run** — easy to grep, easy to prune.
- **HC ping at the very end** — guarantees we only report "alive" if every preceding step (workflow dispatch, skill run, optional commit) actually completed.

---

## Monitoring & alerting

A Healthchecks.io account monitors all 4 droplet workloads. Each check has a Telegram webhook attached pointing at `@templargin_droplet_alerts_bot` (or whatever the dedicated alerting bot is named) — when a check fails to ping within its grace window, HC fires a POST to Telegram and you get a message.

| Check | Schedule (cron-style) | Grace | What's monitored |
|---|---|---|---|
| `insider-monitor` | `30 6 * * 1-5` America/New_York | 30 min | This pipeline; ping at end of `run-insider.sh` |
| `crm-health` | `0 8 * * *` America/New_York | 30 min | CRM `/health` cron; ping at end of `run-health.sh` |
| `crm-clearsessions` | `30 3 * * *` America/New_York | 15 min | CRM session purge; ping at end of `run-clearsessions.sh` |
| `tuning-bot` | every 10 min (heartbeat) | 5 min | Tuning bot daemon; ping every ~4 min from inside its main loop |

The tuning-bot heartbeat is implemented in `crm/ai_agents/services/tuning_bot/bot.py::run()`. Env vars `TUNING_BOT_HEARTBEAT_URL` and `TUNING_BOT_HEARTBEAT_PERIOD` (default 240s) control it.

Channel & check IDs are managed through Healthchecks.io's web UI. The webhook channel POSTs to `https://api.telegram.org/bot<TOKEN>/sendMessage` with a JSON body referencing `$NAME` and `$NOW` HC template variables.

Test alerting end-to-end:

```bash
# Force one failure — Telegram alert lands within seconds
curl -fsS https://hc-ping.com/<CHECK_UUID>/fail

# Restore "up"
curl -fsS https://hc-ping.com/<CHECK_UUID>
```

---

## The /extract-options-warrants skill

Lives in the repo at `.claude/skills/extract-options-warrants/SKILL.md`. Invoked by the runner script.

What it does, step by step:

1. Glob `data/companies/*.json`. For each, parse and check if `valuation.options is None` OR `valuation.warrants is None`. Skip the rest.
2. For each ticker needing work, attempt to read `data/footnotes/TICKER.txt` (the file is created by the upstream `daily.yml` workflow via `scraper/footnotes.py`, which fetches the latest 10-Q/10-K HTML, strips it to text, and saves keyword-anchored sections).
3. Reason over the footnote text using the decision rules in SKILL.md:
   - Explicit count present ("X options outstanding" / "Y warrants outstanding") → use it.
   - Equity-plan footnote describes ONLY RSUs / restricted stock / PSUs with no options granted → `options = 0`.
   - Filing has NO warrants section anywhere AND no other mention of warrants → `warrants = 0`.
   - Section is truncated, ambiguous, or only shows a partial figure (e.g., one investor's holding) → leave `null`.
   - Genuinely unclear → leave `null`. **Never guess a specific number.**
4. Update the JSON files in place using Read + Edit. Preserve every other field exactly.
5. Print one line per ticker: `[TICKER] options=X warrants=Y` plus a summary line.

The skill is **read-only with respect to git** — the wrapper script handles commit + push. This keeps the skill simple and lets us swap in alternate commit paths (PR-based, batch, manual review) without changing the skill itself.

### Why this works well

Claude reasons cleanly over filing language. Sample output from a real run on May 13, 2026:

| Ticker | Decision | Why (paraphrased from skill's reasoning) |
|---|---|---|
| AERA | options=0, warrants=5,260,943 | 2026 Equity Plan adopted Mar 1 after period end; no prior options. Warrants count read from disclosure table. |
| LODE | options=0, warrants=2,756,970 | EPS note explicitly says dilutive equivalents are "limited to outstanding warrants" — confirms no options exist. |
| ONT | options=2,405,102 | Summed 2017 Plan (2,134,182) + 2013 Plan (270,920) from explicit activity rollforward tables. |
| MKTW | options=0, warrants=0 | Equity awards are RSUs and SARs only; EPS exclusion table lists only RSUs + SARs, no options or warrants. |
| NSPR | options=null, warrants=25,828,164 | Options bundled with warrants/RSUs/preferred in a 39.3M aggregate anti-dilutive count; can't isolate options. Warrants total stated explicitly elsewhere. |

The reasoning quality is meaningfully better than what a hand-rolled regex extraction would produce.

---

## Operational commands

### Manual pipeline run

```bash
ssh root@142.93.227.10 'sudo -u cron-runner -H /home/cron-runner/run-insider.sh'
```

Runs the full pipeline now. Takes ~13–15 min. Logs to a new file in `/home/cron-runner/logs/insider-*.log`.

### Test the skill alone (no workflow re-run)

```bash
ssh root@142.93.227.10 'sudo -u cron-runner -H bash -c "set -a; source /home/cron-runner/.shared.env; source /home/cron-runner/.insider-monitor.env; set +a; cd /opt/insider-monitor && git pull -q && claude --dangerously-skip-permissions -p \"/extract-options-warrants\""'
```

Useful when you just want to re-extract from existing footnote files without spending 10 minutes re-scraping EDGAR.

### Read the latest pipeline log

```bash
ssh root@142.93.227.10 'ls -t /home/cron-runner/logs/insider-*.log | head -1 | xargs cat'
```

### Tail the cron log (all jobs)

```bash
ssh root@142.93.227.10 'tail -f /home/cron-runner/logs/cron.log'
```

### View / edit the crontab

```bash
ssh root@142.93.227.10 'sudo -u cron-runner -H crontab -l'    # view
ssh root@142.93.227.10 'sudo -u cron-runner -H crontab -e'    # edit (vim)
```

### Pause / resume

Comment the line in `crontab -e`:

```
# 30 6 * * 1-5 /home/cron-runner/run-insider.sh >> /home/cron-runner/logs/cron.log 2>&1
```

Uncomment to resume.

### Deploy a code change

Push to `main` as usual. Next morning's run will `git pull` automatically. No droplet-side action needed.

If you change `/home/cron-runner/run-insider.sh` itself (not in the repo), update by scping or sshing in.

### Force-fix a stuck workflow

If the GH workflow hangs forever (rare), the script gives up after 30 min and proceeds. You can manually cancel the stuck run:

```bash
gh run cancel <RUN_ID> -R templargin/insider-monitor
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Log shows `Not logged in · Please run /login` after `claude -p ...` | Env file not auto-exporting or token expired | Confirm script has `set -a; source ...; set +a` block. If still failing, renew via `claude setup-token` on Mac and update env file. |
| `gh: command not found` | gh CLI removed from droplet | Re-install: `ssh root@142.93.227.10 'apt install -y gh'` |
| `gh workflow run` returns 401/403 | Token expired or revoked | Re-auth via the `gh auth login --with-token` flow in the Authentication section. |
| `git push` returns 403 | Same gh-token issue (git uses the same credential helper) | Same fix as above. |
| Workflow hangs > 30 min | GH Actions queue is backed up or run got stuck | Script gives up; manually inspect via `gh run view <id>`. Cancel + manually trigger if needed. |
| Skill outputs "Processed: 0 tickers" | No new survivors, OR all already filled, OR no footnote files present | Expected on quiet days. Check `data/footnotes/` for files. |
| Skill commits, but page still shows `—` | Pages deploy lag (1–5 min after push) | Wait + refresh. Verify deploy: `gh api repos/templargin/insider-monitor/pages/builds/latest --jq .status` |
| Script can't find `claude` | PATH issue under cron | The script uses bare `claude`; if path changes, hardcode `/usr/bin/claude`. |
| Cron doesn't fire at all | `cron` service stopped on droplet | `ssh root@142.93.227.10 'systemctl status cron'` then `systemctl start cron` |

---

## What lives where in the repo

| Path | Purpose |
|---|---|
| `.github/workflows/daily.yml` | The scrape/filter/build workflow. Still runs on GH Actions, dispatched by the droplet. Has a `schedule:` cron as a backup but the droplet is the primary trigger. |
| `.github/workflows/apply-extracts.yml` | `workflow_dispatch` endpoint used by the retired Claude extraction routine; kept as a manual fixup channel. |
| `.claude/skills/extract-options-warrants/SKILL.md` | The Claude skill the droplet invokes. |
| `scraper/footnotes.py` | Fetches latest 10-Q/10-K from EDGAR, strips HTML, extracts keyword-anchored sections to `data/footnotes/TICKER.txt`. Run during `daily.yml`. |
| `scraper/pipeline.py::update_company_data` | When rewriting a company JSON, **preserves existing `valuation.options` / `valuation.warrants` if XBRL returns null** — prevents the skill's hard-earned values from being clobbered by the next daily refresh. |
| `data/companies/TICKER.json` | Per-ticker data the skill reads and writes. |
| `data/footnotes/TICKER.txt` | Pre-fetched footnote text from the latest 10-Q/10-K. Source of truth for the skill. |

---

## What's been retired

History worth keeping for context (and so we don't go back):

- **Claude routine "insider-monitor daily trigger"** (`trig_01PvnrspdnbaZVyviSMsm1LR`) — *disabled*. Tried to fire `daily.yml` from a Claude routine; couldn't because routines lack `gh` and didn't have a viable workflow-dispatch path through the GitHub MCP.
- **Claude routine "insider-monitor options/warrants extraction"** (`trig_0151mfJNsAWAJH3mruSruB6a`) — *disabled*. Did work, but slowly (file-by-file commits via the GitHub MCP `push_files` API, which is itself rate-limited). Replaced by the droplet skill which uses normal `git push`.
- **GH Actions `schedule:` cron on `daily.yml`** — *still active as backup*. Fires `30 10 * * 1-5` UTC (= 6:30am EDT / 5:30am EST). The droplet's pre-flight `git diff --staged --quiet` makes the second daily fire a no-op when the droplet already ran. Belt and suspenders.

---

## Setting up from scratch (recipe)

If we ever need to rebuild this on a different droplet:

1. Install dependencies:
   ```bash
   apt update && apt install -y git python3 python3-venv gh
   curl -fsSL https://claude.ai/install.sh | bash   # or equivalent
   ```
2. Create user + auth:
   ```bash
   useradd -m -s /bin/bash cron-runner
   # Pipe in your GH token:
   echo "$GH_TOKEN" | sudo -u cron-runner -H gh auth login --with-token
   sudo -u cron-runner -H gh auth setup-git
   ```
3. Clone repo:
   ```bash
   mkdir -p /opt/insider-monitor
   chown cron-runner:cron-runner /opt/insider-monitor
   sudo -u cron-runner -H git clone https://github.com/templargin/insider-monitor.git /opt/insider-monitor
   sudo -u cron-runner -H bash -c "cd /opt/insider-monitor && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
   sudo -u cron-runner -H git -C /opt/insider-monitor config user.name "insider-monitor[bot]"
   sudo -u cron-runner -H git -C /opt/insider-monitor config user.email "actions@users.noreply.github.com"
   ```
4. Write the env files:
   ```bash
   # Shared (used by every Claude-running job)
   sudo -u cron-runner tee /home/cron-runner/.shared.env <<EOF
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-...
   EOF

   # insider-monitor-specific
   sudo -u cron-runner tee /home/cron-runner/.insider-monitor.env <<EOF
   HC_PING_URL=https://hc-ping.com/<your-check-uuid>
   EOF

   chmod 600 /home/cron-runner/.shared.env /home/cron-runner/.insider-monitor.env
   ```
5. Copy `run-insider.sh` (full source above) into `/home/cron-runner/run-insider.sh`, chmod 755, chown cron-runner.
6. Add the crontab line:
   ```bash
   sudo -u cron-runner -H bash -c "(crontab -l 2>/dev/null; echo '30 6 * * 1-5 /home/cron-runner/run-insider.sh >> /home/cron-runner/logs/cron.log 2>&1') | crontab -"
   ```
7. Test manually: `sudo -u cron-runner -H /home/cron-runner/run-insider.sh`

---

## Limitations and known gaps

- **Dead-man's switch in place (May 2026).** Healthchecks.io monitors all 4 droplet workloads; missed pings fire a Telegram webhook → `@templargin_droplet_alerts_bot` → your DM. See "Monitoring & alerting" above.
- **`gh` token is your personal account.** Rotating it (e.g., revoking from claude.ai web UI) invalidates the droplet copy. Workaround: re-pipe the new token. Better long-term: a fine-grained PAT scoped to `templargin/insider-monitor` with `actions:write` + `contents:write` (5-minute setup, single-purpose, can be rotated without affecting your Mac).
- **Single point of failure.** Droplet down → no morning page. GH Actions `schedule:` cron is a partial backup but fires late. Acceptable risk for a personal tool.
- **Skill is conservative.** Some footnotes are genuinely ambiguous and the skill leaves them `null`. You can manually edit a `data/companies/TICKER.json` and push if you want to override.
- **Workflow / skill share Max quota.** The skill costs maybe ~50K tokens per ticker × 10 tickers/day = ~500K/day. CRM `/health` runs at 8am and uses another ~100K. Well within the Max plan's daily budget.

---

## Decommission

If shutting insider-monitor down entirely (leaves CRM `/health` running on the same droplet):

```bash
# 1. Remove the cron entry (preserve CRM ones)
ssh root@142.93.227.10 'sudo -u cron-runner -H bash -c "crontab -l | grep -v run-insider.sh | crontab -"'

# 2. Remove the runner + clone
ssh root@142.93.227.10 'rm /home/cron-runner/run-insider.sh && rm -rf /opt/insider-monitor'

# 3. Disable / archive the repo on GitHub (optional)
```

Don't shut down the droplet — it's shared with CRM.
