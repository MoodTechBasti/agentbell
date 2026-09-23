"""Regression tests for the runtime findings of the audit's fourth review round.

W2-R1  Linux watch: a stop signal that watch's parent relays to watch alone
       (uv run, a nested watch, a wrapper script) reaches the command, even
       though the parent sits in watch's own process group. timeout(1),
       which signals the whole group, still reaches it once.
RBD-1  bot_running(): two probes at the same moment do not see each other
       as a running bot.
RBD-2  uninstall does not tell the user to stop a bot its own first step
       stops.
RBD-3  verify does not judge the ntfy topic of a Telegram-only setup, as
       doctor does not.
"""

import errno
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: sets the suite env)
import test_audit2_watch as w2  # noqa: E402
import test_audit3_diagnostics as d3  # noqa: E402
import test_audit3_watch as w3  # noqa: E402
import agentbell as an  # noqa: E402

# A wrapper that passes SIGTERM and SIGINT on to its child alone, as uv run
# or a Node script does. It writes ".relaying" once it can relay.
RELAY = r'''
import signal, subprocess, sys
state, argv = sys.argv[1], sys.argv[2:]
child = []
def relay(signum, frame):
    child[0].send_signal(signum)
signal.signal(signal.SIGTERM, relay)
signal.signal(signal.SIGINT, relay)
child.append(subprocess.Popen(argv))
open(state + ".relaying", "w").close()
sys.exit(child[0].wait())
'''


@unittest.skipUnless(w3.LINUX_SIGINFO, "the sender of a signal is known on Linux")
class TestWatchUnderASignalingParent(w2._WatchProcessCase):
    """W2-R1: which parent in watch's own group already reached the command."""

    def setUp(self):
        super().setUp()
        with open(self.child, "w", encoding="utf-8") as fh:
            fh.write(w3.SENDER_CHILD)

    def start(self, parent):
        proc = subprocess.Popen(
            parent + self.watch_argv(), env=self.env, cwd=self.tmp,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.addCleanup(self._stop, proc)
        _, watch = self.ready()
        self.assertEqual(os.getpgid(watch), proc.pid)    # one group: parent, watch, command
        w2._wait_for(lambda: w3._blocked(watch, signal.SIGTERM), what="watch to wait")
        return proc, watch

    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    def senders(self):
        with open(self.state + ".senders", encoding="utf-8") as fh:
            return fh.read().split()

    def test_a_relaying_parent_reaches_the_command(self):
        relay = os.path.join(self.tmp, "relay.py")
        with open(relay, "w", encoding="utf-8") as fh:
            fh.write(RELAY)
        proc, watch = self.start([sys.executable, relay, self.state])
        w2._wait_for(lambda: os.path.exists(self.state + ".relaying"), what="the relay")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 4)
        self.assertEqual(self.senders(), ["%d:%d" % (signal.SIGTERM, watch)])
        self.assertIn("failed (exit 4)", self.wait_for_push()[-1]["body"])

    def test_a_nested_watch_reaches_the_command(self):
        proc, watch = self.start([sys.executable, w2.AGENTBELL_PY, "watch", "--quiet", "--"])
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 4)
        self.assertEqual(self.senders(), ["%d:%d" % (signal.SIGTERM, watch)])

    @unittest.skipUnless(shutil.which("timeout"), "timeout(1)")
    def test_timeout_signals_the_group_and_is_not_passed_on_again(self):
        # SIGALRM is timeout's own "time is up"
        proc, _ = self.start(["timeout", "60"])
        proc.send_signal(signal.SIGALRM)
        self.assertEqual(proc.wait(timeout=30), 124)
        self.assertEqual(self.senders(), ["%d:%d" % (signal.SIGTERM, proc.pid)])
        self.assertIn("failed (exit 4)", self.wait_for_push()[-1]["body"])

    @unittest.skipUnless(shutil.which("timeout"), "timeout(1)")
    def test_timeout_foreground_signals_watch_alone(self):
        proc, watch = self.start(["timeout", "-s", "TERM", "--foreground", "60"])
        proc.send_signal(signal.SIGALRM)
        self.assertEqual(proc.wait(timeout=30), 124)
        self.assertEqual(self.senders(), ["%d:%d" % (signal.SIGTERM, watch)])


class TestBotProbe(unittest.TestCase):
    """RBD-1: a probe held the bot's exclusive lock for a moment."""

    def setUp(self):
        state = tempfile.mkdtemp(prefix="agentbell-probe-")
        self.addCleanup(shutil.rmtree, state, True)
        patch = unittest.mock.patch.dict(os.environ, {an.STATE_DIR_ENV: state})
        patch.start()
        self.addCleanup(patch.stop)
        an.release_bot_lock(an.acquire_bot_lock())       # a stopped bot's empty file

    @unittest.skipIf(os.name == "nt", "flock")
    def test_another_probe_is_no_bot(self):
        import fcntl
        fd = os.open(an._bot_lock_path(), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)   # a probe in the middle of its check
        self.assertFalse(an.bot_running())
        fcntl.flock(fd, fcntl.LOCK_UN)
        lock = an.acquire_bot_lock()
        self.addCleanup(an.release_bot_lock, lock)
        self.assertTrue(an.bot_running())

    @unittest.skipUnless(os.name == "nt", "Windows has no shared lock")
    def test_windows_tries_a_busy_lock_again(self):
        busy = OSError(errno.EACCES, "locked")
        with unittest.mock.patch.object(an, "_lock_bot_fd", side_effect=[busy, None, None]):
            self.assertFalse(an.bot_running())          # another probe's moment
        with unittest.mock.patch.object(an, "_lock_bot_fd", side_effect=[busy, busy]):
            self.assertTrue(an.bot_running())           # a bot


class TestUninstallAdvice(unittest.TestCase):
    """RBD-2: 'stop it first' next to a plan whose first step stops it."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentbell-uninstall-")
        self.addCleanup(shutil.rmtree, self.home, True)
        old_home = base._set_home(self.home)
        self.addCleanup(base._restore_home, old_home)
        env = {an.STATE_DIR_ENV: os.path.join(self.home, "state")}
        # every agent config purge_report() looks at lives in the temp home
        for key in ("XDG_CONFIG_HOME", "XDG_BIN_HOME"):
            env[key] = os.path.join(self.home, key.lower())
        for patch in (
            unittest.mock.patch.dict(os.environ, env),
            # never the real pipx or pip --user site of this machine
            unittest.mock.patch.object(an, "_pipx_installed", return_value=None),
            unittest.mock.patch.object(an, "_user_site_dirs", return_value=(None, None)),
            unittest.mock.patch.object(an, "bot_running", return_value=True),
            unittest.mock.patch.object(an, "_read_bot_lock", return_value={"pid": 4242}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        for key in ("KIMI_CODE_HOME", "QWEN_HOME", "CODEX_HOME"):
            os.environ.pop(key, None)

    def warning(self):
        report = an.purge_report(project=self.home)
        found = [w for w in report["warnings"] if "bot is running" in w]
        self.assertEqual(len(found), 1, report["warnings"])
        return report["entries"], found[0]

    def test_a_bot_without_the_service_must_be_stopped_first(self):
        _, warning = self.warning()
        self.assertEqual(warning, "an agentbell bot is running (pid 4242); stop it first, "
                                  "otherwise it will recreate state files")

    def test_the_service_is_stopped_by_the_first_step(self):
        service = (an.launchd_plist_path() if sys.platform == "darwin"
                   else an.systemd_unit_path())
        os.makedirs(os.path.dirname(service))
        open(service, "w").close()
        entries, warning = self.warning()
        self.assertEqual(entries[0]["kind"], "service")
        self.assertEqual(warning, "an agentbell bot is running (pid 4242); unless it is the "
                                  "bot service, which is stopped first, stop it yourself, "
                                  "otherwise it will recreate state files")


class TestVerifyTelegramOnly(d3._Sandbox):
    """RBD-3: doctor said 'ntfy not used', verify judged the ntfy topic."""

    premium_config = d3.TestDoctorTelegramOnly.premium_config

    def delivery(self, channels, topic):
        with base.dev_keypair():
            cfg = self.premium_config(channels)
            cfg.data["ntfy"]["topic"] = topic
            cfg.save()
            verify = an.verify_report(cfg)["checks"]
            with unittest.mock.patch.object(an.NtfyChannel, "poll"):
                doctor = an.doctor_checks(cfg)
        return ([c for c in verify if c["name"] == "delivery"],
                [c for c in doctor if c["name"].startswith("ntfy")])

    def test_the_topic_of_a_telegram_only_setup_is_not_judged(self):
        for topic in ("", "abc", "a b"):
            with self.subTest(topic=topic):
                delivery, doctor = self.delivery(["telegram"], topic)
                self.assertEqual([c["status"] for c in delivery], [an.OK])
                self.assertIn("ntfy not used", delivery[0]["detail"])
                self.assertEqual([c["status"] for c in doctor], [an.OK])

    def test_a_setup_that_uses_ntfy_still_needs_a_topic(self):
        delivery, _ = self.delivery(["os"], "")        # `ask` falls back to ntfy
        self.assertEqual([c["status"] for c in delivery], [an.FAIL])
        self.assertIn("no ntfy topic configured", delivery[0]["detail"])


if __name__ == "__main__":
    unittest.main()
