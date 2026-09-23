"""Audit 3, cluster json-hooks: hook ownership for odd matchers and
interpreters, file modes and encodings of host configs, Claude's
permission prompt, user-tuned --min-duration, the uninstall report, and the
commands `integrate` hands to self-integrating agents.

Run with: python3 -m unittest discover -s tests
"""

import argparse
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agentbell as an  # noqa: E402
import test_agentbell as base  # noqa: E402

# root ignores file modes, so a read-only directory would be written anyway
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class _HomeCase(unittest.TestCase):
    """A throwaway home (Kimi and Qwen homes included) and agentbell resolved
    to a stand-in launcher, never to one on the PATH of the machine."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.old_home = base._set_home(self.home)
        self.old_argv0 = sys.argv[0]
        sys.argv[0] = base._fake_launcher(self.tmp)
        self.patches = [
            unittest.mock.patch.object(an.shutil, "which", lambda _prog: None),
            unittest.mock.patch.dict(os.environ, {
                "KIMI_CODE_HOME": os.path.join(self.home, ".kimi-code"),
                "QWEN_HOME": os.path.join(self.home, ".qwen")}),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        base._restore_home(self.old_home)
        sys.argv[0] = self.old_argv0
        for root in (self.tmp, self.home):
            for dirpath, dirnames, _files in os.walk(root):
                for name in dirnames:
                    with contextlib.suppress(OSError):
                        os.chmod(os.path.join(dirpath, name), 0o755)
            shutil.rmtree(root, ignore_errors=True)

    def _write(self, rel, text, mode="w"):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as fh:
            fh.write(text)
        return path

    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _commands(self, path):
        data = json.loads(self._read(path))
        return [(event, group.get("matcher"), entry["command"])
                for event, groups in data.get("hooks", {}).items()
                for group in groups for entry in group["hooks"]]

    def _run(self, func, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                func(*args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()


class TestOwnershipEdges(_HomeCase):
    def test_a_list_matcher_does_not_crash_install_or_uninstall(self):
        # HC7: the matcher was hashed as is - TypeError: unhashable type: 'list'
        user = {"matcher": ["*"], "hooks": [
            {"type": "command", "command": "agentbell hook run_completed --agent gemini"}]}
        path = self._write(".gemini/settings.json",
                           json.dumps({"hooks": {"AfterAgent": [user]}}))
        self.assertTrue(an.install_hooks("gemini")["changed"])
        self.assertIn(("AfterAgent", ["*"], "agentbell hook run_completed --agent gemini"),
                      self._commands(path))
        self.assertTrue(an.install_hooks("gemini", add=False)["changed"])
        self.assertEqual(json.loads(self._read(path)), {"hooks": {"AfterAgent": [user]}})

    def test_any_interpreter_before_agentbell_py_is_ours(self):
        # HC9: pypy3 and free-threaded python3.13t.exe did not match
        for command in (
                "C:/Python313/python3.13t.exe /x/agentbell.py hook started --agent claude --silent",
                "/opt/pypy/bin/pypy3.10 /src/agentbell.py hook started --agent claude --silent",
                "'/usr/bin/pypy3' '/src/a b/agentbell.py' hook started --agent claude --silent"):
            self.assertEqual(an._parse_our_hook_command(command), ("started", "claude"), command)
        # a wrapper in front of the launcher is still the user's
        self.assertIsNone(an._parse_our_hook_command(
            "nice agentbell hook run_completed --agent claude"))

    def test_hooks_generated_under_pypy_are_reinstalled_and_removed(self):
        script = base._fake_launcher(self.tmp, "agentbell.py")
        with unittest.mock.patch.object(sys, "argv", [script]), \
                unittest.mock.patch.object(sys, "executable", "/opt/pypy/bin/pypy3"):
            self.assertTrue(an.install_hooks("claude")["changed"])
            again = an.install_hooks("claude")
            self.assertFalse(again["changed"])
            self.assertEqual(again["notes"], [])
            removed = an.install_hooks("claude", add=False)
        self.assertTrue(removed["changed"])
        self.assertEqual(removed["notes"], [])
        self.assertFalse(an._file_contains_our_hook(an.claude_settings_path()))

    def test_non_ascii_windows_path_runs_bare(self):
        # HC13: a quoted path breaks in Codex's cmd fallback; Jörg needs no quotes
        for shell in ("cmd", "powershell"):
            self.assertEqual(an._windows_command_line(["C:\\Users\\J\u00f6rg\\agentbell.exe"], shell),
                             "C:/Users/J\u00f6rg/agentbell.exe")
        self.assertTrue(an._windows_command_line(["C:\\J\u00f6 Do\\agentbell.exe"], "powershell")
                        .startswith("& '"))

    def test_the_location_blind_helper_is_gone(self):
        # HC14: ownership is _is_owned_json_hook (command plus location) only
        self.assertFalse(hasattr(an, "_contains_our_hook"))


class TestHostConfigFiles(_HomeCase):
    def test_a_non_utf8_settings_file_is_refused_not_a_traceback(self):
        # IW-9: UnicodeDecodeError escaped cmd_hooks; the other agents still install
        path = self._write(".gemini/settings.json", b'{"theme": "Gr\xfcn"}\n', mode="wb")
        code, out, err = self._run(an.main, ["hooks", "install", "gemini", "claude"])
        self.assertEqual(code, 1)
        self.assertIn("hooks for gemini not changed", err)
        self.assertIn("is not UTF-8", err)
        self.assertIn("installed hooks for claude", out)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b'{"theme": "Gr\xfcn"}\n')

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_a_new_config_follows_the_umask(self):
        # HC8: a new file was forced to 0644 whatever the umask said
        old = os.umask(0o077)
        try:
            an.install_hooks("claude")
            an.install_hooks("kimi")
        finally:
            os.umask(old)
        for path in (an.claude_settings_path(), an.kimi_config_path()):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, path)
        # an existing file keeps its own mode, a caller's mode still wins
        existing = self._write("other.json", "{}")
        os.chmod(existing, 0o640)
        an.write_json_atomic(existing, {"a": 1})
        self.assertEqual(os.stat(existing).st_mode & 0o777, 0o640)
        an.write_json_atomic(existing, {"a": 2}, mode=0o600)
        self.assertEqual(os.stat(existing).st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt" or IS_ROOT, "POSIX directory modes")
    def test_the_nix_hint_is_only_for_a_generated_file(self):
        # S7: agentbell's own config.json in a read-only dir is no Nix file
        folder = os.path.join(self.tmp, "cfg")
        os.makedirs(folder)
        plain = os.path.join(folder, "config.json")
        an.write_json_atomic(plain, {}, mode=0o600)
        store = os.path.join(self.tmp, "store")
        os.makedirs(store)
        target = os.path.join(store, "settings.json")
        an.write_json_atomic(target, {})
        link = os.path.join(self.tmp, "settings.json")
        os.symlink(target, link)
        os.chmod(folder, 0o555)
        os.chmod(store, 0o555)
        with self.assertRaises(OSError) as ctx:
            an.write_json_atomic(plain, {"a": 1}, mode=0o600)
        self.assertIn(f"cannot write {plain}", str(ctx.exception))
        self.assertNotIn("Nix", str(ctx.exception))
        with self.assertRaises(OSError) as ctx:
            an.write_json_atomic(link, {"a": 1})
        self.assertIn("a symlink to", str(ctx.exception))
        self.assertIn("Nix/home-manager", str(ctx.exception))


class TestClaudePermissionPrompt(_HomeCase):
    """N8: Claude Code's Notification matcher permission_prompt fires when
    a permission dialog is shown."""

    def _notification(self, path):
        return [(matcher, command) for event, matcher, command in self._commands(path)
                if event == "Notification"]

    def test_install_wires_it_and_uninstall_removes_it(self):
        path = an.claude_settings_path()
        self.assertTrue(an.install_hooks("claude")["changed"])
        wired = self._notification(path)
        self.assertIn(("permission_prompt",
                       an._hook_command("permission_required", "claude")), wired)
        entry = [e for g in json.loads(self._read(path))["hooks"]["Notification"]
                 if g["matcher"] == "permission_prompt" for e in g["hooks"]][0]
        self.assertIs(entry["async"], True)
        self.assertFalse(an.install_hooks("claude")["changed"])
        result = an.install_hooks("claude", add=False)
        self.assertTrue(result["changed"])
        self.assertEqual(result["notes"], [])
        self.assertFalse(os.path.exists(path) and self._notification(path))

    def test_an_older_install_gains_it_on_reinstall(self):
        hooks = an.claude_event_hooks()
        hooks["Notification"] = [g for g in hooks["Notification"]
                                 if g["matcher"] != "permission_prompt"]
        path = self._write(".claude/settings.json", json.dumps({"hooks": hooks}))
        self.assertTrue(an.install_hooks("claude")["changed"])
        self.assertEqual(sorted(m for m, _c in self._notification(path)),
                         ["agent_needs_input", "permission_prompt"])

    def test_a_user_written_permission_prompt_hook_survives(self):
        user = "afplay ping.aiff; agentbell hook permission_required --agent claude"
        mine = "agentbell hook input_required --agent claude"
        path = self._write(".claude/settings.json", json.dumps({"hooks": {"Notification": [
            {"matcher": "permission_prompt", "hooks": [{"type": "command", "command": mine}]}]}}))
        an.install_hooks("claude")
        data = json.loads(self._read(path))
        data["hooks"]["Notification"].append(
            {"matcher": "permission_prompt", "hooks": [{"type": "command", "command": user}]})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertFalse(an.install_hooks("claude")["changed"])
        an.install_hooks("claude", add=False)
        self.assertEqual(sorted(self._notification(path)),
                         [("permission_prompt", user), ("permission_prompt", mine)])


class TestTunedMinDuration(_HomeCase):
    """N2: a reinstall reset a --min-duration the user tuned to 60."""

    def _tune(self, path, value):
        text = self._read(path)
        self.assertIn(f"--min-duration {an.HOOK_MIN_DURATION}", text)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace(f"--min-duration {an.HOOK_MIN_DURATION}",
                                  f"--min-duration {value}"))

    def test_reinstall_keeps_it_for_every_agent_that_writes_it(self):
        agents = {"claude": an.claude_settings_path, "qwen-code": an.qwen_settings_path,
                  "codex": an.codex_config_path, "kimi": an.kimi_config_path}
        for agent, where in agents.items():
            an.install_hooks(agent)
            self._tune(where(), 300)
            self.assertFalse(an.install_hooks(agent)["changed"], agent)
            text = self._read(where())
            self.assertIn("--min-duration 300", text, agent)
            self.assertNotIn(f"--min-duration {an.HOOK_MIN_DURATION}", text, agent)

    def test_a_moved_binary_keeps_it_too(self):
        path = an.claude_settings_path()
        an.install_hooks("claude")
        self._tune(path, 5)
        sys.argv[0] = base._fake_launcher(tempfile.mkdtemp(dir=self.tmp))
        self.assertTrue(an.install_hooks("claude")["changed"])
        stop = [c for event, _m, c in self._commands(path) if event == "Stop"]
        self.assertEqual(stop, [an._hook_command("run_completed", "claude") + " --min-duration 5"])

    def test_a_wrapper_value_is_not_taken_over(self):
        path = an.claude_settings_path()
        an.install_hooks("claude")
        data = json.loads(self._read(path))
        data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command":
                                      "say hi; agentbell hook run_completed --agent claude "
                                      "--min-duration 7"}]})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        sys.argv[0] = base._fake_launcher(tempfile.mkdtemp(dir=self.tmp))
        an.install_hooks("claude")
        ours = [c for _e, _m, c in self._commands(path)
                if c.startswith(an._hook_command("run_completed", "claude"))]
        self.assertEqual(ours, [an._hook_command("run_completed", "claude")
                                + f" --min-duration {an.HOOK_MIN_DURATION}"])


class TestUninstallReport(_HomeCase):
    """HC3: the full uninstall said '(already gone)' and 'Done' while
    hooks the user wrote kept calling agentbell."""

    def _uninstall(self):
        report = {"entries": an._agent_hook_entries(), "warnings": []}
        with unittest.mock.patch.object(an, "purge_report", lambda project=None: report):
            return self._run(an.cmd_uninstall, argparse.Namespace(project=self.tmp, yes=True))

    def test_hooks_the_user_wrote_are_reported_as_kept(self):
        path = self._write(".claude/settings.json", json.dumps({"hooks": {
            "Notification": [{"matcher": "permission_prompt", "hooks": [
                {"type": "command", "command": "agentbell hook input_required --agent claude"}]}],
            "Stop": [{"hooks": [{"type": "command", "command":
                                 "afplay x.aiff; agentbell hook run_completed --agent claude"}]}],
        }}))
        an.install_hooks("claude")
        code, out, _err = self._uninstall()
        self.assertEqual(code, 0)
        self.assertIn(f"kept     claude hooks in {an.claude_settings_path()}", out)
        self.assertIn("2 hook command(s) mention agentbell", out)
        self.assertNotIn("already gone", out)
        self.assertNotIn("Done.", out)
        self.assertIn("Not done: 1 agent config(s) still run agentbell", out)
        self.assertEqual(len(self._commands(path)), 2)          # ours went, theirs stayed

    def test_a_clean_removal_still_says_done(self):
        an.install_hooks("claude")
        code, out, _err = self._uninstall()
        self.assertEqual(code, 0)
        self.assertIn("removed  claude hooks in", out)
        self.assertIn("Done. Fresh start:", out)


class TestInstallReport(_HomeCase):
    def test_init_and_hooks_share_one_report(self):
        # S10: one helper prints the changed, unchanged and refused lines
        self._write(".gemini/settings.json", "{ // comment\n}\n")
        _code, out, err = self._run(an._install_and_report, "claude", None, True, "  ")
        self.assertEqual(out, f"  installed hooks for claude: {an.claude_settings_path()}\n")
        _code, out, err = self._run(an._install_and_report, "claude", None, True, "  ")
        self.assertEqual(out, "  hooks for claude already installed (nothing changed)\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertIs(an._install_and_report("gemini", None, True), False)
            self.assertIs(an._install_and_report("claude", None, False), True)
        self.assertEqual(out.getvalue(), "removed for claude\n")
        self.assertIn("agentbell: hooks for gemini not changed:", err.getvalue())

    def test_next_steps_do_not_promise_every_repo_for_rule_files(self):
        # N6: cursor/windsurf/... get a rule file in the current repo only
        cfg = an.Config(data=an.default_config(), path=os.path.join(self.tmp, "c.json"))
        with unittest.mock.patch.object(an, "find_agents", lambda: ["claude", "cursor"]):
            _code, out, _err = self._run(an.print_next_steps, cfg)
        self.assertNotIn("every repo", out)
        self.assertIn("cursor: a rule file in the current repo only", out)
        with unittest.mock.patch.object(an, "find_agents", lambda: ["claude"]):
            _code, out, _err = self._run(an.print_next_steps, cfg)
        self.assertNotIn("rule file", out)


class TestIntegrateContract(_HomeCase):
    SPACED = ["C:\\Users\\Jo Do\\Python312\\python.exe", "C:\\src\\agent bell\\agentbell.py"]

    def test_windows_commands_run_in_cmd_and_get_a_powershell_prefix(self):
        # IW-4: shlex.quote gave 'C:\...' - a filename in cmd, a string in PowerShell
        with unittest.mock.patch.object(an, "agentbell_command", lambda: self.SPACED), \
                unittest.mock.patch.object(an.os, "name", "nt"):
            manifest = an.integration_manifest(agent="myagent")
        cmd = '"C:/Users/Jo Do/Python312/python.exe" "C:/src/agent bell/agentbell.py"'
        self.assertEqual(manifest["command_prefix"], cmd)
        self.assertEqual(manifest["commands"]["started_silent"],
                         cmd + " hook started --agent myagent --silent")
        self.assertEqual(manifest["powershell_command_prefix"],
                         "& 'C:/Users/Jo Do/Python312/python.exe' 'C:/src/agent bell/agentbell.py'")
        for event in manifest["events"]:
            self.assertTrue(event["command"].startswith(cmd + " hook "), event)
        guide = an.integration_guide(dict(manifest, platform="Windows"))
        self.assertIn(manifest["powershell_command_prefix"], guide)
        self.assertNotIn("'C:\\", guide)

    def test_a_plain_windows_path_needs_no_powershell_variant(self):
        with unittest.mock.patch.object(an, "agentbell_command",
                                        lambda: ["C:\\Users\\J\u00f6rg\\agentbell.exe"]), \
                unittest.mock.patch.object(an.os, "name", "nt"):
            manifest = an.integration_manifest(agent="myagent")
        self.assertEqual(manifest["command_prefix"], "C:/Users/J\u00f6rg/agentbell.exe")
        self.assertIsNone(manifest["powershell_command_prefix"])

    def test_a_checkout_is_run_through_python_on_posix(self):
        if os.name == "nt":
            self.skipTest("POSIX quoting")
        script = os.path.join(self.tmp, "a b", "agentbell.py")
        with unittest.mock.patch.object(an, "agentbell_command", lambda: [sys.executable, script]):
            manifest = an.integration_manifest(agent="myagent")
        prefix = f"{shlex.quote(sys.executable)} {shlex.quote(script)}"
        self.assertEqual(manifest["commands"]["smoke"],
                         prefix + " hook run_completed --agent myagent --force")
        self.assertIsNone(manifest["powershell_command_prefix"])

    def test_ask_exit_codes_include_3_and_free_text_is_no_approval(self):
        # N4
        manifest = an.integration_manifest(agent="myagent")
        ask = manifest["exit_codes"]["ask"]
        self.assertIn("3", ask)
        self.assertIn("NOT an approval", ask["0"])
        self.assertIn('"approved"', ask["0"])
        guide = an.integration_guide(manifest)
        self.assertIn("3 setup", guide)
        self.assertIn('NOT an approval: use --json, check "approved"', guide)


if __name__ == "__main__":
    unittest.main()
