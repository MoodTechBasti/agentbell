"""Findings of the pre-release review after 1.7.0.

SAVE    `config set` and `init` died with a traceback when config.json is a
        symlink into a read-only target (Nix/home-manager); the writer's
        message was right but nothing caught it
CHAT    `init --telegram-token` without `--telegram-chat` erased the chat id
        that was already saved
DOCTOR  a Telegram channel that is listed but not set up was reported as
        "not configured", without saying what is missing or how to add it
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agentbell as an  # noqa: E402
import test_agentbell as base  # noqa: E402

CAN_SYMLINK = os.name != "nt"
# root ignores file modes, so a read-only target would be written anyway
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

NTFY = {"server": "https://ntfy.example", "topic": "prerelease-topic-0123456789abcdef"}


def _config_file(directory, data, name="config.json"):
    path = os.path.join(directory, name)
    cfg = an.Config(an.default_config(), path=path)
    an._deep_merge(cfg.data, data)
    cfg.save()
    return path


def _exit_message(func, *args):
    """What the process would print and exit with: a SystemExit carrying text."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            func(*args)
        except SystemExit as exc:
            return exc.code
    return None


@unittest.skipUnless(CAN_SYMLINK, "os.symlink requires admin or developer mode on Windows")
@unittest.skipIf(IS_ROOT, "root writes read-only files anyway")
class TestConfigSaveOnAReadOnlyLink(unittest.TestCase):
    """SAVE: agentbell's own config.json, linked into a read-only store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._remove_tree)
        self.store = os.path.join(self.tmp, "store")
        os.makedirs(self.store)
        self.target = _config_file(self.store, {"ntfy": NTFY})
        os.chmod(self.target, 0o444)
        os.chmod(self.store, 0o555)
        os.makedirs(os.path.join(self.tmp, "cfg"))
        self.link = os.path.join(self.tmp, "cfg", "config.json")
        os.symlink(self.target, self.link)
        self.before = self._read(self.target)

    def _remove_tree(self):
        os.chmod(self.store, 0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _assert_clean_refusal(self, message):
        self.assertIsInstance(message, str, "a message, not a traceback")
        self.assertTrue(message.startswith(f"{an.PROG}: cannot write "), message)
        self.assertIn("a symlink to", message)
        self.assertIn("Nix/home-manager", message)
        self.assertEqual(self._read(self.target), self.before)
        self.assertTrue(os.path.islink(self.link))

    def test_config_set_exits_with_the_writers_message(self):
        with unittest.mock.patch.dict(os.environ, {"AGENTBELL_CONFIG": self.link}):
            message = _exit_message(
                an.main, ["config", "set", "ntfy.topic", "another-topic-0123456789abcdef"])
        self._assert_clean_refusal(message)

    def test_init_exits_with_the_same_message(self):
        args = base._init_args(topic="another-topic-0123456789abcdef", no_test=True)
        with unittest.mock.patch.dict(os.environ, {"AGENTBELL_CONFIG": self.link}):
            message = _exit_message(an.cmd_init, args)
        self._assert_clean_refusal(message)

    def test_the_command_line_prints_no_traceback(self):
        env = dict(os.environ, AGENTBELL_CONFIG=self.link, HOME=self.tmp,
                   AGENTBELL_STATE_DIR=os.path.join(self.tmp, "state"))
        proc = subprocess.run(
            [sys.executable, an.__file__, "config", "set", "ntfy.topic",
             "another-topic-0123456789abcdef"],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("cannot write", proc.stderr)
        self.assertEqual(self._read(self.target), self.before)


class TestInitKeepsTheStoredChatId(unittest.TestCase):
    """CHAT: a new bot token is not a reason to forget the chat."""

    def _init(self, saved, **flags):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = _config_file(directory, {"ntfy": NTFY, **saved})
        args = base._init_args(no_test=True, no_hooks=True, **flags)
        with unittest.mock.patch.dict(os.environ, {"AGENTBELL_CONFIG": path}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            an.cmd_init(args)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_new_token_alone_keeps_the_saved_chat_id(self):
        out = self._init({"telegram": {"bot_token": "111:old", "chat_id": "42"}},
                         telegram_token="222:new")
        self.assertEqual(out["telegram"], {"bot_token": "222:new", "chat_id": "42"})
        self.assertEqual(out["channels"], ["ntfy", "telegram"])

    def test_an_explicit_chat_id_replaces_the_saved_one(self):
        out = self._init({"telegram": {"bot_token": "111:old", "chat_id": "42"}},
                         telegram_token="222:new", telegram_chat="77")
        self.assertEqual(out["telegram"], {"bot_token": "222:new", "chat_id": "77"})

    def test_a_token_without_any_chat_id_stays_unusable_and_unlisted(self):
        out = self._init({}, telegram_token="222:new")
        self.assertEqual(out["telegram"], {"bot_token": "222:new", "chat_id": None})
        self.assertEqual(out["channels"], ["ntfy"])

    def test_no_telegram_flag_leaves_the_saved_telegram_alone(self):
        out = self._init({"telegram": {"bot_token": "111:old", "chat_id": "42"},
                          "channels": ["ntfy", "telegram"]})
        self.assertEqual(out["telegram"], {"bot_token": "111:old", "chat_id": "42"})


class TestDoctorExplainsAnUnsetTelegram(unittest.TestCase):
    """DOCTOR: say what is missing and what to run, and that ntfy still works."""

    def _telegram_check(self, telegram, channels):
        ntfy = base.MockNtfy()      # an answering server: doctor polls it, and an
        self.addCleanup(ntfy.stop)  # unreachable one costs the full poll timeout
        cfg = base.make_config(ntfy.url)
        cfg.data["telegram"] = telegram
        cfg.data["channels"] = channels
        rows = [c for c in an.doctor_checks(cfg) if c["name"] == "telegram"]
        return rows[0] if rows else None

    def test_nothing_set_up_names_both_gaps_and_both_ways_out(self):
        check = self._telegram_check({"bot_token": None, "chat_id": None},
                                     ["ntfy", "telegram"])
        self.assertEqual(check["status"], an.FAIL)
        self.assertIn("bot token", check["detail"])
        self.assertIn("chat id", check["detail"])
        self.assertIn("ntfy still", check["detail"])
        self.assertIn("agentbell init", check["fix"])
        self.assertIn("config set channels ntfy", check["fix"])

    def test_a_missing_chat_id_is_the_only_gap_named(self):
        check = self._telegram_check({"bot_token": "1:x", "chat_id": None},
                                     ["ntfy", "telegram"])
        self.assertEqual(check["status"], an.FAIL)
        self.assertIn("chat id", check["detail"])
        self.assertNotIn("bot token", check["detail"])
        self.assertIn("config set telegram.chat_id", check["fix"])

    def test_a_missing_bot_token_is_the_only_gap_named(self):
        check = self._telegram_check({"bot_token": None, "chat_id": "42"},
                                     ["ntfy", "telegram"])
        self.assertEqual(check["status"], an.FAIL)
        self.assertIn("bot token", check["detail"])
        self.assertNotIn("chat id", check["detail"])
        self.assertIn("init --telegram-token", check["fix"])
        self.assertIn("chat id", check["fix"])        # it says the saved id is kept

    def test_half_set_up_credentials_are_noticed_even_when_unlisted(self):
        check = self._telegram_check({"bot_token": "1:x", "chat_id": None}, ["ntfy"])
        self.assertIsNotNone(check, "a saved token with no chat id deserves a row")
        self.assertEqual(check["status"], an.WARN)
        self.assertIn("chat id", check["detail"])

    def test_no_credentials_and_not_listed_stays_silent(self):
        self.assertIsNone(self._telegram_check({"bot_token": None, "chat_id": None},
                                               ["ntfy"]))

    def test_a_complete_setup_is_not_a_failure(self):
        check = self._telegram_check({"bot_token": "1:x", "chat_id": "42"},
                                     ["ntfy", "telegram"])
        self.assertIsNone(check, "a complete setup only gets the 'telegram bot' row")


if __name__ == "__main__":
    unittest.main()
