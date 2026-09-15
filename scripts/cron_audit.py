#!/usr/bin/env python3
"""openclaw-cron-audit — find OpenClaw cron jobs that are failing without telling anyone.

A cron job can fail on every run and still look fine: its output goes nowhere, its
failure alert goes to the same nowhere, and `bestEffort` mutes what is left. This
audits every job for exactly those failure modes:

  route         announce delivery that cannot resolve a recipient
                (delivery.channel "last" in an isolated session, or Discord/Slack with no `to`)
  errors        consecutive run errors nobody looked at
  delivery      the run succeeded but its output was not delivered
  alert-route   a failure alert that points at a channel with no recipient
  alert-failed  the failure alert itself could not be delivered
  no-alert      no failure alert configured on the job
  best-effort   bestEffort=true, which suppresses the failure alert entirely
  overdue       the scheduler stopped firing the job
  never-ran     recurring job enabled for a week that has never run

It reports CHANGES, not state. The last report is fingerprinted in a state file, so a
known problem is announced once, not every run. New and resolved findings are called
out, and the full picture is re-posted as a digest every --digest-hours. When nothing
changed it prints NO_REPLY, which an OpenClaw command-payload cron job suppresses.

Modes:
  (default)   change report for a cron job; updates the state file
  --full      print every current finding; state untouched
  --json      machine-readable findings; state untouched
  --heal      also repair what can be repaired safely (see README)
  --strict    exit 1 when any error-level finding exists
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, time

HOUR_MS = 3600_000
NO_REPLY = "NO_REPLY"
SEVERITIES = ("err", "warn", "note")

# Channels whose announce target must name an explicit recipient.
NEEDS_RECIPIENT = {"discord", "slack", "mattermost", "matrix"}

KIND_LABEL = {
    "route": "broken delivery route", "errors": "failing runs", "delivery": "undelivered output",
    "alert-route": "broken failure-alert route", "alert-failed": "undelivered failure alert",
    "no-alert": "missing failure alert", "best-effort": "bestEffort mute", "overdue": "overdue schedule",
    "never-ran": "never ran", "truncated": "incomplete job list", "blind": "audit could not run",
}


def short_hash(text):
    return hashlib.sha1(text.encode()).hexdigest()[:8]


def finding(key, sev, text, fix=None, fixed_text=None, label=None):
    return {"key": key, "sev": sev, "text": text, "fix": fix, "fixed_text": fixed_text, "label": label}


# --------------------------------------------------------------------------- checks

def route_problem(delivery):
    """Why an announce delivery cannot resolve a recipient, or None."""
    delivery = delivery or {}
    if delivery.get("mode") != "announce":
        return None
    ch = delivery.get("channel")
    if not ch or ch == "last":
        return 'delivery.channel is "last" or unset, and an isolated cron session has no last channel'
    return recipient_problem(ch, delivery.get("to"), "delivery")


def recipient_problem(channel, to, field):
    if channel in NEEDS_RECIPIENT and not to:
        return f"{field}.channel={channel} but {field}.to is unset, so there is no recipient"
    # Discord rejects a bare id: "Use "channel:<id>" for channels or "user:<id>" for DMs".
    if channel == "discord" and to and not str(to).startswith(("channel:", "user:")):
        return f'{field}.to "{to}" has no "channel:" or "user:" prefix, which Discord rejects'
    return None


def alert_route_problem(alert):
    """Why a per-job failure alert cannot be delivered, or None. An unset channel falls
    back to the job's own delivery, which route_problem already covers."""
    alert = alert or {}
    ch = alert.get("channel")
    if ch == "last":
        return 'failureAlert.channel is "last", which an isolated cron session cannot resolve'
    return recipient_problem(ch, alert.get("to"), "failureAlert")


def audit(jobs, now, routes=None, alert_args=None, never_ran_days=7, overdue_hours=1):
    """Returns a list of findings. Pure: reads job records, runs nothing.

    A finding carries `fix` (argv for `openclaw cron ...`) when --heal could repair it.
    """
    routes = routes or {}
    out = []
    for j in jobs:
        jid = str(j.get("id") or "?")
        name = j.get("name") or j.get("displayName") or "?"
        agent = j.get("agentId") or j.get("effectiveAgentId") or "-"
        st = j.get("state") or {}
        enabled = j.get("enabled", True)
        tag = f"`{name}` ({agent}, {jid[:8]})"
        # A finding on a disabled job cannot fire, so it is a note, not an error.
        sev = "err" if enabled else "note"
        icon = "🔴" if enabled else "⚪"

        def add(kind, severity, text, suffix="", fix=None, fixed_text=None):
            out.append(finding(f"{jid}:{kind}{suffix}", severity, text, fix, fixed_text, tag))

        problem = route_problem(j.get("delivery"))
        if problem and not enabled:
            add("route", "note", f"⚪ {tag} — disabled, route broken ({problem}); fix before re-enabling")
        elif problem:
            r = routes.get(agent)
            fix = fixed = None
            if r and r.get("channel") and r.get("to"):
                fix = ["edit", jid, "--announce", "--channel", r["channel"], "--to", r["to"],
                       "--no-best-effort-deliver"]
                if r.get("accountId"):
                    fix += ["--account", r["accountId"]]
                fixed = (f"🔧 {tag} — auto-routed to {r['channel']}:{r['to']} (agent default). "
                         f"Was: {problem}. **Re-point it at the right thread.**")
            add("route", "err", f"🔴 {tag} — fails on every run: {problem}", fix=fix, fixed_text=fixed)

        n = st.get("consecutiveErrors") or 0
        if n:
            last = (st.get("lastError") or "?")[:180]
            # Key on the error text, not the count: a climbing counter is not "new" every
            # run, but a different failure is.
            add("errors", sev, f"{icon} {tag} — {n} consecutive failed run(s): {last}", ":" + short_hash(last))

        # A run that printed NO_REPLY is "not-delivered" on purpose; the gateway records why.
        suppressed = j.get("deliverySuppressionReason") or st.get("deliverySuppressionReason")
        if (enabled and not suppressed and st.get("lastRunStatus") == "ok"
                and st.get("lastDeliveryStatus") == "not-delivered"):
            err = (st.get("lastDeliveryError") or "no error recorded")[:180]
            add("delivery", "warn", f"🟡 {tag} — last run succeeded but its output was not delivered: {err}",
                ":" + short_hash(err))

        alert = j.get("failureAlert")
        aproblem = alert_route_problem(alert)
        if aproblem:
            add("alert-route", sev, f"{icon} {tag} — failure alert cannot be delivered: {aproblem}")

        if enabled and st.get("lastFailureNotificationDeliveryStatus") == "not-delivered":
            err = (st.get("lastFailureNotificationDeliveryError") or "no error recorded")[:180]
            add("alert-failed", "err", f"🔴 {tag} — its last failure alert was not delivered: {err}",
                ":" + short_hash(err))

        if enabled and not alert:
            fix = ["edit", jid, *alert_args] if alert_args else None
            add("no-alert", "warn", f"🟡 {tag} — no failure alert configured",
                fix=fix, fixed_text=f"🔧 {tag} — had no failure alert; armed it")

        if enabled and (j.get("delivery") or {}).get("bestEffort") is True:
            add("best-effort", "warn", f"🟡 {tag} — bestEffort=true suppresses its failure alert; "
                                       f"it can die silently")

        if enabled:
            nxt = j.get("nextRunAtMs", st.get("nextRunAtMs"))
            if isinstance(nxt, int) and nxt < now - overdue_hours * HOUR_MS:
                add("overdue", "warn", f"🟡 {tag} — overdue by {(now - nxt) // HOUR_MS}h; "
                                       f"the scheduler is not firing it")
            recurring = (j.get("schedule") or {}).get("kind") in ("cron", "every")
            ran = st.get("lastRunAtMs") or j.get("lastRunAtMs")
            if recurring and not ran and j.get("createdAtMs", now) < now - never_ran_days * 24 * HOUR_MS:
                add("never-ran", "warn", f"🟡 {tag} — enabled for over {never_ran_days} days and has never run")
    return out


# --------------------------------------------------------------------------- report

def render(found, healed, prev, now, total_jobs, digest_hours=168, hint=None):
    """Builds the change report. Returns (text, new_state). Pure.

    `prev` is the last state: {"keys": [...], "labels": {key: tag}, "lastReportMs": ms}.
    """
    by_key = {f["key"]: f for f in found}
    labels = dict(prev.get("labels") or {})
    labels.update({k: f["label"] for k, f in by_key.items() if f.get("label")})
    prev_keys = set(prev.get("keys") or [])
    keys = set(by_key)
    new_keys, gone_keys = keys - prev_keys, prev_keys - keys
    digest = now - int(prev.get("lastReportMs") or 0) > digest_hours * HOUR_MS

    state = {"keys": sorted(keys), "labels": {k: labels[k] for k in keys if k in labels},
             "lastReportMs": int(prev.get("lastReportMs") or 0)}

    # Nothing healed, appeared or cleared, and no digest due: stay quiet.
    if not healed and not new_keys and not gone_keys and not digest:
        return NO_REPLY, state

    state["lastReportMs"] = now
    lines = ["**🧭 Cron audit**"]
    if healed:
        lines += ["", "__Healed__"] + healed
    if digest:
        # A digest prints the whole picture once; it does not also repeat it as "New".
        new_keys, gone_keys = set(), set()
    if new_keys:
        lines += ["", "__New__"]
        lines += [by_key[k]["text"] for sev in SEVERITIES for k in sorted(new_keys) if by_key[k]["sev"] == sev]
    if gone_keys:
        lines += ["", "__Resolved__"]
        for k in sorted(gone_keys):
            kind = k.split(":")[1] if ":" in k else k
            who = labels.get(k) or f"`{k.split(':')[0][:8]}`"
            lines.append(f"✅ {who} — {KIND_LABEL.get(kind, kind)} cleared")
    if digest and keys:
        lines += ["", f"__Still open ({len(keys)})__ — digest"]
        lines += [f["text"] for sev in SEVERITIES for f in sorted(found, key=lambda f: f["key"]) if f["sev"] == sev]
    elif digest and not healed:
        lines += ["", f"✅ Digest: all {total_jobs} cron jobs look healthy."]
    elif keys:
        counts = {s: sum(1 for f in found if f["sev"] == s) for s in SEVERITIES}
        tail = f"_Still open: {counts['err']} errors, {counts['warn']} warnings, {counts['note']} notes on disabled jobs."
        lines += ["", tail + (f" Full list: `{hint}`_" if hint else "_")]
    return "\n".join(lines), state


def render_full(found, total_jobs):
    if not found:
        return f"✅ All {total_jobs} cron jobs look healthy."
    lines = [f"**🧭 Cron audit** — {len(found)} finding(s) across {total_jobs} jobs"]
    for sev, title in (("err", "Errors"), ("warn", "Warnings"), ("note", "Disabled jobs")):
        group = [f["text"] for f in sorted(found, key=lambda f: f["key"]) if f["sev"] == sev]
        if group:
            lines += ["", f"__{title}__"] + group
    return "\n".join(lines)


# --------------------------------------------------------------------------- I/O

def read_state(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"keys": [], "labels": {}, "lastReportMs": 0}


def write_state(path, state):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(state, fh)
    except OSError:
        pass  # an audit that cannot persist still reports; it just repeats itself


def find_openclaw(explicit):
    # Cron command payloads run under `sh -lc` with a PATH that often lacks the npm
    # global bin dir, so do not trust PATH alone.
    return (explicit or os.environ.get("OPENCLAW_BIN") or shutil.which("openclaw")
            or os.path.expanduser("~/.npm-global/bin/openclaw"))


class Blind(Exception):
    """The audit could not see the job list."""


def load_jobs(openclaw, input_path=None):
    """Returns (jobs, extra_findings)."""
    if input_path:
        with open(input_path) as fh:
            data = json.load(fh)
    else:
        try:
            r = subprocess.run([openclaw, "cron", "list", "--all", "--json"],
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            raise Blind(f"`{openclaw}` is not usable ({e})")
        if r.returncode != 0:
            raise Blind(f"`cron list` exit {r.returncode}\n{r.stderr.strip()[:400]}")
        try:
            data = json.loads(r.stdout)
        except ValueError:
            raise Blind(f"`cron list` returned non-JSON\n{r.stdout.strip()[:300]}")
    if isinstance(data, list):
        return data, []
    jobs = data.get("jobs") or []
    extra = []
    if data.get("hasMore"):
        # `cron list` pages (limit 200 on 2026.9.x) and the CLI has no --offset.
        extra.append(finding("audit:truncated", "err",
                             f"🔴 Audit saw only {len(jobs)} of {data.get('total', '?')} jobs; "
                             f"the rest were not checked", label="`cron list`"))
    return jobs, extra


def load_routes(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path) as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}


def alert_args_from(ns):
    if not (ns.alert_channel and ns.alert_to):
        return None
    args = ["--failure-alert", "--failure-alert-after", str(ns.alert_after),
            "--failure-alert-channel", ns.alert_channel, "--failure-alert-to", ns.alert_to,
            "--failure-alert-cooldown", ns.alert_cooldown, "--failure-alert-exclude-skipped"]
    if ns.alert_account:
        args += ["--failure-alert-account-id", ns.alert_account]
    return args


def parse_args(argv):
    home = os.environ.get("OPENCLAW_STATE_DIR") or os.path.expanduser("~/.openclaw")
    env = os.environ.get
    p = argparse.ArgumentParser(description="Audit OpenClaw cron jobs for silent failures.")
    p.add_argument("--full", action="store_true", help="print every current finding; state untouched")
    p.add_argument("--json", action="store_true", help="print findings as JSON; state untouched")
    p.add_argument("--heal", action="store_true", help="repair broken routes and arm missing failure alerts")
    p.add_argument("--strict", action="store_true", help="exit 1 when any error-level finding exists")
    p.add_argument("--openclaw", help="openclaw binary (default: $OPENCLAW_BIN, PATH, ~/.npm-global/bin)")
    p.add_argument("--input", help="read `cron list --all --json` output from a file instead of the gateway")
    p.add_argument("--state", default=env("CRON_AUDIT_STATE", os.path.join(home, "state", "cron-audit.json")))
    p.add_argument("--routes", default=env("CRON_AUDIT_ROUTES", os.path.join(home, "cron-routes.json")),
                   help="per-agent default delivery routes used by --heal")
    p.add_argument("--alert-channel", default=env("CRON_AUDIT_ALERT_CHANNEL"))
    p.add_argument("--alert-to", default=env("CRON_AUDIT_ALERT_TO"))
    p.add_argument("--alert-account", default=env("CRON_AUDIT_ALERT_ACCOUNT"))
    p.add_argument("--alert-after", type=int, default=int(env("CRON_AUDIT_ALERT_AFTER", "3")))
    p.add_argument("--alert-cooldown", default=env("CRON_AUDIT_ALERT_COOLDOWN", "12h"))
    p.add_argument("--digest-hours", type=float, default=168)
    p.add_argument("--never-ran-days", type=float, default=7)
    p.add_argument("--overdue-hours", type=float, default=1)
    ns = p.parse_args(argv)
    if ns.heal and ns.input:
        p.error("--heal edits live jobs and cannot be combined with --input")
    return ns


def main(argv=None):
    ns = parse_args(sys.argv[1:] if argv is None else argv)
    openclaw = find_openclaw(ns.openclaw)
    now = int(time.time() * 1000)
    reporting = not (ns.full or ns.json)

    try:
        jobs, extra = load_jobs(openclaw, ns.input)
    except Blind as e:
        msg = f"⛔ **Cron audit cannot run**: {e}"
        if not reporting:
            print(msg, file=sys.stderr)
            return 2
        # Say it once, then stay quiet until that changes too: a wedged gateway lasts
        # hours, and the audit must not become the spam. Previous findings are kept so
        # recovery does not re-announce all of them as new.
        prev = read_state(ns.state)
        key = "audit:blind:" + short_hash(msg)
        keys = set(prev.get("keys") or [])
        if key in keys:
            print(NO_REPLY)
        else:
            print(msg)
            keys = {k for k in keys if not k.startswith("audit:blind:")} | {key}
            write_state(ns.state, {"keys": sorted(keys), "labels": prev.get("labels") or {},
                                   "lastReportMs": prev.get("lastReportMs") or 0})
        return 0

    found = extra + audit(jobs, now, load_routes(ns.routes), alert_args_from(ns),
                          ns.never_ran_days, ns.overdue_hours)

    healed = []
    if ns.heal:
        kept = []
        for f in found:
            if not f["fix"]:
                kept.append(f)
                continue
            res = subprocess.run([openclaw, "cron", *f["fix"]], capture_output=True, text=True, timeout=120)
            if res.returncode == 0:
                healed.append(f["fixed_text"])
            else:
                kept.append(dict(f, sev="err", text=f"{f['text']} (auto-heal failed: {res.stderr.strip()[:200]})"))
        found = kept

    if ns.json:
        print(json.dumps({"jobs": len(jobs), "healed": healed,
                          "findings": [{k: f[k] for k in ("key", "sev", "text")} for f in found]},
                         ensure_ascii=False, indent=1))
    elif ns.full:
        print("\n".join(["**Healed**"] + healed + [""]) if healed else "", end="")
        print(render_full(found, len(jobs)))
    else:
        hint = f"python3 {os.path.abspath(sys.argv[0])} --full"
        text, state = render(found, healed, read_state(ns.state), now, len(jobs), ns.digest_hours, hint)
        print(text)
        write_state(ns.state, state)

    # The cron path always exits 0: the report text is the signal. A non-zero exit would
    # make cron mark the audit itself as failed, and it would alarm about itself forever.
    return 1 if ns.strict and any(f["sev"] == "err" for f in found) else 0


if __name__ == "__main__":
    sys.exit(main())
