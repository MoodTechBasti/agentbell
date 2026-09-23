"""Regression tests for the runtime findings of the audit's fifth review round.

WF-2   Linux watch: timeout(1)'s arguments are read the way its getopt reads
       them (bundled short options, long-option prefixes); a spelling that
       cannot be read counts as "not a group kill", so the signal is passed on.
CR3-2  Codex install never deletes an unmarked `features.hooks = true`, not
       even one right above agentbell's block.
CR3-3  `hooks uninstall` fails only when agentbell's own wiring stays; a note
       about the user's own agentbell hook is information and exits 0.
CR3-4  doctor and verify name a Codex config that is not UTF-8 instead of
       calling it "not registered" / "not wired up".
WF-1   the webhook reads the request body before any refusal, so a client on
       Windows gets the 401/403/413 instead of a connection reset.
WF-3   the suite's temp dirs live under one root that is removed at exit.
"""

import io
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: sets the suite env)
import test_audit2_watch as w2  # noqa: E402
import test_audit3_watch as w3  # noqa: E402
import test_audit4_configs as c4  # noqa: E402
import agentbell as an  # noqa: E402

HAS_TIMEOUT = bool(shutil.which("timeout")) and os.path.isdir("/proc")


class TestTimeoutArguments(unittest.TestCase):
    """WF-2: -vs TERM or --sig TERM hid --foreground, and the TERM that only
    reached watch was swallowed. /proc is faked: these are argument lists."""

    @staticmethod
    def kills_its_group(options, name="timeout"):
        argv = [name] + options + ["60", "sleep", "60"]
        with unittest.mock.patch("builtins.open", side_effect=_fake_proc(argv)):
            return an._kills_its_group(12345)

    def test_foreground_after_a_bundled_or_abbreviated_option(self):
        for options in (["-vs", "TERM", "--foreground"], ["-vk", "8", "--foreground"],
                        ["--sig", "TERM", "--foreground"], ["--kill", "8", "--foreground"],
                        ["-sTERM", "-f"], ["--sig=TERM", "--fore"], ["-vf"], ["-pvf"],
                        ["--verb", "-s", "TERM", "--foreground"]):
            self.assertFalse(self.kills_its_group(options), options)

    def test_without_foreground_it_is_a_group_kill(self):
        for options in ([], ["-s", "TERM"], ["-vs", "TERM"], ["-vk", "8"], ["--kill", "8", "-v"],
                        ["--p", "--sig=TERM"], ["-ps", "TERM"], ["-s", "TERM", "--"],
                        ["-k", "5", "--", "--foreground"]):
            self.assertTrue(self.kills_its_group(options), options)

    def test_a_spelling_it_cannot_read_is_passed_on(self):
        # --ver is --verbose or --version: getopt refuses it, and so does this
        for options in (["--ver"], ["-x"], ["--frobnicate"]):
            self.assertFalse(self.kills_its_group(options), options)
        self.assertFalse(self.kills_its_group([], name="sleep"))

    @unittest.skipUnless(sys.platform.startswith("linux") and HAS_TIMEOUT,
                         "timeout(1) and /proc (Linux)")
    def test_a_real_timeout_process(self):
        for options, expected in ((["-vs", "TERM", "--foreground"], False), (["-vs", "TERM"], True)):
            proc = subprocess.Popen(["timeout"] + options + ["60", "sleep", "60"])
            try:
                # the fork has timeout's name only once it has exec'ed
                w2._wait_for(lambda: _comm(proc.pid) == "timeout", what="timeout to start")
                self.assertIs(an._kills_its_group(proc.pid), expected, options)
            finally:
                proc.kill()
                proc.wait()


def _comm(pid):
    with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
        return fh.read().strip()


def _fake_proc(argv):
    """open() of /proc/<pid>/comm and cmdline for a made-up timeout process."""
    def fake(path, mode="r", **_kw):
        if path.endswith("/comm"):
            return io.StringIO(argv[0] + "\n")
        return io.BytesIO("\0".join(argv).encode() + b"\0")
    return fake


@unittest.skipUnless(w3.LINUX_SIGINFO and HAS_TIMEOUT, "the sender of a signal is known on Linux")
class TestWatchUnderTimeoutForeground(w2._WatchProcessCase):
    """WF-2 end to end: `timeout -vs TERM --foreground` signals watch alone."""

    def setUp(self):
        super().setUp()
        with open(self.child, "w", encoding="utf-8") as fh:
            fh.write(w3.SENDER_CHILD)

    def test_the_command_gets_the_term_through_watch(self):
        proc = subprocess.Popen(
            ["timeout", "-vs", "TERM", "--foreground", "60"] + self.watch_argv(),
            env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        _, watch = self.ready()
        w2._wait_for(lambda: w3._blocked(watch, signal.SIGTERM), what="watch to wait")
        proc.send_signal(signal.SIGALRM)     # timeout's own "time is up"
        self.assertEqual(proc.wait(timeout=30), 124)
        with open(self.state + ".senders", encoding="utf-8") as fh:
            self.assertEqual(fh.read().split(), ["%d:%d" % (signal.SIGTERM, watch)])


class TestCodexProfileFlag(c4._Home):
    """CR3-2: a [profiles.x] flag right above our block was taken for ours."""

    ORIGINAL = 'model = "m"\n\n[profiles.fast]\nmodel = "x"\nfeatures.hooks = true\n'

    def test_the_profile_flag_survives_a_reinstall_after_our_flag_is_gone(self):
        path = an.codex_config_path()
        self.write(path, self.ORIGINAL)
        an.install_hooks("codex")
        text = self.read(path).decode("utf-8")
        mine = "features.hooks = true  " + an.CODEX_FLAG_MARKER + "\n"
        self.assertIn('model = "x"\nfeatures.hooks = true\n\n' + an.TOML_START, text)
        self.write(path, text.replace(mine, ""))       # the user deleted our line
        self.assertTrue(an.install_hooks("codex")["changed"])
        text = self.read(path).decode("utf-8")
        self.assertIn('model = "x"\nfeatures.hooks = true\n\n' + an.TOML_START, text)
        self.assertLess(text.index(mine), text.index("[profiles.fast]"))
        self.assertFalse(an.install_hooks("codex")["changed"])
        an.install_hooks("codex", add=False)
        self.assertEqual(self.read(path).decode("utf-8"), self.ORIGINAL)


class TestUninstallExitCode(c4._Home):
    """CR3-3: every note made `hooks uninstall` exit 1, forever."""

    WRAPPER = ('{"hooks":{"Stop":[{"hooks":[{"type":"command","command":'
               '"sh -c \'agentbell hook run_completed --agent claude\' || true"}]}]}}')

    def uninstall(self, *agents):
        return self.call(an.cmd_hooks, base._Args(sub="uninstall", agent=list(agents),
                                                  project=self.project))

    def test_the_users_own_agentbell_hook_is_a_note_not_a_failure(self):
        self.write(an.claude_settings_path(), self.WRAPPER)
        for agents in (["claude"], ["claude"], ["all"]):
            code, out, _err = self.uninstall(*agents)
            self.assertEqual(code, 0, agents)
            self.assertIn("nothing to remove for claude\n", out)
            self.assertIn("left in place in", out)
        self.assertEqual(self.read(an.claude_settings_path()).decode("utf-8"), self.WRAPPER)

    def test_a_stray_kimi_marker_is_a_refusal(self):
        self.write(an.kimi_config_path(), 'default_model = "k"\n' + an.TOML_START + "\n")
        code, out, _err = self.uninstall("kimi")
        self.assertEqual(code, 1)
        self.assertIn("left in place for kimi\n", out)
        self.assertIn("marker without its pair", out)

    def test_a_stray_rule_file_marker_is_a_refusal(self):
        self.write(os.path.join(self.project, "AGENTS.md"),
                   "# rules\n" + an.BLOCK_START + "\nagentbell hook run_completed --agent aider\n")
        code, out, _err = self.uninstall("aider")
        self.assertEqual(code, 1)
        self.assertIn("left in place for aider\n", out)


class TestUnreadableCodexConfig(c4._Home):
    """CR3-4: doctor said "not registered" and "hooks install codex"."""

    def setUp(self):
        super().setUp()
        self.path = an.codex_config_path()
        self.write(self.path, b'model = "caf\xe9"\n[mcp_servers.agentbell]\ncommand = "x"\n')

    def test_doctor_names_the_file_instead_of_not_registered(self):
        data = an.default_config()
        data["ntfy"].update({"server": "http://127.0.0.1:1", "topic": "audit5-topic-0123456789"})
        cfg = an.Config(data, path=an.config_path())
        with unittest.mock.patch.object(an.NtfyChannel, "poll", return_value=[]), \
                unittest.mock.patch.object(an, "find_agents", lambda: ["codex"]):
            checks = an.doctor_checks(cfg)
        config = [c for c in checks if c["name"] == "codex config"]
        self.assertEqual(len(config), 1)
        self.assertEqual(config[0]["status"], an.WARN)
        self.assertIn(self.path, config[0]["detail"])
        self.assertIn("not UTF-8", config[0]["detail"])
        self.assertFalse([c for c in checks if c["name"] == "mcp"])
        self.assertFalse([c for c in checks if "codex" in (c.get("fix") or "")])
        # mcp add refuses the same file, with the same words
        with self.assertRaisesRegex(RuntimeError, "is not UTF-8 text"):
            an._read_toml(self.path)

    def test_verify_says_it_cannot_read_the_config(self):
        for agent in (None, "codex"):
            report = an.verify_report(an.Config(an.default_config()), agent=agent)
            rows = [c for c in report["checks"] if c["name"] == "agent codex"]
            self.assertEqual(len(rows), 1, agent)
            self.assertIn("not UTF-8", rows[0]["detail"])
            self.assertNotIn(self.path, rows[0]["detail"])    # verify never prints a path
            self.assertFalse([c for c in report["checks"] if c["name"] == "agents"])


class TestWebhookReadsTheBodyFirst(unittest.TestCase):
    """WF-1: a refusal sent before the body was read reached a Windows client
    as a connection reset. The server now waits for the body, then answers."""

    @classmethod
    def setUpClass(cls):
        cls.ntfy = base.MockNtfy()
        cls.cfg = base.make_config(cls.ntfy.url, topic="wf1")
        cls.port = base._free_port()
        cls.cfg.data["webhook"] = {"listen": "127.0.0.1", "port": cls.port, "token": "tok"}
        base._start_webhook(cls.cfg, cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def exchange(self, headers, body):
        """Headers first, the body only when the server has not answered in
        0.3s; returns (answered before the body, status line)."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"POST /notify HTTP/1.1\r\n" + b"".join(h + b"\r\n" for h in headers)
                     + b"Content-Length: %d\r\n\r\n" % len(body))
        early = bool(select.select([sock], [], [], 0.3)[0])
        if not early:
            sock.sendall(body)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return early, data.split(b"\r\n", 1)[0]

    def test_every_refusal_waits_for_the_body(self):
        body = b'{"message": "x"}'
        sent = len(self.ntfy.posts.get("wf1", []))
        for headers, code in (([b"Host: attacker.example"], b" 403 "),
                              ([b"Host: 127.0.0.1", b"Origin: https://evil.example"], b" 403 "),
                              ([b"Host: 127.0.0.1", b"Authorization: Bearer nope"], b" 401 "),
                              ([b"Host: 127.0.0.1", b"Authorization: Bearer tok"], b" 413 ")):
            data = body if code != b" 413 " else b"x" * (an.WEBHOOK_MAX_BODY + 10)
            early, status = self.exchange(headers, data)
            self.assertFalse(early, headers)
            self.assertIn(code, status, headers)
        self.assertEqual(len(self.ntfy.posts.get("wf1", [])), sent)

    def test_a_valid_request_still_goes_through(self):
        early, status = self.exchange([b"Host: 127.0.0.1", b"Authorization: Bearer tok"],
                                      b'{"message": "hello"}')
        self.assertFalse(early)
        self.assertIn(b" 200 ", status)


class TestTempDirsAreRemoved(unittest.TestCase):
    """WF-3: every run left agentbell-tests-* and tmp* dirs behind."""

    def test_temp_dirs_land_under_the_suite_root(self):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, True)
        self.assertEqual(os.path.dirname(os.path.dirname(path)), base._TEST_ROOT)

    def test_a_tree_that_cannot_go_is_reported(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "a"))
        calls = []
        real = shutil.rmtree

        def rmtree(path, ignore_errors=False):
            calls.append(ignore_errors)
            if not ignore_errors:
                raise PermissionError(13, "in use", path)
            real(path, ignore_errors=True)
        with unittest.mock.patch.object(base.shutil, "rmtree", rmtree), \
                unittest.mock.patch.object(base.sys, "stderr", new_callable=io.StringIO) as err:
            base._remove_tree(root)
        self.assertEqual(calls, [False, True])
        self.assertIn(f"could not remove all of {root}", err.getvalue())
        self.assertFalse(os.path.exists(root))

    def test_the_root_is_removed_at_exit(self):
        code = ("import atexit, os, sys; sys.path.insert(0, %r); import test_agentbell as b; "
                "print(b._TEST_ROOT)" % os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        root = out.stdout.strip()
        self.assertTrue(root)
        self.assertFalse(os.path.exists(root))


if __name__ == "__main__":
    unittest.main()
