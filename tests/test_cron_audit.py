import json, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "cron_audit.py")
sys.path.insert(0, os.path.dirname(SCRIPT))
import cron_audit as ca  # noqa: E402

H = ca.HOUR_MS
NOW = 1_800_000_000_000


def job(**kw):
    j = {"id": "aaaaaaaa-1", "name": "digest", "agentId": "main", "enabled": True,
         "createdAtMs": NOW - 30 * 24 * H, "schedule": {"kind": "cron", "expr": "0 9 * * *"},
         "delivery": {"mode": "announce", "channel": "discord", "to": "channel:1"},
         "failureAlert": {"after": 3, "channel": "discord", "to": "channel:1"},
         "nextRunAtMs": NOW + H, "state": {"lastRunAtMs": NOW - H, "lastRunStatus": "ok"}}
    j.update(kw)
    return j


def kinds(found):
    return sorted(f["key"].split(":")[1] for f in found)


class Checks(unittest.TestCase):
    def test_healthy_job_has_no_findings(self):
        self.assertEqual(ca.audit([job()], NOW), [])

    def test_last_channel_is_a_broken_route(self):
        found = ca.audit([job(delivery={"mode": "announce", "channel": "last"})], NOW)
        self.assertEqual(kinds(found), ["route"])
        self.assertEqual(found[0]["sev"], "err")

    def test_discord_without_recipient_is_a_broken_route(self):
        found = ca.audit([job(delivery={"mode": "announce", "channel": "discord"})], NOW)
        self.assertEqual(kinds(found), ["route"])

    def test_no_deliver_mode_is_not_checked_for_route(self):
        self.assertEqual(ca.audit([job(delivery={"mode": "none"})], NOW), [])

    def test_disabled_job_findings_are_notes(self):
        found = ca.audit([job(enabled=False, delivery={"mode": "announce", "channel": "last"},
                              state={"consecutiveErrors": 4, "lastError": "boom"})], NOW)
        self.assertEqual(kinds(found), ["errors", "route"])
        self.assertTrue(all(f["sev"] == "note" for f in found))

    def test_error_key_follows_text_not_count(self):
        a = ca.audit([job(state={"consecutiveErrors": 2, "lastError": "boom"})], NOW)
        b = ca.audit([job(state={"consecutiveErrors": 9, "lastError": "boom"})], NOW)
        c = ca.audit([job(state={"consecutiveErrors": 9, "lastError": "other"})], NOW)
        self.assertEqual(a[0]["key"], b[0]["key"])
        self.assertNotEqual(a[0]["key"], c[0]["key"])

    def test_missing_alert_best_effort_overdue_never_ran(self):
        j = job(failureAlert=None, delivery={"mode": "announce", "channel": "discord", "to": "channel:1",
                                             "bestEffort": True},
                nextRunAtMs=NOW - 5 * H, state={})
        self.assertEqual(kinds(ca.audit([j], NOW)), ["best-effort", "never-ran", "no-alert", "overdue"])

    def test_undelivered_output_only_counts_after_a_successful_run(self):
        ok = job(state={"lastRunAtMs": NOW, "lastRunStatus": "ok", "lastDeliveryStatus": "not-delivered",
                        "lastDeliveryError": "403"})
        failed = job(state={"lastRunAtMs": NOW, "lastRunStatus": "error", "lastDeliveryStatus": "not-delivered"})
        self.assertEqual(kinds(ca.audit([ok], NOW)), ["delivery"])
        self.assertEqual(ca.audit([failed], NOW), [])

    def test_intentional_silence_is_not_a_delivery_failure(self):
        j = job(deliverySuppressionReason="silent",
                state={"lastRunAtMs": NOW, "lastRunStatus": "ok", "lastDeliveryStatus": "not-delivered"})
        self.assertEqual(ca.audit([j], NOW), [])

    def test_discord_bare_id_is_a_broken_route(self):
        j = job(delivery={"mode": "announce", "channel": "discord", "to": "123456789012345678"},
                failureAlert={"channel": "discord", "to": "123"})
        self.assertEqual(kinds(ca.audit([j], NOW)), ["alert-route", "route"])
        self.assertEqual(ca.audit([job(delivery={"mode": "announce", "channel": "discord", "to": "user:1"})], NOW), [])

    def test_alert_route_and_alert_delivery(self):
        j = job(failureAlert={"channel": "discord"},
                state={"lastRunAtMs": NOW, "lastFailureNotificationDeliveryStatus": "not-delivered"})
        self.assertEqual(kinds(ca.audit([j], NOW)), ["alert-failed", "alert-route"])

    def test_fixes_are_offered_only_when_configured(self):
        broken = job(failureAlert=None, delivery={"mode": "announce", "channel": "last"})
        bare = ca.audit([broken], NOW)
        self.assertTrue(all(f["fix"] is None for f in bare))
        routes = {"main": {"channel": "discord", "to": "channel:9", "accountId": "bot"}}
        alert = ["--failure-alert", "--failure-alert-to", "channel:9"]
        fixes = {f["key"].split(":")[1]: f["fix"] for f in ca.audit([broken], NOW, routes, alert)}
        self.assertIn("channel:9", fixes["route"])
        self.assertEqual(fixes["route"][-2:], ["--account", "bot"])
        self.assertEqual(fixes["no-alert"][:2], ["edit", "aaaaaaaa-1"])


class Report(unittest.TestCase):
    def setUp(self):
        self.broken = ca.audit([job(delivery={"mode": "announce", "channel": "last"})], NOW)
        self.fresh = {"keys": [], "labels": {}, "lastReportMs": NOW - H}

    def test_new_finding_is_announced_once(self):
        text, state = ca.render(self.broken, [], self.fresh, NOW, 1)
        self.assertIn("__New__", text)
        text2, _ = ca.render(self.broken, [], state, NOW + H, 1)
        self.assertEqual(text2, ca.NO_REPLY)

    def test_resolved_finding_names_the_job(self):
        _, state = ca.render(self.broken, [], self.fresh, NOW, 1)
        text, state2 = ca.render([], [], state, NOW + H, 1)
        self.assertIn("✅ `digest` (main, aaaaaaaa) — broken delivery route cleared", text)
        self.assertEqual(state2["keys"], [])

    def test_digest_when_due(self):
        _, state = ca.render(self.broken, [], self.fresh, NOW, 1)
        text, _ = ca.render(self.broken, [], state, NOW + 200 * H, 1, digest_hours=168)
        self.assertIn("Still open (1)", text)
        self.assertNotIn("__New__", text)

    def test_healthy_digest_is_a_heartbeat(self):
        text, _ = ca.render([], [], {"keys": [], "lastReportMs": 0}, NOW, 12)
        self.assertIn("all 12 cron jobs look healthy", text)


class Cli(unittest.TestCase):
    def run_cli(self, data, *args, state=None):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "jobs.json")
            with open(src, "w") as fh:
                json.dump(data, fh)
            state = state or os.path.join(d, "state.json")
            return subprocess.run([sys.executable, SCRIPT, "--input", src, "--state", state,
                                   "--routes", os.path.join(d, "none.json"), *args],
                                  capture_output=True, text=True)

    def test_json_mode(self):
        r = self.run_cli({"jobs": [job(delivery={"mode": "announce", "channel": "last"})]}, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["findings"][0]["sev"], "err")

    def test_strict_exit_code(self):
        r = self.run_cli({"jobs": [job(delivery={"mode": "announce", "channel": "last"})]}, "--full", "--strict")
        self.assertEqual(r.returncode, 1)

    def test_truncated_list_is_an_error(self):
        r = self.run_cli({"jobs": [job()], "total": 250, "hasMore": True}, "--json")
        self.assertIn("only 1 of 250", r.stdout)

    def test_heal_refuses_input_file(self):
        r = self.run_cli({"jobs": []}, "--heal")
        self.assertEqual(r.returncode, 2)

    def test_blind_is_reported_once_and_keeps_findings(self):
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "state.json")
            with open(state, "w") as fh:
                json.dump({"keys": ["x:route"], "labels": {"x:route": "`x`"}, "lastReportMs": NOW}, fh)
            cmd = [sys.executable, SCRIPT, "--openclaw", os.path.join(d, "missing"), "--state", state]
            first = subprocess.run(cmd, capture_output=True, text=True).stdout
            second = subprocess.run(cmd, capture_output=True, text=True).stdout
            self.assertIn("cannot run", first)
            self.assertEqual(second.strip(), ca.NO_REPLY)
            with open(state) as fh:
                self.assertIn("x:route", json.load(fh)["keys"])


if __name__ == "__main__":
    unittest.main()
