---
name: openclaw-cron-audit
description: Audit OpenClaw cron jobs for failures that stay silent — announce delivery that cannot resolve a recipient (channel "last" in an isolated session, Discord with no "channel:" target), error streaks nobody saw, output or failure alerts that were never delivered, bestEffort muting, jobs without failure alerts, overdue schedules and jobs that never ran. Use when the user asks why a cron job "does nothing", whether scheduled jobs are healthy, wants a cron health check or watchdog, or before re-enabling an old job.
metadata: {"openclaw": {"emoji": "🧭", "requires": {"bins": ["python3", "openclaw"]}}}
---

# OpenClaw Cron Audit

A cron job can fail on every run and look fine: its output goes to an unresolvable target,
its failure alert falls back to the same target, and `bestEffort` mutes the rest. This skill
finds those jobs from `openclaw cron list --all --json`. Python 3 standard library only.

## Run it

```bash
python3 {baseDir}/scripts/cron_audit.py --full      # every current finding, human-readable
python3 {baseDir}/scripts/cron_audit.py --json      # the same as JSON
python3 {baseDir}/scripts/cron_audit.py             # change report for a cron job (prints NO_REPLY when nothing changed)
```

`--full` and `--json` never write state, so they are safe to run any time. Add `--strict` to
exit 1 when an error-level finding exists.

## Reading findings

| Icon | Meaning |
|---|---|
| 🔴 | enabled job that fails or will fail silently — fix now |
| 🟡 | enabled job at risk (no failure alert, bestEffort, overdue, never ran, undelivered output) |
| ⚪ | disabled job with a problem — harmless until someone re-enables it |

Checks: `route`, `errors`, `delivery`, `alert-route`, `alert-failed`, `no-alert`, `best-effort`,
`overdue`, `never-ran`. See `README.md` for what each one means and why it is silent.

## Fixing what it finds

Pick the delivery mode by what the job does — do not blindly add `--announce`:

- The agent posts its own results (monitors, hunts, anything that must stay quiet when there is
  nothing to say): `openclaw cron edit <id> --no-deliver`. With `--announce` the runner ships the
  final turn text on every quiet cycle.
- The runner should deliver (digests, reports, reminders): pin it fully, e.g.
  `openclaw cron edit <id> --announce --channel discord --account <accountId> --to "channel:<id>" --no-best-effort-deliver`.
- Missing alert: `openclaw cron edit <id> --failure-alert --failure-alert-after 3 --failure-alert-channel <ch> --failure-alert-to <dest> --failure-alert-cooldown 12h`.

Ask the user before editing jobs. Never re-enable a ⚪ job without fixing its route first.

## Automatic repair (`--heal`)

`--heal` edits live jobs, so only use it when the user asked for it:

- repairs broken routes on **enabled** jobs from per-agent defaults in `~/.openclaw/cron-routes.json`
  (template: `{baseDir}/references/cron-routes.example.json`); repaired jobs are reported loudly so a
  human re-points them at the right thread;
- arms missing failure alerts when a destination is given with `--alert-channel` and `--alert-to`
  (or `CRON_AUDIT_ALERT_CHANNEL` / `CRON_AUDIT_ALERT_TO`, optional `--alert-account`).

Without that configuration `--heal` repairs nothing and only reports.

## Scheduling it as a watchdog

```bash
openclaw cron add --name cron-audit --cron "17 */6 * * *" --session isolated \
  --command "python3 {baseDir}/scripts/cron_audit.py --heal" --timeout-seconds 180 \
  --announce --channel discord --account <accountId> --to "channel:<id>"
```

It posts only when something appears, clears or gets healed, plus a weekly digest (which says
"all N cron jobs look healthy" when there is nothing to report, so silence never means the audit died).
If the gateway is unreachable it says so once and stays quiet until that changes.
