"""Audit 2026-09-22, cluster toml-mcp: Codex/Kimi TOML writers and the MCP server.

M12  removing [mcp_servers.agentbell] must leave everything else byte-identical
M2   hooks install must never turn a config with inline hooks into invalid TOML
L    `mcp add codex` repairs a stale command path
M7   the MCP server survives batches, junk and a broken config.json
L    JSON-RPC notifications get no response
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

an = base.an

try:
    import tomllib
except ImportError:      # 3.9 / 3.10: structure is still checked, parsing is skipped
    tomllib = None


class _Home(unittest.TestCase):
    """A throwaway HOME (and USERPROFILE) with a fake installed launcher."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.old_home = base._set_home(self.home)
        self.old_kimi = os.environ.pop("KIMI_CODE_HOME", None)
        self.old_argv0 = sys.argv[0]
        sys.argv[0] = base._fake_launcher(self.tmp)

    def tearDown(self):
        base._restore_home(self.old_home)
        if self.old_kimi is not None:
            os.environ["KIMI_CODE_HOME"] = self.old_kimi
        sys.argv[0] = self.old_argv0
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data.encode("utf-8"))

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8")

    def assertToml(self, text):
        if tomllib is not None:
            return tomllib.loads(text)
        return None


OURS = '[mcp_servers.agentbell]\ncommand = "/old/agentbell"\nargs = ["mcp"]\n'
AGENT_OPS = ("# agent-ops:start (managed by agent-ops, do not edit)\n"
             "# trust = \"sha256:abc123\"\n"
             "# agent-ops:end\n")


class TestCodexMcpRemovalKeepsForeignText(_Home):
    """M12: only [mcp_servers.agentbell] and its sub-tables may go."""

    def remove(self, before):
        path = an.codex_config_path()
        self.write(path, before)
        changed = an._remove_codex_mcp_block()
        return changed, self.read(path)

    def test_agent_ops_marker_after_our_table_survives(self):
        head = 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n\n'
        tail = AGENT_OPS + '\n[tui]\ntheme = "dark"\n'
        changed, after = self.remove(head + OURS + "\n" + tail)
        self.assertTrue(changed)
        self.assertEqual(after, head + tail)
        data = self.assertToml(after)
        if data is not None:
            self.assertEqual(data["mcp_servers"], {"other": {"command": "x"}})

    def test_comment_glued_to_our_last_key_survives(self):
        head = 'model = "gpt-5"\n'
        tail = AGENT_OPS + '[tui]\ntheme = "dark"\n'
        changed, after = self.remove(head + OURS + tail)
        self.assertTrue(changed)
        self.assertEqual(after, head + tail)

    def test_sub_tables_go_even_when_they_are_not_adjacent(self):
        env = '[mcp_servers.agentbell.env]\nNTFY = "1"\n'
        other = '[mcp_servers.other]\ncommand = "x"\n'
        before = OURS + AGENT_OPS + other + env + "# trailing note\n"
        changed, after = self.remove(before)
        self.assertTrue(changed)
        self.assertEqual(after, AGENT_OPS + other + "# trailing note\n")
        data = self.assertToml(after)
        if data is not None:
            self.assertNotIn("agentbell", data["mcp_servers"])

    def test_crlf_file_stays_crlf_and_byte_identical(self):
        head = 'model = "gpt-5"\n\n'
        tail = AGENT_OPS + '\n[tui]\ntheme = "dark"\n'
        crlf = lambda text: text.replace("\n", "\r\n")  # noqa: E731
        changed, after = self.remove(crlf(head + OURS + "\n" + tail))
        self.assertTrue(changed)
        self.assertEqual(after, crlf(head + tail))

    def test_our_table_last_with_and_without_trailing_newline(self):
        head = 'model = "gpt-5"\n' + AGENT_OPS
        for ours in (OURS, OURS.rstrip("\n")):
            with self.subTest(trailing_newline=ours.endswith("\n")):
                changed, after = self.remove(head + ours)
                self.assertTrue(changed)
                self.assertEqual(after, head)

    def test_header_comment_and_interleaved_comment_are_ours(self):
        ours = ('[mcp_servers.agentbell]  # phone notifications\n'
                'command = "/old/agentbell"\n'
                '# the stdio server\n'
                'args = [\n  "mcp",\n]\n')
        changed, after = self.remove('model = "gpt-5"\n\n' + ours + "\n" + AGENT_OPS)
        self.assertTrue(changed)
        self.assertEqual(after, 'model = "gpt-5"\n\n' + AGENT_OPS)

    def test_mention_in_a_comment_only_is_not_a_table(self):
        before = '# [mcp_servers.agentbell] was removed by hand\nmodel = "gpt-5"\n'
        changed, after = self.remove(before)
        self.assertFalse(changed)
        self.assertEqual(after, before)

    def test_add_then_remove_restores_the_original_bytes(self):
        path = an.codex_config_path()
        original = 'model = "gpt-5"\n\n[tui]\ntheme = "dark"\n' + AGENT_OPS
        self.write(path, original)
        an._mcp_add_codex("/opt/bin/agentbell")
        self.assertIn("[mcp_servers.agentbell]", self.read(path))
        self.assertTrue(an._remove_codex_mcp_block())
        self.assertEqual(self.read(path), original)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_mode_is_kept(self):
        path = an.codex_config_path()
        self.write(path, 'model = "gpt-5"\n' + OURS)
        os.chmod(path, 0o600)
        self.assertTrue(an._remove_codex_mcp_block())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_uninstall_entry_uses_the_same_safe_removal(self):
        path = an.codex_config_path()
        head = 'model = "gpt-5"\n\n'
        tail = AGENT_OPS + '\n[tui]\ntheme = "dark"\n'
        self.write(path, head + OURS + "\n" + tail)
        entries = [e for e in an._mcp_entries(self.tmp) if "Codex" in e["label"]]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["apply"]())
        self.assertEqual(self.read(path), head + tail)


INLINE_CODEX = {
    "root inline hooks table":
        'model = "gpt-5"\nhooks = { Stop = [ { hooks = [ { type = "command", command = "lint.sh" } ] } ] }\n',
    "[hooks] with an inline Stop array":
        '[hooks]\nStop = [ { hooks = [ { type = "command", command = "lint.sh" } ] } ]\n',
    "[hooks] with an inline UserPromptSubmit array":
        '[hooks]\nUserPromptSubmit = [ { hooks = [ { type = "command", command = "x" } ] } ]\n',
    "multi-line inline array under [hooks]":
        '[hooks]\nStop = [\n  { hooks = [ { type = "command", command = "lint.sh" } ] },\n]\n',
    "root dotted hooks.Stop":
        'hooks.Stop = [ { hooks = [ { type = "command", command = "lint.sh" } ] } ]\n'
        '[tui]\ntheme = "dark"\n',
    "quoted dotted key":
        '"hooks" . "Stop" = []\n',
    "plain [hooks.Stop] table":
        '[hooks.Stop]\nx = 1\n',
    "plain [hooks.UserPromptSubmit] table":
        '[hooks.UserPromptSubmit]\nx = 1\n',
}

COMPATIBLE_CODEX = {
    "[hooks.state] written by Codex": '[hooks.state]\ntrust = "abc"\n',
    "[hooks] with another event": '[hooks]\nSessionStart = [ { hooks = [ { type = "command", command = "x" } ] } ]\n',
    "user [[hooks.Stop]] tables": ('[[hooks.Stop]]\nmatcher = "x"\n[[hooks.Stop.hooks]]\n'
                                   'type = "command"\ncommand = "lint.sh"\n'),
    "root dotted hooks.Other": 'hooks.Other = 1\n[tui]\ntheme = "dark"\n',
    "hooks key in another table": '[profiles.work]\nhooks = { Stop = [] }\n',
    "hooks key inside a string": 'note = "hooks = {}"\n',
}

INLINE_KIMI = {
    "root inline hooks array": 'hooks = [ { event = "Stop", command = "lint.sh" } ]\n[models]\nmodel = "k"\n',
    "plain [hooks] table": '[hooks]\nx = 1\n',
    "[hooks.state] sub-table": '[hooks.state]\nx = 1\n',
    "root dotted hooks.x": 'hooks.x = 1\n',
}

COMPATIBLE_KIMI = {
    "user [[hooks]] tables": '[[hooks]]\nevent = "Stop"\ncommand = "lint.sh"\n',
    "user [[hooks]] with a sub-table": ('[[hooks]]\nevent = "Stop"\ncommand = "lint.sh"\n'
                                        '[hooks.extra]\nx = 1\n'),
}


class TestInlineHooksAreNotBroken(_Home):
    """M2: appending [[hooks...]] next to inline hooks is a TOMLDecodeError."""

    def check_refused(self, agent, path, fixtures):
        for label, text in fixtures.items():
            with self.subTest(label):
                self.assertToml(text)             # the fixture itself is valid
                self.write(path, text)
                result = an.install_hooks(agent)
                self.assertFalse(result["changed"])
                self.assertTrue(any("invalid TOML" in note and "nothing was written" in note
                                    for note in result["notes"]), result["notes"])
                self.assertEqual(self.read(path), text)

    def check_installed(self, agent, path, fixtures):
        for label, text in fixtures.items():
            with self.subTest(label):
                self.write(path, text)
                result = an.install_hooks(agent)
                self.assertTrue(result["changed"], result)
                self.assertToml(self.read(path))
                self.assertTrue(an.install_hooks(agent, add=False)["changed"])
                self.assertToml(self.read(path))

    def test_codex_refuses_inline_hooks(self):
        self.check_refused("codex", an.codex_config_path(), INLINE_CODEX)

    def test_the_note_names_what_clashes(self):
        for agent, path, text, clash in (
                ("codex", an.codex_config_path(), INLINE_CODEX["root inline hooks table"],
                 "`hooks = ...`"),
                ("codex", an.codex_config_path(), INLINE_CODEX["[hooks] with an inline Stop array"],
                 "`hooks.Stop = ...`"),
                ("codex", an.codex_config_path(), INLINE_CODEX["plain [hooks.Stop] table"],
                 "`[hooks.Stop]`"),
                ("kimi", an.kimi_config_path(), INLINE_KIMI["[hooks.state] sub-table"],
                 "`[hooks.state]`")):
            with self.subTest(clash):
                self.write(path, text)
                notes = an.install_hooks(agent)["notes"]
                self.assertEqual(len(notes), 1)
                self.assertIn(clash, notes[0])
                self.assertIn(f"hooks install {agent}", notes[0])

    def test_codex_still_installs_next_to_compatible_hooks(self):
        self.check_installed("codex", an.codex_config_path(), COMPATIBLE_CODEX)

    def test_kimi_refuses_inline_hooks(self):
        self.check_refused("kimi", an.kimi_config_path(), INLINE_KIMI)

    def test_kimi_still_installs_next_to_array_tables(self):
        self.check_installed("kimi", an.kimi_config_path(), COMPATIBLE_KIMI)

    @unittest.skipIf(tomllib is None, "tomllib requires 3.11+")
    def test_every_fixture_really_breaks_without_the_guard(self):
        """The refused shapes are real conflicts, not false alarms."""
        for fixtures, block in ((INLINE_CODEX, an.codex_hooks_block()),
                                (INLINE_KIMI, an.kimi_hooks_block())):
            for label, text in fixtures.items():
                with self.subTest(label):
                    with self.assertRaises(tomllib.TOMLDecodeError):
                        tomllib.loads(text + "\n" + block)
        for fixtures, block in ((COMPATIBLE_CODEX, an.codex_hooks_block()),
                                (COMPATIBLE_KIMI, an.kimi_hooks_block())):
            for label, text in fixtures.items():
                with self.subTest(label):
                    tomllib.loads(text + "\n" + block)


class TestCodexMcpStaleCommand(_Home):
    """`mcp add codex` must repair a command path from an old install."""

    def test_stale_command_is_repaired_and_the_rest_kept(self):
        path = an.codex_config_path()
        before = ('model = "gpt-5"\r\n\r\n'
                  '[mcp_servers.agentbell]\r\n'
                  'command = "/home/u/src/agentbell/agentbell.py"  # checkout\r\n'
                  'args = ["mcp"]\r\n'
                  'startup_timeout_sec = 20\r\n'
                  '\r\n' + AGENT_OPS.replace("\n", "\r\n")
                  + '[mcp_servers.agentbell.env]\r\nX = "1"\r\n')
        self.write(path, before)
        status = an._mcp_add_codex("/home/u/.local/bin/agentbell")
        self.assertIn("updated", status)
        after = self.read(path)
        self.assertEqual(after, before.replace(
            'command = "/home/u/src/agentbell/agentbell.py"  # checkout',
            'command = "/home/u/.local/bin/agentbell"'))
        self.assertEqual(an._mcp_add_codex("/home/u/.local/bin/agentbell"), "already present")
        self.assertEqual(self.read(path), after)

    def test_windows_path_round_trips_without_a_rewrite(self):
        path = an.codex_config_path()
        binary = "C:\\Users\\u\\AppData\\Roaming\\Python\\Scripts\\agentbell.exe"
        self.write(path, 'model = "gpt-5"\n')
        self.assertTrue(an._mcp_add_codex(binary).startswith("written"))
        written = self.read(path)
        data = self.assertToml(written)
        if data is not None:
            self.assertEqual(data["mcp_servers"]["agentbell"]["command"], binary)
        self.assertEqual(an._mcp_add_codex(binary), "already present")
        self.assertEqual(self.read(path), written)

    def test_quoted_or_spaced_header_is_repaired_not_duplicated(self):
        path = an.codex_config_path()
        for header in ('[mcp_servers."agentbell"]', "[ mcp_servers . agentbell ]"):
            with self.subTest(header):
                self.write(path, header + '\ncommand = "/old/agentbell"\nargs = ["mcp"]\n')
                self.assertIn("updated", an._mcp_add_codex("/opt/new/agentbell"))
                after = self.read(path)
                self.assertEqual([line for line in after.splitlines() if line.startswith("[")],
                                 [header], after)
                data = self.assertToml(after)
                if data is not None:
                    self.assertEqual(data["mcp_servers"]["agentbell"]["command"],
                                     "/opt/new/agentbell")
                self.assertTrue(an._remove_codex_mcp_block())
                self.assertEqual(self.read(path), "")

    def test_mcp_add_configs_reports_the_repair(self):
        path = an.codex_config_path()
        self.write(path, OURS)
        rows = dict(an.mcp_add_configs("/opt/new/agentbell", clients=["codex"]))
        self.assertIn("updated", rows["codex+chatgpt"])
        data = self.assertToml(self.read(path))
        if data is not None:
            self.assertEqual(data["mcp_servers"]["agentbell"],
                             {"command": "/opt/new/agentbell", "args": ["mcp"]})


def run_mcp(*messages):
    """Feed raw lines to mcp_loop; return (parsed stdout lines, stderr text)."""
    payload = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages)
    stdin_buf = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")))
    stdout_buf = io.TextIOWrapper(io.BytesIO())
    stderr = io.StringIO()
    old_stdin, old_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin, sys.stdout = stdin_buf, stdout_buf
        with contextlib.redirect_stderr(stderr):
            an.mcp_loop()
        stdout_buf.flush()
        output = stdout_buf.buffer.getvalue().decode("utf-8")
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
    return [json.loads(line) for line in output.splitlines()], stderr.getvalue()


def ping(request_id):
    return {"jsonrpc": "2.0", "id": request_id, "method": "ping"}


class TestMcpServerSurvives(unittest.TestCase):
    """M7: one bad message is one error response, never the end of the server."""

    def test_batch_is_answered_as_a_batch(self):
        replies, _ = run_mcp(
            [ping(1), {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 7],
            ping(3))
        self.assertEqual(len(replies), 2)
        batch = replies[0]
        self.assertIsInstance(batch, list)
        self.assertEqual([r.get("id") for r in batch], [1, 2, None])
        self.assertEqual(batch[0]["result"], {})
        self.assertIn("tools", batch[1]["result"])
        self.assertEqual(batch[2]["error"]["code"], -32600)
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 3, "result": {}})

    def test_empty_batch_and_non_objects_are_invalid_requests(self):
        replies, _ = run_mcp([], 42, '"text"', "null", ping(9))
        self.assertEqual([r["error"]["code"] for r in replies[:4]], [-32600] * 4)
        self.assertTrue(all(r["id"] is None for r in replies[:4]))
        self.assertEqual(replies[4]["id"], 9)

    def test_unparsable_line_gets_a_parse_error(self):
        replies, _ = run_mcp("{not json", "[" * 100000, ping(2))
        self.assertEqual([r["error"]["code"] for r in replies[:2]], [-32700, -32700])
        self.assertIsNone(replies[0]["id"])
        self.assertEqual(replies[2]["id"], 2)

    def test_bad_params_and_method_are_protocol_errors(self):
        replies, _ = run_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["notify"]},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "notify", "arguments": "hi"}},
            {"jsonrpc": "2.0", "id": 3, "method": 5},
            ping(4))
        self.assertEqual([r["error"]["code"] for r in replies[:3]], [-32602, -32602, -32600])
        self.assertEqual(replies[3]["id"], 4)

    def test_broken_config_fails_the_call_not_the_server(self):
        tmp = tempfile.mkdtemp()
        try:
            broken = os.path.join(tmp, "config.json")
            with open(broken, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "notify", "arguments": {"message": "hi"}}}
            ask = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "ask_approval", "arguments": {"message": "ok?"}}}
            with unittest.mock.patch.object(an, "config_path", lambda: broken):
                replies, _ = run_mcp(call, ask, ping(3))
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(len(replies), 3)
        for reply in replies[:2]:
            self.assertTrue(reply["result"]["isError"])
            self.assertIn("cannot read config", reply["result"]["content"][0]["text"])
        self.assertEqual(replies[2], {"jsonrpc": "2.0", "id": 3, "result": {}})

    def test_config_of_the_wrong_shape_is_an_error_reply(self):
        tmp = tempfile.mkdtemp()
        try:
            wrong = os.path.join(tmp, "config.json")
            with open(wrong, "w", encoding="utf-8") as fh:
                fh.write("[1, 2]")
            call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "notify", "arguments": {"message": "hi"}}}
            with unittest.mock.patch.object(an, "config_path", lambda: wrong):
                replies, _ = run_mcp(call, ping(2))
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["id"], 1)
        self.assertTrue("error" in replies[0] or replies[0]["result"].get("isError"), replies[0])
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 2, "result": {}})


class TestMcpNotificationsGetNoResponse(unittest.TestCase):
    """JSON-RPC: a message without an id is a notification - never answered."""

    def test_notifications_are_silent(self):
        replies, err = run_mcp(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled",
             "params": {"requestId": 1, "reason": "user"}},
            {"jsonrpc": "2.0", "method": "notifications/unknown"},
            ping(5))
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 5, "result": {}}])
        self.assertEqual(err, "")

    def test_a_request_sent_as_a_notification_is_not_run_but_logged(self):
        calls = []
        with unittest.mock.patch.object(an, "mcp_tool_call",
                                        lambda *a: calls.append(a) or "sent"):
            replies, err = run_mcp(
                {"jsonrpc": "2.0", "method": "tools/call",
                 "params": {"name": "notify", "arguments": {"message": "hi"}}},
                {"jsonrpc": "2.0", "method": "ping"},
                ping(6))
        self.assertEqual(calls, [])
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 6, "result": {}}])
        self.assertIn("tools/call", err)
        self.assertIn("without an id", err)

    def test_all_notification_batch_gets_no_response(self):
        replies, _ = run_mcp([{"jsonrpc": "2.0", "method": "notifications/initialized"}],
                             ping(1))
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 1, "result": {}}])


if __name__ == "__main__":
    unittest.main()
