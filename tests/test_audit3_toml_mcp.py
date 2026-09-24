"""Audit round 3, cluster toml-mcp: Codex/Kimi TOML writers and MCP registrations.

HC5/S3  hooks install + uninstall keep every foreign byte (final newline, CRLF)
HC2     Kimi: the user's own agentbell hook under another event is theirs
HC10    Codex: a hand-written agentbell hook is seen, not duplicated
HC11    Kimi: a commented-out hook is not an installed hook
HC12/IW-8  a refused config makes `hooks install` exit 1, like JSONC does
HC1     `mcp add codex` repairs command and args together, and leaves a runner alone
HC4/IW-3  MCP registrations from a checkout run agentbell.py through Python;
        doctor warns about a command the client cannot start
HC6     a project-scoped MCP config may not lead outside the project
N3      `mcp add --print` gives Zed users a `context_servers` snippet
S8      mcp_handle builds its errors with _rpc_error
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import only: sets the sandbox env)
import agentbell as an  # noqa: E402

try:
    import tomllib
except ImportError:      # 3.9 / 3.10: structure is still checked, parsing is skipped
    tomllib = None

BINARY = "/opt/bin/agentbell"


class _Home(unittest.TestCase):
    """A throwaway HOME and config dirs; agentbell is at BINARY."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentbell-audit3-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.addCleanup(base._restore_home, base._set_home(self.home))
        for patcher in (
                unittest.mock.patch.dict(os.environ, {
                    "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
                    "APPDATA": os.path.join(self.home, "AppData"),
                    "KIMI_CODE_HOME": os.path.join(self.home, ".kimi-code"),
                    "QWEN_HOME": os.path.join(self.home, ".qwen"),
                    an.STATE_DIR_ENV: os.path.join(self.tmp, "state"),
                    an.CONFIG_FILE_ENV: os.path.join(self.tmp, "config.json"),
                }),
                unittest.mock.patch.object(an, "agentbell_binary", lambda: BINARY)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def hooks_cli(self, sub, agent, project=None):
        """(exit code, stdout) of `agentbell hooks <sub> <agent>`."""
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                an.cmd_hooks(base._Args(sub=sub, agent=[agent], project=project))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def toml(self, path):
        if tomllib is None:
            return None
        with open(path, "rb") as fh:
            return tomllib.load(fh)


FIXTURES = {
    "lf": b'model = "o3"\n\n[tui]\ntheme = "dark"\n',
    "crlf": b'model = "o3"\r\n\r\n[tui]\r\ntheme = "dark"\r\n',
    "no final newline": b'model = "o3"\n[tui]\ntheme = "dark"',
    "two final newlines": b'model = "o3"\n[tui]\ntheme = "dark"\n\n',
    "trailing comment": b'model = "o3"\n[tui]\ntheme = "dark"\n# trailing note\n',
    "trailing whitespace": b'model = "o3"\n[tui]\ntheme = "dark"\n   \n',
    "comment only": b"# my config\n",
    "crlf comment only": b"# my config\r\n",
    "empty": b"",
}


class TestTomlRoundTripKeepsBytes(_Home):
    """HC5/S3: install then uninstall used to rstrip the text before the block
    and rewrite every line end in the platform's style."""

    AGENTS = {
        "codex": (lambda: an.codex_config_path(), an.install_codex_hooks,
                  lambda: an.uninstall_codex_hooks()),
        "kimi": (lambda: an.kimi_config_path(), an.install_kimi_hooks,
                 lambda: an.uninstall_kimi_hooks()["changed"]),
    }

    def test_install_then_uninstall_is_byte_identical(self):
        for agent, (path_of, install, uninstall) in self.AGENTS.items():
            for name, original in FIXTURES.items():
                with self.subTest(agent=agent, fixture=name):
                    path = path_of()
                    self.write(path, original)
                    self.assertTrue(install()["changed"])
                    installed = self.read(path)
                    if b"\r\n" in original:
                        self.assertNotIn(b"\n", installed.replace(b"\r\n", b""))
                    elif original:
                        self.assertNotIn(b"\r", installed)
                    self.assertTrue(uninstall())
                    self.assertEqual(self.read(path), original)

    def test_a_stale_block_is_replaced_without_touching_the_rest(self):
        for agent, (path_of, install, uninstall) in self.AGENTS.items():
            with self.subTest(agent=agent):
                path = path_of()
                original = FIXTURES["crlf"] + b"# after\r\n"
                self.write(path, original)
                with unittest.mock.patch.object(an, "agentbell_binary", lambda: "/old/agentbell"):
                    install()
                self.assertTrue(install()["changed"])        # new binary path: repaired
                text = self.read(path)
                self.assertIn(BINARY.encode(), text)
                self.assertNotIn(b"/old/agentbell", text)
                self.assertNotIn(b"\n", text.replace(b"\r\n", b""))
                self.assertTrue(uninstall())
                self.assertEqual(self.read(path), original)

    def test_mcp_add_codex_writes_the_file_line_end(self):
        path = an.codex_config_path()
        original = b'model = "o3"\r\n# c\r\n[profiles.x]\r\nmodel = "gpt"\r\n'
        self.write(path, original)
        self.assertIn("written", an._mcp_add_codex(BINARY))
        text = self.read(path)
        self.assertTrue(text.startswith(original))
        self.assertNotIn(b"\n", text.replace(b"\r\n", b""))
        self.assertTrue(an._remove_codex_mcp_block())
        self.assertEqual(self.read(path), original)


KIMI_USER_HOOK = ('[[hooks]]\nevent = "PreToolUse"\nmatcher = "Shell"\n'
                  'command = "agentbell hook input_required --agent kimi"\ntimeout = 10\n')


class TestKimiOwnershipByLocation(_Home):
    """HC2 and HC11: Kimi's marker-less path judged ownership by the command
    text alone, comments included."""

    def test_the_users_own_hook_under_another_event_stays(self):
        path = an.kimi_config_path()
        original = 'default_model = "k2"\n\n# my own\n' + KIMI_USER_HOOK
        self.write(path, original)
        self.assertEqual(dict((a, s) for a, s, _, _ in an.hooks_status())["kimi"], "not installed")
        self.assertTrue(an.install_kimi_hooks()["changed"])
        self.assertIn(b"hook run_completed --agent kimi", self.read(path))
        self.assertTrue(an.uninstall_kimi_hooks()["changed"])
        self.assertEqual(self.read(path).decode(), original)

    def test_without_markers_only_the_lifecycle_tables_go(self):
        path = an.kimi_config_path()
        self.write(path, KIMI_USER_HOOK)
        an.install_kimi_hooks()
        stripped = self.read(path).decode().replace(an.TOML_START + "\n", "").replace(
            an.TOML_END + "\n", "")
        self.write(path, stripped)
        result = an.install_kimi_hooks()          # markers gone: no second copy
        self.assertFalse(result["changed"])
        self.assertIn("markers are gone", result["notes"][0])
        self.assertEqual(self.read(path).decode(), stripped)
        removed = an.uninstall_kimi_hooks()
        self.assertTrue(removed["changed"])
        left = self.read(path).decode()
        self.assertIn(KIMI_USER_HOOK, left)
        self.assertEqual(left.count("[[hooks]]"), 1)

    def test_a_commented_out_hook_is_not_installed(self):
        path = an.kimi_config_path()
        self.write(path, 'default_model = "k2"\n# disabled for now:\n# [[hooks]]\n'
                         '# event = "Stop"\n# command = "agentbell hook run_completed --agent kimi"\n')
        self.assertFalse(an.AGENT_SPECS["kimi"]["status"](None))
        self.assertTrue(an.install_kimi_hooks()["changed"])
        data = self.toml(path)
        if data is not None:
            self.assertEqual([hook["event"] for hook in data["hooks"]],
                             ["UserPromptSubmit", "Stop", "StopFailure"])

    def test_a_wrapper_is_a_user_wrapper_and_gets_no_second_hook(self):
        path = an.kimi_config_path()
        original = ('[[hooks]]\nevent = "Stop"\n'
                    'command = "sh -c \'agentbell hook run_completed --agent kimi; say done\'"\n')
        self.write(path, original)
        result = an.install_kimi_hooks()
        self.assertFalse(result["changed"])
        self.assertIn("user-owned", result["notes"][0])
        self.assertEqual(self.read(path).decode(), original)
        self.assertEqual(dict((a, s) for a, s, _, _ in an.hooks_status())["kimi"], "user wrapper")


class TestCodexHandWrittenHook(_Home):
    """HC10: status looked only for the marker, and install appended a
    second Stop hook next to the user's identical one."""

    HOOK = ('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
            'command = "agentbell hook run_completed --agent codex"\n')

    def status(self):
        return dict((agent, status) for agent, status, _, _ in an.hooks_status())["codex"]

    def test_an_identical_hand_written_hook_counts_and_is_not_duplicated(self):
        path = an.codex_config_path()
        self.write(path, self.HOOK)
        self.assertEqual(self.status(), "installed")
        result = an.install_codex_hooks()
        self.assertFalse(result["changed"])
        self.assertIn("markers are gone", result["notes"][0])
        self.assertEqual(self.read(path).decode(), self.HOOK)

    def test_uninstall_removes_what_status_counts(self):
        # `agentbell uninstall` lists what status reports; "already gone"
        # for a hook that stays would be a lie
        path = an.codex_config_path()
        other = ('[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "say done"\n')
        for before, after in (('model = "o3"\n\n' + self.HOOK, 'model = "o3"\n\n'),
                              (self.HOOK + other, "[[hooks.Stop]]\n" + other)):
            with self.subTest(before):
                self.write(path, before)
                self.assertTrue(an.uninstall_codex_hooks())
                self.assertEqual(self.read(path).decode(), after)
                self.assertEqual(self.status(), "not installed")

    def test_a_wrapper_reads_user_wrapper(self):
        path = an.codex_config_path()
        wrapped = self.HOOK.replace('"agentbell hook run_completed --agent codex"',
                                    "\"sh -c 'agentbell hook run_completed --agent codex; x'\"")
        self.write(path, wrapped)
        self.assertEqual(self.status(), "user wrapper")
        self.assertFalse(an.install_codex_hooks()["changed"])
        self.assertEqual(self.read(path).decode(), wrapped)

    def test_the_users_hook_for_another_event_does_not_block_install(self):
        path = an.codex_config_path()
        other = self.HOOK.replace("Stop", "PreToolUse").replace(
            "run_completed", "input_required")
        self.write(path, other)
        self.assertEqual(self.status(), "not installed")
        self.assertTrue(an.install_codex_hooks()["changed"])
        self.assertTrue(an.uninstall_codex_hooks())
        self.assertEqual(self.read(path).decode(), other)


class TestRefusedInstallExitCode(_Home):
    """HC12/IW-8: a refused TOML or rule file exited 0, a refused JSONC 1."""

    def test_every_refusal_exits_1(self):
        project = os.path.join(self.tmp, "proj")
        cases = {
            "codex": (an.codex_config_path(), '[hooks]\nStop = [{ hooks = [] }]\n'),
            "kimi": (an.kimi_config_path(), "hooks = []\n"),
            "claude": (an.claude_settings_path(), "// my settings\n{}\n"),
            "aider": (os.path.join(project, "AGENTS.md"),
                      "# Rules\n" + an.BLOCK_START + "\nstray\n"),
        }
        for agent, (path, text) in cases.items():
            with self.subTest(agent):
                self.write(path, text)
                code, _out = self.hooks_cli("install", agent, project)
                self.assertEqual(code, 1)
                self.assertEqual(self.read(path).decode(), text)

    def test_an_install_that_is_already_there_exits_0(self):
        self.assertEqual(self.hooks_cli("install", "codex")[0], 0)
        code, out = self.hooks_cli("install", "codex")
        self.assertEqual(code, 0)
        self.assertIn("already installed", out)


class TestCodexMcpRepair(_Home):
    """HC1: the stale-path repair swapped `command` and kept `args`."""

    def test_an_interpreter_registration_is_repaired_as_a_whole(self):
        path = an.codex_config_path()
        self.write(path, '[mcp_servers.agentbell]\r\ncommand = "/usr/bin/python3"\r\n'
                         'args = ["/home/u/src/agentbell/agentbell.py", "mcp"]\r\n'
                         'startup_timeout_sec = 20\r\n')
        self.assertIn("updated", an._mcp_add_codex(BINARY))
        self.assertEqual(self.read(path).decode(),
                         f'[mcp_servers.agentbell]\r\ncommand = "{BINARY}"\r\n'
                         'args = ["mcp"]\r\nstartup_timeout_sec = 20\r\n')
        self.assertEqual(an._mcp_add_codex(BINARY), "already present")

    def test_a_runner_the_user_chose_is_left_alone(self):
        path = an.codex_config_path()
        for command, args in (("uvx", '["agentbell", "mcp"]'),
                              ("pipx", '["run", "agentbell", "mcp"]'),
                              ("/usr/bin/true", '["mcp"]'),
                              ("/opt/bin/agentbell", '[\n  "mcp",\n]')):
            with self.subTest(command):
                original = f'[mcp_servers.agentbell]\ncommand = "{command}"\nargs = {args}\n'
                self.write(path, original)
                status = an._mcp_add_codex(BINARY)
                self.assertTrue(status.startswith("left unchanged"), status)
                self.assertIn(f'command = "{BINARY}"', status)
                self.assertEqual(self.read(path).decode(), original)


class TestMcpFromACheckout(_Home):
    """HC4/IW-3: every MCP writer registered the non-executable agentbell.py."""

    SCRIPT = "/home/u/src/agentbell/agentbell.py"

    def test_every_writer_runs_the_script_through_python(self):
        argv = [sys.executable, self.SCRIPT, "mcp"]
        rows = dict(an.mcp_add_configs(self.SCRIPT, clients=["gemini", "vscode", "codex",
                                                             "opencode"]))
        self.assertFalse([row for row in rows.values() if row.startswith("FAILED")], rows)
        with open(an.gemini_settings_path(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["mcpServers"]["agentbell"],
                             {"command": argv[0], "args": argv[1:]})
        with open(an.vscode_mcp_path(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["servers"]["agentbell"],
                             {"type": "stdio", "command": argv[0], "args": argv[1:]})
        with open(an.opencode_config_path(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["mcp"]["agentbell"]["command"], argv)
        data = self.toml(an.codex_config_path())
        if data is not None:
            self.assertEqual(data["mcp_servers"]["agentbell"],
                             {"command": argv[0], "args": argv[1:]})
        self.assertEqual(an._mcp_add_codex(self.SCRIPT), "already present")
        snippet = an.mcp_snippet(self.SCRIPT)
        self.assertIn(json.dumps(argv[0]), snippet)
        self.assertNotIn(f'command = "{self.SCRIPT}"', snippet)

    def test_an_installed_launcher_is_registered_as_is(self):
        an.mcp_add_configs(BINARY, clients=["gemini"])
        with open(an.gemini_settings_path(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["mcpServers"]["agentbell"],
                             {"command": BINARY, "args": ["mcp"]})

    def mcp_checks(self):
        data = an.default_config()
        data["ntfy"].update({"server": "http://127.0.0.1:1", "topic": "audit3-topic-0123456789"})
        cfg = an.Config(data, path=an.config_path())
        with unittest.mock.patch.object(an.NtfyChannel, "poll", return_value=[]):
            return [c for c in an.doctor_checks(cfg) if c["name"] == "mcp"]

    def test_doctor_warns_about_a_command_the_client_cannot_start(self):
        self.write(an.gemini_settings_path(), json.dumps(
            {"mcpServers": {"agentbell": {"command": self.SCRIPT, "args": ["mcp"]}}}))
        self.write(an.codex_config_path(), '[mcp_servers.agentbell]\n'
                   f'command = {an.toml_string(sys.executable)}\nargs = ["x.py", "mcp"]\n')
        checks = self.mcp_checks()
        self.assertEqual([c["status"] for c in checks], [an.OK, an.WARN])
        self.assertIn("codex/chatgpt-desktop", checks[0]["detail"])
        self.assertNotIn("gemini", checks[0]["detail"])
        self.assertIn(self.SCRIPT, checks[1]["detail"])
        self.assertEqual(checks[1]["fix"], "agentbell mcp add gemini")

    def test_doctor_without_any_registration_still_says_so(self):
        checks = self.mcp_checks()
        self.assertEqual([c["status"] for c in checks], [an.WARN])
        self.assertIn("not registered", checks[0]["detail"])


@unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
class TestProjectMcpSymlink(_Home):
    """HC6: `mcp add --project` wrote through a symlink the repo shipped."""

    def test_a_link_out_of_the_project_is_refused(self):
        project = os.path.join(self.tmp, "repo")
        target = os.path.join(self.home, "secret", "other.json")
        original = '{"theme":  "Ümlaut", "tokens": [1,2,3]}'
        self.write(target, original)
        for client, path in (("cursor", an.cursor_mcp_path(project)),
                             ("kimi", an.kimi_mcp_path(project)),
                             ("qwen-code", an.qwen_settings_path(project)),
                             ("opencode", os.path.join(project, "opencode.json"))):
            with self.subTest(client):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.symlink(target, path)
                rows = dict(an.mcp_add_configs(BINARY, project=project, clients=[client]))
                self.assertTrue(rows[client].startswith("FAILED"), rows)
                self.assertIn("outside", rows[client])
                self.assertEqual(self.read(target).decode(), original)
                self.assertTrue(os.path.islink(path))

    def test_a_regular_project_file_is_still_written(self):
        project = os.path.join(self.tmp, "repo")
        os.makedirs(project)
        rows = dict(an.mcp_add_configs(BINARY, project=project, clients=["cursor"]))
        self.assertTrue(rows["cursor"].startswith("written"), rows)


class TestZedSnippet(unittest.TestCase):
    """N3: Zed reads custom MCP servers from "context_servers" in its
    settings.json (zed.dev/docs/ai/mcp, checked 2026-09-23)."""

    def test_print_has_a_context_servers_block(self):
        snippet = an.mcp_snippet(BINARY)
        zed = snippet.split("Zed (settings.json", 1)[1].split("\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(json.loads(zed), {"context_servers": {"agentbell": {
            "command": BINARY, "args": ["mcp"], "env": {}}}})
        self.assertNotIn("Zed", snippet.split("\n", 1)[0])


class TestRpcErrors(unittest.TestCase):
    """S8: the -32601/-32603 replies go through _rpc_error like the others."""

    def test_unknown_method_and_internal_error(self):
        self.assertEqual(an.mcp_handle({"jsonrpc": "2.0", "id": 7, "method": "nope"}),
                         an._rpc_error(7, -32601, "method not found: nope"))
        with unittest.mock.patch.object(an, "mcp_tool_call", side_effect=ValueError("boom")):
            self.assertEqual(an.mcp_handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                            "params": {"name": "notify"}}),
                             an._rpc_error(8, -32603, "boom"))


if __name__ == "__main__":
    unittest.main()
