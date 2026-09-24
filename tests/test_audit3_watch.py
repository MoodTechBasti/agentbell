"""Regression tests for the `watch` findings of the audit's third review round.

W1/IW-1  Windows: a .bat or .cmd gets its arguments as typed. cmd.exe does
         not run what follows `&` or drop `^`, and an argument it would
         change anyway (% " line breaks) is refused with exit 127.
W6       Windows: what CreateProcess finds is started as before; the PATH
         fallback takes only files CreateProcess can start.
W2/W3    Linux: a signal from watch's own process group (timeout(1)) is not
         passed on a second time (tested in test_audit4_runtime); one sent to
         watch alone is, also while watch is the terminal's foreground job.
W7       A hangup sent to watch without a terminal reaches the command.
W4/HR-3  A bad config value or an unusable state dir costs the push, never
         the command's exit code.
W5       Ctrl-C while the push hangs after the command has ended queues the
         push and lets watch exit.
W9       A push that went out on one channel is not reported as not sent.
"""

import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock

import test_agentbell as base
import test_audit2_watch as w2
import agentbell as an

LINUX_SIGINFO = sys.platform.startswith("linux")

# The watched command for the sender tests: it takes its signals with
# sigtimedwait, so it can note who sent each one, and it keeps noting them
# for the 0.6s it takes to "clean up" after the first.
SENDER_CHILD = r'''
import os, signal, sys, time
state = sys.argv[1]
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGQUIT, signal.SIGHUP)
EXIT = {signal.SIGINT: 3, signal.SIGTERM: 4, signal.SIGQUIT: 5, signal.SIGHUP: 6}
signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
with open(state + ".tmp", "w") as fh:
    fh.write("%d %d" % (os.getpid(), os.getppid()))
os.replace(state + ".tmp", state + ".ready")
first = signal.sigtimedwait(SIGNALS, 30)
if first is None:
    sys.exit(0)
got = [first]
deadline = time.monotonic() + 0.6
while time.monotonic() < deadline:
    info = signal.sigtimedwait(SIGNALS, max(0.0, deadline - time.monotonic()))
    if info is not None:
        got.append(info)
with open(state + ".senders", "w") as fh:
    fh.write(" ".join("%d:%d" % (i.si_signo, i.si_pid) for i in got))
sys.exit(EXIT[first.si_signo])
'''


def _blocked(pid, signum):
    """Whether watch (`pid`) takes `signum` with its sender by now.

    It blocks the signal and waits in sigtimedwait, which unblocks the
    signals it waits for while it sleeps.
    """
    try:
        with open(f"/proc/{pid}/wchan", encoding="utf-8") as fh:
            if "sigtimedwait" in fh.read():
                return True
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            mask = next(line for line in fh if line.startswith("SigBlk:")).split()[1]
    except (OSError, StopIteration):
        return False
    return bool(int(mask, 16) >> (signum - 1) & 1)


def _stderr_lines(run):
    """run()'s result and the agentbell lines it wrote to stderr."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        result = run()
    return result, [line for line in stderr.getvalue().splitlines()
                    if line.startswith(an.PROG + ":")]


class TestBatchCommandLine(unittest.TestCase):
    """W1/IW-1: the command line cmd.exe gets for a batch file (any OS)."""

    def tail(self, argv):
        line = an._batch_command_line(argv)
        self.assertIn('cmd.exe" /d /v:off /s /c "', line)
        return line.split(" /c ", 1)[1]

    def test_plain_arguments_stay_as_they_are(self):
        self.assertEqual(
            self.tail(["C:\\t\\npm.cmd", "install", "--save-dev", "./src/a.js",
                       "@scope/pkg", "\u00fcber"]),
            '""C:\\t\\npm.cmd" install --save-dev ./src/a.js @scope/pkg \u00fcber"')

    def test_metacharacters_are_quoted(self):
        args = ["a&whoami", "x|y", "lodash@^4.17.0", "<Button>", "one two",
                "(x86)", "--env=prod", "", "C:\\dir\\"]
        self.assertEqual(
            self.tail(["C:\\Program Files\\t (x86)\\abtool.cmd"] + args),
            '""C:\\Program Files\\t (x86)\\abtool.cmd" "a&whoami" "x|y" "lodash@^4.17.0" '
            '"<Button>" "one two" "(x86)" "--env=prod" "" "C:\\dir\\\\""')

    def test_what_cmd_would_change_is_refused(self):
        for arg in ("50%", "%PATH%", 'say "hi"', "a\nb", "a\rb"):
            with self.subTest(arg=arg), self.assertRaises(ValueError):
                an._batch_command_line(["C:\\t\\abtool.cmd", arg])


@unittest.skipUnless(os.name == "nt", "Windows batch files and PATHEXT")
class TestBatchFilesOnWindows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ntfy = base.MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        # a directory name cmd.exe would trip over unquoted
        self.tools = os.path.join(root, "a b&c (x86)")
        self.work = os.path.join(root, "work")
        os.makedirs(self.tools)
        os.makedirs(self.work)
        # npm's shim does the same: hand %* to a program
        with open(os.path.join(self.tools, "dump.py"), "w") as fh:
            fh.write("import json, os, sys\n"
                     "with open(os.path.join(os.path.dirname(__file__), 'argv.json'), 'w') as fh:\n"
                     "    json.dump(sys.argv[1:], fh)\n")
        with open(os.path.join(self.tools, "abtool.cmd"), "w") as fh:
            fh.write('@echo off\r\n"%s" "%%~dp0dump.py" %%*\r\nexit /b 5\r\n' % sys.executable)
        path = self.tools + os.pathsep + os.environ.get("PATH", "")
        patcher = unittest.mock.patch.dict(os.environ, {
            "PATH": path, "PATHEXT": ".COM;.EXE;.BAT;.CMD;.VBS;.JS"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.work)       # where a `>file` that cmd.exe ran would land
        self.cfg = base.make_config(self.ntfy.url, topic="win-batch")

    def received(self):
        path = os.path.join(self.tools, "argv.json")
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return json.load(fh)

    def test_arguments_reach_the_program_as_typed(self):
        args = ["lodash@^4.17.0", "x|y", "a&whoami>pwned.txt", "<Button>", "one two",
                "!bang!", "semi;colon,comma=eq", "", "C:\\dir\\", "\u00fcber"]
        result = an.run_watch(self.cfg, ["abtool"] + args)
        self.assertEqual(result["exit_code"], 5)
        self.assertEqual(self.received(), args)
        self.assertEqual(os.listdir(self.work), [])

    def test_a_path_to_the_batch_file_is_protected_too(self):
        script = os.path.join(self.tools, "abtool.cmd")
        result = an.run_watch(self.cfg, [script, "a&whoami>pwned.txt"])
        self.assertEqual(result["exit_code"], 5)
        self.assertEqual(self.received(), ["a&whoami>pwned.txt"])
        self.assertEqual(os.listdir(self.work), [])

    def test_what_cmd_would_change_is_refused_and_reported(self):
        result, lines = _stderr_lines(lambda: an.run_watch(self.cfg, ["abtool", "50%"]))
        self.assertEqual(result["exit_code"], 127)
        self.assertIsNone(self.received())
        self.assertIn("could not be started", result["message"])
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("refusing to pass '50%' to a batch file", lines[0])
        self.assertIn("could not be started", self.ntfy.posts["win-batch"][-1]["body"])

    def test_a_program_createprocess_finds_is_not_replaced_by_a_script(self):
        """W6: a hostname.js next to the user's files must not beat hostname.exe."""
        with open(os.path.join(self.work, "hostname.js"), "w") as fh:
            fh.write("WScript.Echo('js');\n")
        result = an.run_watch(self.cfg, ["hostname"])
        self.assertEqual(result["exit_code"], 0, result["message"])

    def test_the_path_lookup_skips_scripts_in_the_working_directory(self):
        with open(os.path.join(self.work, "abtool.js"), "w") as fh:
            fh.write("WScript.Echo('js');\n")
        result = an.run_watch(self.cfg, ["abtool", "plain"])
        self.assertEqual(result["exit_code"], 5, result["message"])
        self.assertEqual(self.received(), ["plain"])


@unittest.skipUnless(LINUX_SIGINFO and w2.HAS_PTY, "Linux pseudo-terminal")
class TestSigintToWatchInTheForeground(w2._WatchProcessCase):
    """W3: kill -INT <watch pid> is not a key press, even in the foreground."""

    # the pseudo-terminal helpers, without running that class's tests twice
    start = w2.TestWatchKeepsTheTerminal.start
    _cleanup = w2.TestWatchKeepsTheTerminal._cleanup
    read_until = w2.TestWatchKeepsTheTerminal.read_until
    finish = w2.TestWatchKeepsTheTerminal.finish

    def test_sigint_to_watch_alone_reaches_the_command(self):
        self.start()
        _, watch = self.ready()
        w2._wait_for(lambda: _blocked(watch, signal.SIGINT), what="watch to wait")
        os.kill(watch, signal.SIGINT)
        w2._wait_for(lambda: os.path.exists(self.state + ".signals"), timeout=10,
                     what="the command to get SIGINT")
        self.assertEqual(self.finish(), 3, self.output)
        self.assertEqual(self.signals(), [signal.SIGINT])
        self.assertIn("failed (exit 3)", self.wait_for_push()[-1]["body"])


@unittest.skipIf(os.name == "nt", "POSIX signals")
class TestHangupWithoutATerminal(w2._WatchProcessCase):

    def test_sighup_to_watch_reaches_the_command(self):
        """W7: cron, CI or supervisord (setpgid, no setsid): nothing else hangs it up.

        The launcher leads a new session without a terminal; watch, its
        child, is not the session leader.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))"]
            + self.watch_argv(),
            env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            _, watch = self.ready()
            os.kill(watch, signal.SIGHUP)
            w2._wait_for(lambda: os.path.exists(self.state + ".signals"), timeout=10,
                         what="the command to get SIGHUP")
            self.assertEqual(proc.wait(timeout=15), 6)
        finally:
            with contextlib.suppress(OSError):
                open(self.state + ".go", "w").close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(self.signals(), [signal.SIGHUP])
        self.assertIn("failed (exit 6)", self.wait_for_push()[-1]["body"])


class TestWatchKeepsTheExitCode(unittest.TestCase):
    """W4/HR-3: whatever breaks the push, watch exits with the command's code."""

    @classmethod
    def setUpClass(cls):
        cls.ntfy = base.MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def run_watch(self, cfg):
        return _stderr_lines(lambda: an.run_watch(
            cfg, [sys.executable, "-c", "raise SystemExit(3)"]))

    def test_an_unusable_state_dir(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        not_a_dir = os.path.join(tmp, "state")
        open(not_a_dir, "w").close()
        cfg = base.make_config(self.ntfy.url, topic="w4-state")
        with unittest.mock.patch.dict(os.environ, {"AGENTBELL_STATE_DIR": not_a_dir}):
            result, lines = self.run_watch(cfg)
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("notification error - FileExistsError", lines[0])
        self.assertIn("failed (exit 3)", self.ntfy.posts["w4-state"][-1]["body"])

    def test_a_config_value_of_the_wrong_kind(self):
        # a priority name no longer is one (audit 4, APR2-4)
        for key, value, error in (("channels", 5, "TypeError"),):
            with self.subTest(key=key):
                cfg = base.make_config(self.ntfy.url, topic="hr3")
                cfg.data[key] = value
                result, lines = self.run_watch(cfg)
                self.assertEqual(result["exit_code"], 3)
                self.assertEqual(len(lines), 1, lines)
                self.assertIn(f"notification error - {error}", lines[0])

    def test_the_cli_exits_with_the_command_code(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBELL")}
        env.update(HOME=tmp, USERPROFILE=tmp, XDG_CONFIG_HOME=tmp, XDG_STATE_HOME=tmp,
                   AGENTBELL_CONFIG_DIR=os.path.join(tmp, "config"),
                   AGENTBELL_STATE_DIR=os.path.join(tmp, "state"))
        os.makedirs(env["AGENTBELL_CONFIG_DIR"])
        with open(os.path.join(env["AGENTBELL_CONFIG_DIR"], "config.json"), "w") as fh:
            json.dump({"ntfy": {"server": self.ntfy.url, "topic": "hr3-cli"}, "channels": 5}, fh)
        proc = subprocess.run(
            [sys.executable, w2.AGENTBELL_PY, "watch", "--quiet", "--",
             sys.executable, "-c", "raise SystemExit(3)"],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("notification error - TypeError", proc.stderr)


@unittest.skipIf(os.name == "nt", "POSIX signals")
class TestCtrlCWhileThePushHangs(unittest.TestCase):
    """W5: after the command, Ctrl-C queues the push instead of waiting ~33s for it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.dict(
            os.environ, {"AGENTBELL_STATE_DIR": os.path.join(self.tmp, "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        # a Python handler to shield, whatever the runner has installed
        for signum in (signal.SIGINT, signal.SIGHUP):
            self.addCleanup(signal.signal, signum,
                            signal.signal(signum, lambda *args: None))

    def run_watch(self, raised, channels=("ntfy",)):
        cfg = base.make_config("http://127.0.0.1:9", topic="w5-inprocess")
        cfg.data["channels"] = list(channels)
        calls = []

        def publish(cfg, channel, item, timeout=10.0):
            calls.append(channel)
            signal.raise_signal(raised)   # the key press, while the send hangs
            return {"channel": channel, "ok": True}

        with unittest.mock.patch.object(an, "_publish_channel", side_effect=publish):
            result, lines = _stderr_lines(lambda: an.run_watch(
                cfg, [sys.executable, "-c", "raise SystemExit(7)"]))
        return result, lines, calls

    def test_ctrl_c_queues_the_push_and_skips_the_other_channels(self):
        result, lines, calls = self.run_watch(signal.SIGINT, channels=("ntfy", "os"))
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(calls, ["ntfy"])            # no retry, no second channel
        self.assertEqual(result["notification"]["queued"], ["ntfy", "os"])
        self.assertEqual(lines, [f"{an.PROG}: ntfy, os interrupted - "
                                 "notification queued for later delivery"])
        items = [item for _, item in an._read_item_files(an.queue_dir())]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["last_error"]["ntfy"], "interrupted by SIGINT")
        self.assertEqual(an.read_history()[-1]["event"], "queued")

    def test_a_hangup_while_sending_does_not_stop_the_push(self):
        """Closing the terminal right after the command still pushes."""
        result, lines, calls = self.run_watch(signal.SIGHUP)
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(calls, ["ntfy"])
        self.assertTrue(result["notification"]["ok"])
        self.assertNotIn("queued", result["notification"])
        self.assertEqual(lines, [])

    def test_ctrl_c_ends_a_real_watch_whose_push_hangs(self):
        """The reported case: a server that accepts and never answers."""
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        accepted = threading.Event()
        held = []

        def accept():
            with contextlib.suppress(OSError):
                held.append(server.accept()[0])
                accepted.set()

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.addCleanup(lambda: [conn.close() for conn in held])
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBELL")}
        env.update(HOME=self.tmp, USERPROFILE=self.tmp, XDG_CONFIG_HOME=self.tmp,
                   XDG_STATE_HOME=self.tmp, AGENTBELL_CONFIG_DIR=os.path.join(self.tmp, "config"),
                   AGENTBELL_STATE_DIR=os.path.join(self.tmp, "state"))
        os.makedirs(env["AGENTBELL_CONFIG_DIR"])
        with open(os.path.join(env["AGENTBELL_CONFIG_DIR"], "config.json"), "w") as fh:
            json.dump({"ntfy": {"server": "http://127.0.0.1:%d" % server.getsockname()[1],
                                "topic": "w5-blackhole"}}, fh)
        proc = subprocess.Popen(
            [sys.executable, w2.AGENTBELL_PY, "watch", "--quiet", "--",
             sys.executable, "-c", "raise SystemExit(7)"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        try:
            self.assertTrue(accepted.wait(20), "watch never tried to send")
            proc.send_signal(signal.SIGINT)
            _, err = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 7, err)
        self.assertIn(b"ntfy interrupted - notification queued for later delivery", err)
        self.assertEqual(len(os.listdir(os.path.join(env["AGENTBELL_STATE_DIR"], "queue"))), 1)


class TestWatchReportsAPartialPush(unittest.TestCase):

    def test_a_push_sent_on_one_channel_is_not_called_not_sent(self):
        """W9: ntfy delivered, Telegram refused it for good."""
        cfg = base.make_config("http://example.test")
        outcome = {"delivered": ["ntfy"], "transient": {},
                   "permanent": {"telegram": "HTTP 401 Unauthorized"}}
        with unittest.mock.patch.object(an, "_publish_item_channels", return_value=outcome), \
                unittest.mock.patch.object(an, "auto_drain"):
            result, lines = _stderr_lines(lambda: an.run_watch(
                cfg, [sys.executable, "-c", "raise SystemExit(7)"]))
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(lines, [f"{an.PROG}: notification sent via ntfy, failed on "
                                 f"telegram: HTTP 401 Unauthorized - see '{an.PROG} doctor'"])


if __name__ == "__main__":
    unittest.main()
