"""Regression tests for the 2026-09-22 audit, bot cluster.

A2 (SIGTERM is a clean stop), A5 (pid reuse in status/uninstall), the bot
lock race, `bot install-service` failures, "approve 2" on Telegram, chat id
discovery in groups, the two kinds of 409, and bot token hygiene (A8).
"""

import contextlib
import io
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (also points state/config at a temp dir)
import agentbell as an  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Telegram's own wording for the two different 409 Conflicts
TG_409_POLLER = (
    "HTTP 409 from https://api.telegram.org/bot<redacted>/getUpdates?timeout=25: "
    '{"ok":false,"error_code":409,"description":"Conflict: terminated by other '
    'getUpdates request; make sure that only one bot instance is running"}')
TG_409_WEBHOOK = (
    "HTTP 409 from https://api.telegram.org/bot<redacted>/getUpdates?timeout=25: "
    '{"ok":false,"error_code":409,"description":"Conflict: can\'t use getUpdates '
    'method while webhook is active; use deleteWebhook to delete the webhook first"}')


def _lock_path():
    return os.path.join(an.state_dir(), "bot.lock")


def _remove_bot_files():
    for path in (_lock_path(), an._bot_state_path()):
        try:
            os.remove(path)
        except OSError:
            pass


# The child imports agentbell from this checkout, points it at the mock Bot
# API and stands in for a licensed install. Everything else is the real CLI.
_BOT_CHILD = """\
import sys
sys.path.insert(0, sys.argv[1])
import agentbell as an
an.TG_API_BASE = sys.argv[2]
an.premium_enabled = lambda cfg: True
an.main(["bot", "run"])
"""


class TestBotStopsCleanlyOnSigterm(unittest.TestCase):
    """A2: `systemctl stop` / `kill <pid>` must not look like a crash."""

    @unittest.skipIf(os.name == "nt", "SIGTERM cannot be caught on Windows")
    def test_sigterm_exits_zero_and_releases_the_lock(self):
        tg = base.MockTelegram()
        sandbox = tempfile.mkdtemp(prefix="agentbell-sigterm-")
        try:
            env = dict(os.environ)
            for key in ("AGENTBELL_CONFIG", an.LICENSE_ENV):
                env.pop(key, None)
            for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
                env[key] = os.path.join(sandbox, key.lower())
            env[an.CONFIG_DIR_ENV] = os.path.join(sandbox, "config")
            env[an.STATE_DIR_ENV] = state = os.path.join(sandbox, "state")
            os.makedirs(env[an.CONFIG_DIR_ENV])
            with open(os.path.join(env[an.CONFIG_DIR_ENV], "config.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"telegram": {"bot_token": "123:abc", "chat_id": "42"},
                           "channels": ["telegram"]}, fh)
            # an answered button proves the child is inside its poll loop,
            # i.e. past installing the SIGTERM handler
            approval_id = "abcdef0123456789"
            os.makedirs(os.path.join(state, "tg-pending"))
            with open(os.path.join(state, "tg-pending", f"{approval_id}.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"approval_id": approval_id, "created": time.time(),
                           "expires": time.time() + 600}, fh)
            tg.queue_update({"update_id": 7, "callback_query": {
                "id": "cq", "data": f"agentbell|{approval_id}|approved",
                "message": {"message_id": 1, "chat": {"id": 42}, "text": "Q?"}}})
            script = os.path.join(sandbox, "bot_child.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(_BOT_CHILD)
            child = subprocess.Popen(
                [sys.executable, script, ROOT, tg.url], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            answer = os.path.join(state, "tg-answers", f"{approval_id}.json")
            deadline = time.monotonic() + 30
            while not os.path.exists(answer) and child.poll() is None \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(answer), "bot never answered the button")
            self.assertTrue(os.path.exists(os.path.join(state, "bot.lock")))
            child.send_signal(signal.SIGTERM)
            out, err = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, f"stdout={out!r} stderr={err!r}")
            self.assertIn("bot stopped", out)
            # released: the file stays, emptied (a clean stop, not a crash)
            with open(os.path.join(state, "bot.lock"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "")
        finally:
            if "child" in locals() and child.poll() is None:
                child.kill()
                child.communicate()
            tg.stop()
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_service_managers_restart_crashes_only(self):
        # a non-zero exit (crash) restarts; the exit 0 of a requested stop
        # does not - under systemd and under launchd alike
        self.assertIn("Restart=on-failure", an.SYSTEMD_UNIT)
        plist = plistlib.loads(an.LAUNCHD_PLIST.format(
            arguments="<string>/opt/agentbell/bin/agentbell</string>",
            environment="").encode("utf-8"))
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["ProgramArguments"][1:], ["bot", "run"])


class TestBotLivenessIgnoresReusedPids(base._TelegramFixture):
    """A5: status, uninstall, doctor and ask ask the bot lock, not a pid."""

    def setUp(self):
        super().setUp()
        _remove_bot_files()
        # purge_report() looks at the home directory; never the real one
        self.home = tempfile.mkdtemp(prefix="agentbell-liveness-")
        self.old_home = base._set_home(self.home)

    def tearDown(self):
        base._restore_home(self.old_home)
        shutil.rmtree(self.home, ignore_errors=True)
        _remove_bot_files()

    def _status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            an.print_bot_status(self._tg_cfg())
        return out.getvalue()

    def test_a_reused_pid_is_not_a_running_bot(self):
        # the pid of a live process (this one) that holds no bot lock
        record = {"pid": os.getpid(), "ts": time.time(), "start": "not-this-process"}
        an.write_json_atomic(an._bot_state_path(), record)
        an.write_json_atomic(_lock_path(), record)
        status = self._status()
        self.assertIn("bot:       NOT running", status)
        self.assertIn("lock:      stale", status)
        self.assertFalse(an.bot_heartbeat_fresh())       # doctor and ask buttons
        self.assertFalse(any("bot is running" in w for w in an.purge_report()["warnings"]))

    def test_the_live_bot_is_still_reported(self):
        self.addCleanup(an.release_bot_lock, an.acquire_bot_lock())
        an.write_bot_heartbeat()
        status = self._status()
        self.assertIn("bot:       running", status)
        self.assertIn("lock:      held by the running bot", status)
        self.assertTrue(an.bot_heartbeat_fresh())
        self.assertTrue(any("bot is running" in w for w in an.purge_report()["warnings"]))


class TestBotLockRace(unittest.TestCase):
    """Two bots that start together must not both get the lock."""

    def setUp(self):
        _remove_bot_files()

    def tearDown(self):
        _remove_bot_files()

    def test_two_starts_over_a_stale_lock_do_not_both_win(self):
        an.write_json_atomic(_lock_path(), {"pid": 99999999, "start": "gone"})
        first = an.acquire_bot_lock()
        try:
            with self.assertRaises(SystemExit) as refused:
                an.acquire_bot_lock()
            self.assertIn(f"already running (pid {os.getpid()})", str(refused.exception.code))
        finally:
            an.release_bot_lock(first)

    def test_a_released_lock_lets_the_next_bot_start(self):
        an.release_bot_lock(an.acquire_bot_lock())
        with open(_lock_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")     # stopped, not crashed
        self.assertFalse(an.bot_running())
        an.release_bot_lock(an.acquire_bot_lock())


@unittest.skipIf(os.name == "nt", "no service installer on Windows (by design)")
class TestInstallServiceReportsFailure(unittest.TestCase):
    """`bot install-service` exited 0 when nothing was installed."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentbell-service-")
        self.old_home = base._set_home(self.home)
        cfg = an.Config({"telegram": {"bot_token": "123:abc", "chat_id": "42"}})
        self.calls = []
        self.returncode = 0
        for patch in (
            unittest.mock.patch.object(an, "Config", lambda *a, **k: cfg),
            unittest.mock.patch.object(an, "premium_enabled", lambda cfg: True),
            unittest.mock.patch.object(an, "check_license_key", lambda key: True),
            # never the real systemctl/launchctl of this machine
            unittest.mock.patch.object(an.subprocess, "run", self._fake_run),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        base._restore_home(self.old_home)
        shutil.rmtree(self.home, ignore_errors=True)

    def _fake_run(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        # `launchctl unload` of a job that is not loaded may fail; that is fine
        return subprocess.CompletedProcess(cmd, 0 if "unload" in cmd else self.returncode)

    def _install(self):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                an.cmd_bot(base._Args(sub="install-service"))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def _systemd(self, present=True):
        real_isdir = os.path.isdir

        def isdir(path):
            if path == "/run/systemd/system":
                return present
            return real_isdir(path)

        with unittest.mock.patch.object(an.sys, "platform", "linux"), \
                unittest.mock.patch.object(an.os.path, "isdir", isdir), \
                unittest.mock.patch.object(an.shutil, "which",
                                           lambda name: "/usr/bin/systemctl"):
            yield

    def test_failing_systemctl_is_an_error(self):
        self.returncode = 1
        with self._systemd():
            code, out, err = self._install()
        self.assertEqual(code, 1)
        self.assertIn("NOT started", err)
        self.assertIn("'systemctl --user daemon-reload' failed (exit 1)", err)
        self.assertNotIn("enabled and started", out)

    def test_failing_enable_is_an_error(self):
        def run(cmd, *args, **kwargs):
            self.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1 if "enable" in cmd else 0)

        with self._systemd(), unittest.mock.patch.object(an.subprocess, "run", run):
            code, _out, err = self._install()
        self.assertEqual(code, 1)
        self.assertIn("'systemctl --user enable agentbell-bot' failed (exit 1)", err)

    def test_missing_systemctl_binary_is_an_error(self):
        def run(cmd, *args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", cmd[0])

        with self._systemd(), unittest.mock.patch.object(an.subprocess, "run", run):
            code, _out, err = self._install()
        self.assertEqual(code, 1)
        self.assertIn("could not run", err)

    def test_no_systemd_is_an_error_with_the_workaround(self):
        with self._systemd(present=False):
            code, _out, err = self._install()
        self.assertEqual(code, 1)
        self.assertIn("NOT started", err)
        self.assertIn("nohup", err)
        self.assertEqual(self.calls, [])

    def test_unwritable_service_dir_is_a_clear_error(self):
        with self._systemd():
            blocker = os.path.dirname(os.path.dirname(an.systemd_unit_path()))
            os.makedirs(os.path.dirname(blocker), exist_ok=True)
            with open(blocker, "w", encoding="utf-8") as fh:
                fh.write("not a directory")
            code, _out, _err = self._install()
        self.assertIsInstance(code, str)
        self.assertIn("could not write the service file", code)
        self.assertEqual(self.calls, [])

    def test_failing_launchctl_is_an_error(self):
        self.returncode = 1
        with unittest.mock.patch.object(an.sys, "platform", "darwin"):
            code, _out, err = self._install()
        self.assertEqual(code, 1)
        self.assertIn("'launchctl load", err)
        self.assertTrue(os.path.exists(an.launchd_plist_path()))

    def test_success_still_exits_zero(self):
        with self._systemd():
            code, out, err = self._install()
        self.assertEqual(code, 0, err)
        self.assertIn("enabled and started", out)
        self.assertIn(["systemctl", "--user", "enable", "agentbell-bot"], self.calls)
        self.assertIn(["systemctl", "--user", "restart", "agentbell-bot"], self.calls)


class TestTypedApproveIsNotAButton(base._TelegramFixture):
    """'approve 2' typed on Telegram is a reply, not the APPROVED <id> body."""

    APPROVAL_ID = "abcdef0123456789"

    def test_parse_requires_this_questions_id(self):
        self.assertEqual(an._parse_answer("approve 2", approval_id=self.APPROVAL_ID),
                         ("answer", "approve 2"))
        self.assertEqual(an._parse_answer("approve add", approval_id=self.APPROVAL_ID),
                         ("answer", "approve add"))
        self.assertEqual(
            an._parse_answer(f"APPROVED {self.APPROVAL_ID}", approval_id=self.APPROVAL_ID),
            ("approved", ""))
        self.assertEqual(
            an._parse_answer(f"DENIED {self.APPROVAL_ID}", approval_id=self.APPROVAL_ID),
            ("denied", ""))
        # typed by hand from the question's "ID:" line, in any case
        self.assertEqual(an._parse_answer(f"approve {self.APPROVAL_ID.upper()}",
                                          approval_id=self.APPROVAL_ID), ("approved", ""))
        # a typed denial with a stray number still denies
        self.assertEqual(an._parse_answer("denied 2", approval_id=self.APPROVAL_ID)[0],
                         "denied")

    def test_telegram_text_reply_approve_2_does_not_approve(self):
        cfg = self._tg_cfg()
        before = len(self.tg.requests)
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(
            result=an.run_ask(cfg, "Which option?", timeout_seconds=20, print_status=False)),
            daemon=True)
        thread.start()
        sent = None
        deadline = time.monotonic() + 20
        while sent is None and time.monotonic() < deadline:
            sent = next((r for r in self.tg.requests[before:]
                         if r["method"] == "sendMessage"), None)
            time.sleep(0.05)
        self.assertIsNotNone(sent)
        # the pending marker learns the question's message id right after send
        pending = None
        while time.monotonic() < deadline:
            pending = next(iter(an.pending_markers("tg-pending")), None)
            if pending and pending.get("question_message_id"):
                break
            time.sleep(0.05)
        self.assertTrue(pending and pending.get("question_message_id"))
        self.tg.queue_update({"update_id": 900, "message": {
            "message_id": pending["question_message_id"] + 1,
            "chat": {"id": 42, "type": "private"}, "text": "approve 2"}})
        an.bot_poll_once(cfg, poll_timeout=1)
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        outcome = holder["result"]
        self.assertFalse(outcome["approved"])
        self.assertFalse(outcome["denied"])
        self.assertEqual(outcome["answer"], "approve 2")


class TestChatIdDiscoverySkipsGroups(unittest.TestCase):
    PRIVATE = {"update_id": 1, "message": {"chat": {"id": 42, "type": "private"}}}
    GROUP = {"update_id": 2, "message": {"chat": {"id": -1001, "type": "supergroup"}}}
    CHANNEL = {"update_id": 3, "channel_post": {"chat": {"id": -1002, "type": "channel"}}}

    def _find(self, updates):
        with unittest.mock.patch.object(an.TelegramChannel, "_call",
                                        staticmethod(lambda *a, **k: updates)):
            return an.TelegramChannel.find_chat_id("123:abc")

    def test_only_a_private_chat_is_taken(self):
        self.assertEqual(self._find([self.PRIVATE, self.GROUP, self.CHANNEL]), 42)
        self.assertIsNone(self._find([self.GROUP]))
        self.assertIsNone(self._find([self.CHANNEL]))

    def test_init_says_why_it_asks_for_the_chat_id(self):
        directory = tempfile.mkdtemp(prefix="agentbell-init-")
        path = os.path.join(directory, "config.json")
        old_home = base._set_home(directory)
        answers = iter(["",          # subscribed on the phone
                        "y",         # configure Telegram
                        "123:abc",   # bot token
                        "",          # message sent to the bot
                        "42",        # chat id typed by hand
                        ""])         # no quiet hours
        stdout = io.StringIO()
        try:
            with unittest.mock.patch.dict(os.environ, {"AGENTBELL_CONFIG": path}), \
                    unittest.mock.patch.object(an.sys.stdin, "isatty", return_value=True), \
                    unittest.mock.patch("builtins.input", lambda prompt="": next(answers)), \
                    unittest.mock.patch.object(an, "premium_enabled", lambda cfg: True), \
                    unittest.mock.patch.object(an.TelegramChannel, "validate_token",
                                               staticmethod(lambda token: "testbot")), \
                    unittest.mock.patch.object(an.TelegramChannel, "_call",
                                               staticmethod(lambda *a, **k: [self.GROUP])), \
                    contextlib.redirect_stdout(stdout):
                an.cmd_init(base._init_args(
                    non_interactive=False, server=an.DEFAULT_NTFY_SERVER,
                    topic="initchat-topic-0123456789abcdef", no_test=True))
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        finally:
            base._restore_home(old_home)
            shutil.rmtree(directory, ignore_errors=True)
        self.assertIn("No private message to the bot found", stdout.getvalue())
        self.assertEqual(saved["telegram"]["chat_id"], "42")


class TestTelegram409IsReportedAccurately(base._TelegramFixture):
    def test_the_two_conflicts_and_other_errors(self):
        self.assertIn("another program is polling", an._bot_poll_error(TG_409_POLLER))
        self.assertIn("webhook is active", an._bot_poll_error(TG_409_WEBHOOK))
        # the same description on the HTTP 200 {"ok": false} path
        self.assertIn("another program is polling", an._bot_poll_error(
            "Telegram error: Conflict: terminated by other getUpdates request; "
            "make sure that only one bot instance is running"))
        # "409" inside an offset is not a conflict
        bad_gateway = ("HTTP 502 from https://api.telegram.org/bot<redacted>/getUpdates"
                       "?timeout=25&offset=1409: Bad Gateway")
        self.assertEqual(an._bot_poll_error(bad_gateway), bad_gateway)

    def test_bot_status_shows_the_second_poller(self):
        _remove_bot_files()
        real_sleep = time.sleep

        def sleep(seconds):
            # the loop's error backoff: stop the bot there, like Ctrl-C
            if threading.current_thread() is threading.main_thread():
                raise KeyboardInterrupt
            real_sleep(seconds)

        err, out = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(an, "bot_poll_once",
                                        side_effect=RuntimeError(TG_409_POLLER)), \
                unittest.mock.patch.object(an.time, "sleep", sleep), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            an.run_bot(self._tg_cfg(), poll_timeout=1)
        self.assertIn("another program is polling this bot token", err.getvalue())
        self.assertNotIn("webhook", err.getvalue())
        self.assertIn("another program is polling", an._read_bot_state()["last_error"])
        self.assertFalse(an.bot_running())
        _remove_bot_files()


class TestBotTokenHygiene(unittest.TestCase):
    """A8: one clear message; invisible characters never reach urllib."""

    def _prompt(self, *answers):
        replies = iter(answers)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            token = an.prompt_bot_token(reader=lambda prompt: next(replies))
        return token, out.getvalue()

    def test_paste_debris_around_the_token_is_dropped(self):
        self.assertEqual(an._telegram_token("\ufeff123:abc\u200b\r\n"), "123:abc")
        self.assertEqual(an._telegram_token(" \u2060123:abc\u200d "), "123:abc")

    def test_invisible_character_inside_is_a_clean_error_not_a_traceback(self):
        seen = []

        def http_request(url, *args, **kwargs):
            seen.append(url)
            return 200, b'{"ok": true, "result": {"username": "x"}}'

        for token in ("123:ab\u200bcdef", "123:ab\ufeffcdef", "123:abc\u00e9def"):
            with unittest.mock.patch.object(an, "http_request", http_request):
                with self.assertRaises(an.PermanentError) as caught:
                    an.TelegramChannel._call(token, "getMe")
            self.assertNotIn("cdef", str(caught.exception))
            self.assertIn("invisible character", str(caught.exception))
        self.assertEqual(seen, [])

    def test_a_malformed_token_is_reported_once(self):
        token, out = self._prompt("123:ab c", "")
        self.assertIsNone(token)
        self.assertEqual(out.count("invalid bot token"), 1, out)
        self.assertNotIn("Telegram rejected", out)
        token, out = self._prompt("123:ab\u200bc", "")
        self.assertIsNone(token)
        self.assertIn("invisible character", out)

    def test_a_pasted_token_is_saved_clean(self):
        with unittest.mock.patch.object(an.TelegramChannel, "_call",
                                        staticmethod(lambda *a, **k: {"username": "b"})):
            token, _out = self._prompt("\ufeff123:abc\u200b")
        self.assertEqual(token, "123:abc")

    def test_a_token_telegram_rejects_names_telegram_once(self):
        def rejected(*args, **kwargs):
            raise an.PermanentError("HTTP 401 from https://api.telegram.org/bot<redacted>"
                                    "/getMe: Unauthorized")

        with unittest.mock.patch.object(an.TelegramChannel, "_call", staticmethod(rejected)):
            token, out = self._prompt("123:abc", "")
        self.assertIsNone(token)
        self.assertEqual(out.count("invalid bot token"), 1, out)
        self.assertIn("Telegram rejected it", out)
        self.assertIn("Unauthorized", out)


if __name__ == "__main__":
    unittest.main()
