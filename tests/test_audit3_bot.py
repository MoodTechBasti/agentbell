"""Audit 2026-09-22, review round 1, cluster "bot": regression tests.

BOT-1/IW-3  the service runs a command that works from a checkout, and Type=exec
BOT-5       the service gets this shell's config and state paths; a key that
            only lives in the environment is refused
BOT-7       a missing launchctl is an error message, not a traceback
BOT-2       uninstall stops, disables and deletes the bot service
BOT-4/IW-5  the deferred flush shares the bot's drain budget
IW-7        a failed bot.json write does not stop the bot
BOT-9 (BOT-6, BOT-8, IW-10/S5, S9)  the bot lock is a kernel lock
"""

import contextlib
import io
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (also points state/config at a temp dir)
import agentbell as an  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Takes the bot lock in the state dir of its environment and holds it until
# its stdin closes.
_HOLDER = """\
import sys
sys.path.insert(0, sys.argv[1])
import agentbell as an
try:
    lock = an.acquire_bot_lock()
except SystemExit as exc:
    print("refused", exc.code, flush=True)
    sys.exit(0)
print("locked", flush=True)
sys.stdin.read()
an.release_bot_lock(lock)
"""


def _run(func, *args):
    """(exit code, stdout, stderr) of a command function."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            func(*args)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _systemd(present=True):
    """A Linux host with (or without) a running systemd; never the real one."""
    real_isdir = os.path.isdir
    with unittest.mock.patch.object(an.sys, "platform", "linux"), \
            unittest.mock.patch.object(
                an.os.path, "isdir",
                lambda path: present if path == "/run/systemd/system" else real_isdir(path)), \
            unittest.mock.patch.object(
                an.shutil, "which",
                lambda name, *a, **k: "/usr/bin/systemctl" if name == "systemctl" else None):
        yield


class _ServiceFixture(unittest.TestCase):
    """A temp home, a licensed config, agentbell run from a checkout
    (agentbell.py, not executable) and a recording stand-in for systemctl."""

    @classmethod
    def setUpClass(cls):
        cls.keypair = base.dev_keypair()
        cls.keypair.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.keypair.__exit__(None, None, None)

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentbell-audit3-service-")
        self.old_home = base._set_home(self.home)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(base._restore_home, self.old_home)
        self.script = os.path.join(self.home, "checkout", "agentbell.py")
        os.makedirs(os.path.dirname(self.script))
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write("# a checkout\n")
        os.chmod(self.script, 0o644)
        self.cfg = an.Config({"telegram": {"bot_token": "123:abc", "chat_id": "42"},
                              "license": an.make_license_key("c", seed=base.TEST_SEED)})
        self.calls = []
        self.returncode = 0
        for patch in (
            unittest.mock.patch.object(an, "Config", lambda *a, **k: self.cfg),
            unittest.mock.patch.object(an, "agentbell_binary", lambda: self.script),
            unittest.mock.patch.object(an.subprocess, "run", self._fake_run),
            unittest.mock.patch.dict(os.environ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(an.LICENSE_ENV, None)
        # every agent config purge_report() looks at lives in the temp home
        for key in ("XDG_CONFIG_HOME", "XDG_BIN_HOME", "APPDATA", "LOCALAPPDATA"):
            os.environ[key] = os.path.join(self.home, key.lower())
        for key in ("KIMI_CODE_HOME", "QWEN_HOME", "CODEX_HOME"):
            os.environ.pop(key, None)

    def _fake_run(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.returncode)

    def _install(self):
        return _run(an.cmd_bot, base._Args(sub="install-service"))

    def _unit(self):
        with open(an.systemd_unit_path(), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        argv = shlex.split(next(line for line in lines
                                if line.startswith("ExecStart="))[len("ExecStart="):])
        env = dict(shlex.split(line[len("Environment="):])[0].split("=", 1)
                   for line in lines if line.startswith("Environment="))
        return lines, argv, env

    def _pinned(self):
        return {an.CONFIG_FILE_ENV: os.path.abspath(an.config_path()),
                an.STATE_DIR_ENV: os.path.abspath(an.state_dir())}


@unittest.skipIf(os.name == "nt", "no service installer on Windows (by design)")
class TestServiceCommand(_ServiceFixture):
    """BOT-1/IW-3: ExecStart ran agentbell.py itself (mode 644, exit 126),
    and Type=simple let `enable --now` report that as started."""

    def test_unit_runs_the_script_with_python(self):
        with _systemd():
            code, out, err = self._install()
        self.assertEqual(code, 0, err)
        lines, argv, _env = self._unit()
        self.assertEqual(argv, [sys.executable, self.script, "bot", "run"])
        self.assertTrue(os.access(argv[0], os.X_OK))
        self.assertIn("Type=exec", lines)
        # restart, not `enable --now`: a bot already running (an older
        # version, the old unit) must run this unit afterwards
        self.assertEqual(self.calls[1:], [["systemctl", "--user", "enable", "agentbell-bot"],
                                          ["systemctl", "--user", "restart", "agentbell-bot"]])

    def test_plist_runs_the_script_with_python(self):
        with unittest.mock.patch.object(an.sys, "platform", "darwin"):
            code, _out, err = self._install()
        self.assertEqual(code, 0, err)
        with open(an.launchd_plist_path(), "rb") as fh:
            plist = plistlib.load(fh)
        self.assertEqual(plist["ProgramArguments"],
                         [sys.executable, self.script, "bot", "run"])

    def test_no_systemd_hint_runs_the_same_command(self):
        with _systemd(present=False):
            code, _out, err = self._install()
        self.assertEqual(code, 1)
        self.assertIn(f"nohup {shlex.join([sys.executable, self.script])} bot run", err)

    def test_unit_words_are_quoted_for_systemd(self):
        self.assertEqual(an._systemd_quote('a "b" 100% \\x'), '"a \\"b\\" 100%% \\\\x"')


@unittest.skipIf(os.name == "nt", "no service installer on Windows (by design)")
class TestServiceEnvironment(_ServiceFixture):
    """BOT-5: a user manager or launchd does not see the shell's AGENTBELL_*
    variables, so the service read another config (or none)."""

    def test_unit_pins_the_config_and_state_of_this_shell(self):
        with _systemd():
            code, _out, err = self._install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self._unit()[2], self._pinned())

    def test_plist_pins_the_config_and_state_of_this_shell(self):
        with unittest.mock.patch.object(an.sys, "platform", "darwin"):
            code, _out, err = self._install()
        self.assertEqual(code, 0, err)
        with open(an.launchd_plist_path(), "rb") as fh:
            self.assertEqual(plistlib.load(fh)["EnvironmentVariables"], self._pinned())

    def test_a_key_only_in_the_environment_is_refused(self):
        os.environ[an.LICENSE_ENV] = self.cfg.data.pop("license")
        with _systemd():
            code, _out, _err = self._install()
        self.assertIn("agentbell license activate", str(code))
        self.assertFalse(os.path.exists(an.systemd_unit_path()))
        self.assertEqual(self.calls, [])


@unittest.skipIf(os.name == "nt", "no service installer on Windows (by design)")
class TestMissingLaunchctl(_ServiceFixture):
    """BOT-7: `launchctl unload` ran outside _run_service_step."""

    def test_missing_launchctl_is_an_error_message(self):
        def run(cmd, *args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", cmd[0])

        with unittest.mock.patch.object(an.sys, "platform", "darwin"), \
                unittest.mock.patch.object(an.subprocess, "run", run):
            code, _out, err = self._install()
        self.assertEqual(code, 1)
        self.assertIn("'launchctl load", err)
        self.assertIn("could not run", err)


@unittest.skipIf(os.name == "nt", "no service installer on Windows (by design)")
class TestUninstallRemovesTheService(_ServiceFixture):
    """BOT-2: the unit stayed enabled after uninstall and restarted the
    deleted binary every 10 s."""

    def setUp(self):
        super().setUp()
        for patch in (
            # never the real pipx or pip --user site of this machine
            unittest.mock.patch.object(an, "_pipx_installed", return_value=None),
            unittest.mock.patch.object(an, "_user_site_dirs", return_value=(None, None)),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        with _systemd():
            self._install()
        # what `systemctl enable` leaves on disk
        self.wants = os.path.join(os.path.dirname(an.systemd_unit_path()),
                                  "default.target.wants", "agentbell-bot.service")
        os.makedirs(os.path.dirname(self.wants))
        os.symlink(an.systemd_unit_path(), self.wants)
        self.calls.clear()

    def _service_entry(self):
        entries = an.purge_report(project=self.home)["entries"]
        self.assertEqual(entries[0]["kind"], "service")    # before the binary goes
        return entries[0]

    def test_the_service_is_disabled_and_deleted_first(self):
        with _systemd():
            entry = self._service_entry()
            self.assertIn("disable --now agentbell-bot", entry["action"])
            self.assertTrue(entry["apply"]())
        self.assertEqual(self.calls, [
            ["systemctl", "--user", "disable", "--now", "agentbell-bot"],
            ["systemctl", "--user", "daemon-reload"]])
        self.assertFalse(os.path.exists(an.systemd_unit_path()))
        self.assertFalse(os.path.lexists(self.wants))

    def test_a_failed_disable_still_deletes_it_and_says_how_to_stop(self):
        self.returncode = 1
        with _systemd():
            entry = self._service_entry()
            with self.assertRaises(RuntimeError) as failed:
                entry["apply"]()
        self.assertIn("disable --now agentbell-bot' failed (exit 1)", str(failed.exception))
        self.assertIn("systemctl --user stop agentbell-bot", str(failed.exception))
        self.assertFalse(os.path.exists(an.systemd_unit_path()))
        self.assertFalse(os.path.lexists(self.wants))

    def test_without_systemd_the_unit_is_only_deleted(self):
        with _systemd(present=False):
            self.assertTrue(self._service_entry()["apply"]())
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(an.systemd_unit_path()))

    def test_the_launchd_plist_is_unloaded_and_deleted(self):
        with unittest.mock.patch.object(an.sys, "platform", "darwin"):
            self._install()
            self.calls.clear()
            self.assertTrue(self._service_entry()["apply"]())
        self.assertEqual(self.calls, [["launchctl", "unload", an.launchd_plist_path()]])
        self.assertFalse(os.path.exists(an.launchd_plist_path()))


class TestBotLoop(base._TelegramFixture):
    """One cycle of run_bot, with the Telegram poll stubbed out."""

    def setUp(self):
        super().setUp()
        self.polls = 0
        self.drains, self.flushes = [], []
        for patch in (
            unittest.mock.patch.object(an, "bot_poll_once", self._poll),
            unittest.mock.patch.object(
                an, "drain_queue",
                lambda cfg, limit=None, deadline=None: self.drains.append(deadline)),
            unittest.mock.patch.object(
                an, "flush_deferred",
                lambda cfg, deadline=None: self.flushes.append(deadline)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _poll(self, cfg, offset=None, poll_timeout=25):
        self.polls += 1
        if self.polls > 1:
            raise KeyboardInterrupt           # Ctrl-C after one full cycle
        return 1

    def test_the_deferred_flush_shares_the_drain_budget(self):
        """BOT-4/IW-5: flush_deferred had no deadline; a hung ntfy stalled
        the bot for ~100 s, past the 60 s heartbeat."""
        code, out, _err = _run(an.run_bot, self._tg_cfg())
        self.assertEqual(code, 0)
        self.assertIn("bot stopped", out)
        self.assertEqual(len(self.drains), 1)
        self.assertIsNotNone(self.drains[0])
        self.assertEqual(self.flushes, self.drains)

    def test_no_flush_once_the_drain_used_the_budget(self):
        with unittest.mock.patch.object(an, "BOT_DRAIN_BUDGET_SECONDS", -1.0):
            _run(an.run_bot, self._tg_cfg())
        self.assertEqual(len(self.drains), 1)
        self.assertEqual(self.flushes, [])

    def test_a_failed_state_write_does_not_stop_the_bot(self):
        """IW-7: Windows refuses to replace bot.json while another process
        reads it; that PermissionError ended the bot with a traceback."""
        real = an.write_json_atomic

        def write(path, data, mode=None):
            if path == an._bot_state_path():
                raise PermissionError(13, "Access is denied", path)
            return real(path, data, mode=mode)

        with unittest.mock.patch.object(an, "write_json_atomic", write):
            code, out, err = _run(an.run_bot, self._tg_cfg())
        self.assertEqual(code, 0)
        self.assertIn("bot stopped", out)
        self.assertEqual(self.polls, 2)                  # it kept polling
        self.assertIn(f"could not update {an._bot_state_path()}", err)
        self.assertFalse(an.bot_running())               # and released the lock


class TestKernelBotLock(base._TelegramFixture):
    """BOT-9: a kernel lock held for the bot's lifetime replaces the pid
    check, the start tokens and the guard directory."""

    def setUp(self):
        super().setUp()
        self.state = tempfile.mkdtemp(prefix="agentbell-audit3-lock-")
        self.addCleanup(shutil.rmtree, self.state, True)
        patch = unittest.mock.patch.dict(os.environ, {an.STATE_DIR_ENV: self.state})
        patch.start()
        self.addCleanup(patch.stop)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
            for pipe in (child.stdin, child.stdout):
                pipe.close()

    def _spawn(self, code):
        child = subprocess.Popen([sys.executable, "-c", code, ROOT], env=dict(os.environ),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def _status(self):
        return _run(an.print_bot_status, self._tg_cfg())[1]

    def test_a_live_process_with_the_recorded_pid_is_no_bot(self):
        """BOT-6: without /proc (macOS) a reused pid kept the bot 'running'
        and refused every new start."""
        unrelated = self._spawn("import sys; sys.stdin.read()")
        record = {"pid": unrelated.pid, "ts": 0}
        an.write_json_atomic(an._bot_lock_path(), record)
        an.write_json_atomic(an._bot_state_path(), record)
        # the macOS code path before the fix: no start token on this host
        with unittest.mock.patch.object(an, "_process_start_token",
                                        lambda pid=None: "", create=True):
            self.assertFalse(an.bot_running())
            self.assertIn("bot:       NOT running", self._status())
            an.release_bot_lock(an.acquire_bot_lock())

    def test_a_killed_bot_frees_the_lock_at_once(self):
        holder = self._spawn(_HOLDER)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        self.assertTrue(an.bot_running())
        self.assertIn(f"lock:      held by the running bot (pid {holder.pid})", self._status())
        with self.assertRaises(SystemExit) as refused:
            an.acquire_bot_lock()
        self.assertIn(f"already running (pid {holder.pid})", str(refused.exception.code))
        holder.kill()                                  # SIGKILL / TerminateProcess
        holder.communicate()
        self.assertFalse(an.bot_running())
        self.assertIn(f"lock:      stale (left by pid {holder.pid}, which no longer holds it)",
                      self._status())
        an.release_bot_lock(an.acquire_bot_lock())
        self.assertIn("lock:      none", self._status())   # a clean stop empties it

    def test_one_of_several_parallel_starts_wins(self):
        """BOT-8: two starts that broke a stale guard together both won."""
        an.write_json_atomic(an._bot_lock_path(), {"pid": 99999999, "ts": 0})
        holders = [self._spawn(_HOLDER) for _ in range(5)]
        # every loser has answered while the winner still holds the lock
        results = [holder.stdout.readline().strip() for holder in holders]
        self.assertEqual(results.count("locked"), 1, results)
        for result in results:
            if result != "locked":
                self.assertIn("another agentbell bot is already running", result)
        for holder in holders:
            holder.stdin.close()

    def test_a_leftover_guard_directory_does_not_block_a_start(self):
        """BOT-8: a bot.lock.guard left by a killed bot refused starts for 30 s."""
        os.mkdir(an._bot_lock_path() + ".guard")
        an.release_bot_lock(an.acquire_bot_lock())

    def test_the_lock_writes_only_names_uninstall_owns(self):
        """IW-10/S5: bot.lock.guard was not in STATE_DIR_NAMES, so uninstall
        kept it in a shared state dir and called it someone else's."""
        created = []
        real_mkdir, real_open = os.mkdir, os.open

        def record(path):
            if os.path.dirname(os.path.abspath(path)) == os.path.abspath(self.state):
                created.append(os.path.basename(path))

        def mkdir(path, *args, **kwargs):
            record(path)
            return real_mkdir(path, *args, **kwargs)

        def open_(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                record(path)
            return real_open(path, flags, *args, **kwargs)

        with unittest.mock.patch.object(an.os, "mkdir", mkdir), \
                unittest.mock.patch.object(an.os, "open", open_):
            an.release_bot_lock(an.acquire_bot_lock())
        self.assertIn("bot.lock", created)
        mirror = os.path.join(self.state, "mirror")
        os.mkdir(mirror)
        for name in created:
            open(os.path.join(mirror, name), "w").close()
        self.assertEqual(an._split_owned(mirror, an.STATE_DIR_NAMES)[1], [])


if __name__ == "__main__":
    unittest.main()
