"""Regression tests for the `watch` findings of audit 2026-09-22 (review 3).

A1  The command keeps the terminal: password prompts, Ctrl-Z, Ctrl-\\ and a
    closing terminal behave as they do without watch, and the push arrives.
A3  Windows: watch never sends a console control event of its own.
M20 Windows: `watch -- npm test` finds npm.cmd (PATHEXT).
L   A push that fails for good is reported on stderr.

The terminal tests run watch as a real process on a real pseudo-terminal,
either as the foreground job of a small job-control shell (the usual case)
or as the session leader itself (ssh -t host cmd, docker run -it).
"""

import contextlib
import io
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error

import test_agentbell as base
import agentbell as an

try:
    import fcntl  # noqa: F401 - the launcher needs it; its absence skips the tests
    import termios
except ImportError:  # Windows
    termios = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBELL_PY = os.path.join(REPO, "agentbell.py")
HAS_PTY = os.name == "posix" and termios is not None and hasattr(termios, "TIOCSCTTY")

# Makes its terminal (fds 0-2) the controlling terminal of a new session.
# "exec": watch itself is the session leader. "shell": a minimal job-control
# shell runs watch as its foreground job, prints "<stopped>" when the job
# stops and resumes it on the next input line, and hangs up the job when
# the terminal closes, as bash does.
LAUNCHER = r'''
import fcntl, os, resource, signal, sys, termios
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
mode, argv = sys.argv[1], sys.argv[2:]
if mode == "exec":
    os.execv(argv[0], argv)
JOB_SIGNALS = (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP, signal.SIGINT, signal.SIGQUIT)
for sig in JOB_SIGNALS:
    signal.signal(sig, signal.SIG_IGN)
job = os.fork()
if job == 0:
    os.setpgid(0, 0)
    os.tcsetpgrp(0, os.getpid())
    for sig in JOB_SIGNALS:
        signal.signal(sig, signal.SIG_DFL)
    os.execv(argv[0], argv)
try:
    os.setpgid(job, job)
except OSError:
    pass
os.tcsetpgrp(0, job)
signal.signal(signal.SIGHUP, lambda signum, frame: os.killpg(job, signal.SIGHUP))
while True:
    _, status = os.waitpid(job, os.WUNTRACED)
    if os.WIFSTOPPED(status):
        os.tcsetpgrp(0, os.getpgrp())
        os.write(1, b"<stopped>\n")
        os.read(0, 64)
        os.tcsetpgrp(0, job)
        os.killpg(job, signal.SIGCONT)
        continue
    code = os.waitstatus_to_exitcode(status)
    sys.exit(code if code >= 0 else 128 - code)
'''

# The watched command. It notes every signal it gets, takes 0.6s to "clean
# up" after the first one (longer than subprocess.run's 0.25s SIGKILL
# window) and exits with a code that names the signal. "prompt" asks for a
# password on /dev/tty the way sudo, ssh and gpg do.
CHILD = r'''
import os, signal, sys, time
state, mode = sys.argv[1], sys.argv[2]
EXIT = {signal.SIGINT: 3, signal.SIGTERM: 4, signal.SIGQUIT: 5, signal.SIGHUP: 6}
got = []
def on_signal(signum, frame):
    got.append(signum)
    if len(got) > 1:
        return
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline:
        time.sleep(0.02)
    with open(state + ".signals", "w") as fh:
        fh.write(" ".join(str(s) for s in got))
    os._exit(EXIT[signum])
for signum in EXIT:
    signal.signal(signum, on_signal)
with open(state + ".tmp", "w") as fh:
    fh.write("%d %d" % (os.getpid(), os.getppid()))
os.replace(state + ".tmp", state + ".ready")
if mode == "prompt":
    tty = os.open("/dev/tty", os.O_RDWR)
    os.write(tty, b"Password: ")
    sys.exit(0 if os.read(tty, 64).strip() == b"secret" else 9)
parent = os.getppid()
deadline = time.monotonic() + 30
while not os.path.exists(state + ".go") and time.monotonic() < deadline:
    if os.getppid() != parent:
        sys.exit(8)          # watch is gone: do not linger as an orphan
    time.sleep(0.02)
sys.exit(0)
'''


def _process_state(pid):
    """One-letter process state ('T' = stopped), '' when it is gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        pass
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return out[:1]


def _wait_for(predicate, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


class _WatchProcessCase(unittest.TestCase):
    """Runs `agentbell.py watch` as its own process against a mock ntfy."""

    @classmethod
    def setUpClass(cls):
        cls.ntfy = base.MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentbell-watch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.topic = "w" + os.urandom(4).hex()
        home = os.path.join(self.tmp, "home")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBELL")}
        env.update(
            HOME=home, USERPROFILE=home,
            XDG_CONFIG_HOME=os.path.join(home, ".config"),
            XDG_STATE_HOME=os.path.join(home, ".local", "state"),
            AGENTBELL_CONFIG_DIR=os.path.join(self.tmp, "config"),
            AGENTBELL_STATE_DIR=os.path.join(self.tmp, "state"),
        )
        self.env = env
        os.makedirs(env["AGENTBELL_CONFIG_DIR"])
        with open(os.path.join(env["AGENTBELL_CONFIG_DIR"], "config.json"), "w") as fh:
            json.dump({"ntfy": {"server": self.ntfy.url, "topic": self.topic}}, fh)
        self.state = os.path.join(self.tmp, "child")
        self.child = os.path.join(self.tmp, "child.py")
        with open(self.child, "w", encoding="utf-8") as fh:
            fh.write(CHILD)

    def watch_argv(self, mode="wait"):
        return [sys.executable, AGENTBELL_PY, "watch", "--",
                sys.executable, self.child, self.state, mode]

    def ready(self):
        """(child pid, watch pid) once the command is running."""
        path = self.state + ".ready"
        _wait_for(lambda: os.path.exists(path), what="the command to start")
        with open(path, encoding="utf-8") as fh:
            pid, parent = fh.read().split()
        return int(pid), int(parent)

    def signals(self):
        with open(self.state + ".signals", encoding="utf-8") as fh:
            return [int(s) for s in fh.read().split()]

    def posts(self):
        return self.ntfy.posts.get(self.topic, [])

    def wait_for_push(self):
        _wait_for(lambda: self.posts(), timeout=15, what="the push")
        return self.posts()


@unittest.skipUnless(HAS_PTY, "needs a POSIX pseudo-terminal")
class TestWatchKeepsTheTerminal(_WatchProcessCase):

    def start(self, mode="wait", launcher="shell"):
        path = os.path.join(self.tmp, "launcher.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(LAUNCHER)
        master, slave = os.openpty()
        self.proc = subprocess.Popen(
            [sys.executable, path, launcher] + self.watch_argv(mode),
            stdin=slave, stdout=slave, stderr=slave, env=self.env, cwd=self.tmp)
        os.close(slave)
        self.master = master
        self.output = b""
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with contextlib.suppress(OSError):
            open(self.state + ".go", "w").close()
        if self.master is not None:
            os.close(self.master)
            self.master = None
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def read_until(self, needle, timeout=10.0):
        deadline = time.monotonic() + timeout
        while needle not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {needle!r} on the terminal: {self.output!r}")
            readable, _, _ = select.select([self.master], [], [], remaining)
            if not readable:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                raise AssertionError(f"terminal closed before {needle!r}: {self.output!r}")
            self.output += chunk

    def finish(self):
        """Exit status of the launcher; the terminal output is drained."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.master], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                self.output += chunk
            elif self.proc.poll() is not None:
                break
        return self.proc.wait(timeout=15)

    def hang_up(self):
        os.close(self.master)
        self.master = None

    def test_a_password_prompt_reads_the_terminal(self):
        self.start(mode="prompt")
        self.read_until(b"Password: ")
        os.write(self.master, b"secret\n")
        self.assertEqual(self.finish(), 0, self.output)
        self.assertIn("succeeded (exit 0)", self.wait_for_push()[-1]["body"])

    def test_ctrl_c_reaches_the_command_once_and_the_push_arrives(self):
        self.start()
        self.ready()
        os.write(self.master, b"\x03")
        self.assertEqual(self.finish(), 3, self.output)
        self.assertEqual(self.signals(), [signal.SIGINT])
        posts = self.wait_for_push()
        self.assertEqual(len(posts), 1)
        self.assertIn("failed (exit 3)", posts[-1]["body"])

    def test_ctrl_backslash_does_not_take_watch_down(self):
        self.start()
        self.ready()
        os.write(self.master, b"\x1c")
        self.assertEqual(self.finish(), 5, self.output)
        self.assertEqual(self.signals(), [signal.SIGQUIT])
        self.assertIn("failed (exit 5)", self.wait_for_push()[-1]["body"])

    def test_ctrl_z_stops_the_command_and_fg_resumes_it(self):
        self.start()
        child, _ = self.ready()
        os.write(self.master, b"\x1a")
        self.read_until(b"<stopped>")
        _wait_for(lambda: _process_state(child) == "T", what="the command to stop")
        open(self.state + ".go", "w").close()
        os.write(self.master, b"fg\n")
        self.assertEqual(self.finish(), 0, self.output)
        self.assertIn("succeeded (exit 0)", self.wait_for_push()[-1]["body"])

    def test_sigterm_to_watch_alone_is_forwarded_once(self):
        self.start()
        _, watch = self.ready()
        os.kill(watch, signal.SIGTERM)
        self.assertEqual(self.finish(), 4, self.output)
        self.assertEqual(self.signals(), [signal.SIGTERM])
        self.assertIn("failed (exit 4)", self.wait_for_push()[-1]["body"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "hangup delivery checked on Linux")
    def test_closing_the_terminal_still_pushes(self):
        """The shell hangs up the whole job: the command gets SIGHUP once."""
        self.start()
        self.ready()
        self.hang_up()
        self.proc.wait(timeout=15)
        posts = self.wait_for_push()
        self.assertIn("failed (exit 6)", posts[-1]["body"])
        self.assertEqual(self.signals(), [signal.SIGHUP])

    @unittest.skipUnless(sys.platform.startswith("linux"), "hangup delivery checked on Linux")
    def test_a_hangup_of_watch_as_session_leader_reaches_the_command(self):
        """ssh -t host agentbell watch ...: only the session leader gets SIGHUP."""
        self.start(launcher="exec")
        self.ready()
        self.hang_up()
        self.proc.wait(timeout=15)
        posts = self.wait_for_push()
        self.assertIn("failed (exit 6)", posts[-1]["body"])
        self.assertEqual(self.signals(), [signal.SIGHUP])


@unittest.skipIf(os.name == "nt", "POSIX signals")
class TestWatchSignalsWithoutATerminal(_WatchProcessCase):

    def test_sigint_to_watch_alone_is_forwarded_once(self):
        """A supervisor's send_signal(SIGINT) targets watch only; pass it on."""
        proc = subprocess.Popen(self.watch_argv(), env=self.env, cwd=self.tmp,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        try:
            _, watch = self.ready()
            self.assertEqual(watch, proc.pid)
            proc.send_signal(signal.SIGINT)
            proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.signals(), [signal.SIGINT])
        self.assertIn("failed (exit 3)", self.wait_for_push()[-1]["body"])

    def test_ignored_signals_stay_ignored_for_the_command(self):
        """nohup, or `cmd &` in a script: the command must inherit SIG_IGN."""
        probe = ("import signal, sys; sys.exit(0 if all("
                 "signal.getsignal(s) == signal.SIG_IGN "
                 "for s in (signal.SIGINT, signal.SIGHUP)) else 9)")
        cfg = base.make_config(self.ntfy.url, topic=self.topic)
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGHUP)}
        try:
            for s in previous:
                signal.signal(s, signal.SIG_IGN)
            result = an.run_watch(cfg, [sys.executable, "-c", probe])
            after = {s: signal.getsignal(s) for s in previous}
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(after, {s: signal.SIG_IGN for s in previous})


class TestWatchReportsAFailedPush(unittest.TestCase):
    """The exit code stays the command's; stderr says no push went out."""

    def _run(self, cfg, code=7):
        """run_watch's result and the lines it wrote to stderr (warnings aside)."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = an.run_watch(cfg, [sys.executable, "-c", f"raise SystemExit({code})"])
        lines = [line for line in stderr.getvalue().splitlines()
                 if line.startswith(an.PROG + ":")]
        return result, lines

    def test_a_rejected_push_is_one_stderr_line(self):
        cfg = base.make_config("http://example.test")

        def forbidden(*args, **kwargs):
            raise urllib.error.HTTPError("http://example.test/t", 403, "Forbidden",
                                         {}, io.BytesIO(b'{"error":"forbidden"}'))

        with unittest.mock.patch.object(an.OPENER, "open", side_effect=forbidden):
            result, lines = self._run(cfg)
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("notification not sent", lines[0])
        self.assertIn("HTTP 403", lines[0])
        self.assertIn("agentbell doctor", lines[0])

    def test_a_missing_topic_is_reported(self):
        cfg = base.make_config("http://example.test", topic="")
        result, lines = self._run(cfg, code=0)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("notification not sent - ntfy: invalid ntfy topic", lines[0])

    def test_a_channel_that_failed_next_to_one_that_worked_is_named(self):
        cfg = base.make_config("http://example.test")
        outcome = {"delivered": ["ntfy"], "transient": {},
                   "permanent": {"telegram": "HTTP 401 Unauthorized"}}
        with unittest.mock.patch.object(an, "_publish_item_channels", return_value=outcome), \
                unittest.mock.patch.object(an, "auto_drain"):
            result, lines = self._run(cfg)
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("telegram: HTTP 401", lines[0])
        self.assertIn("sent via ntfy", lines[0])

    def test_queued_and_suppressed_pushes_are_not_called_failures(self):
        cfg = base.make_config("http://example.test")
        outcome = {"delivered": [], "transient": {"ntfy": "connection refused"}, "permanent": {}}
        with unittest.mock.patch.object(an, "_publish_item_channels", return_value=outcome), \
                unittest.mock.patch.object(an, "enqueue_item"):
            _, lines = self._run(cfg)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("queued for later delivery", lines[0])
        self.assertNotIn("not sent", lines[0])
        with unittest.mock.patch.object(an, "suppressed_by_quiet_hours", return_value=True):
            _, lines = self._run(cfg)
        self.assertEqual(lines, [])


@unittest.skipUnless(os.name == "nt", "Windows console and PATHEXT")
class TestWatchOnWindows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ntfy = base.MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def test_a_cmd_shim_on_path_is_found(self):
        """npm, yarn and pnpm are .cmd files; CreateProcess only tries .exe."""
        tools = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tools, True)
        with open(os.path.join(tools, "abtool.cmd"), "w") as fh:
            fh.write("@echo off\r\nexit /b 5\r\n")
        path = tools + os.pathsep + os.environ.get("PATH", "")
        cfg = base.make_config(self.ntfy.url, topic="win-cmd")
        with unittest.mock.patch.dict(os.environ, {"PATH": path}):
            result = an.run_watch(cfg, ["abtool", "one two"])
        self.assertEqual(result["exit_code"], 5)
        self.assertIn("abtool 'one two' failed (exit 5)", result["message"])

    def test_ctrl_c_while_spawning_sends_no_console_event(self):
        """Every process on the console already got Ctrl-C / Ctrl-Break.

        A CTRL_BREAK aimed at the command's pid lands elsewhere: on watch
        itself, or on the command while it is still starting. Runs in its
        own process: a watch that dies of Ctrl-Break must not take the test
        runner with it, and os.kill is stubbed so nothing reaches the console.
        """
        script = (
            "import json, os, signal, subprocess, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import agentbell as an\n"
            "real_popen = subprocess.Popen\n"
            "def popen(*args, **kwargs):\n"
            "    signal.raise_signal(signal.SIGINT)\n"
            "    signal.raise_signal(signal.SIGBREAK)\n"
            "    return real_popen(*args, **kwargs)\n"
            "kills = []\n"
            "an.subprocess.Popen = popen\n"
            "an.os.kill = lambda *args: kills.append(args)\n"
            "cfg = an.Config(an.default_config())\n"
            "cfg.data['ntfy'].update(server=sys.argv[2], topic='win-race')\n"
            "result = an.run_watch(cfg, [sys.executable, '-c', 'raise SystemExit(4)'])\n"
            "print(json.dumps({'exit': result['exit_code'], 'kills': kills}))\n"
        )
        proc = subprocess.run([sys.executable, "-c", script, REPO, self.ntfy.url],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outcome = json.loads(proc.stdout.splitlines()[-1])
        self.assertEqual(outcome, {"exit": 4, "kills": []})
        self.assertIn("exit 4", self.ntfy.posts["win-race"][-1]["body"])


if __name__ == "__main__":
    unittest.main()
