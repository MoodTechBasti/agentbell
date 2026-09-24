"""Audit 2026-09-22, cluster json-hooks: who owns a hook entry, JSONC and
symlinked settings, read-only link targets, hooks from a checkout, and hook
command quoting for the shells Windows hosts use.

Run with: python3 -m unittest discover -s tests
"""

import builtins
import contextlib
import io
import json
import os
import shlex
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

HAS_TOMLLIB = sys.version_info >= (3, 11)
CAN_SYMLINK = os.name != "nt"
# root ignores file modes, so a read-only target would be written anyway
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class _HomeCase(unittest.TestCase):
    """A throwaway home, and agentbell resolved to a stand-in launcher - never
    to an agentbell that happens to be on the PATH of the machine."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.old_home = base._set_home(self.home)
        self.old_argv0 = sys.argv[0]
        self.launcher = base._fake_launcher(self.tmp)
        sys.argv[0] = self.launcher
        self.which = unittest.mock.patch.object(an.shutil, "which", lambda _prog: None)
        self.which.start()

    def tearDown(self):
        self.which.stop()
        base._restore_home(self.old_home)
        sys.argv[0] = self.old_argv0
        for root in (self.tmp, self.home):
            for dirpath, dirnames, _files in os.walk(root):
                for name in dirnames:
                    with contextlib.suppress(OSError):
                        os.chmod(os.path.join(dirpath, name), 0o755)
            shutil.rmtree(root, ignore_errors=True)

    def _path(self, rel):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def _write_json(self, rel, data):
        path = self._path(rel)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return path

    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _commands(self, path):
        data = json.loads(self._read(path))
        return [(event, group.get("matcher"), entry["command"])
                for event, groups in data.get("hooks", {}).items()
                for group in groups for entry in group["hooks"]]

    def _cli(self, *argv):
        """Run `agentbell <argv>` in-process: (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                an.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()


class TestUserWrittenHooksSurvive(_HomeCase):
    """M4: reinstall and uninstall touch only entries agentbell wrote."""

    USER_HOOKS = {
        "Notification": [{"matcher": "permission_prompt", "hooks": [
            {"type": "command", "command": "agentbell hook input_required --agent claude"}]}],
        "Stop": [
            {"hooks": [{"type": "command", "command":
                        "afplay /System/Library/Sounds/Glass.aiff; "
                        "agentbell hook run_completed --agent claude"}]},
            {"hooks": [{"type": "command", "command":
                        "agentbell hook run_completed --agent claude; "
                        "afplay /System/Library/Sounds/Glass.aiff"}]},
            {"hooks": [{"type": "command", "command":
                        "agentbell hook run_completed --agent claude && say done"}]},
            {"hooks": [{"type": "command", "command":
                        "agentbell hook run_completed --agent claude --priority high"}]},
        ],
    }

    def _user_commands(self):
        return sorted(entry["command"] for groups in self.USER_HOOKS.values()
                      for group in groups for entry in group["hooks"])

    def _install_then_add_user_hooks(self):
        path = an.claude_settings_path()
        self.assertTrue(an.install_hooks("claude")["changed"])
        data = json.loads(self._read(path))
        for event, groups in json.loads(json.dumps(self.USER_HOOKS)).items():
            data["hooks"].setdefault(event, []).extend(groups)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def test_reinstall_keeps_the_users_own_agentbell_hooks(self):
        path = self._install_then_add_user_hooks()
        before = self._commands(path)
        result = an.install_hooks("claude")
        self.assertFalse(result["changed"], result)
        self.assertEqual(sorted(self._commands(path)), sorted(before))
        # the user's permission_prompt hook is still under its own matcher
        self.assertIn(("Notification", "permission_prompt",
                       "agentbell hook input_required --agent claude"), self._commands(path))

    def test_a_moved_binary_still_replaces_only_our_entries(self):
        path = self._install_then_add_user_hooks()
        old = an._hook_prefix("claude") + " hook "
        sys.argv[0] = base._fake_launcher(tempfile.mkdtemp(dir=self.tmp))   # binary moved
        new = an._hook_prefix("claude") + " hook "
        self.assertNotEqual(old, new)
        self.assertTrue(an.install_hooks("claude")["changed"])
        commands = self._commands(path)
        ours = [c for _e, _m, c in commands if c.startswith(new)]
        self.assertEqual(len(ours), 5, commands)       # one per hook, the stale ones replaced
        self.assertFalse([c for _e, _m, c in commands if c.startswith(old)])
        self.assertEqual(sorted(c for _e, _m, c in commands if c not in ours),
                         self._user_commands())

    def test_uninstall_leaves_user_hooks_and_says_so(self):
        path = self._install_then_add_user_hooks()
        result = an.install_hooks("claude", add=False)
        self.assertTrue(result["changed"])
        self.assertEqual(sorted(c for _e, _m, c in self._commands(path)), self._user_commands())
        self.assertTrue(any("left in place" in note and "5 hook command" in note
                            for note in result["notes"]), result["notes"])

    def test_an_old_agentbell_entry_is_still_ours(self):
        # an entry agentbell wrote before (other binary path, no flags) is
        # repaired, not kept as a second hook
        path = self._write_json(".claude/settings.json", {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "/old/bin/agentbell hook run_completed --agent claude"}
        ]}]}})
        an.install_hooks("claude")
        stop = [c for event, _m, c in self._commands(path) if event == "Stop"]
        self.assertEqual(len(stop), 1, stop)
        self.assertIn("--min-duration", stop[0])

    def test_qwen_keeps_a_hook_in_an_event_agentbell_does_not_use(self):
        path = self._write_json(".qwen/settings.json", {"hooks": {"Notification": [{"hooks": [
            {"type": "command", "command": "agentbell hook input_required --agent qwen-code"}
        ]}]}})
        an.install_hooks("qwen-code")
        an.install_hooks("qwen-code")
        self.assertIn(("Notification", None, "agentbell hook input_required --agent qwen-code"),
                      self._commands(path))
        result = an.install_hooks("qwen-code", add=False)
        self.assertEqual(self._commands(path),
                         [("Notification", None, "agentbell hook input_required --agent qwen-code")])
        self.assertTrue(any("left in place" in note for note in result["notes"]))

    def test_gemini_keeps_a_hook_under_another_matcher(self):
        path = self._write_json(".gemini/settings.json", {"hooks": {"AfterAgent": [
            {"matcher": "my_agent", "hooks": [
                {"type": "command", "command": "agentbell hook run_completed --agent gemini"}]}
        ]}})
        an.install_hooks("gemini")
        an.install_hooks("gemini", add=False)
        self.assertEqual(self._commands(path),
                         [("AfterAgent", "my_agent", "agentbell hook run_completed --agent gemini")])

    def test_only_the_generated_command_shapes_are_ours(self):
        ours = [
            "/usr/local/bin/agentbell hook run_completed --agent claude --min-duration 60",
            "/usr/bin/python3 /src/agentbell/agentbell.py hook started --agent claude --silent",
            "'/usr/bin/python3' '/src/a b/agentbell.py' hook run_failed --agent claude",
            "C:/Users/Jo/AppData/Local/Programs/Python/Python312/Scripts/agentbell.exe "
            "hook run_completed --agent gemini",
            "& 'C:/Users/Jo Do/Scripts/agentbell.exe' hook run_completed --agent gemini",
            '"C:/Users/Jo Do/python.exe" "C:/src/agentbell.py" hook started --agent kimi --silent',
            "'C:\\Users\\Jo Do\\agentbell.exe' hook run_completed --agent x",
        ]
        users = [
            "agentbell hook run_completed --agent claude; afplay done.aiff",
            "agentbell hook run_completed --agent claude && say done",
            "agentbell hook run_completed --agent claude | tee -a log",
            "agentbell hook run_completed --agent claude --priority high",
            "agentbell hook run_completed --agent claude --min-duration soon",
            "afplay done.aiff; agentbell hook run_completed --agent claude",
            "python3 notify.py hook run_completed --agent claude",
            "bash -c 'agentbell hook run_completed --agent claude'",
        ]
        for command in ours:
            self.assertTrue(an._is_our_hook_command(command), command)
        for command in users:
            self.assertFalse(an._is_our_hook_command(command), command)

    @unittest.skipUnless(HAS_TOMLLIB, "tomllib is 3.11+")
    def test_codex_keeps_a_user_hook_that_calls_agentbell_inside_the_block(self):
        import tomllib
        path = an.codex_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        user = ("[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = \"command\"\n"
                "command = \"agentbell hook run_completed --agent codex && say done\"\n")
        block = an.codex_hooks_block()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(block.replace(an.TOML_END, user + an.TOML_END, 1))
        an.install_codex_hooks()
        self.assertIn("&& say done", self._read(path))
        tomllib.loads(self._read(path))
        an.uninstall_codex_hooks()
        text = self._read(path)
        self.assertIn("&& say done", text)
        self.assertNotIn(an.TOML_START, text)
        tomllib.loads(text)


class TestJsoncSettingsAreRefused(_HomeCase):
    """L-jsonc: comments or trailing commas are refused, never a traceback,
    never a rewrite that drops the comments."""

    JSONC = '{\n  // my model\n  "model": "opus",\n  /* keep */\n  "hooks": {}\n}\n'

    def test_install_and_uninstall_refuse_a_commented_file(self):
        path = self._path(".claude/settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.JSONC)
        for sub in ("install", "uninstall"):
            code, out, err = self._cli("hooks", sub, "claude")
            self.assertEqual(code, 1, (out, err))
            self.assertNotIn("Traceback", err)
            self.assertIn("has comments that a rewrite would drop", err)
            self.assertEqual(self._read(path), self.JSONC)
        # the install refusal carries the hooks to add by hand
        _code, _out, err = self._cli("hooks", "install", "claude")
        self.assertIn('"Stop"', err)
        self.assertIn("hook run_completed --agent claude", err)

    def test_a_trailing_comma_is_refused_and_the_next_agent_still_runs(self):
        text = '{"general": {"vimMode": true},}\n'
        path = self._path(".gemini/settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        code, out, err = self._cli("hooks", "install", "gemini", "claude")
        self.assertEqual(code, 1)
        self.assertIn("is not valid JSON", err)
        self.assertEqual(self._read(path), text)
        self.assertIn("installed hooks for claude", out)
        self.assertTrue(an._file_contains_our_hook(an.claude_settings_path()))

    def test_a_non_object_file_is_refused(self):
        path = self._path(".claude/settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[]\n")
        with self.assertRaises(RuntimeError):
            an.install_hooks("claude")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"hooks": ["x"]}\n')
        with self.assertRaises(RuntimeError):
            an.install_hooks("claude")
        self.assertEqual(self._read(path), '{"hooks": ["x"]}\n')

    def test_an_empty_file_is_an_empty_object(self):
        path = self._path(".claude/settings.json")
        open(path, "w").close()
        self.assertTrue(an.install_hooks("claude")["changed"])
        self.assertTrue(an._file_contains_our_hook(path))

    def test_init_reports_a_refused_config_and_goes_on(self):
        old_config = os.environ.get(an.CONFIG_DIR_ENV)
        os.environ[an.CONFIG_DIR_ENV] = os.path.join(self.tmp, "config")
        args = base._init_args(non_interactive=False, no_hooks=False, no_test=True,
                               topic="audit2-json-hooks-topic")
        out, err = io.StringIO(), io.StringIO()
        try:
            with unittest.mock.patch.object(an.sys.stdin, "isatty", lambda: True), \
                    unittest.mock.patch.object(builtins, "input", lambda *_a: ""), \
                    unittest.mock.patch.object(an, "find_agents", lambda: ["claude"]), \
                    unittest.mock.patch.object(
                        an, "install_hooks",
                        unittest.mock.Mock(side_effect=RuntimeError("settings.json refused"))), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                an.cmd_init(args)
        finally:
            if old_config is None:
                os.environ.pop(an.CONFIG_DIR_ENV, None)
            else:
                os.environ[an.CONFIG_DIR_ENV] = old_config
        self.assertIn("hooks for claude not changed: settings.json refused", err.getvalue())
        self.assertIn("NEXT STEPS", out.getvalue())


@unittest.skipUnless(CAN_SYMLINK, "os.symlink requires admin or developer mode on Windows")
class TestSymlinkedSettings(_HomeCase):
    """L-json-symlink: a dotfiles link is updated where it points."""

    def test_install_and_uninstall_write_through_the_link(self):
        dotfiles = os.path.join(self.tmp, "dotfiles")
        os.makedirs(dotfiles)
        real = os.path.join(dotfiles, "claude.json")
        with open(real, "w", encoding="utf-8") as fh:
            json.dump({"model": "opus"}, fh)
        os.chmod(real, 0o600)
        link = self._path(".claude/settings.json")
        os.symlink(real, link)
        self.assertTrue(an.install_hooks("claude")["changed"])
        self.assertTrue(os.path.islink(link))
        self.assertTrue(an._file_contains_our_hook(real))
        self.assertEqual(json.loads(self._read(real))["model"], "opus")
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)
        self.assertTrue(an.install_hooks("claude", add=False)["changed"])
        self.assertTrue(os.path.islink(link))
        self.assertEqual(json.loads(self._read(real)), {"model": "opus"})
        self.assertFalse(os.path.lexists(real + ".tmp"))


@unittest.skipUnless(CAN_SYMLINK, "os.symlink requires admin or developer mode on Windows")
@unittest.skipIf(IS_ROOT, "root writes read-only files anyway")
class TestReadOnlyLinkTarget(_HomeCase):
    """A6: a config linked into a read-only store (Nix/home-manager) gets a
    clear message and exit 1, not a traceback; nothing is changed."""

    def _link_into_store(self, rel, text):
        store = os.path.join(self.tmp, "nix-store")
        os.makedirs(store, exist_ok=True)
        os.chmod(store, 0o755)
        target = os.path.join(store, rel.replace("/", "-"))
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(target, 0o444)
        os.chmod(store, 0o555)
        link = self._path(rel)
        os.symlink(target, link)
        # the message names the resolved target (/var is /private/var on macOS)
        return link, os.path.realpath(target)

    def test_toml_and_json_writers_explain_and_change_nothing(self):
        cases = {
            "codex": (".codex/config.toml", 'model = "gpt-5"\n'),
            "kimi": (".kimi-code/config.toml", 'default_model = "k2"\n'),
            "claude": (".claude/settings.json", '{"model": "opus"}\n'),
        }
        links = {agent: self._link_into_store(rel, text) for agent, (rel, text) in cases.items()}
        code, out, err = self._cli("hooks", "install", *cases)
        self.assertEqual(code, 1, (out, err))
        self.assertNotIn("Traceback", err)
        for agent, (link, target) in links.items():
            self.assertIn(f"hooks for {agent} not changed: cannot write {link} "
                          f"(a symlink to {target})", err)
            self.assertTrue(os.path.islink(link))
            self.assertEqual(self._read(target), cases[agent][1])
        self.assertIn("Nix/home-manager", err)

    def test_writer_error_is_an_oserror_with_the_paths(self):
        link, target = self._link_into_store(".claude/settings.json", "{}\n")
        with self.assertRaises(OSError) as caught:
            an.write_json_atomic(link, {"a": 1})
        self.assertIn(f"cannot write {link} (a symlink to {target})", str(caught.exception))
        self.assertEqual(self._read(target), "{}\n")


class TestHooksFromACheckout(_HomeCase):
    """M8: agentbell.py is not executable; a hook must run it through Python."""

    def _checkout(self):
        """agentbell resolved as a checkout: not on PATH, argv[0] foreign."""
        return unittest.mock.patch.object(sys, "argv", ["python -m unittest"])

    def test_hook_commands_use_the_interpreter(self):
        with self._checkout():
            script = an.agentbell_binary()
            prefix = f"{shlex.quote(sys.executable)} {shlex.quote(script)}"
            self.assertTrue(script.endswith(".py"))
            self.assertEqual(an.agentbell_command(), [sys.executable, script])
            if os.name != "nt":
                self.assertTrue(an._hook_command("run_failed", "claude").startswith(prefix + " hook"))
                self.assertIn(prefix + " hook started --agent codex", an.codex_hooks_block())
                self.assertIn(prefix + " hook started --agent kimi", an.kimi_hooks_block())
            plugin = an._render_opencode_plugin()
            self.assertIn("const BIN = " + json.dumps([sys.executable, script]), plugin)
            an.install_hooks("claude")
            # the new shape is recognized as ours: reinstall is a no-op, uninstall removes it
            self.assertFalse(an.install_hooks("claude")["changed"])
            self.assertTrue(an.install_hooks("claude", add=False)["changed"])
            self.assertFalse(an._file_contains_our_hook(an.claude_settings_path()))

    @unittest.skipIf(os.name == "nt", "runs the hook through /bin/sh")
    def test_the_generated_hook_actually_runs(self):
        state = os.path.join(self.tmp, "state")
        with self._checkout():
            command = an._hook_command("started", "claude") + " --silent"
        env = dict(os.environ, AGENTBELL_STATE_DIR=state, HOME=self.home, PATH="/usr/bin:/bin")
        proc = subprocess.run(["/bin/sh", "-c", command], env=env, input="{}",
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.listdir(os.path.join(state, "runs")))    # the start marker

    def test_an_installed_launcher_stays_one_token(self):
        self.assertEqual(an.agentbell_command(), [os.path.abspath(self.launcher)])
        plugin = an._render_opencode_plugin()
        self.assertIn("const BIN = " + json.dumps(os.path.abspath(self.launcher)) + "\n", plugin)


class TestWindowsHookQuoting(unittest.TestCase):
    """M19: each Windows host runs hook commands in its own shell. Checked by
    hand on Windows 11 (Python 3.14, PowerShell 5.1, cmd, Git Bash): each
    host's form ran agentbell from a path with a space, in the shell that
    host uses, and wrote its start marker. These tests pin the forms."""

    SPACED = ["C:\\Users\\Jo Do\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
              "C:\\src\\agent bell\\agentbell.py"]
    PLAIN = ["C:\\Users\\Jo\\AppData\\Local\\Programs\\Python\\Python312\\Scripts\\agentbell.exe"]

    def test_a_plain_path_is_bare_with_forward_slashes(self):
        # runs unquoted in cmd, PowerShell and Git Bash alike
        for shell in ("cmd", "powershell"):
            self.assertEqual(
                an._windows_command_line(self.PLAIN, shell),
                "C:/Users/Jo/AppData/Local/Programs/Python/Python312/Scripts/agentbell.exe")

    def test_a_spaced_path_gets_the_hosts_quoting(self):
        self.assertEqual(
            an._windows_command_line(self.SPACED, "powershell"),
            "& 'C:/Users/Jo Do/AppData/Local/Programs/Python/Python312/python.exe' "
            "'C:/src/agent bell/agentbell.py'")
        self.assertEqual(
            an._windows_command_line(self.SPACED, "cmd"),
            '"C:/Users/Jo Do/AppData/Local/Programs/Python/Python312/python.exe" '
            '"C:/src/agent bell/agentbell.py"')

    def test_powershell_quotes_are_doubled(self):
        line = an._windows_command_line(["C:\\Users\\O'Neil\u2019s\\agentbell.exe"], "powershell")
        self.assertEqual(line, "& 'C:/Users/O''Neil\u2019\u2019s/agentbell.exe'")

    def _windows(self, argv):
        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(an, "agentbell_command", lambda: argv))
        stack.enter_context(unittest.mock.patch.object(an.os, "name", "nt"))
        return stack

    def test_each_host_gets_the_form_its_shell_runs(self):
        with self._windows(self.SPACED):
            claude = an._hook_command("run_completed", "claude")
            gemini = an._hook_command("run_completed", "gemini")
            qwen = an.qwen_event_hooks()
            codex = an.codex_hooks_block()
            kimi = an.kimi_hooks_block()
        self.assertTrue(claude.startswith('"C:/Users/Jo Do/'), claude)      # Git Bash
        self.assertTrue(gemini.startswith("& 'C:/Users/Jo Do/"), gemini)    # PowerShell
        self.assertIn("command = \"& 'C:/Users/Jo Do/", codex)              # PowerShell
        self.assertIn('command = "\\"C:/Users/Jo Do/', kimi)               # cmd
        # Qwen's cmd.exe escapes quotes as \" - its hooks ask for PowerShell
        qwen_entries = [entry for groups in qwen.values() for group in groups
                        for entry in group["hooks"]]
        self.assertEqual(len(qwen_entries), 3)
        for entry in qwen_entries:
            self.assertEqual(entry["shell"], "powershell")
            self.assertTrue(entry["command"].startswith("& 'C:/Users/Jo Do/"), entry)
        # uninstall and self-heal must recognize every generated form
        for command in (claude, gemini):
            self.assertEqual(an._parse_our_hook_command(command),
                             ("run_completed", command.rsplit(" ", 1)[1]))
        for entry in qwen_entries:
            self.assertTrue(an._is_our_hook_command(entry["command"]), entry)

    def test_qwen_gets_no_shell_key_elsewhere(self):
        if os.name == "nt":
            self.skipTest("POSIX shape")
        entries = [entry for groups in an.qwen_event_hooks().values() for group in groups
                   for entry in group["hooks"]]
        self.assertFalse([entry for entry in entries if "shell" in entry])

    def test_status_sees_the_double_quoted_form_in_raw_text(self):
        # raw JSON and TOML escape its quotes: \"C:/.../agentbell.exe\" hook
        argv = ["C:\\Users\\Jo Do\\Scripts\\agentbell.exe"]
        with self._windows(argv):
            claude = an._hook_command("run_completed", "claude")
            kimi = an.kimi_hooks_block()
        self.assertIn('\\"', json.dumps({"command": claude}))
        self.assertTrue(an._OUR_HOOK_RE.search(json.dumps({"command": claude})))
        self.assertTrue(an._OUR_HOOK_RE.search(kimi.replace(an.TOML_START, "")))

    @unittest.skipUnless(HAS_TOMLLIB, "tomllib is 3.11+")
    def test_generated_toml_stays_valid_and_ours(self):
        import tomllib
        with self._windows(self.SPACED):
            codex = tomllib.loads(an.codex_hooks_block())
            kimi = tomllib.loads(an.kimi_hooks_block())
        commands = ([h["command"] for g in codex["hooks"]["Stop"] for h in g["hooks"]]
                    + [h["command"] for h in kimi["hooks"]])
        self.assertTrue(commands)
        for command in commands:
            self.assertTrue(an._is_our_hook_command(command), command)


if __name__ == "__main__":
    unittest.main()
