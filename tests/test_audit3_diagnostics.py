"""Regression tests for the diagnostics findings of the third audit round.

ntfy credentials on a server change (N1, D1, D4), doctor's server fix (D2),
server URL and topic validation (D5, D6), verify/history on history they
cannot use (D7), Telegram-only doctor (D8), verify on a bad server (D9),
short-topic warning (D10) and init's quiet-hours prompt (D11, D12).
Every test runs in its own temp HOME, state dir and config file.
"""

import builtins
import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: sets the suite env)
import agentbell as an  # noqa: E402

TOPIC = "audit3-topic-0123456789ab"


class _Sandbox(unittest.TestCase):
    """Fresh HOME, state dir and config file per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentbell-audit3-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old_home = base._set_home(os.path.join(self.tmp, "home"))
        self.addCleanup(base._restore_home, old_home)
        patcher = unittest.mock.patch.dict(os.environ, {
            an.STATE_DIR_ENV: os.path.join(self.tmp, "state"),
            an.CONFIG_FILE_ENV: os.path.join(self.tmp, "config.json"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, server="https://ntfy.example", topic=TOPIC, **ntfy):
        data = an.default_config()
        data["ntfy"].update({"server": server, "topic": topic}, **ntfy)
        cfg = an.Config(data, path=an.config_path())
        cfg.save()
        return cfg

    def saved(self):
        with open(an.config_path(), encoding="utf-8") as fh:
            return json.load(fh)

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

    def run_init_interactive(self, answers, **overrides):
        answers = iter(answers)
        stdin = unittest.mock.MagicMock()
        stdin.isatty.return_value = True
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(answers)

        args = base._init_args(non_interactive=False, no_test=True, topic=TOPIC,
                               **overrides)
        with unittest.mock.patch.object(an.sys, "stdin", stdin), \
                unittest.mock.patch.object(builtins, "input", fake_input):
            result = self.capture(an.cmd_init, args)
        return result, prompts


class TestCredentialsFollowTheServer(_Sandbox):
    """N1/D1/D4: one rule for config set and init - a real server change
    drops ntfy.auth and ntfy.action_auth, the same server keeps them."""

    def set_server(self, old, new):
        cfg = self.config(server=old, auth="owner:s3cret", action_auth="tk_btn")
        _, _, err = self.capture(an.config_set, cfg, "ntfy.server", new)
        return self.saved()["ntfy"], err

    def assert_cleared(self, old, new):
        saved, err = self.set_server(old, new)
        self.assertEqual((saved["auth"], saved["action_auth"]), (None, None), (old, new))
        self.assertIn("ntfy.auth was cleared", err)
        self.assertIn("ntfy.action_auth was cleared", err)

    def assert_kept(self, old, new):
        saved, err = self.set_server(old, new)
        self.assertEqual((saved["auth"], saved["action_auth"]),
                         ("owner:s3cret", "tk_btn"), (old, new))
        self.assertNotIn("cleared", err)

    def test_an_empty_stored_server_is_the_default_server(self):
        # D1: the credential went to ntfy.sh; it used to follow to the new host
        self.assert_cleared("", "http://127.0.0.1:8080")
        self.assert_cleared(None, "https://other.example")

    def test_empty_and_default_are_the_same_server(self):
        self.assert_kept("", an.DEFAULT_NTFY_SERVER)
        self.assert_kept(an.DEFAULT_NTFY_SERVER, "")

    def test_case_slash_and_default_port_are_the_same_server(self):
        # D4: the letter case used to wipe both credentials
        self.assert_kept("http://ntfy.home.lan:8080", "HTTP://NTFY.HOME.LAN:8080")
        self.assert_kept("https://ntfy.home.lan", "https://ntfy.home.lan:443/")
        self.assert_kept("http://ntfy.home.lan", "http://ntfy.home.lan:80")

    def test_fixing_a_broken_port_on_the_same_host_keeps_them(self):
        # D4: the old value never received a send; the typo fix used to wipe them
        self.assert_kept("http://ntfy.home.lan:8o80", "http://ntfy.home.lan:8080")

    def test_a_real_change_still_clears_them(self):
        self.assert_cleared("https://a.example", "https://b.example")
        self.assert_cleared("http://ntfy.home.lan:8080", "http://ntfy.home.lan:9090")
        self.assert_cleared("http://ntfy.home.lan:8080", "https://ntfy.home.lan:8080")
        self.assert_cleared("http://ntfy.home.lan:8o80", "http://evil.example:8080")
        self.assert_cleared("https://ntfy.example/a", "https://ntfy.example/b")

    def test_noninteractive_init_from_an_empty_server(self):
        # D1: init kept the ntfy.sh credential and the test push carried it
        self.config(server="", auth="tk_for_ntfysh", action_auth="tk_btn")
        code, _, err = self.capture(
            an.cmd_init, base._init_args(server="https://self.example", no_test=True))
        self.assertIsNone(code)
        saved = self.saved()["ntfy"]
        self.assertEqual(saved["server"], "https://self.example")
        self.assertEqual((saved["auth"], saved["action_auth"]), (None, None))
        self.assertIn("ntfy.auth was cleared", err)

    def test_init_with_a_new_password_does_not_report_it_cleared(self):
        self.config(server="https://old.example", auth="old:pw", action_auth="tk_btn")
        _, _, err = self.capture(an.cmd_init, base._init_args(
            server="https://new.example", ntfy_auth="new:pw", no_test=True))
        saved = self.saved()["ntfy"]
        self.assertEqual((saved["auth"], saved["action_auth"]), ("new:pw", None))
        self.assertNotIn("ntfy.auth was cleared", err)
        self.assertIn("ntfy.action_auth was cleared", err)


class TestDoctorServerFix(_Sandbox):
    """D2: the fix for an invalid stored server never points at ntfy.sh."""

    def test_the_fix_asks_for_the_users_own_server(self):
        cfg = self.config(server="http://ntfy.home.lan:8o80", auth="basti:hunter2")
        checks = [c for c in an.doctor_checks(cfg) if c["name"] == "ntfy server"]
        self.assertEqual([c["status"] for c in checks], [an.FAIL])
        self.assertIn("config set ntfy.server <url>", checks[0]["fix"])
        self.assertNotIn(an.DEFAULT_NTFY_SERVER, checks[0]["fix"])


class TestServerUrlTypos(unittest.TestCase):
    """D5: a mistyped scheme became the host "https" and queued forever."""

    def test_a_mistyped_scheme_is_refused(self):
        for value in ("https//ntfy.example.com", "https:/ntfy.example.com",
                      "http:/127.0.0.1:8080", "http//localhost:8080"):
            with self.assertRaises(RuntimeError, msg=value):
                an.normalize_server(value)

    def test_plain_hosts_still_work(self):
        self.assertEqual(an.normalize_server("ntfy.example.com"), "https://ntfy.example.com")
        self.assertEqual(an.normalize_server("localhost:8080"), "https://localhost:8080")
        self.assertEqual(an.normalize_server("http://127.0.0.1:8080/"), "http://127.0.0.1:8080")


class TestTopicRule(_Sandbox):
    """D6: a trailing newline passed; an overlong topic was called invalid."""

    def test_a_trailing_newline_is_invalid(self):
        topic = "audit3-topic-abcdef\n"
        self.assertEqual(an.rate_topic(topic)[0], an.FAIL)
        with self.assertRaises(RuntimeError):
            an.validate_topic(topic)
        code, _, _ = self.capture(an.config_set, self.config(), "ntfy.topic", topic)
        self.assertIn("is not valid", code)
        self.assertEqual(self.saved()["ntfy"]["topic"], TOPIC)

    def test_an_overlong_topic_is_called_too_long(self):
        for length in (55, 65, 80):
            status, problem = an.rate_topic("a" * length)
            self.assertEqual(status, an.FAIL)
            self.assertIn(f"too long ({length} chars", problem)
        with self.assertRaises(RuntimeError):
            an.validate_topic("a" * 65)
        an.validate_topic("a" * 64)

    def test_valid_topics_still_pass(self):
        self.assertEqual(an.rate_topic(TOPIC), (an.OK, None))
        self.assertEqual(an.rate_topic("abc")[0], an.WARN)
        self.assertEqual(an.rate_topic("")[0], an.FAIL)


class TestHistoryVerifyCannotUse(_Sandbox):
    """D7: an unreadable history and a non-string project crashed verify."""

    def write_history(self, *records):
        os.makedirs(an.state_dir(), exist_ok=True)
        with open(an.history_path(), "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def unreadable(self):
        error = PermissionError(13, "Permission denied", an.history_path())
        return unittest.mock.patch.object(an, "read_history", side_effect=error)

    def test_verify_warns_without_the_path(self):
        cfg = self.config()
        with self.unreadable():
            report = an.verify_report(cfg)
        history = [c for c in report["checks"] if c["name"] == "history"]
        self.assertEqual([c["status"] for c in history], [an.WARN])
        self.assertIn("Permission denied", history[0]["detail"])
        self.assertNotIn(an.state_dir(), json.dumps(report))

    def test_verify_json_still_prints_json(self):
        self.config()
        args = an.build_parser().parse_args(["verify", "--json"])
        with self.unreadable():
            _, out, _ = self.capture(an.cmd_verify, args)
        self.assertIn("history", [c["name"] for c in json.loads(out)["checks"]])

    def test_history_is_a_clean_error(self):
        args = an.build_parser().parse_args(["history"])
        with self.unreadable():
            code, _, _ = self.capture(an.cmd_history, args)
        self.assertIsInstance(code, str)
        self.assertIn("Permission denied", code)

    def test_a_non_string_project_is_skipped(self):
        cfg = self.config()
        now = datetime.datetime.now().isoformat(timespec="seconds")
        project = os.path.join(self.tmp, "repo")
        os.makedirs(project)
        self.write_history(
            {"ts": now, "event": "hook.run_completed", "agent": "claude", "project": 5},
            {"ts": now, "event": "hook.run_completed", "agent": "claude",
             "project": project})
        report = an.verify_report(cfg, project=project)
        claude = [row for row in report["agents"] if row["agent"] == "claude"]
        self.assertEqual([row["count"] for row in claude], [1])


class TestDoctorTelegramOnly(_Sandbox):
    """D8: doctor blamed an unreachable ntfy on a Telegram-only setup."""

    def premium_config(self, channels):
        cfg = self.config(server="http://127.0.0.1:1")
        cfg.data["channels"] = list(channels)
        cfg.data["telegram"] = {"bot_token": "123:abc", "chat_id": "42"}
        cfg.data["license"] = an.make_license_key("audit3", seed=base.TEST_SEED)
        cfg.save()
        return cfg

    def doctor(self, cfg):
        failure = an.TransientError("cannot reach the server")
        with unittest.mock.patch.object(an.NtfyChannel, "poll",
                                        side_effect=failure) as poll:
            checks = an.doctor_checks(cfg)
        return checks, poll

    def test_telegram_only_does_not_check_ntfy(self):
        with base.dev_keypair():
            checks, poll = self.doctor(self.premium_config(["telegram"]))
        poll.assert_not_called()
        self.assertFalse([c for c in checks if c["name"].startswith("ntfy")
                          and c["status"] != an.OK])
        self.assertIn("ntfy", [c["name"] for c in checks])

    def test_ask_still_needs_ntfy_without_telegram(self):
        # channels ["os"]: `ask` falls back to ntfy, so doctor still checks it
        with base.dev_keypair():
            checks, poll = self.doctor(self.premium_config(["os"]))
        poll.assert_called_once()
        self.assertIn(an.FAIL, [c["status"] for c in checks if c["name"] == "ntfy server"])

    def test_telegram_without_premium_falls_back_to_ntfy(self):
        cfg = self.premium_config(["telegram"])     # no dev_keypair: key invalid
        checks, poll = self.doctor(cfg)
        poll.assert_called_once()


class TestVerifyBadServer(_Sandbox):
    """D9: verify said [OK] delivery for a server every send refuses."""

    def test_an_invalid_server_fails_delivery_without_the_url(self):
        cfg = self.config(server="http://127.0.0.1:8o80")
        report = an.verify_report(cfg)
        delivery = [c for c in report["checks"] if c["name"] == "delivery"]
        self.assertEqual([c["status"] for c in delivery], [an.FAIL])
        self.assertIn("server URL is not valid", delivery[0]["detail"])
        self.assertNotIn("8o80", json.dumps(report))

    def test_a_valid_server_is_ok(self):
        report = an.verify_report(self.config())
        delivery = [c for c in report["checks"] if c["name"] == "delivery"]
        self.assertEqual([c["status"] for c in delivery], [an.OK])


class TestShortTopicWarning(_Sandbox):
    """D10: config set accepted a guessable topic without a word."""

    def test_a_short_topic_warns_and_is_stored(self):
        cfg = self.config()
        _, _, err = self.capture(an.config_set, cfg, "ntfy.topic", "abc")
        self.assertIn("warning: short topic", err)
        self.assertIn("agentbell config set ntfy.topic ", err)
        self.assertEqual(self.saved()["ntfy"]["topic"], "abc")

    def test_a_long_topic_is_silent(self):
        cfg = self.config()
        _, _, err = self.capture(an.config_set, cfg, "ntfy.topic", TOPIC + "x")
        self.assertEqual(err, "")


class TestInitKeepsQuietHours(_Sandbox):
    """D11/D12: Enter keeps the current windows; 'none' clears them; init
    and config set parse them with the same function."""

    def setUp(self):
        super().setUp()
        cfg = self.config()
        cfg.data["quiet_hours"] = [{"start": "22:00", "end": "07:00"}]
        cfg.data["quiet_hours_mode"] = "defer"
        cfg.save()

    def test_enter_keeps_the_windows(self):
        # server, subscribed, Telegram, quiet hours, mode: Enter everywhere
        (code, _, _), prompts = self.run_init_interactive(["", "", "", "", ""])
        self.assertIsNone(code)
        self.assertIn("[22:00-07:00]", [p for p in prompts if "Quiet hours" in p][0])
        saved = self.saved()
        self.assertEqual(saved["quiet_hours"], [{"start": "22:00", "end": "07:00"}])
        self.assertEqual(saved["quiet_hours_mode"], "defer")

    def test_none_clears_them(self):
        (code, _, _), _ = self.run_init_interactive(["", "", "", "none"])
        self.assertIsNone(code)
        self.assertEqual(self.saved()["quiet_hours"], [])

    def test_the_flag_and_config_set_share_the_parser(self):
        code, _, _ = self.capture(an.cmd_init, base._init_args(
            no_test=True, quiet_hours="none"))
        self.assertIsNone(code)
        self.assertEqual(self.saved()["quiet_hours"], [])
        cfg = an.Config()
        with self.assertRaises(SystemExit) as ctx:
            an.config_set(cfg, "quiet_hours", "22:00-07:30, 7:3O-8:00")
        self.assertIn("invalid quiet-hours window '7:3O-8:00'", str(ctx.exception))
        self.assertEqual(an._coerce_quiet_hours(" 22:00 - 07:30 , 12:00-13:00 "),
                         [{"start": "22:00", "end": "07:30"},
                          {"start": "12:00", "end": "13:00"}])


if __name__ == "__main__":
    unittest.main()
