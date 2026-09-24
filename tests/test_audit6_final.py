"""Regression tests for the last leftovers before 1.7.0.

BUY    the purchase hints pointed to a dead checkout ("Reply to your purchase
       email", "€4.99"); they now say how to get a key: e-mail or an issue
MCP    `mcp add` listed the MCP tools without agent, yes_label and no_label;
       the list now comes from MCP_TOOLS
AUTH   `config set ntfy.auth none` warned about a plain-http credential while
       clearing it, and showed the cleared value as "<redacted>"
DIRS   `uninstall --yes` left the empty .cursor/rules, .windsurf/rules,
       .continue/rules and .clinerules folders behind
LOCK   an ntfy marker that could not be locked, with Telegram configured,
       ended in a thread traceback and an ask on Telegram alone; now the
       ask fails with one error line and exit 3
"""

import contextlib
import errno
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: its classes do not re-run)
import agentbell as an  # noqa: E402

EMAIL = "basti@moodtechsolutions.com"
ISSUES = "https://github.com/MoodTechBasti/agentbell/issues"


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _temp_config(data):
    """A config file of its own, named by AGENTBELL_CONFIG for the test."""
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "config.json")
    cfg = an.Config(an.default_config(), path=path)
    an._deep_merge(cfg.data, data)
    cfg.save()
    return directory, path


class TestPurchaseHints(unittest.TestCase):
    """BUY: every hint names a way to get a key that works."""

    def test_an_invalid_key_points_to_email_and_issues(self):
        with self.assertRaises(SystemExit) as caught:
            an.cmd_license(_Args(sub="activate", key="AB1-not-a-key"))
        message = str(caught.exception.code)
        self.assertIn("invalid license key", message)
        self.assertIn(EMAIL, message)
        self.assertIn(ISSUES, message)
        self.assertNotIn("purchase", message)

    def test_the_premium_refusal_says_how_to_get_a_key(self):
        self.assertIn(EMAIL, an.LICENSE_PREMIUM_MSG)
        self.assertIn(ISSUES, an.LICENSE_PREMIUM_MSG)
        self.assertNotIn("€", an.LICENSE_PREMIUM_MSG)

    def test_init_offers_telegram_with_a_way_to_get_a_key(self):
        directory, path = _temp_config({"ntfy": {"server": "https://ntfy.example",
                                                 "topic": "buy-topic-0123456789abcdef"}})
        self.addCleanup(shutil.rmtree, directory)
        prompts = []

        def answer(prompt=""):
            prompts.append(prompt)
            return "y" if "Configure Telegram" in prompt else ""

        args = base._init_args(non_interactive=False, no_test=True, no_hooks=True)
        stdout = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"AGENTBELL_CONFIG": path}), \
                unittest.mock.patch.object(an.sys.stdin, "isatty", return_value=True), \
                unittest.mock.patch("builtins.input", side_effect=answer), \
                unittest.mock.patch.object(an, "http_request", return_value=(200, b"")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop(an.LICENSE_ENV, None)
            an.cmd_init(args)
        self.assertTrue(any("License key" in prompt for prompt in prompts), prompts)
        out = stdout.getvalue()
        self.assertIn(EMAIL, out)
        self.assertIn(ISSUES, out)
        self.assertNotIn("€", out)


class TestMcpAddToolList(unittest.TestCase):
    """MCP: the tools `mcp add` announces are the ones the server offers."""

    def test_the_signatures_come_from_the_schemas(self):
        self.assertEqual([an.mcp_tool_signature(tool) for tool in an.MCP_TOOLS],
                         ["notify(message, title?, priority?, tags?, agent?)",
                          "ask_approval(message, timeout_seconds?, yes_label?, no_label?)"])

    def test_mcp_add_prints_every_parameter(self):
        project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            an.cmd_mcp(_Args(sub="add", print_only=False, client=["cursor"], project=project))
        out = stdout.getvalue()
        for tool in an.MCP_TOOLS:
            self.assertIn(an.mcp_tool_signature(tool), out)
        for name in ("agent?", "yes_label?", "no_label?"):
            self.assertIn(name, out)


class TestClearingACredential(unittest.TestCase):
    """AUTH: no cleartext warning for a credential that is being removed."""

    def setUp(self):
        directory, self.path = _temp_config({"ntfy": {
            "server": "http://127.0.0.1:9", "topic": "auth-topic-0123456789abcdef",
            "auth": "user:pass", "action_auth": "tk_publish_only"}})
        self.addCleanup(shutil.rmtree, directory)

    def _set(self, key, value):
        stdout, stderr = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"AGENTBELL_CONFIG": self.path}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            an.cmd_config(_Args(sub="set", key=key, value=value))
        return stdout.getvalue(), stderr.getvalue()

    def test_clearing_is_silent_and_shows_null(self):
        for key in ("ntfy.auth", "ntfy.action_auth"):
            out, err = self._set(key, "none")
            self.assertNotIn("plain http", err, key)
            self.assertEqual(out.strip(), f"{key} = null")
        self.assertIsNone(an.Config(path=self.path).data["ntfy"]["auth"])

    def test_setting_one_still_warns_and_is_redacted(self):
        out, err = self._set("ntfy.auth", "other:secret")
        self.assertIn("plain http", err)
        self.assertEqual(out.strip(), 'ntfy.auth = "<redacted>"')
        self.assertNotIn("other:secret", out + err)

    def test_a_server_change_warns_about_the_credential_that_stays(self):
        _out, err = self._set("ntfy.server", "http://127.0.0.1:9")
        self.assertIn("plain http", err)


class TestUninstallRemovesEmptyRuleFolders(unittest.TestCase):
    """DIRS: the folders agentbell's rule files sat in go when they end up empty."""

    AGENTS = ("cursor", "windsurf", "continue", "cline")
    FOLDERS = (".cursor", ".windsurf", ".continue", ".clinerules")

    def setUp(self):
        self.project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.project)
        with contextlib.redirect_stdout(io.StringIO()):
            for agent in self.AGENTS:
                an.install_hooks(agent, project=self.project)
        for folder in self.FOLDERS:
            self.assertTrue(os.path.isdir(os.path.join(self.project, folder)), folder)

    def _uninstall(self):
        notes = {}
        for entry in an._project_entries(self.project):
            if entry["label"].split()[0] in self.AGENTS:
                result = entry["apply"]()
                self.assertTrue(result["changed"], entry["label"])
                notes[entry["label"].split()[0]] = result["notes"]
        return notes

    def test_empty_folders_are_removed_and_reported(self):
        plan = [entry["action"] for entry in an._project_entries(self.project)
                if entry["label"].split()[0] in self.AGENTS]
        self.assertTrue(all("if that leaves them empty" in action for action in plan), plan)
        notes = self._uninstall()
        self.assertEqual(os.listdir(self.project), [])
        rules = os.path.join(self.project, ".cursor", "rules")
        self.assertEqual(notes["cursor"], [
            f"removed the empty folder(s) {rules}, {os.path.dirname(rules)}"])
        self.assertEqual(notes["cline"], [
            f"removed the empty folder(s) {os.path.join(self.project, '.clinerules')}"])

    def test_a_folder_with_other_files_stays(self):
        mine = os.path.join(self.project, ".cursor", "rules", "mine.mdc")
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write("my rule\n")
        other = os.path.join(self.project, ".continue", "config.yaml")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("models: []\n")
        notes = self._uninstall()
        self.assertTrue(os.path.isfile(mine))
        self.assertTrue(os.path.isfile(other))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".continue", "rules")))
        self.assertEqual(notes["cursor"], [])
        self.assertEqual(notes["continue"], [
            f"removed the empty folder(s) {os.path.join(self.project, '.continue', 'rules')}"])
        self.assertEqual(sorted(os.listdir(self.project)), [".continue", ".cursor"])

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_linked_folder_is_left_alone(self):
        elsewhere = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, elsewhere)
        cursor = os.path.join(self.project, ".cursor")
        shutil.rmtree(cursor)
        os.symlink(elsewhere, cursor)
        with contextlib.redirect_stdout(io.StringIO()):
            an.install_hooks("cursor", project=self.project)
        notes = self._uninstall()
        self.assertEqual(notes["cursor"], [])
        self.assertTrue(os.path.islink(cursor))
        self.assertTrue(os.path.isdir(os.path.join(elsewhere, "rules")))


class TestMarkerThatCannotBeLocked(base._TelegramFixture):
    """LOCK: no marker lock means no ask, with or without Telegram."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ntfy = base.MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()
        super().tearDownClass()

    def _ask(self, channels, failing):
        """Run `agentbell ask` with the marker lock of `failing` channels
        raising ENOLCK. Returns (exit code, stderr, thread errors, seconds)."""
        directory, path = _temp_config(self._tg_cfg(ntfy_url=self.ntfy.url,
                                                    channels=channels).data)
        self.addCleanup(shutil.rmtree, directory)
        real_create, real_lock = an._create_marker, an._lock_bot_fd
        doomed = set()

        def create(marker):
            fd = real_create(marker)
            if os.path.basename(os.path.dirname(marker)) in failing:
                doomed.add(fd)
            return fd

        def lock(fd, unlock=False, shared=False):
            if fd in doomed and not unlock and not shared:
                doomed.discard(fd)
                raise OSError(errno.ENOLCK, "No locks available")
            return real_lock(fd, unlock=unlock, shared=shared)

        thread_errors = []
        stderr = io.StringIO()
        args = _Args(message="Deploy?", timeout=30, yes_label=None, no_label=None,
                     no_buttons=False, json=True, channel=None)
        started = time.monotonic()
        with unittest.mock.patch.dict(os.environ, {"AGENTBELL_CONFIG": path}), \
                unittest.mock.patch.object(an, "_create_marker", create), \
                unittest.mock.patch.object(an, "_lock_bot_fd", lock), \
                unittest.mock.patch.object(threading, "excepthook",
                                           lambda hook: thread_errors.append(hook.exc_value)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                an.cmd_ask(args)
        return caught.exception.code, stderr.getvalue(), thread_errors, time.monotonic() - started

    def _assert_failed(self, result, channel):
        code, err, thread_errors, seconds = result
        self.assertEqual(code, 3)
        self.assertEqual(thread_errors, [])
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertIn("cannot lock the approval marker", lines[0])
        self.assertIn(channel, lines[0])
        self.assertLess(seconds, 20)
        # a question that went out ends unanswered; none is left open
        for name in ("ntfy-pending", "tg-pending"):
            self.assertTrue(all(marker.get("closed") for marker in an.pending_markers(name)),
                            name)
        self.assertEqual(an.pending_markers("ntfy-pending"), [])

    def test_ntfy_marker_with_telegram_configured(self):
        self._assert_failed(self._ask(("ntfy", "telegram"), {"ntfy-pending"}), "ntfy-pending")

    def test_telegram_marker_with_ntfy_configured(self):
        self._assert_failed(self._ask(("ntfy", "telegram"), {"tg-pending"}), "tg-pending")

    def test_ntfy_alone(self):
        self._assert_failed(self._ask(("ntfy",), {"ntfy-pending"}), "ntfy-pending")


if __name__ == "__main__":
    unittest.main()
