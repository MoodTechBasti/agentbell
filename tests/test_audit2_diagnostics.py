"""Regression tests for the diagnostics findings of the 2026-09-22 audit.

doctor/verify/history/test/notify/init/webhook/config set. Every test runs
in its own temp HOME, state dir and config file. Run with:
python3 -m unittest discover -s tests
"""

import builtins
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: sets the suite env)
import agentbell as an  # noqa: E402


class _Sandbox(unittest.TestCase):
    """Fresh HOME, state dir and config file per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentbell-audit2-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old_home = base._set_home(os.path.join(self.tmp, "home"))
        self.addCleanup(base._restore_home, old_home)
        for patcher in (
                unittest.mock.patch.dict(os.environ, {
                    an.STATE_DIR_ENV: os.path.join(self.tmp, "state"),
                    an.CONFIG_FILE_ENV: os.path.join(self.tmp, "config.json"),
                }),
                # nothing here tests the retry backoff; an unreachable
                # localhost port must not cost seconds of sleep
                unittest.mock.patch.object(an, "RETRY_ATTEMPTS", 1)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def config(self, server="http://127.0.0.1:1", topic="audit2-topic-0123456789",
               channels=("ntfy",), **extra):
        data = an.default_config()
        data["ntfy"].update({"server": server, "topic": topic})
        data["channels"] = list(channels)
        data.update(extra)
        cfg = an.Config(data, path=an.config_path())
        cfg.save()
        return cfg

    def write_history_bytes(self, payload):
        os.makedirs(an.state_dir(), exist_ok=True)
        with open(an.history_path(), "wb") as fh:
            fh.write(payload)

    @staticmethod
    def capture(func, *args, **kwargs):
        """(return value or SystemExit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = func(*args, **kwargs)
            except SystemExit as exc:
                result = exc.code
        return result, out.getvalue(), err.getvalue()

    @staticmethod
    def check(checks, name):
        return [c for c in checks if c["name"] == name]


class TestServerUrlValidation(_Sandbox):
    """L-doctor-url: a stored server doctor cannot use is reported, never a
    traceback; `config set` refuses a URL that cannot be opened."""

    def test_normalize_server_refuses_urls_that_cannot_be_opened(self):
        for bad in ("http://127.0.0.1:8o80", "https://ntfy.example:99999", "https://",
                    "http://[::1", "https://ntfy.example?auth=x", "https://ntfy.example/?",
                    "https://ntfy.example#x", "https://ntfy .example",
                    "https://ntfy.example\x00x"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                an.normalize_server(bad)
        for good, normalized in (("ntfy.sh", "https://ntfy.sh"),
                                 ("http://[::1]:8080/", "http://[::1]:8080"),
                                 ("https://host.example/ntfy", "https://host.example/ntfy"),
                                 ("http://server.invalid", "http://server.invalid")):
            self.assertEqual(an.normalize_server(good), normalized)

    def test_credentials_in_the_url_are_refused_without_echoing_them(self):
        with self.assertRaises(RuntimeError) as ctx:
            an.normalize_server("https://owner:hunter2@ntfy.example")
        self.assertNotIn("hunter2", str(ctx.exception))
        self.assertIn("ntfy.auth", str(ctx.exception))

    def test_config_set_refuses_a_non_numeric_port(self):
        cfg = self.config(server="https://ntfy.sh")
        with self.assertRaises(SystemExit) as ctx:
            an.config_set(cfg, "ntfy.server", "http://127.0.0.1:8o80")
        self.assertIn("not a valid server URL", str(ctx.exception))
        with open(cfg.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["ntfy"]["server"], "https://ntfy.sh")

    def test_doctor_reports_a_bad_stored_server_instead_of_crashing(self):
        cfg = self.config(server="ftp://example.invalid")
        checks = an.doctor_checks(cfg)            # used to raise RuntimeError
        server = self.check(checks, "ntfy server")
        self.assertEqual([c["status"] for c in server], [an.FAIL])
        self.assertIn("only http:// and https://", server[0]["detail"])
        self.assertTrue(server[0]["fix"].startswith("agentbell config set ntfy.server "))
        topic = self.check(checks, "ntfy topic")[0]
        self.assertEqual(topic["status"], an.OK)
        self.assertNotIn("None", topic["detail"])

    def test_doctor_names_a_bad_port_instead_of_blaming_the_network(self):
        cfg = self.config(server="http://127.0.0.1:8o80")
        with unittest.mock.patch.object(an.NtfyChannel, "poll") as poll:
            checks = an.doctor_checks(cfg)
        poll.assert_not_called()
        server = self.check(checks, "ntfy server")
        self.assertEqual(len(server), 1)
        self.assertIn("not a valid server URL", server[0]["detail"])
        self.assertNotIn("unreachable", server[0]["detail"])

    def test_a_bad_stored_port_fails_the_send_instead_of_queueing_forever(self):
        cfg = self.config(server="http://127.0.0.1:8o80")
        result = an.send_notification(cfg, "never deliverable")
        self.assertFalse(result["ok"])
        self.assertNotIn("queued", result)
        self.assertFalse(os.path.isdir(an.queue_dir()) and os.listdir(an.queue_dir()))
        self.assertIn("not a valid server URL", " ".join(result["errors"]))

    def test_init_keeping_a_bad_stored_server_exits_cleanly(self):
        cfg = self.config(server="ftp://example.invalid")
        args = base._init_args(no_test=True)
        code, out, _ = self.capture(an.cmd_init, args)   # used to raise RuntimeError
        self.assertIsInstance(code, str)
        self.assertIn("only http:// and https://", code)
        self.assertIn("--server", code)
        with open(cfg.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["ntfy"]["server"], "ftp://example.invalid")


class TestHistoryDamage(_Sandbox):
    """L-history-utf8: one bad line must not crash history/verify/doctor,
    and the damage is counted and reported."""

    VALID = b'{"event": "notify", "message": "fine", "priority": "normal"}\n'
    LATIN1 = b'{"event": "notify", "message": "gr\xfcn", "priority": "normal"}\n'
    BROKEN = b'{"event": "notify", "mess\n'
    NOT_OBJECT = b'[1, 2, 3]\n'

    def setUp(self):
        super().setUp()
        self.write_history_bytes(self.VALID + self.LATIN1 + self.BROKEN
                                 + self.NOT_OBJECT + b"\n")

    def test_read_history_repairs_skips_and_counts(self):
        damage = {}
        records = an.read_history(limit=0, damage=damage)
        self.assertEqual([r["message"] for r in records], ["fine", "gr�n"])
        self.assertEqual(damage, {"repaired": 1, "skipped": 2})
        self.assertEqual(len(an.read_history(limit=0)), 2)   # damage stays optional

    def test_history_command_lists_and_reports_the_damage(self):
        args = an.build_parser().parse_args(["history"])
        code, out, err = self.capture(an.cmd_history, args)
        self.assertIsNone(code)
        self.assertIn("fine", out)
        self.assertIn("gr�n", out)
        self.assertIn("2 unreadable line(s) skipped", err)
        self.assertIn("1 line(s) with invalid UTF-8", err)

    def test_history_json_stays_parseable(self):
        args = an.build_parser().parse_args(["history", "--json"])
        _, out, err = self.capture(an.cmd_history, args)
        self.assertEqual(len(json.loads(out)), 2)
        self.assertIn("damaged history", err)

    def test_verify_warns_without_naming_the_file(self):
        cfg = self.config()
        report = an.verify_report(cfg, since_seconds=3600)
        history = self.check(report["checks"], "history")
        self.assertEqual([c["status"] for c in history], [an.WARN])
        self.assertIn("2 unreadable line(s) skipped", history[0]["detail"])
        self.assertNotIn(an.state_dir(), json.dumps(report))

    def test_doctor_warns_and_names_the_file(self):
        cfg = self.config()
        with unittest.mock.patch.object(an.NtfyChannel, "poll", return_value=[]):
            history = self.check(an.doctor_checks(cfg), "history")
        self.assertEqual([c["status"] for c in history], [an.WARN])
        self.assertIn(an.history_path(), history[0]["detail"])

    def test_doctor_reports_an_unreadable_history_instead_of_hiding_it(self):
        os.remove(an.history_path())
        os.makedirs(an.history_path())          # open() fails on a directory
        cfg = self.config()
        with unittest.mock.patch.object(an.NtfyChannel, "poll", return_value=[]):
            history = self.check(an.doctor_checks(cfg), "history")
        self.assertEqual([c["status"] for c in history], [an.WARN])
        self.assertIn("cannot read", history[0]["detail"])

    def test_clean_history_reports_nothing(self):
        self.write_history_bytes(self.VALID)
        damage = {}
        an.read_history(damage=damage)
        self.assertIsNone(an.history_damage_note(damage))
        args = an.build_parser().parse_args(["history"])
        _, _, err = self.capture(an.cmd_history, args)
        self.assertEqual(err, "")


class TestNumericPriority(_Sandbox):
    """L-priority-int: a number where a priority name belongs."""

    def test_history_lists_a_numeric_priority_and_odd_types(self):
        self.write_history_bytes(json.dumps({
            "event": "notify", "priority": 4, "ts": 12345, "channels": ["ntfy", 7],
            "message": 99}).encode() + b"\n")
        args = an.build_parser().parse_args(["history"])
        code, out, _ = self.capture(an.cmd_history, args)
        self.assertIsNone(code)                 # used to raise ValueError
        row = out.splitlines()[1]
        self.assertIn("high", row)
        self.assertIn("ntfy,7", row)
        self.assertIn("99", row)

    def test_queue_list_shows_a_numeric_priority_by_name(self):
        directory = an.ensure_state_dir(an.queue_dir())
        with open(os.path.join(directory, "q1.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "q1", "created": time.time(), "message": "numbered",
                       "priority": 4, "channels": ["ntfy"]}, fh)
        data = an.queue_list_data()
        self.assertEqual(data["queue"][0]["priority"], "high")
        _, out, _ = self.capture(an.print_queue_list, data)   # used to raise
        self.assertIn("high", out)
        self.assertIn("numbered", out)

    def test_a_numeric_priority_is_sent_and_recorded_as_its_name(self):
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = self.config(server=ntfy.url)
        for sent, header, name in ((4, "4", "high"), ("5", "5", "urgent"),
                                   ("high", "4", "high"), (float("inf"), "3", "inf")):
            with self.subTest(sent=sent):
                result = an.send_notification(cfg, f"prio {sent}", priority=sent)
                self.assertTrue(result["ok"])
                self.assertEqual(ntfy.posts[cfg.data["ntfy"]["topic"]][-1]["headers"]
                                 ["Priority"], header)
                self.assertEqual(an.read_history(limit=1)[0]["priority"], name)

    def test_a_numeric_priority_is_not_held_by_quiet_hours(self):
        """4 is `high`: above the default quiet-hours threshold (3)."""
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = self.config(server=ntfy.url, quiet_hours=[{"start": "00:00", "end": "23:59"}])
        cfg.data["quiet_hours_min_priority"] = 4
        result = an.send_notification(cfg, "late but important", priority=4)
        self.assertFalse(result["suppressed"])
        self.assertEqual(len(ntfy.posts[cfg.data["ntfy"]["topic"]]), 1)


class TestTopicRule(_Sandbox):
    """L-topic-len: doctor, verify and `config set` apply one topic rule."""

    def _doctor_status(self, topic):
        cfg = self.config(topic=topic)
        with unittest.mock.patch.object(an.NtfyChannel, "poll", return_value=[]):
            return self.check(an.doctor_checks(cfg), "ntfy topic")[0]["status"]

    def _verify_status(self, topic):
        cfg = self.config(topic=topic)
        return self.check(an.verify_report(cfg, since_seconds=60)["checks"],
                          "delivery")[0]["status"]

    def _config_set_accepts(self, topic):
        cfg = self.config()
        try:
            an.config_set(cfg, "ntfy.topic", topic)
            return True
        except SystemExit:
            return False

    def test_doctor_verify_and_config_set_agree_on_every_length(self):
        expected = {8: an.WARN, an.MIN_GUESSABLE_TOPIC_LEN - 1: an.WARN,
                    an.MIN_GUESSABLE_TOPIC_LEN: an.OK, an.MAX_TOPIC_LEN: an.OK,
                    an.MAX_TOPIC_LEN + 1: an.FAIL, 64: an.FAIL}
        for length, status in expected.items():
            topic = "t" * length
            with self.subTest(length=length):
                self.assertEqual(self._doctor_status(topic), status)
                self.assertEqual(self._verify_status(topic), status)
                self.assertEqual(an.rate_topic(topic)[0], status)
                self.assertEqual(self._config_set_accepts(topic), status != an.FAIL)

    def test_invalid_characters_fail_everywhere(self):
        self.assertEqual(self._doctor_status("bad topic!"), an.FAIL)
        self.assertEqual(self._verify_status("bad topic!"), an.FAIL)
        self.assertFalse(self._config_set_accepts("bad topic!"))

    def test_verify_explains_the_problem_without_the_topic(self):
        topic = "x" * 60
        cfg = self.config(topic=topic)
        delivery = self.check(an.verify_report(cfg, since_seconds=60)["checks"],
                              "delivery")[0]
        self.assertIn("too long (60 chars", delivery["detail"])
        self.assertNotIn(topic, json.dumps(delivery))


class TestVerifyMcpNotifyEvidence(_Sandbox):
    """L-verify-mcp: an MCP notify naming an agent proves the MCP path. It is
    the documented proof for an MCP-only host, never for an installed hook."""

    def _records(self, *records):
        now = time.time()
        stamped = []
        for offset, record in enumerate(records):
            record = dict(record)
            record.setdefault("ts", base._iso(now - 60 + offset))
            stamped.append(record)
        return stamped

    def _report(self, records, slug, installed=False):
        cfg = self.config()
        rows = [(slug, "installed", "x", "hook")] if installed else []
        with unittest.mock.patch.object(an, "read_history", return_value=records), \
                unittest.mock.patch.object(an, "hooks_status", return_value=rows):
            return an.verify_report(cfg, agent=slug, since_seconds=3600)

    def test_mcp_notify_does_not_prove_an_installed_hook(self):
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = self.config(server=ntfy.url)
        with unittest.mock.patch.object(an, "Config", lambda *a, **kw: cfg):
            self.assertEqual(an.mcp_tool_call("notify", {"message": "hi",
                                                         "agent": "claude"}), "sent")
        with unittest.mock.patch.object(an, "hooks_status",
                                        return_value=[("claude", "installed", "x", "hook")]):
            report = an.verify_report(cfg, agent="claude", since_seconds=3600)
        self.assertFalse(report["verified"])        # used to be True
        row = report["agents"][0]
        self.assertEqual((row["count"], row["delivered"], row["notify_calls"]), (1, 1, 1))
        line = self.check(report["checks"], "agent claude")
        self.assertEqual([c["status"] for c in line], [an.WARN])
        self.assertIn("1 of them MCP notify call(s)", line[0]["detail"])
        self.assertIn("no event from the installed hook yet", line[0]["detail"])
        self.assertEqual(line[0]["fix"], "finish one real agent turn, then run this again")

    def test_held_mcp_notify_does_not_prove_an_installed_hook_either(self):
        records = self._records({"event": "deferred", "source_event": "notify",
                                 "agent": "claude"})
        report = self._report(records, "claude", installed=True)
        self.assertFalse(report["verified"])
        self.assertEqual(report["agents"][0]["held"], 1)   # still reported as held

    def test_a_hook_event_proves_the_installed_hook_next_to_mcp_calls(self):
        records = self._records(
            {"event": "hook.run_completed", "agent": "claude", "delivered": ["ntfy"]},
            {"event": "notify", "agent": "claude", "delivered": ["ntfy"]})
        report = self._report(records, "claude", installed=True)
        self.assertTrue(report["verified"])
        line = self.check(report["checks"], "agent claude")
        self.assertEqual([c["status"] for c in line], [an.OK])
        self.assertIn("2 event(s)", line[0]["detail"])
        self.assertIn("1 of them MCP notify call(s)", line[0]["detail"])

    def test_an_mcp_only_host_is_still_proven_by_its_notify_call(self):
        """The integration guide promises this for hosts without lifecycle events."""
        records = self._records({"event": "notify", "agent": "a2-mcponly",
                                 "delivered": ["ntfy"]})
        report = self._report(records, "a2-mcponly")
        self.assertTrue(report["verified"])
        self.assertIn("1 of them MCP notify call(s)",
                      self.check(report["checks"], "agent a2-mcponly")[0]["detail"])

    def test_failed_mcp_notify_calls_stay_a_fail(self):
        records = self._records({"event": "notify", "agent": "a2-mcpfail",
                                 "delivered": [], "errors": {"ntfy": "down"}})
        report = self._report(records, "a2-mcpfail")
        self.assertFalse(report["verified"])
        line = self.check(report["checks"], "agent a2-mcpfail")
        self.assertEqual([c["status"] for c in line], [an.FAIL])
        self.assertIn("NO event reached any channel", line[0]["detail"])


class TestTestCommandTelegramOnly(_Sandbox):
    """L-test-blame: `agentbell test` with Telegram as the only channel."""

    def setUp(self):
        super().setUp()
        self.tg = base.MockTelegram()
        self.addCleanup(self.tg.stop)
        patcher = unittest.mock.patch.object(an, "TG_API_BASE", self.tg.url)
        patcher.start()
        self.addCleanup(patcher.stop)
        keypair = base.dev_keypair()
        keypair.__enter__()
        self.addCleanup(keypair.__exit__, None, None, None)

    def telegram_config(self, topic="audit2-topic-0123456789"):
        return self.config(topic=topic, channels=("telegram",),
                           telegram={"bot_token": "123:abc", "chat_id": "42"},
                           license=an.make_license_key("audit2", seed=base.TEST_SEED))

    def run_test_command(self, cfg):
        with unittest.mock.patch.object(an, "Config", lambda *a, **kw: cfg):
            return self.capture(an.cmd_test, base._Args(no_wait=False))

    def test_telegram_delivery_is_success_not_an_ntfy_failure(self):
        code, out, err = self.run_test_command(self.telegram_config())
        self.assertEqual(code, 0)                 # used to be 1: "ntfy was not"
        self.assertIn("sent via telegram", out)
        self.assertNotIn("ntfy was not", err)
        self.assertEqual(sum(1 for r in self.tg.requests if r["method"] == "sendMessage"), 1)

    def test_no_ntfy_topic_is_not_required_for_telegram_only(self):
        code, out, err = self.run_test_command(self.telegram_config(topic=""))
        self.assertEqual(code, 0)
        self.assertNotIn("ntfy is not configured", err)
        self.assertIn("via telegram", out)

    def test_a_telegram_failure_names_telegram_and_skips_the_ntfy_hint(self):
        self.tg.fail_send = True
        code, _, err = self.run_test_command(self.telegram_config())
        self.assertEqual(code, 1)
        self.assertIn("NOT delivered", err)
        self.assertIn("telegram", err)
        self.assertNotIn("ntfy app", err)
        self.assertIn("1. what went wrong?", err)

    def test_doctor_send_reports_telegram_delivery_as_ok(self):
        cfg = self.telegram_config()
        with unittest.mock.patch.object(an.NtfyChannel, "poll", return_value=[]):
            delivery = self.check(an.doctor_checks(cfg, send=True), "delivery")
        self.assertEqual([c["status"] for c in delivery], [an.OK])
        self.assertIn("telegram", delivery[0]["detail"])

    def test_ntfy_setups_keep_the_ntfy_hints(self):
        cfg = self.config(server="http://127.0.0.1:1")      # nothing listening
        code, _, err = self.run_test_command(cfg)
        self.assertEqual(code, 1)
        self.assertIn("1. is the topic subscribed in the ntfy app?   topic: ", err)
        self.assertIn("2. what went wrong?                           agentbell history", err)


class TestNotifyQuiet(_Sandbox):
    """L-notify-quiet: --quiet silences success output only."""

    def notify(self, argv, cfg):
        args = an.build_parser().parse_args(["notify"] + argv)
        with unittest.mock.patch.object(an, "Config", lambda *a, **kw: cfg):
            return self.capture(an.cmd_notify, args)

    def test_a_failure_is_reported_on_stderr_even_with_quiet(self):
        cfg = self.config()        # no premium: an explicit telegram send fails
        code, out, err = self.notify(["x", "--quiet", "--channel", "telegram"], cfg)
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("error: telegram:", err)

    def test_a_failure_goes_to_stderr_without_quiet_too(self):
        code, out, err = self.notify(["x", "--channel", "telegram"], self.config())
        self.assertEqual(code, 3)
        self.assertNotIn("error", out)
        self.assertIn("error: telegram:", err)

    def test_success_with_quiet_prints_nothing(self):
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        code, out, err = self.notify(["x", "--quiet"], self.config(server=ntfy.url))
        self.assertIsNone(code)
        self.assertEqual((out, err), ("", ""))


class TestInitQuietHoursTypo(_Sandbox):
    """L-init-quiet: a typo in the quiet-hours prompt asks again."""

    def run_init(self, answers, **overrides):
        self.config(topic="audit2-old-topic-0123456789")
        answers = iter(answers)
        stdin = unittest.mock.MagicMock()
        stdin.isatty.return_value = True
        args = base._init_args(non_interactive=False, no_test=True,
                               topic="audit2-new-topic-0123456789", **overrides)
        with unittest.mock.patch.object(an.sys, "stdin", stdin), \
                unittest.mock.patch.object(builtins, "input",
                                           lambda prompt="": next(answers)):
            result = self.capture(an.cmd_init, args)
        with open(an.config_path(), encoding="utf-8") as fh:
            return result, json.load(fh)

    def test_a_typo_asks_again_and_keeps_every_other_answer(self):
        # server (keep), "subscribed" Enter, no Telegram, typo, fixed, mode
        (code, out, _), saved = self.run_init(
            ["", "", "n", "22:00-7:3O", "22:00-07:30,12:00-13:00", "defer"])
        self.assertIsNone(code)                  # used to SystemExit, nothing saved
        self.assertIn("invalid quiet-hours window '22:00-7:3O'", out)
        self.assertEqual(saved["ntfy"]["topic"], "audit2-new-topic-0123456789")
        self.assertEqual(saved["quiet_hours"], [{"start": "22:00", "end": "07:30"},
                                                {"start": "12:00", "end": "13:00"}])
        self.assertEqual(saved["quiet_hours_mode"], "defer")

    def test_blank_after_a_typo_means_no_quiet_hours(self):
        (code, _, _), saved = self.run_init(["", "", "n", "lunch", ""])
        self.assertIsNone(code)
        self.assertEqual(saved["quiet_hours"], [])

    def test_the_flag_still_refuses_a_typo(self):
        self.config(topic="audit2-old-topic-0123456789")
        args = base._init_args(no_test=True, quiet_hours="22:00-7:3O")
        code, _, _ = self.capture(an.cmd_init, args)
        self.assertIn("invalid quiet-hours window '22:00-7:3O'", code)


def _ipv6_loopback_available():
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
        return True
    except OSError:
        return False


def _free_port_v6():
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.bind(("::1", 0))
        return sock.getsockname()[1]


class TestWebhookListen(_Sandbox):
    """L-webhook-ipv6: `webhook.listen` may be the IPv6 loopback."""

    def _serve(self, listen, port):
        # like the suite's other webhook tests: a daemon thread that serves
        # until the process exits (webhook_server exposes no shutdown handle)
        cfg = self.config(webhook={"listen": listen, "port": port, "token": None})
        errors = []

        def run():
            try:
                an.webhook_server(cfg)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threading.Thread(target=run, daemon=True).start()
        deadline = time.monotonic() + 5
        last = None
        while time.monotonic() < deadline and not errors:
            try:
                with urllib.request.urlopen(f"http://[::1]:{port}/healthz", timeout=0.5) as r:
                    return json.loads(r.read())
            except OSError as exc:
                last = exc
                time.sleep(0.05)
        self.fail(f"webhook on {listen} never answered: {errors!r}, last error {last!r}")

    @unittest.skipUnless(_ipv6_loopback_available(), "no IPv6 loopback on this machine")
    def test_listens_on_ipv6_loopback(self):
        self.assertTrue(self._serve("::1", _free_port_v6())["ok"])

    @unittest.skipUnless(_ipv6_loopback_available(), "no IPv6 loopback on this machine")
    def test_bracketed_ipv6_loopback_needs_no_token_either(self):
        self.assertTrue(self._serve("[::1]", _free_port_v6())["ok"])

    def test_a_busy_port_is_a_clean_error(self):
        with socket.socket() as busy:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                # Windows: without it, SO_REUSEADDR lets the server share the port
                busy.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            cfg = self.config(webhook={"listen": "127.0.0.1", "port": port, "token": None})
            # never block the suite: if the bind unexpectedly worked, return
            with unittest.mock.patch("http.server.ThreadingHTTPServer.serve_forever"):
                code, _, _ = self.capture(an.webhook_server, cfg)
        self.assertIsInstance(code, str)          # used to be a raw OSError
        self.assertIn(f"cannot listen on 127.0.0.1 port {port}", code)


class TestConfigSetActionAuth(_Sandbox):
    """L-action-auth: `config set ntfy.action_auth` as the README implies."""

    def test_set_redact_and_clear(self):
        cfg = self.config(server="https://ntfy.example")
        self.assertEqual(an.config_set(cfg, "ntfy.action_auth", "tk_scoped"), "tk_scoped")
        with open(cfg.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["ntfy"]["action_auth"], "tk_scoped")
        self.assertNotIn("tk_scoped", json.dumps(an.redacted_config(cfg.data)))
        args = an.build_parser().parse_args(["config", "set", "ntfy.action_auth", "tk_two"])
        with unittest.mock.patch.object(an, "Config", lambda *a, **kw: cfg):
            _, out, _ = self.capture(an.cmd_config, args)
        self.assertIn("<redacted>", out)
        self.assertNotIn("tk_two", out)
        self.assertIsNone(an.config_set(cfg, "ntfy.action_auth", "none"))

    def test_the_account_credential_is_refused(self):
        cfg = self.config(server="https://ntfy.example")
        cfg.data["ntfy"]["auth"] = "owner:s3cret"
        with self.assertRaises(SystemExit) as ctx:
            an.config_set(cfg, "ntfy.action_auth", "owner:s3cret")
        self.assertIn("your ntfy.auth credential", str(ctx.exception))
        self.assertNotIn("s3cret", str(ctx.exception))
        self.assertIsNone(cfg.data["ntfy"].get("action_auth"))

    def test_the_buttons_carry_the_token_that_was_set(self):
        cfg = self.config(server="https://ntfy.example")
        cfg.data["ntfy"]["auth"] = "owner:s3cret"
        an.config_set(cfg, "ntfy.action_auth", "tk_scoped")
        actions = an.ask_actions("https://ntfy.example", "t-responses", "ab12", "Yes",
                                 "No", cfg.data["ntfy"])
        self.assertEqual(actions[0]["headers"]["Authorization"], "Bearer tk_scoped")

    def test_cleartext_server_warns(self):
        cfg = self.config(server="http://ntfy.example")
        _, _, err = self.capture(an.config_set, cfg, "ntfy.action_auth", "tk_scoped")
        self.assertIn("plain http", err)

    def test_a_server_change_clears_both_credentials(self):
        cfg = self.config(server="https://old.example")
        cfg.data["ntfy"].update({"auth": "owner:s3cret", "action_auth": "tk_old"})
        _, _, err = self.capture(an.config_set, cfg, "ntfy.server", "https://new.example")
        self.assertIsNone(cfg.data["ntfy"]["auth"])
        self.assertIsNone(cfg.data["ntfy"]["action_auth"])
        self.assertIn("ntfy.auth was cleared", err)
        self.assertIn("ntfy.action_auth was cleared", err)
        with open(cfg.path, encoding="utf-8") as fh:
            saved = json.load(fh)["ntfy"]
        self.assertEqual((saved["auth"], saved["action_auth"]), (None, None))

    def test_the_same_server_keeps_the_credentials(self):
        cfg = self.config(server="https://same.example")
        cfg.data["ntfy"].update({"auth": "owner:s3cret", "action_auth": "tk_keep"})
        _, _, err = self.capture(an.config_set, cfg, "ntfy.server", "same.example/")
        self.assertEqual(cfg.data["ntfy"]["auth"], "owner:s3cret")
        self.assertEqual(cfg.data["ntfy"]["action_auth"], "tk_keep")
        self.assertNotIn("cleared", err)


if __name__ == "__main__":
    unittest.main()
