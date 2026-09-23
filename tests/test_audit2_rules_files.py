"""Audit 2026-09-22, cluster rules-files: shared rule files and the OpenCode plugin.

Run with: python3 -m unittest discover -s tests
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

import test_agentbell as base
import agentbell as an


# cp1252 text with CRLF line ends, as Notepad or an older Windows tool saves
# it: ü, ß, an en dash (0x96) and é are not UTF-8.
CP1252_CRLF = b"# Projekt\r\n\r\nGr\xfc\xdfe aus M\xfcnchen \x96 caf\xe9\r\n"


def _block(agent):
    return f"{an.BLOCK_START}\n{an._instructions_text(agent).rstrip()}\n{an.BLOCK_END}"


class _ProjectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.old_home = base._set_home(self.home)
        self.old_argv0 = sys.argv[0]
        sys.argv[0] = base._fake_launcher(self.tmp)
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project)

    def tearDown(self):
        base._restore_home(self.old_home)
        sys.argv[0] = self.old_argv0
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def _path(self, name="AGENTS.md"):
        return os.path.join(self.project, name)

    def _write(self, data, name="AGENTS.md"):
        with open(self._path(name), "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def _read(self, name="AGENTS.md"):
        with open(self._path(name), "rb") as fh:
            return fh.read()

    def _status(self):
        return {agent: status for agent, status, _, _ in an.hooks_status(project=self.project)}

    def _hooks_cli(self, sub, agent):
        out, err = io.StringIO(), io.StringIO()
        self.exit_code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                an.cmd_hooks(base._Args(sub=sub, agent=[agent], project=self.project))
            except SystemExit as exc:   # a refused install exits 1 (IW-8)
                self.exit_code = exc.code
        return out.getvalue(), err.getvalue()


class TestNonUtf8AgentsMd(_ProjectCase):
    """M6: a cp1252/latin-1 AGENTS.md crashed status, doctor and verify."""

    def test_status_verify_and_doctor_read_a_cp1252_agents_md(self):
        self._write(CP1252_CRLF)
        self.assertEqual(self._status()["aider"], "not installed")
        cfg = an.Config(an.default_config(), path=os.path.join(self.tmp, "config.json"))
        report = an.verify_report(cfg, project=self.project)
        self.assertEqual(report["repair_notices"], [])
        cwd = os.getcwd()
        os.chdir(self.project)      # doctor checks the rule files of the cwd
        try:
            names = [check["name"] for check in an.doctor_checks(cfg)]
        finally:
            os.chdir(cwd)
        self.assertIn("config", names)

    def test_install_and_uninstall_keep_every_foreign_byte(self):
        self._write(CP1252_CRLF)
        result = an.install_hooks("aider", project=self.project)
        self.assertTrue(result["changed"])
        after = self._read()
        self.assertTrue(after.startswith(CP1252_CRLF), after[:60])
        self.assertIn(b"--agent aider", after)
        self.assertEqual(self._status()["aider"], "installed")
        self.assertFalse(an.install_hooks("aider", project=self.project)["changed"])
        self.assertTrue(an.install_hooks("aider", project=self.project, add=False)["changed"])
        self.assertEqual(self._read(), CP1252_CRLF)

    def test_stale_block_repair_keeps_foreign_bytes(self):
        stale = (an.BLOCK_START + "\r\n- run `agentbell hook run_completed --agent aider`\r\n"
                 + an.BLOCK_END + "\r\n").encode("ascii")
        self._write(CP1252_CRLF + stale + b"Tsch\xfc\xdf\r\n")
        self.assertEqual(self._status()["aider"], "update needed")
        self.assertTrue(an.install_hooks("aider", project=self.project)["changed"])
        after = self._read()
        self.assertTrue(after.startswith(CP1252_CRLF))
        self.assertTrue(after.endswith(an.BLOCK_END.encode() + b"\r\nTsch\xfc\xdf\r\n"))
        self.assertEqual(self._status()["aider"], "installed")

    def test_opencode_install_and_purge_survive_a_cp1252_agents_md(self):
        # opencode install removes its legacy AGENTS.md block on every run
        self._write(CP1252_CRLF)
        an.install_hooks("opencode", project=self.project)
        self.assertEqual(self._read(), CP1252_CRLF)
        an.install_hooks("aider", project=self.project)
        self.assertTrue(an._install_block_file(self._path(), None, add=False))
        self.assertEqual(self._read(), CP1252_CRLF)

    def test_utf16_agents_md_is_refused_with_a_note(self):
        # Windows PowerShell 5.1 `echo ... > AGENTS.md` writes UTF-16LE. A
        # UTF-8 block appended to it would leave a file no editor reads right.
        original = "﻿# rules\r\n".encode("utf-16-le")
        self._write(original)
        self.assertEqual(self._status()["aider"], "not installed")
        out, _err = self._hooks_cli("install", "aider")
        self.assertEqual(self._read(), original)
        self.assertIn("hooks for aider NOT installed", out)
        self.assertIn("UTF-16", out)
        self.assertNotIn("already installed", out)


class TestRuleFileLineEndings(_ProjectCase):
    """L-aider-crlf: rewriting a shared rule file kept only LF line ends."""

    def _assert_crlf_only(self, data):
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), data)

    def test_stale_block_repair_keeps_crlf(self):
        text = ("# Team rules\r\n\r\n- be nice\r\n\r\n" + an.BLOCK_START + "\r\n"
                "- run `agentbell hook run_completed --agent aider`\r\n"
                + an.BLOCK_END + "\r\n\r\n## More\r\n- after\r\n")
        self._write(text)
        self.assertTrue(an.install_hooks("aider", project=self.project)["changed"])
        after = self._read()
        self._assert_crlf_only(after)
        self.assertTrue(after.startswith(b"# Team rules\r\n\r\n- be nice\r\n\r\n"))
        self.assertTrue(after.endswith(b"\r\n\r\n## More\r\n- after\r\n"))
        self.assertEqual(self._status()["aider"], "installed")
        self.assertFalse(an.install_hooks("aider", project=self.project)["changed"])

    def test_append_and_remove_keep_crlf(self):
        original = b"# Team rules\r\n\r\n- be nice\r\n"
        self._write(original)
        self.assertTrue(an.install_hooks("aider", project=self.project)["changed"])
        self._assert_crlf_only(self._read())
        self.assertEqual(self._status()["aider"], "installed")
        self.assertTrue(an.install_hooks("aider", project=self.project, add=False)["changed"])
        self.assertEqual(self._read(), original)

    def test_lf_file_stays_lf(self):
        original = b"# rules\n\n- keep\n"
        self._write(original, ".rules")
        self.assertTrue(an.install_hooks("zed", project=self.project)["changed"])
        after = self._read(".rules")
        self.assertNotIn(b"\r", after)
        self.assertTrue(an.install_hooks("zed", project=self.project, add=False)["changed"])
        self.assertEqual(self._read(".rules"), original)

    def test_mixed_block_from_an_older_install_is_healed_to_the_file_ending(self):
        # v1.6.3 on Linux appended an LF block to a CRLF file
        self._write("# Team rules\r\n\r\n" + _block("aider") + "\n")
        self.assertTrue(an.install_hooks("aider", project=self.project)["changed"])
        after = self._read()
        self.assertTrue(after.startswith(b"# Team rules\r\n\r\n"))
        self.assertEqual(after.count(b"\n") - after.count(b"\r\n"), 1)   # the user's last line end
        self.assertFalse(an.install_hooks("aider", project=self.project)["changed"])


class TestAiderInstallMessage(_ProjectCase):
    """L-aider-already: a no-op Aider install said "already installed" while
    `hooks status` said "not installed"."""

    CASES = {
        "a doc mentions the start marker": "# Docs\n\nOur marker is `" + an.BLOCK_START + "`.\n",
        "the legacy OpenCode block": _block("opencode") + "\n",
        "the Aider block next to a second block": _block("aider") + "\n\n" + _block("opencode") + "\n",
    }

    def test_install_that_changes_nothing_says_not_installed_and_why(self):
        for name, text in self.CASES.items():
            with self.subTest(name):
                self._write(text)
                out, _err = self._hooks_cli("install", "aider")
                self.assertEqual(self._read(), text.encode())
                self.assertEqual(self._status()["aider"], "not installed")
                self.assertNotIn("already installed", out)
                self.assertIn("hooks for aider NOT installed", out)
                self.assertIn("note: aider:", out)
                self.assertIn("left it unchanged", out)

    def test_current_block_still_reads_already_installed(self):
        an.install_hooks("aider", project=self.project)
        out, _err = self._hooks_cli("install", "aider")
        self.assertIn("hooks for aider already installed (nothing changed)", out)
        self.assertNotIn("note:", out)

    @unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
    def test_symlinked_agents_md_is_not_reported_installed(self):
        target = os.path.join(self.tmp, "elsewhere.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("# shared\n")
        os.symlink(target, self._path())
        out, err = self._hooks_cli("install", "aider")
        self.assertIn("hooks for aider NOT installed", out)
        self.assertIn("is a symlink", err)
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# shared\n")


class TestClinerulesFile(_ProjectCase):
    """L-clinerules: older Cline used one `.clinerules` file; install crashed
    with FileExistsError creating the `.clinerules/` folder."""

    def test_block_goes_into_the_file_and_comes_out_again(self):
        original = b"- Always write tests first.\r\n"
        self._write(original, ".clinerules")
        result = an.install_hooks("cline", project=self.project)
        self.assertTrue(result["changed"])
        self.assertEqual(result["path"], self._path(".clinerules"))
        after = self._read(".clinerules")
        self.assertTrue(after.startswith(original))
        self.assertIn(b"--agent cline", after)
        rows = {agent: (status, path) for agent, status, path, _ in
                an.hooks_status(project=self.project)}
        self.assertEqual(rows["cline"], ("installed", self._path(".clinerules")))
        self.assertFalse(an.install_hooks("cline", project=self.project)["changed"])
        entries = [e for e in an._project_entries(self.project) if e["label"].startswith("cline")]
        self.assertEqual(len(entries), 1)
        self.assertIn(".clinerules", entries[0]["label"])
        self.assertTrue(an.install_hooks("cline", project=self.project, add=False)["changed"])
        self.assertEqual(self._read(".clinerules"), original)
        self.assertEqual(self._status()["cline"], "not installed")

    def test_folder_layout_is_unchanged(self):
        os.makedirs(self._path(".clinerules"))
        result = an.install_hooks("cline", project=self.project)
        self.assertEqual(result["path"], os.path.join(self.project, ".clinerules/agentbell.md"))
        self.assertTrue(os.path.isfile(result["path"]))

    @unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
    def test_a_folder_that_cannot_be_created_is_a_note_not_a_crash(self):
        os.symlink(os.path.join(self.tmp, "missing"), self._path(".clinerules"))
        result = an.install_hooks("cline", project=self.project)
        self.assertFalse(result["changed"])
        self.assertTrue(any("cannot create" in note for note in result["notes"]),
                        result["notes"])
        out, _err = self._hooks_cli("install", "cline")
        self.assertIn("hooks for cline NOT installed", out)


class TestInitHookLoop(_ProjectCase):
    """`init` installs hooks for detected agents: the same crash and message."""

    def test_init_fills_a_clinerules_file_and_reports_a_skipped_aider(self):
        self._write("- Always write tests first.\n", ".clinerules")
        self._write(_block("opencode") + "\n")         # Aider must leave this alone
        args = base._init_args(non_interactive=False, no_hooks=False, no_test=True,
                               server="http://127.0.0.1:9",
                               topic="rules-files-topic-0123456789abcdef")
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.project)                         # init installs into the cwd
        try:
            with unittest.mock.patch.dict(os.environ, {
                    "AGENTBELL_CONFIG": os.path.join(self.tmp, "config.json")}), \
                    unittest.mock.patch.object(an.sys.stdin, "isatty", return_value=True), \
                    unittest.mock.patch("builtins.input", return_value=""), \
                    unittest.mock.patch.object(an, "find_agents",
                                               return_value=["aider", "cline"]), \
                    contextlib.redirect_stdout(out):
                an.cmd_init(args)
        finally:
            os.chdir(cwd)
        text = out.getvalue()
        self.assertIn(f"installed hooks for cline: {os.path.join('.', '.clinerules')}", text)
        self.assertIn(b"--agent cline", self._read(".clinerules"))
        self.assertIn("hooks for aider NOT installed (nothing changed)", text)
        self.assertIn("note: aider: AGENTS.md already holds an agentbell block", text)
        self.assertNotIn("already installed", text)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TestOpenCodeTurnDuration(unittest.TestCase):
    """L-opencode-duration: the plugin's turn start could come from the
    previous turn, so short turns reported (and pushed) the user's idle time.

    Replays events through the real plugin under node with a fake clock:
    `{"at": ms}` sets Date.now(), no real sleeps.
    """

    HARNESS = r"""
import { pathToFileURL } from "node:url";
let clock = 1_000_000;
Date.now = () => clock;
const { AgentBell } = await import(pathToFileURL(process.argv[2]).href);
const calls = [];
const $ = (strings, ...values) => {
  const parts = [];
  strings.forEach((s, i) => { if (s.trim()) parts.push(s.trim()); if (i < values.length) {
    const v = values[i]; Array.isArray(v) ? parts.push(...v) : parts.push(String(v)); } });
  calls.push(parts);
  const p = Promise.resolve({});
  p.quiet = () => p; p.nothrow = () => p;
  return p;
};
const hooks = await AgentBell({ $ });
for (const step of JSON.parse(process.argv[3])) {
  if (step.at !== undefined) { clock = 1_000_000 + step.at; continue; }
  await hooks.event({ event: step });
}
console.log(JSON.stringify(calls));
"""

    def _run(self, script):
        tmp = tempfile.mkdtemp(prefix="agentbell-plugin-")
        self.addCleanup(shutil.rmtree, tmp, True)
        plugin = os.path.join(tmp, "agentbell.mjs")
        with open(plugin, "w", encoding="utf-8") as fh:
            fh.write(an.OPENCODE_PLUGIN.replace("__AGENTBELL_BIN__", json.dumps("agentbell"))
                     .replace("__MIN_DURATION__", str(an.HOOK_MIN_DURATION)))
        harness = os.path.join(tmp, "harness.mjs")
        with open(harness, "w", encoding="utf-8") as fh:
            fh.write(self.HARNESS)
        out = subprocess.run(["node", harness, plugin, json.dumps(script)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return [c[1:] for c in json.loads(out.stdout.strip().splitlines()[-1])]

    @staticmethod
    def _prompt(sid, message_id, created_ms):
        return {"type": "message.updated", "properties": {"info": {
            "id": message_id, "role": "user", "sessionID": sid,
            "time": {"created": 1_000_000 + created_ms}}}}

    @staticmethod
    def _idle(sid):
        return {"type": "session.idle", "properties": {"sessionID": sid}}

    @staticmethod
    def _duration(call):
        return call[call.index("--duration") + 1] if "--duration" in call else None

    def test_prompt_resent_after_the_turn_does_not_start_the_next_one(self):
        # OpenCode's diff summary re-sends the finished turn's prompt after
        # session.idle. The next turn was then timed from that re-send and
        # included every minute the user spent reading the answer.
        calls = self._run([
            self._prompt("s", "m1", 0),
            {"at": 5_000}, self._idle("s"),
            {"at": 6_000}, self._prompt("s", "m1", 0),           # re-sent, old prompt
            {"at": 606_000}, self._prompt("s", "m2", 606_000),   # ten minutes later
            {"at": 611_000}, self._idle("s"),
        ])
        self.assertEqual([self._duration(c) for c in calls], ["5", "5"])

    def test_idle_swallowed_by_the_dedupe_still_ends_its_turn(self):
        calls = self._run([
            self._prompt("s", "a", 0),
            {"at": 100_000}, self._idle("s"),
            {"at": 102_000}, self._prompt("s", "b", 102_000),
            {"at": 105_000}, self._idle("s"),                    # within 10 s: no push
            {"at": 705_000}, self._prompt("s", "c", 705_000),
            {"at": 710_000}, self._idle("s"),
        ])
        self.assertEqual([self._duration(c) for c in calls], ["100", "5"])

    def test_prompt_without_a_timestamp_still_starts_the_turn(self):
        calls = self._run([
            {"type": "message.updated", "properties": {"info": {"role": "user", "sessionID": "s"}}},
            {"at": 70_000}, self._idle("s"),
        ])
        self.assertEqual([self._duration(c) for c in calls], ["70"])

    def test_queued_prompt_does_not_restart_a_running_turn(self):
        calls = self._run([
            self._prompt("s", "m1", 0),
            {"at": 30_000}, self._prompt("s", "m2", 30_000),     # typed while busy
            {"at": 90_000}, self._idle("s"),
        ])
        self.assertEqual([self._duration(c) for c in calls], ["90"])


if __name__ == "__main__":
    unittest.main()
