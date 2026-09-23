"""Audit round 4, cluster configs: Codex/Kimi TOML configs, rule files, integrate.

CFG-1   marker-less Codex/Kimi hooks with a moved binary are repaired in place;
        status and doctor do not call a hook that runs a missing file installed
CFG-2   a symlinked AGENTS.md: removal says the block is still there and fails
CFG-3   Codex install keeps a user's `features.hooks = true` under another table
CFG-4   removing marker-less hook tables keeps the comments after them
CFG-5/INT-2  a non-UTF-8 config.toml is refused in one line, not a traceback
CFG-6   the json-hooks tests do not read rule files from the developer's cwd
CFG-7/INT-3  the uninstall plan finds the Codex MCP table the way doctor does
INT-1   integrate's canonical MCP entry is the one `mcp add` writes
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import only: sets the sandbox env)
import agentbell as an  # noqa: E402

class _Home(unittest.TestCase):
    """A throwaway HOME, cwd and config dirs; agentbell is a launcher that exists."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentbell-audit4-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.home)
        os.makedirs(self.project)
        self.addCleanup(base._restore_home, base._set_home(self.home))
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.project)
        self.binary = base._fake_launcher(self.tmp)
        # where agentbell used to be: absolute, gone, and in the forward-slash
        # form hooks are written in on Windows too
        self.old = os.path.join(self.tmp, "gone", "agentbell.py").replace("\\", "/")
        for patcher in (
                unittest.mock.patch.dict(os.environ, {
                    "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
                    "KIMI_CODE_HOME": os.path.join(self.home, ".kimi-code"),
                    "QWEN_HOME": os.path.join(self.home, ".qwen"),
                    an.STATE_DIR_ENV: os.path.join(self.tmp, "state"),
                    an.CONFIG_FILE_ENV: os.path.join(self.tmp, "config.json"),
                }),
                unittest.mock.patch.object(an.shutil, "which", lambda _prog: None),
                unittest.mock.patch.object(an, "agentbell_binary", lambda: self.binary)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def call(self, func, *args):
        """(exit code, stdout, stderr) of func(*args)."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = func(*args)
                code = result if type(result) is int else 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def status(self, agent):
        return {row[0]: row[1] for row in an.hooks_status(self.project)}[agent]

    def hook_checks(self):
        data = an.default_config()
        data["ntfy"].update({"server": "http://127.0.0.1:1", "topic": "audit4-topic-0123456789"})
        cfg = an.Config(data, path=an.config_path())
        with unittest.mock.patch.object(an.NtfyChannel, "poll", return_value=[]), \
                unittest.mock.patch.object(an, "find_agents", lambda: []):
            return [c for c in an.doctor_checks(cfg) if c["name"] == "agent hooks"]


def _kimi_tables(prefix):
    return "".join(
        f'[[hooks]]\r\nevent = "{event}"\r\ncommand = {an.toml_string(prefix + " " + rest)}\r\n'
        "timeout = 10\r\n\r\n"
        for event, rest in (("UserPromptSubmit", "hook started --agent kimi --silent"),
                            ("Stop", "hook run_completed --agent kimi --min-duration 60"),
                            ("StopFailure", "hook run_failed --agent kimi")))


def _codex_table(prefix):
    command = an.toml_string(prefix + " hook run_completed --agent codex --min-duration 60")
    return ('model = "o3"\n\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n'
            f'type = "command"\ncommand = {command}\nasync = true\n\n'
            '# ---- trusted projects ----\n[projects."/home/u/x"]\ntrust_level = "trusted"\n')


class TestMovedBinary(_Home):
    """CFG-1: install left a marker-less hook on a gone path, and status said OK."""

    def test_kimi_tables_get_the_current_path_in_place(self):
        path = an.kimi_config_path()
        old_prefix = "/usr/bin/python3 " + self.old
        original = 'default_model = "k2"\r\n\r\n' + _kimi_tables(old_prefix)
        self.write(path, original)
        self.assertEqual(self.status("kimi"), "update needed")
        result = an.install_hooks("kimi")
        self.assertTrue(result["changed"])
        self.assertIn("updated the hook commands", result["notes"][0])
        # only the path changed: the CRLFs and the flags (the tuned 60 s) stay
        self.assertEqual(self.read(path).decode("utf-8"),
                         original.replace(old_prefix, an._hook_prefix("kimi")))
        self.assertEqual(self.status("kimi"), "installed")
        self.assertFalse(an.install_hooks("kimi")["changed"])

    def test_codex_table_gets_the_current_path_in_place(self):
        path = an.codex_config_path()
        original = _codex_table(self.old)
        self.write(path, original)
        self.assertEqual(self.status("codex"), "update needed")
        code, out, _err = self.call(an._install_and_report, "codex", None, True)
        self.assertEqual(code, 0)
        self.assertIn("updated the hook commands", out)
        self.assertEqual(self.read(path).decode("utf-8"),
                         original.replace(self.old, an._hook_prefix("codex")))
        self.assertEqual(self.status("codex"), "installed")
        code, out, _err = self.call(an._install_and_report, "codex", None, True)
        self.assertIn("already installed", out)

    def test_doctor_does_not_call_a_missing_binary_installed(self):
        self.write(an.codex_config_path(), _codex_table(self.old))
        checks = self.hook_checks()
        self.assertEqual([c["status"] for c in checks], [an.WARN])
        self.assertIn("update needed for codex", checks[0]["detail"])
        self.assertEqual(checks[0]["fix"], "agentbell hooks install codex")

    def test_a_marked_block_with_a_gone_path_needs_an_update_too(self):
        path = an.codex_config_path()
        self.write(path, an.codex_hooks_block().replace(an._hook_prefix("codex"), self.old))
        self.assertEqual(self.status("codex"), "update needed")
        self.assertTrue(an.install_hooks("codex")["changed"])
        self.assertEqual(self.status("codex"), "installed")
        # without its end marker install refuses the file: no endless "update needed"
        self.write(path, an.codex_hooks_block().replace(an._hook_prefix("codex"), self.old)
                   .replace(an.TOML_END, ""))
        self.assertEqual(self.status("codex"), "installed")

    def test_a_command_that_still_runs_is_left_as_the_user_wrote_it(self):
        for prefix in ("agentbell", self.binary):
            original = _codex_table(prefix)
            self.write(an.codex_config_path(), original)
            self.assertEqual(self.status("codex"), "installed", prefix)
            self.assertFalse(an.install_hooks("codex")["changed"])
            self.assertEqual(self.read(an.codex_config_path()).decode("utf-8"), original)

    def test_the_users_own_agentbell_hook_under_another_event_is_not_touched(self):
        path = an.codex_config_path()
        own = ("[[hooks.PreToolUse]]\n[[hooks.PreToolUse.hooks]]\ntype = \"command\"\n"
               f"command = \"{self.old} hook input_required --agent codex\"\n")
        original = _codex_table(self.old) + own
        self.write(path, original)
        an.install_hooks("codex")
        self.assertEqual(self.read(path).decode("utf-8"),
                         _codex_table(an._hook_prefix("codex")) + own)


@unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
class TestSymlinkedAgentsMd(_Home):
    """CFG-2: AGENTS.md -> CLAUDE.md read as 'already gone' while the block stayed."""

    def setUp(self):
        super().setUp()
        self.call(an._install_and_report, "aider", self.project, True)
        os.rename(os.path.join(self.project, "AGENTS.md"),
                  os.path.join(self.project, "CLAUDE.md"))
        os.symlink("CLAUDE.md", os.path.join(self.project, "AGENTS.md"))
        self.target = os.path.join(self.project, "CLAUDE.md")
        self.before = self.read(self.target)

    def test_hooks_uninstall_fails_and_says_where_the_block_is(self):
        code, out, _err = self.call(an.cmd_hooks,
                                    base._Args(sub="uninstall", agent=["aider"], project=self.project))
        self.assertEqual(code, 1)
        self.assertIn("AGENTS.md is a symlink", out)
        self.assertIn(os.path.realpath(self.target), out)
        self.assertEqual(self.read(self.target), self.before)
        self.assertTrue(os.path.islink(os.path.join(self.project, "AGENTS.md")))

    def test_the_purge_entries_fail_instead_of_already_gone(self):
        entries = [e for e in an.purge_report(self.project)["entries"] if "AGENTS.md" in e["label"]]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    entry["apply"]()
        self.assertEqual(self.read(self.target), self.before)

    def test_a_link_to_a_file_without_a_block_is_nothing_to_remove(self):
        self.write(self.target, "# Project rules\n")
        code, out, _err = self.call(an.cmd_hooks,
                                    base._Args(sub="uninstall", agent=["aider"], project=self.project))
        self.assertEqual((code, out), (0, "nothing to remove for aider\n"))


class TestCodexFeatureFlag(_Home):
    """CFG-3: install deleted the flag from every table, uninstall never put it back."""

    def test_a_flag_under_a_profile_survives_install_and_uninstall(self):
        original = ('model = "o3"\n\n[profiles.fast]\nmodel = "o4-mini"\n'
                    "features.hooks = true  # I want hooks in this profile\n")
        path = an.codex_config_path()
        self.write(path, original)
        an.install_hooks("codex")
        self.assertIn("features.hooks = true  # I want hooks in this profile\n",
                      self.read(path).decode("utf-8"))
        self.assertFalse(an.install_hooks("codex")["changed"])
        an.install_hooks("codex", add=False)
        self.assertEqual(self.read(path).decode("utf-8"), original)

    def test_the_legacy_flag_above_our_block_still_moves_to_the_top(self):
        path = an.codex_config_path()
        self.write(path, 'model = "gpt-5"\n[model_providers.oss]\nname = "x"\n\n'
                         "features.hooks = true\n\n" + an.codex_hooks_block())
        self.assertTrue(an.install_hooks("codex")["changed"])
        text = self.read(path).decode("utf-8")
        self.assertEqual(text.count("features.hooks = true"), 1)
        self.assertLess(text.index("features.hooks = true  " + an.CODEX_FLAG_MARKER),
                        text.index("[model_providers.oss]"))


class TestUnmarkedHookRemovalKeepsComments(_Home):
    """CFG-4: the comment that introduces the next table went with our hook."""

    def test_the_next_tables_comment_stays(self):
        path = an.codex_config_path()
        self.write(path, _codex_table(an._hook_prefix("codex")))
        self.assertTrue(an.uninstall_codex_hooks())
        self.assertEqual(self.read(path).decode("utf-8"),
                         'model = "o3"\n\n# ---- trusted projects ----\n'
                         '[projects."/home/u/x"]\ntrust_level = "trusted"\n')

    def test_a_comment_above_the_users_own_hook_stays(self):
        command = an.toml_string(an._hook_prefix("codex")
                                 + " hook run_completed --agent codex --min-duration 60")
        path = an.codex_config_path()
        self.write(path, "[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
                         f'type = "command"\ncommand = {command}\nasync = true\n\n'
                         "# my own stop hook below\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
                         'type = "command"\ncommand = "say done"\n')
        self.assertTrue(an.uninstall_codex_hooks())
        self.assertEqual(self.read(path).decode("utf-8"),
                         "# my own stop hook below\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
                         'type = "command"\ncommand = "say done"\n')


class TestNonUtf8Toml(_Home):
    """CFG-5/INT-2: a legacy-encoded config.toml gave a UnicodeDecodeError traceback."""

    DATA = b'# caf\xe9\nmodel = "o3"\n'

    def test_hooks_install_and_uninstall_refuse_in_one_line(self):
        for agent, path in (("codex", an.codex_config_path()), ("kimi", an.kimi_config_path())):
            self.write(path, self.DATA)
            for add in (True, False):
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    self.assertIs(an._install_and_report(agent, None, add), False)
                err = err.getvalue()
                self.assertIn(f"hooks for {agent} not changed", err)
                self.assertIn("is not UTF-8 text", err)
                self.assertEqual(self.read(path), self.DATA)
            code, _out, _err = self.call(an.cmd_hooks, base._Args(sub="install", agent=[agent]))
            self.assertEqual(code, 1)

    def test_mcp_add_codex_reports_failed_and_exits_1(self):
        path = an.codex_config_path()
        self.write(path, self.DATA)
        code, out, err = self.call(an.cmd_mcp, base._Args(sub="add", client=["codex"],
                                                          print_only=False, project=None))
        self.assertEqual(code, 1)
        self.assertIn("FAILED:", out)
        self.assertIn("is not UTF-8 text", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.read(path), self.DATA)


class TestCodexMcpUninstallPlan(_Home):
    """CFG-7/INT-3: the plan used a substring, doctor and the removal parse the table."""

    def codex_entries(self):
        return [e for e in an._mcp_entries(self.project) if "Codex" in e["label"]]

    def test_a_quoted_header_is_listed_and_removed(self):
        path = an.codex_config_path()
        self.write(path, 'model = "o3"\r\n\r\n[mcp_servers."agentbell"]\r\n'
                         'command = "/usr/bin/python3"\r\nargs = ["/x/agentbell.py", "mcp"]\r\n'
                         "\r\n# keep me\r\n")
        entries = self.codex_entries()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["apply"]())
        self.assertEqual(self.read(path), b'model = "o3"\r\n\r\n# keep me\r\n')
        self.assertEqual(self.codex_entries(), [])

    def test_a_comment_that_names_the_table_is_not_an_entry(self):
        self.write(an.codex_config_path(),
                   'model = "o3"\n# [mcp_servers.agentbell] was removed by me\n')
        self.assertEqual(self.codex_entries(), [])

    def test_an_undecodable_config_is_listed_and_its_removal_says_why(self):
        path = an.codex_config_path()
        data = b'# caf\xe9\n[mcp_servers.agentbell]\ncommand = "agentbell"\nargs = ["mcp"]\n'
        self.write(path, data)
        entries = self.codex_entries()
        self.assertEqual(len(entries), 1)
        with self.assertRaisesRegex(RuntimeError, "not UTF-8"):
            entries[0]["apply"]()
        self.assertEqual(self.read(path), data)


class TestIntegrateMcpEntry(_Home):
    """INT-1: the canonical MCP entry named the bare agentbell.py, which cannot start."""

    def test_a_checkout_gets_the_interpreter_like_mcp_add(self):
        script = os.path.join(self.tmp, "agentbell.py")
        self.write(script, "")
        with unittest.mock.patch.object(an, "agentbell_binary", lambda: script):
            manifest = an.integration_manifest(agent="myagent")
            guide = an.integration_guide(manifest)
        entry = manifest["mcp"]["canonical_config"]["mcpServers"]["agentbell"]
        self.assertEqual(entry, {"command": sys.executable, "args": [script, "mcp"]})
        self.assertIn(an.json.dumps(manifest["mcp"]["canonical_config"]), guide)

    def test_a_launcher_is_given_as_is(self):
        entry = an.integration_manifest()["mcp"]["canonical_config"]["mcpServers"]["agentbell"]
        self.assertEqual(entry, {"command": self.binary, "args": ["mcp"]})


class TestJsonHooksTestsIgnoreTheCwd(unittest.TestCase):
    """CFG-6: a Cursor rule in the developer's checkout failed a json-hooks test."""

    def test_the_next_steps_test_passes_next_to_a_cursor_rule(self):
        import test_audit3_json_hooks
        cwd = tempfile.mkdtemp(prefix="agentbell-audit4-cwd-")
        self.addCleanup(shutil.rmtree, cwd, True)
        rule = os.path.join(cwd, ".cursor", "rules", "agentbell.mdc")
        os.makedirs(os.path.dirname(rule))
        with open(rule, "w", encoding="utf-8") as fh:
            fh.write(an.CURSOR_RULE)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd)
        suite = unittest.defaultTestLoader.loadTestsFromName(
            "TestInstallReport.test_next_steps_do_not_promise_every_repo_for_rule_files",
            test_audit3_json_hooks)
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual((result.testsRun, result.failures, result.errors), (1, [], []))
        self.assertTrue(os.path.exists(rule))


if __name__ == "__main__":
    unittest.main()
