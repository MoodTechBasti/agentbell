"""Tests for agentbell. Run with: python3 -m unittest discover -s tests -v"""

import contextlib
import io
import json
import os
import queue
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agentbell as an  # noqa: E402

_TEST_ROOT = tempfile.mkdtemp(prefix="agentbell-tests-")
os.environ["AGENTBELL_STATE_DIR"] = os.path.join(_TEST_ROOT, "state")
os.environ["AGENTBELL_CONFIG_DIR"] = os.path.join(_TEST_ROOT, "config")

# tomllib is 3.11+; the project supports 3.9, so those tests are skipped there
HAS_TOMLLIB = sys.version_info >= (3, 11)


# One throwaway key pair for the whole suite: deriving one costs a few ms and
# every test that needs a *valid* license signs with this instead of the real
# private seed (which only the author's machine has).
TEST_SEED = os.urandom(32)
TEST_PUBLIC_KEY = an._ed25519_public_key(TEST_SEED).hex()


@contextlib.contextmanager
def dev_keypair(seed=TEST_SEED):
    """Mint and verify with a throwaway key pair for the duration of the block.

    Verification uses the hardcoded LICENSE_PUBLIC_KEY and nothing else - by
    design there is no env var or config entry that points it somewhere else -
    so a test that needs a valid key has to patch that constant. The env var
    only hands `make_license_key` a signing seed.
    """
    old_public = an.LICENSE_PUBLIC_KEY
    old_env = os.environ.get(an.LICENSE_SECRET_ENV)
    an.LICENSE_PUBLIC_KEY = an._ed25519_public_key(seed).hex()
    os.environ[an.LICENSE_SECRET_ENV] = seed.hex()
    an._LICENSE_CACHE.clear()
    try:
        yield seed
    finally:
        an.LICENSE_PUBLIC_KEY = old_public
        an._LICENSE_CACHE.clear()
        if old_env is None:
            os.environ.pop(an.LICENSE_SECRET_ENV, None)
        else:
            os.environ[an.LICENSE_SECRET_ENV] = old_env


class MockNtfy:
    """Tiny in-process ntfy server: publish + JSON stream/poll subscribe."""

    def __init__(self, stream_enabled=True, post_503_count=0):
        self.posts = {}          # topic -> list of {"id","body","ts","headers"}
        self.subscribers = {}    # topic -> list of queue.Queue
        self.stream_enabled = stream_enabled
        self.post_503_count = post_503_count  # transient failures on the first N POSTs
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                topic = self.path.strip("/").split("/")[0]
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8")
                if server.post_503_count > 0:
                    server.post_503_count -= 1
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"temporarily unavailable")
                    return
                record = {
                    "id": "".join(random.choice("0123456789abcdef") for _ in range(10)),
                    "body": body,
                    "ts": time.time(),
                    "headers": dict(self.headers),
                }
                server.posts.setdefault(topic, []).append(record)
                if server.stream_enabled:
                    for q in list(server.subscribers.get(topic, [])):
                        q.put(record)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":"x","time":1,"event":"message"}')

            def _write(self, record):
                line = json.dumps({"event": "message", "id": record["id"], "message": record["body"]})
                self.wfile.write((line + "\n").encode())
                self.wfile.flush()

            def do_GET(self):
                path, _, query = self.path.partition("?")
                parts = path.strip("/").split("/")
                topic = parts[0]
                params = {}
                for pair in query.split("&"):
                    if "=" in pair:
                        key, _, value = pair.partition("=")
                        params[key] = value
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                self.wfile.write(b'{"event":"open"}\n')
                self.wfile.flush()
                if params.get("poll") == "1":
                    since = params.get("since", "")
                    if since == "latest":
                        records = server.posts.get(topic, [])[-1:]
                    elif since.isdigit():
                        records = [r for r in server.posts.get(topic, []) if r["ts"] >= int(since)]
                    else:
                        records = list(server.posts.get(topic, []))
                    for record in records:
                        self._write(record)
                    return
                q = queue.Queue()
                server.subscribers.setdefault(topic, []).append(q)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        record = q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    self._write(record)

        return Handler

    def inject(self, topic, body, message_id):
        """Publish with a caller-chosen message id.

        Same record a POST would make, minus the random id: a test that has to
        know an id *before* the message exists (claim/dedupe races) cannot use
        the HTTP path, where the id is only known after the fact.
        """
        record = {"id": message_id, "body": body, "ts": time.time(), "headers": {}}
        self.posts.setdefault(topic, []).append(record)
        if self.stream_enabled:
            for q in list(self.subscribers.get(topic, [])):
                q.put(record)
        return record

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def make_config(server_url, topic="testtopic"):
    return an.Config({
        "ntfy": {"server": server_url, "topic": topic, "auth": None},
        "telegram": {"bot_token": None, "chat_id": None},
        "channels": ["ntfy"],
        "quiet_hours": [],
        "quiet_hours_min_priority": 3,
        "approval_timeout": 60,
        "webhook": {"listen": "127.0.0.1", "port": 0, "token": None},
    })


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_webhook(cfg, port, timeout=5.0):
    """Run webhook_server(cfg) in a thread and wait until it answers."""
    thread = threading.Thread(target=lambda: an.webhook_server(cfg), daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5).read()
            return thread
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


class TestPriorityAndQuietHours(unittest.TestCase):
    def test_priority_mapping(self):
        self.assertEqual(an.PRIORITIES["low"], 2)
        self.assertEqual(an.PRIORITIES["normal"], 3)
        self.assertEqual(an.PRIORITIES["urgent"], 5)

    def test_quiet_hours_inside(self):
        cfg = make_config("http://x")
        cfg.data["quiet_hours"] = [{"start": "22:00", "end": "07:30"}]
        now = an.datetime.datetime(2026, 8, 14, 23, 0)
        self.assertTrue(an.in_quiet_hours(cfg.data["quiet_hours"], now))
        now = an.datetime.datetime(2026, 8, 14, 2, 0)
        self.assertTrue(an.in_quiet_hours(cfg.data["quiet_hours"], now))
        now = an.datetime.datetime(2026, 8, 14, 12, 0)
        self.assertFalse(an.in_quiet_hours(cfg.data["quiet_hours"], now))

    def test_quiet_hours_no_wrap(self):
        cfg = make_config("http://x")
        cfg.data["quiet_hours"] = [{"start": "13:00", "end": "14:00"}]
        now = an.datetime.datetime(2026, 8, 14, 13, 30)
        self.assertTrue(an.in_quiet_hours(cfg.data["quiet_hours"], now))
        now = an.datetime.datetime(2026, 8, 14, 14, 30)
        self.assertFalse(an.in_quiet_hours(cfg.data["quiet_hours"], now))

    def test_suppression(self):
        cfg = make_config("http://x")
        cfg.data["quiet_hours"] = [{"start": "00:00", "end": "23:59"}]
        self.assertTrue(an.suppressed_by_quiet_hours(cfg, 2, force=False))
        self.assertFalse(an.suppressed_by_quiet_hours(cfg, 3, force=False))
        self.assertFalse(an.suppressed_by_quiet_hours(cfg, 2, force=True))

    def test_invalid_window_ignored(self):
        self.assertFalse(an.in_quiet_hours([{"start": "99:00", "end": "10:00"}]))
        self.assertFalse(an.in_quiet_hours([]))


class TestAnswerParsing(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(an._parse_answer("APPROVED abc123")[0], "approved")
        self.assertEqual(an._parse_answer("approve")[0], "approved")
        self.assertEqual(an._parse_answer("YES")[0], "approved")
        self.assertEqual(an._parse_answer("DENIED abc123")[0], "denied")
        self.assertEqual(an._parse_answer("no")[0], "denied")
        self.assertEqual(an._parse_answer("Deploy only staging first")[0], "answer")
        self.assertEqual(an._parse_answer("Deploy only staging first")[1], "Deploy only staging first")
        self.assertEqual(an._parse_answer("")[0], "denied")


class TestNotify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ntfy = MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def test_publish_headers(self):
        cfg = make_config(self.ntfy.url)
        result = an.send_notification(cfg, "hello world", title="Build done",
                                      priority="urgent", tags=["build", "done"])
        self.assertTrue(result["ok"])
        posts = self.ntfy.posts["testtopic"]
        headers, body = posts[-1]["headers"], posts[-1]["body"]
        self.assertEqual(body, "hello world")
        self.assertEqual(headers["Title"], "Build done")
        self.assertEqual(headers["Priority"], "5")
        self.assertEqual(headers["Tags"], "build,done")
        self.assertNotIn("Actions", headers)

    def test_suppressed(self):
        cfg = make_config(self.ntfy.url)
        cfg.data["quiet_hours"] = [{"start": "00:00", "end": "23:59"}]
        before = len(self.ntfy.posts.get("testtopic", []))
        result = an.send_notification(cfg, "quiet please", priority="low")
        self.assertTrue(result["ok"])
        self.assertTrue(result["suppressed"])
        self.assertEqual(len(self.ntfy.posts.get("testtopic", [])), before)

    def test_history_written(self):
        an.write_history({"event": "notify", "message": "x", "priority": "normal"})
        records = an.read_history()
        self.assertTrue(any(r["event"] == "notify" and r["message"] == "x" for r in records))


class TestApprovalFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ntfy = MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def _run_ask_async(self, cfg, **kwargs):
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.update(result=an.run_ask(cfg, **kwargs)),
            daemon=True,
        )
        thread.start()
        return holder, thread

    def test_approve_button(self):
        cfg = make_config(self.ntfy.url, topic="approvals")
        holder, thread = self._run_ask_async(cfg, message="Deploy?", timeout_seconds=20,
                                             print_status=False)
        deadline = time.monotonic() + 5
        while not self.ntfy.posts.get("approvals") and time.monotonic() < deadline:
            time.sleep(0.05)
        headers, body = self.ntfy.posts["approvals"][-1]["headers"], self.ntfy.posts["approvals"][-1]["body"]
        self.assertEqual(headers["Priority"], "4")
        actions = json.loads(headers["Actions"])
        self.assertEqual([a["label"] for a in actions], ["Approve", "Deny"])
        match = an.re.search(r"ID: ([0-9a-f]+)", body)
        approval_id = match.group(1)
        urllib.request.urlopen(
            urllib.request.Request(
                f"{self.ntfy.url}/approvals-responses", method="POST",
                data=f"APPROVED {approval_id}".encode(),
            )
        ).read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        outcome = holder["result"]
        self.assertTrue(outcome["approved"])
        self.assertFalse(outcome["timeout"])

    def test_deny_button(self):
        cfg = make_config(self.ntfy.url, topic="approvals2")
        holder, thread = self._run_ask_async(cfg, message="Deploy?", timeout_seconds=20,
                                             print_status=False)
        deadline = time.monotonic() + 5
        while not self.ntfy.posts.get("approvals2") and time.monotonic() < deadline:
            time.sleep(0.05)
        urllib.request.urlopen(
            urllib.request.Request(
                f"{self.ntfy.url}/approvals2-responses", method="POST",
                data=b"DENIED whatever",
            )
        ).read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        outcome = holder["result"]
        self.assertTrue(outcome["denied"])
        self.assertFalse(outcome["approved"])

    def test_free_text_answer(self):
        cfg = make_config(self.ntfy.url, topic="approvals3")
        holder, thread = self._run_ask_async(cfg, message="Which env?", timeout_seconds=20,
                                             print_status=False, buttons=False)
        deadline = time.monotonic() + 5
        while not self.ntfy.posts.get("approvals3") and time.monotonic() < deadline:
            time.sleep(0.05)
        headers, body = self.ntfy.posts["approvals3"][-1]["headers"], self.ntfy.posts["approvals3"][-1]["body"]
        self.assertNotIn("Actions", headers)
        urllib.request.urlopen(
            urllib.request.Request(
                f"{self.ntfy.url}/approvals3-responses", method="POST",
                data=b"staging",
            )
        ).read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        outcome = holder["result"]
        self.assertTrue(outcome["approved"])
        self.assertEqual(outcome["answer"], "staging")

    def test_timeout(self):
        cfg = make_config(self.ntfy.url, topic="approvals4")
        holder, thread = self._run_ask_async(cfg, message="Deploy?", timeout_seconds=2,
                                             print_status=False)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(holder["result"]["timeout"])
        records = an.read_history()
        self.assertTrue(any(r["event"] == "ask_result" and r["result"] == "timeout" for r in records))


class TestHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.old_argv0 = sys.argv[0]
        sys.argv[0] = "/usr/local/bin/agentbell"

    def tearDown(self):
        if self.old_home:
            os.environ["HOME"] = self.old_home
        else:
            os.environ.pop("HOME", None)
        sys.argv[0] = self.old_argv0
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def _settings(self, agent):
        return os.path.join(self.home, ".claude" if agent == "claude" else ".gemini", "settings.json")

    def test_claude_install_preserves_existing(self):
        path = self._settings("claude")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "lint.sh"}]}]}, "model": "opus"}, fh)
        result = an.install_hooks("claude")
        self.assertTrue(result["changed"])
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual(data["model"], "opus")
        self.assertIn("PreToolUse", data["hooks"])
        self.assertIn("Stop", data["hooks"])
        self.assertIn("StopFailure", data["hooks"])
        self.assertIn("Notification", data["hooks"])
        commands = " ".join(
            h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]
        )
        self.assertIn("agentbell hook run_completed --agent claude", commands)
        # idempotent
        result2 = an.install_hooks("claude")
        self.assertFalse(result2["changed"])

    def test_claude_uninstall(self):
        an.install_hooks("claude")
        result = an.install_hooks("claude", add=False)
        self.assertTrue(result["changed"])
        with open(self._settings("claude")) as fh:
            data = json.load(fh)
        for event in ("Stop", "StopFailure", "Notification"):
            self.assertNotIn(event, data.get("hooks", {}))

    def test_gemini_install(self):
        path = self._settings("gemini")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"model": {"name": "gemini-2.5-pro"}}, fh)
        result = an.install_hooks("gemini")
        self.assertTrue(result["changed"])
        with open(path) as fh:
            data = json.load(fh)
        self.assertIn("AfterAgent", data["hooks"])
        hook = data["hooks"]["AfterAgent"][0]["hooks"][0]
        self.assertIn("agentbell hook run_completed --agent gemini", hook["command"])
        an.install_hooks("gemini", add=False)
        with open(path) as fh:
            data = json.load(fh)
        self.assertNotIn("AfterAgent", data.get("hooks", {}))

    def test_codex_install_uninstall(self):
        result = an.install_hooks("codex")
        self.assertTrue(result["changed"])
        self.assertEqual(result["notes"], [])
        path = an.codex_config_path()
        with open(path) as fh:
            text = fh.read()
        self.assertIn("features.hooks = true", text)
        self.assertIn("[[hooks.Stop]]", text)
        self.assertIn("hook run_completed --agent codex", text)
        self.assertIn(an.TOML_START, text)
        # idempotent
        result2 = an.install_hooks("codex")
        self.assertFalse(result2["changed"])
        self.assertTrue(an.uninstall_codex_hooks())
        with open(path) as fh:
            text = fh.read()
        self.assertNotIn("features.hooks = true", text)
        self.assertNotIn(an.TOML_START, text)

    def test_codex_existing_features_table(self):
        path = an.codex_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write('[features]\nweb_search = true\n')
        result = an.install_hooks("codex")
        with open(path) as fh:
            text = fh.read()
        self.assertNotIn("features.hooks = true", text)   # would be a TOML conflict
        self.assertIn(an.TOML_START, text)                # hooks are installed anyway
        self.assertTrue(any("/hooks" in n for n in result["notes"]), result["notes"])

    def test_cursor_rule(self):
        project = os.path.join(self.tmp, "proj")
        os.makedirs(project)
        result = an.install_hooks("cursor", project=project)
        self.assertTrue(result["changed"])
        path = os.path.join(project, ".cursor", "rules", "agentbell.mdc")
        self.assertTrue(os.path.exists(path))
        an.install_hooks("cursor", project=project, add=False)
        self.assertFalse(os.path.exists(path))

    def test_opencode_plugin_install_and_remove(self):
        project = os.path.join(self.tmp, "proj2")
        os.makedirs(project)
        result = an.install_hooks("opencode", project=project)
        self.assertTrue(result["changed"])
        path = os.path.join(project, ".opencode", "plugin", "agentbell.js")
        self.assertEqual(result["path"], path)
        with open(path) as fh:
            text = fh.read()
        self.assertIn("session.idle", text)
        self.assertIn("run_completed", text)
        self.assertIn("--agent", text)
        self.assertNotIn("__AGENTBELL_BIN__", text)
        # idempotent
        self.assertFalse(an.install_hooks("opencode", project=project)["changed"])
        an.install_hooks("opencode", project=project, add=False)
        self.assertFalse(os.path.exists(path))

    def test_opencode_plugin_removes_duplicate_in_sibling_dir(self):
        """OpenCode loads both plugin/ and plugins/ - a leftover would double-fire."""
        project = os.path.join(self.tmp, "proj_dup")
        dup = os.path.join(project, ".opencode", "plugins", "agentbell.js")
        os.makedirs(os.path.dirname(dup))
        with open(dup, "w") as fh:
            fh.write("// agentbell old copy\n")
        an.install_hooks("opencode", project=project)
        self.assertFalse(os.path.exists(dup))
        self.assertTrue(os.path.exists(
            os.path.join(project, ".opencode", "plugin", "agentbell.js")))

    def test_opencode_install_migrates_legacy_agents_md_block(self):
        project = os.path.join(self.tmp, "proj_legacy")
        os.makedirs(project)
        agents_md = os.path.join(project, "AGENTS.md")
        with open(agents_md, "w") as fh:
            fh.write("# Existing rules\n" + an.BLOCK_START + "\nold\n" + an.BLOCK_END + "\n")
        an.install_hooks("opencode", project=project)
        with open(agents_md) as fh:
            text = fh.read()
        self.assertEqual(text.strip(), "# Existing rules")
        self.assertTrue(os.path.exists(
            os.path.join(project, ".opencode", "plugin", "agentbell.js")))

    @unittest.skipUnless(HAS_TOMLLIB, "tomllib requires 3.11+")
    def test_kimi_install_uninstall(self):
        import tomllib
        path = an.kimi_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write('[models]\nmodel = "kimi-k3"\n')
        result = an.install_hooks("kimi")
        self.assertTrue(result["changed"])
        self.assertEqual(result["notes"], [])
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        self.assertEqual(data["models"]["model"], "kimi-k3")
        events = [h["event"] for h in data["hooks"]]
        self.assertEqual(events, ["UserPromptSubmit", "Stop", "StopFailure"])
        # only event/matcher/command/timeout are allowed - async would break the config
        self.assertTrue(set(data["hooks"][0]) <= {"event", "matcher", "command", "timeout"})
        self.assertIn("hook started --agent kimi --silent", data["hooks"][0]["command"])
        self.assertFalse(an.install_hooks("kimi")["changed"])   # idempotent
        self.assertTrue(an.install_hooks("kimi", add=False)["changed"])
        with open(path) as fh:
            text = fh.read()
        self.assertNotIn(an.TOML_START, text)
        self.assertIn("[models]", text)
        self.assertFalse(an.install_hooks("kimi", add=False)["changed"])

    def test_kimi_stale_block_self_heals(self):
        """A block written by an older version (old binary path) must be repaired."""
        path = an.kimi_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write('[models]\nmodel = "kimi-k3"\n\n'
                     + an.TOML_START + "\n[[hooks]]\nevent = \"Stop\"\n"
                     + "command = \"/old/path/agentbell hook run_completed --agent kimi\"\n"
                     + "timeout = 10\n" + an.TOML_END + "\n")
        result = an.install_hooks("kimi")
        self.assertTrue(result["changed"])
        self.assertTrue(any("updated the hook block" in n for n in result["notes"]))
        with open(path) as fh:
            text = fh.read()
        self.assertNotIn("/old/path", text)
        self.assertIn(an.agentbell_binary(), text)
        self.assertFalse(an.install_hooks("kimi")["changed"])
        an.install_hooks("kimi", add=False)

    @unittest.skipUnless(HAS_TOMLLIB, "tomllib requires 3.11+")
    def test_kimi_uninstall_keeps_config_parsing(self):
        import tomllib
        path = an.kimi_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("[models]\nmodel = \"kimi-k3\"\n")
        an.install_hooks("kimi")
        an.install_hooks("kimi", add=False)
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        self.assertEqual(data["models"]["model"], "kimi-k3")
        self.assertNotIn("hooks", data)

    def test_qwen_install_uninstall(self):
        path = an.qwen_settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"model": "qwen3"}, fh)
        result = an.install_hooks("qwen-code")
        self.assertTrue(result["changed"])
        self.assertTrue(any("disableAllHooks" in n for n in result["notes"]))
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual(data["model"], "qwen3")
        for event in ("UserPromptSubmit", "Stop", "StopFailure"):
            self.assertIn(event, data["hooks"])
            for group in data["hooks"][event]:
                for hook in group["hooks"]:
                    self.assertTrue(hook.get("async"), f"{event} hook must be async")
        commands = " ".join(
            h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]
        )
        self.assertIn("agentbell hook run_completed --agent qwen-code", commands)
        self.assertFalse(an.install_hooks("qwen-code")["changed"])
        self.assertTrue(an.install_hooks("qwen-code", add=False)["changed"])
        with open(path) as fh:
            data = json.load(fh)
        self.assertNotIn("hooks", data)

    def test_qwen_upgrade_self_heals_async(self):
        """Hooks written by 1.4.0 (no async) must be repaired, not treated as current."""
        path = an.qwen_settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"hooks": {"Stop": [{"hooks": [{
                "type": "command",
                "command": an._hook_command("run_completed", "qwen-code")
                           + f" --min-duration {an.HOOK_MIN_DURATION}",
            }]}]}}, fh)
        result = an.install_hooks("qwen-code")
        self.assertTrue(result["changed"])
        with open(path) as fh:
            data = json.load(fh)
        hooks = data["hooks"]["Stop"][0]["hooks"]
        self.assertEqual(len(hooks), 1)
        self.assertTrue(hooks[0].get("async"))
        # and a second run is idempotent
        self.assertFalse(an.install_hooks("qwen-code")["changed"])
        an.install_hooks("qwen-code", add=False)

    def test_qwen_mcp_project_scope(self):
        project = os.path.join(self.tmp, "proj_qwen_mcp")
        os.makedirs(project)
        rows = dict(an.mcp_add_configs("/opt/bin/agentbell", project=project,
                                       clients=["qwen-code"]))
        self.assertNotIn("FAILED", rows.get("qwen-code", "MISSING"))
        path = an.qwen_settings_path(project)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["mcpServers"]["agentbell"]["args"], ["mcp"])
        self.assertTrue(any("Qwen Code (project)" in l for l in
                            [e["label"] for e in an._mcp_entries(project)]))

    def _project_rule_roundtrip(self, agent, relpath):
        project = os.path.join(self.tmp, "proj_" + agent)
        os.makedirs(project)
        path = os.path.join(project, relpath)
        result = an.install_hooks(agent, project=project)
        self.assertTrue(result["changed"])
        self.assertEqual(result["path"], path)
        self.assertTrue(os.path.exists(path))
        with open(path) as fh:
            content = fh.read()
        self.assertIn("agentbell", content)
        # idempotent
        self.assertFalse(an.install_hooks(agent, project=project)["changed"])
        # uninstall
        self.assertTrue(an.install_hooks(agent, project=project, add=False)["changed"])
        self.assertFalse(os.path.exists(path))
        self.assertFalse(an.install_hooks(agent, project=project, add=False)["changed"])

    def test_windsurf_rule(self):
        project = os.path.join(self.tmp, "proj_windsurf")
        os.makedirs(project)
        current = os.path.join(project, ".windsurf", "rules", "agentbell.md")
        legacy = os.path.join(project, ".windsurf", "rules", "agentbell.mdc")
        result = an.install_hooks("windsurf", project=project)
        self.assertTrue(result["changed"])
        self.assertEqual(result["path"], current)
        for path in (current, legacy):
            self.assertTrue(os.path.exists(path), path)
        with open(current) as fh:
            text = fh.read()
        self.assertIn("trigger: always_on", text)
        self.assertIn("--agent windsurf", text)
        with open(legacy) as fh:
            self.assertIn("alwaysApply: true", fh.read())
        # idempotent
        self.assertFalse(an.install_hooks("windsurf", project=project)["changed"])
        # uninstall removes both
        self.assertTrue(an.install_hooks("windsurf", project=project, add=False)["changed"])
        self.assertFalse(os.path.exists(current))
        self.assertFalse(os.path.exists(legacy))
        self.assertFalse(an.install_hooks("windsurf", project=project, add=False)["changed"])
        # status agrees with both formats
        os.makedirs(os.path.dirname(legacy), exist_ok=True)
        with open(legacy, "w") as fh:
            fh.write(an.WINDSURF_LEGACY_RULE)
        status = {agent: s for agent, s, _, _ in an.hooks_status(project=project)}
        self.assertEqual(status["windsurf"], "installed")

    def test_windsurf_uninstall_skips_foreign_files(self):
        project = os.path.join(self.tmp, "proj_ws_foreign")
        os.makedirs(os.path.join(project, ".windsurf", "rules"))
        for name in ("agentbell.md", "agentbell.mdc"):
            path = os.path.join(project, ".windsurf", "rules", name)
            with open(path, "w") as fh:
                fh.write("---\nalwaysApply: true\n---\n# someone else's rule\n")
        self.assertFalse(an.install_hooks("windsurf", project=project, add=False)["changed"])
        for name in ("agentbell.md", "agentbell.mdc"):
            self.assertTrue(os.path.exists(
                os.path.join(project, ".windsurf", "rules", name)))

    def test_cline_rule(self):
        self._project_rule_roundtrip("cline", ".clinerules/agentbell.md")

    def test_continue_rule(self):
        self._project_rule_roundtrip("continue", ".continue/rules/agentbell.md")

    def test_zed_rule(self):
        self._project_rule_roundtrip("zed", ".rules")

    def test_aider_rule(self):
        self._project_rule_roundtrip("aider", "AGENTS.md")

    def test_block_rule_keeps_foreign_content(self):
        project = os.path.join(self.tmp, "proj_keep")
        os.makedirs(project)
        path = os.path.join(project, ".rules")
        with open(path, "w") as fh:
            fh.write("# my own zed rules\n")
        an.install_hooks("zed", project=project)
        an.install_hooks("zed", project=project, add=False)
        with open(path) as fh:
            text = fh.read()
        self.assertEqual(text.strip(), "# my own zed rules")
        self.assertNotIn(an.BLOCK_START, text)

    def test_owned_rule_not_removed_if_foreign(self):
        project = os.path.join(self.tmp, "proj_foreign")
        os.makedirs(os.path.join(project, ".windsurf", "rules"))
        path = os.path.join(project, ".windsurf", "rules", "agentbell.mdc")
        with open(path, "w") as fh:
            fh.write("---\nalwaysApply: true\n---\n# someone else's rule\n")
        self.assertFalse(an.install_hooks("windsurf", project=project, add=False)["changed"])
        self.assertTrue(os.path.exists(path))

    def test_find_agents_detects_by_config_dir(self):
        os.makedirs(os.path.join(self.home, ".kimi-code"))
        os.makedirs(os.path.join(self.home, ".qwen"))
        found = an.find_agents()
        self.assertIn("kimi", found)
        self.assertIn("qwen-code", found)

    def test_hooks_status_lists_all_agents(self):
        agents = [a for a, status, path, _ in an.hooks_status()]
        self.assertEqual(agents, an.AGENTS)
        self.assertEqual(len(agents), 12)

    def test_hooks_status_labels_hook_and_rule_reliability(self):
        reliability = {agent: kind for agent, _, _, kind in an.hooks_status()}
        for agent in ("claude", "codex", "gemini", "kimi", "qwen-code", "opencode"):
            self.assertEqual(reliability[agent], "hook")
        for agent in ("cursor", "windsurf", "cline", "continue", "zed", "aider"):
            self.assertEqual(reliability[agent], "rule")

    def test_unknown_agent_rejected(self):
        with self.assertRaises(SystemExit):
            an.install_hooks("grok")

    def test_aider_status_not_fooled_by_opencode_legacy_block(self):
        """The legacy v1.3 opencode AGENTS.md block must not mark aider installed."""
        project = os.path.join(self.tmp, "proj_legacy_oc")
        os.makedirs(project)
        agents_md = os.path.join(project, "AGENTS.md")
        with open(agents_md, "w") as fh:
            fh.write(an.BLOCK_START + "\n--agent opencode\n" + an.BLOCK_END + "\n")
        status = {a: s for a, s, _, _ in an.hooks_status(project=project)}
        self.assertEqual(status["aider"], "not installed")
        self.assertEqual(status["opencode"], "not installed")


class TestWebhookServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ntfy = MockNtfy()
        cls.cfg = make_config(cls.ntfy.url)
        cls.sock = __import__("socket").socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.port = cls.sock.getsockname()[1]
        cls.sock.close()
        cls.cfg.data["webhook"] = {"listen": "127.0.0.1", "port": cls.port, "token": None}
        cls.thread = threading.Thread(target=lambda: an.webhook_server(cls.cfg), daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 5
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/healthz", timeout=0.5).read()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def test_healthz(self):
        body = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=3).read()
        self.assertIn(b"agentbell", body)

    def test_notify(self):
        body = json.dumps({"message": "webhook hello", "title": "T", "priority": "normal"})
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/notify", method="POST", data=body.encode()
        )
        response = urllib.request.urlopen(request, timeout=5).read()
        self.assertIn(b'"ok": true', response)
        self.assertTrue(self.ntfy.posts.get("testtopic"))

    def test_ask_timeout(self):
        body = json.dumps({"message": "approve?", "timeout_seconds": 2})
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/ask", method="POST", data=body.encode()
        )
        response = json.loads(urllib.request.urlopen(request, timeout=10).read())
        self.assertTrue(response["timeout"])


class TestMCP(unittest.TestCase):
    def test_protocol(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        payload = "\n".join(json.dumps(r) for r in requests) + "\n"
        stdin_buf = io.TextIOWrapper(io.BytesIO(payload.encode()))
        stdout_buf = io.TextIOWrapper(io.BytesIO())
        old_stdin, old_stdout = sys.stdin, sys.stdout
        try:
            sys.stdin = stdin_buf
            sys.stdout = stdout_buf
            an.mcp_loop()
            stdout_buf.flush()
            output = stdout_buf.buffer.getvalue().decode()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        lines = [json.loads(line) for line in output.strip().splitlines()]
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "agentbell")
        tools = lines[1]["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertIn("notify", names)
        self.assertIn("ask_approval", names)


class TestTopicValidation(unittest.TestCase):
    def test_valid(self):
        an.validate_topic("my-topic_123")

    def test_invalid(self):
        with self.assertRaises(RuntimeError):
            an.validate_topic("bad topic!")
        with self.assertRaises(RuntimeError):
            an.validate_topic("")


class TestConfigMerge(unittest.TestCase):
    def test_deep_merge(self):
        base = {"ntfy": {"server": "a", "topic": "t"}, "channels": ["ntfy"]}
        an._deep_merge(base, {"ntfy": {"server": "b"}})
        self.assertEqual(base["ntfy"]["server"], "b")
        self.assertEqual(base["ntfy"]["topic"], "t")

    def test_normalize_server(self):
        self.assertEqual(an.normalize_server("ntfy.sh"), "https://ntfy.sh")
        self.assertEqual(an.normalize_server("https://ntfy.sh/"), "https://ntfy.sh")
        self.assertEqual(an.normalize_server("http://my.server:8080"), "http://my.server:8080")
        self.assertEqual(an.normalize_server(""), "")
        cfg = make_config("ntfy.sh")
        self.assertEqual(an.NtfyChannel(cfg).server(), "https://ntfy.sh")


class TestEd25519(unittest.TestCase):
    """RFC 8032 §7.1 test vectors - the licensing scheme rests on these."""

    # (name, private seed, public key, message, signature) - all hex
    VECTORS = [
        ("TEST 1 (empty message)",
         "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
         "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015"
         "55fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("TEST 2 (1 byte)",
         "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
         "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69d"
         "a085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
        ("TEST 3 (2 bytes)",
         "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
         "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
         "af82",
         "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3a"
         "c18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ]

    def test_rfc8032_vectors(self):
        for name, seed_hex, public_hex, message_hex, signature_hex in self.VECTORS:
            with self.subTest(name):
                seed = bytes.fromhex(seed_hex)
                public = bytes.fromhex(public_hex)
                message = bytes.fromhex(message_hex)
                signature = bytes.fromhex(signature_hex)
                self.assertEqual(an._ed25519_public_key(seed), public)
                self.assertEqual(an._ed25519_sign(seed, message), signature)
                self.assertTrue(an._ed25519_verify(public, message, signature))

    def test_rfc8032_vectors_reject_a_flipped_bit(self):
        for name, _seed_hex, public_hex, message_hex, signature_hex in self.VECTORS:
            with self.subTest(name):
                public = bytes.fromhex(public_hex)
                message = bytes.fromhex(message_hex)
                signature = bytes.fromhex(signature_hex)
                other_message = (bytes([message[0] ^ 1]) + message[1:]) if message else b"\x00"
                self.assertFalse(an._ed25519_verify(public, other_message, signature))
                for index in (0, 32, 63):        # R half, S half, last byte
                    broken = bytearray(signature)
                    broken[index] ^= 1
                    self.assertFalse(an._ed25519_verify(public, message, bytes(broken)))
                broken_key = bytearray(public)
                broken_key[0] ^= 1
                self.assertFalse(an._ed25519_verify(bytes(broken_key), message, signature))

    def test_verify_never_raises_on_garbage(self):
        public = bytes.fromhex(self.VECTORS[0][2])
        for key, message, signature in (
            (b"", b"", b""),
            (public, b"m", b""),
            (public, b"m", b"\x00" * 63),
            (public[:31], b"m", b"\x00" * 64),
            (b"\xff" * 32, b"m", b"\xff" * 64),      # not a curve point
            (public, b"m", b"\xff" * 64),            # S far above the group order
        ):
            self.assertFalse(an._ed25519_verify(key, message, signature))


class TestLicense(unittest.TestCase):
    def test_roundtrip(self):
        with dev_keypair() as seed:
            key = an.make_license_key("customer-123", seed=seed)
            self.assertTrue(key.startswith("AB1-"))
            self.assertTrue(an.check_license_key(key))
            self.assertFalse(an.check_license_key(key + "X"))
            self.assertFalse(an.check_license_key(key.replace("AB1", "AN1", 1)))
        # outside the block the real public key applies again: a key signed by
        # anything else is worthless, which is the whole point of the scheme
        self.assertFalse(an.check_license_key(key))

    def test_a_key_signed_by_another_pair_is_refused(self):
        other = os.urandom(32)
        forged = an.make_license_key("mallory", seed=other)
        with dev_keypair():
            self.assertFalse(an.check_license_key(forged))

    def test_garbage_is_refused_without_raising(self):
        for junk in (None, "", "   ", "garbage", 42, b"AB1-x", ["AB1"],
                     "AB1", "AB1-AAAA", "AB1-AAAA-BBBB-CCCC", "AB1--",
                     "AB1-!!!!-????", "AB1-" + "A" * 40 + "-" + "B" * 103,
                     "AN1-MFTWK3TU-SIGNATURE", "-" * 3):
            self.assertFalse(an.check_license_key(junk), f"accepted {junk!r}")

    def test_expiry(self):
        with dev_keypair() as seed:
            past = (an.datetime.date.today() - an.datetime.timedelta(days=1)).isoformat()
            self.assertFalse(an.check_license_key(
                an.make_license_key("c", expiry=past, seed=seed)))
            self.assertTrue(an.check_license_key(
                an.make_license_key("c", expiry="2999-01-01", seed=seed)))
            self.assertTrue(an.check_license_key(
                an.make_license_key("c", seed=seed)))          # lifetime

    def test_no_signing_seed_means_no_key(self):
        """Every user build is in this state: it can verify, never mint."""
        original = an._signing_seed
        an._signing_seed = lambda seed=None: None
        try:
            self.assertIsNone(an.make_license_key("c"))
        finally:
            an._signing_seed = original

    def test_verification_is_memoized(self):
        with dev_keypair() as seed:
            key = an.make_license_key("memo", seed=seed)
            calls = []
            original = an._ed25519_verify

            def counting(*args):
                calls.append(args)
                return original(*args)

            an._ed25519_verify = counting
            try:
                an._LICENSE_CACHE.clear()
                for _ in range(5):
                    self.assertTrue(an.check_license_key(key))
                self.assertEqual(len(calls), 1, "premium_enabled must not re-verify per send")
            finally:
                an._ed25519_verify = original

    def test_gate(self):
        mock = MockNtfy()
        try:
            cfg = make_config(mock.url)
            cfg.data["telegram"] = {"bot_token": "tok", "chat_id": "123"}
            result = an.send_notification(cfg, "hi", channels=["ntfy", "telegram"])
            self.assertFalse(result["ok"])
            self.assertIn("premium", result["errors"][0])
            # with a valid license, telegram is attempted (fails on network, but no gate error)
            with dev_keypair() as seed:
                cfg.data["license"] = an.make_license_key("c", seed=seed)
                result = an.send_notification(cfg, "hi", channels=["ntfy", "telegram"])
                self.assertFalse(any("premium" in e for e in result.get("errors", [])))
        finally:
            mock.stop()

    def test_env_license(self):
        cfg = make_config("http://x")
        with dev_keypair() as seed:
            key = an.make_license_key("env-customer", seed=seed)
            old = os.environ.get(an.LICENSE_ENV)
            os.environ[an.LICENSE_ENV] = key
            try:
                self.assertTrue(an.premium_enabled(cfg))
            finally:
                if old is None:
                    os.environ.pop(an.LICENSE_ENV, None)
                else:
                    os.environ[an.LICENSE_ENV] = old

    def test_env_var_is_a_signing_seed_only(self):
        """The env var mints (author side); it never decides what is valid."""
        with dev_keypair():
            self.assertTrue(an.check_license_key(an.make_license_key("from-env-seed")))


class TestEventAliases(unittest.TestCase):
    def test_alias_resolution(self):
        self.assertEqual(an.EVENT_ALIASES["done"], "run_completed")
        self.assertEqual(an.EVENT_ALIASES["needs-input"], "input_required")
        self.assertEqual(an.EVENT_ALIASES["failed"], "run_failed")
        self.assertIn("permission_required", an.HOOK_EVENTS)
        spec = an.HOOK_EVENTS["permission_required"]
        self.assertEqual(spec["prio"], "high")


class TestApprovalPollFallback(unittest.TestCase):
    """Approval must succeed even when the stream never delivers (poll path)."""

    def test_poll_only(self):
        mock = MockNtfy(stream_enabled=False)
        try:
            cfg = make_config(mock.url, topic="pollonly")
            holder = {}
            thread = threading.Thread(
                target=lambda: holder.update(
                    result=an.run_ask(cfg, message="Q?", timeout_seconds=20, print_status=False)
                ),
                daemon=True,
            )
            thread.start()
            deadline = time.monotonic() + 5
            while not mock.posts.get("pollonly") and time.monotonic() < deadline:
                time.sleep(0.05)
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{mock.url}/pollonly-responses", method="POST", data=b"APPROVED via poll"
                )
            ).read()
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertTrue(holder["result"]["approved"])
            self.assertFalse(holder["result"]["timeout"])
        finally:
            mock.stop()


class MockTelegram:
    """Tiny in-process Telegram Bot API: sendMessage/answerCallbackQuery/
    editMessageText + getUpdates/getMe."""

    def __init__(self, fail_send=False):
        self.requests = []   # {"method": ..., "body": ...}
        self.updates = []    # queued update dicts served by getUpdates
        self.fail_send = fail_send
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._message_id = 0

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _method(self):
                last = self.path.rstrip("/").split("/")[-1]
                return last.split("?")[0]

            def do_POST(self):
                method = self._method()
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                server.requests.append({"method": method, "body": body})
                if method == "sendMessage":
                    if server.fail_send:
                        self._reply({"ok": False, "description": "boom"})
                        return
                    server._message_id += 1
                    self._reply({"ok": True, "result": {
                        "message_id": server._message_id,
                        "chat": {"id": body.get("chat_id")},
                    }})
                elif method in ("answerCallbackQuery", "editMessageText"):
                    self._reply({"ok": True, "result": True})
                else:
                    self._reply({"ok": True, "result": {}})

            def do_GET(self):
                method = self._method()
                if method == "getUpdates":
                    updates = server.updates[:]
                    server.updates.clear()
                    self._reply({"ok": True, "result": updates})
                elif method == "getMe":
                    self._reply({"ok": True, "result": {"username": "testbot"}})
                else:
                    self._reply({"ok": True, "result": []})

        return Handler

    def queue_update(self, update):
        self.updates.append(update)

    def last(self, method):
        return next((r for r in reversed(self.requests) if r["method"] == method), None)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class _TelegramFixture(unittest.TestCase):
    """Base: points the Telegram API at a mock and activates a test license."""

    @classmethod
    def setUpClass(cls):
        cls.tg = MockTelegram()
        cls.old_base = an.TG_API_BASE
        an.TG_API_BASE = cls.tg.url
        cls.keypair = dev_keypair()
        cls.keypair.__enter__()

    @classmethod
    def tearDownClass(cls):
        an.TG_API_BASE = cls.old_base
        cls.keypair.__exit__(None, None, None)
        cls.tg.stop()

    def setUp(self):
        try:
            os.remove(an._bot_state_path())
        except OSError:
            pass

    def _tg_cfg(self, licensed=True, ntfy_url="https://ntfy.sh", channels=("telegram",)):
        cfg = an.Config({
            "ntfy": {"server": ntfy_url, "topic": "tgtopic", "auth": None},
            "telegram": {"bot_token": "tok123", "chat_id": "42"},
            "channels": list(channels),
            "quiet_hours": [],
            "quiet_hours_min_priority": 3,
            "approval_timeout": 60,
            "webhook": {"listen": "127.0.0.1", "port": 0, "token": None},
        })
        if licensed:
            cfg.data["license"] = an.make_license_key("test-customer", seed=TEST_SEED)
        return cfg


class TestFormatDuration(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(an.format_duration(0), "0s")
        self.assertEqual(an.format_duration(12), "12s")
        self.assertEqual(an.format_duration(252), "4m12s")
        self.assertEqual(an.format_duration(3900), "1h05m")
        self.assertEqual(an.format_duration(-5), "0s")


class TestWatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ntfy = MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def test_success(self):
        cfg = make_config(self.ntfy.url, topic="watch1")
        before = len(self.ntfy.posts.get("watch1", []))
        result = an.run_watch(cfg, [sys.executable, "-c", "exit(0)"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("succeeded (exit 0)", result["message"])
        self.assertIn(" in ", result["message"])
        posts = self.ntfy.posts["watch1"]
        self.assertGreater(len(posts), before)
        self.assertEqual(posts[-1]["headers"]["Priority"], "3")

    def test_failure(self):
        cfg = make_config(self.ntfy.url, topic="watch2")
        result = an.run_watch(cfg, [sys.executable, "-c", "import sys; sys.exit(7)"])
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("failed (exit 7)", result["message"])
        posts = self.ntfy.posts["watch2"]
        self.assertEqual(posts[-1]["headers"]["Priority"], "5")

    def test_missing_command(self):
        cfg = make_config(self.ntfy.url, topic="watch3")
        result = an.run_watch(cfg, ["/no/such/binary-xyz"])
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("could not be started", result["message"])


class TestHookDuration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ntfy = MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def tearDown(self):
        try:
            os.remove(an._run_marker_path("claude"))
        except OSError:
            pass

    def test_duration_from_marker(self):
        cfg = make_config(self.ntfy.url, topic="hookdur")
        an.write_start_marker("claude")
        with open(an._run_marker_path("claude")) as fh:
            data = json.load(fh)
        data["started_at"] = time.time() - 252
        with open(an._run_marker_path("claude"), "w") as fh:
            json.dump(data, fh)
        an.run_hook(cfg, "run_completed", "claude")
        body = self.ntfy.posts["hookdur"][-1]["body"]
        self.assertIn("in 4m12s", body)
        self.assertIsNone(an.read_start_marker("claude"))  # marker consumed

    def test_no_marker_no_duration(self):
        cfg = make_config(self.ntfy.url, topic="hooknodur")
        an.run_hook(cfg, "run_completed", "claude")
        body = self.ntfy.posts["hooknodur"][-1]["body"]
        self.assertNotIn(" in ", body)

    def test_explicit_duration(self):
        cfg = make_config(self.ntfy.url, topic="hookexpl")
        an.run_hook(cfg, "run_failed", "claude", duration=12.9)
        body = self.ntfy.posts["hookexpl"][-1]["body"]
        self.assertIn("in 13s", body)

    def test_started_silent_writes_marker_only(self):
        cfg = make_config(self.ntfy.url, topic="hooksilent")
        before = len(self.ntfy.posts.get("hooksilent", []))
        result = an.run_hook(cfg, "started", "claude", silent=True)
        self.assertTrue(result.get("silent"))
        self.assertEqual(len(self.ntfy.posts.get("hooksilent", [])), before)
        self.assertIsNotNone(an.read_start_marker("claude"))


class TestTelegramAsk(_TelegramFixture):
    def _wait_new_request(self, method="sendMessage", timeout=20.0):
        before = len(self.tg.requests)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            new = [r for r in self.tg.requests[before:] if r["method"] == method]
            if new:
                return new[0]
            time.sleep(0.05)
        return None

    def test_ask_waits_for_bot_answer(self):
        cfg = self._tg_cfg()
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.update(
                result=an.run_ask(cfg, "Deploy?", timeout_seconds=20, print_status=False)
            ),
            daemon=True,
        )
        thread.start()
        sent = self._wait_new_request("sendMessage")
        self.assertIsNotNone(sent)
        body = sent["body"]
        self.assertEqual(body["chat_id"], "42")
        self.assertIn("Approval requested", body["text"])
        self.assertNotIn("reply_markup", body)  # no bot heartbeat -> no dead buttons
        match = an.re.search(r"ID: ([0-9a-f]+)", body["text"])
        an.write_tg_answer(match.group(1), "approved")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        outcome = holder["result"]
        self.assertTrue(outcome["approved"])
        self.assertFalse(outcome["timeout"])
        self.assertEqual(outcome["channel"], "telegram")

    def test_ask_with_bot_alive_attaches_keyboard(self):
        cfg = self._tg_cfg()
        an.write_bot_heartbeat()
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.update(
                result=an.run_ask(cfg, "Deploy?", timeout_seconds=20, print_status=False)
            ),
            daemon=True,
        )
        thread.start()
        body = self._wait_new_request("sendMessage")["body"]
        keyboard = body["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["text"] for b in keyboard[0]], ["Approve", "Deny"])
        self.assertTrue(keyboard[0][0]["callback_data"].startswith("agentbell|"))
        self.assertEqual(len(keyboard[0][0]["callback_data"].split("|")), 3)
        match = an.re.search(r"ID: ([0-9a-f]+)", body["text"])
        an.write_tg_answer(match.group(1), "denied")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(holder["result"]["denied"])

    def test_timeout(self):
        cfg = self._tg_cfg()
        outcome = an.run_ask(cfg, "Deploy?", timeout_seconds=2, print_status=False)
        self.assertTrue(outcome["timeout"])
        records = an.read_history()
        self.assertTrue(any(r["event"] == "ask_result" and r["result"] == "timeout"
                            for r in records))

    def test_explicit_telegram_requires_premium(self):
        cfg = self._tg_cfg(licensed=False)
        with self.assertRaises(RuntimeError):
            an.run_ask(cfg, "Deploy?", timeout_seconds=5, print_status=False,
                       channels=["telegram"])

    def test_derived_channels_drop_telegram_without_license(self):
        cfg = self._tg_cfg(licensed=False, channels=("ntfy", "telegram"))
        self.assertEqual(an.resolve_ask_channels(cfg), ["ntfy"])

    def test_unknown_channel_rejected(self):
        cfg = self._tg_cfg()
        with self.assertRaises(RuntimeError):
            an.resolve_ask_channels(cfg, ["carrier-pigeon"])


class TestBotDaemon(_TelegramFixture):
    def setUp(self):
        super().setUp()
        try:
            os.remove(os.path.join(an.state_dir(), "bot.lock"))
        except OSError:
            pass

    def test_callback_query_writes_answer(self):
        cfg = self._tg_cfg()
        an.write_tg_pending("abcdef0123456789", "Deploy?", 60)
        self.tg.queue_update({
            "update_id": 1,
            "callback_query": {
                "id": "cq1",
                "data": "agentbell|abcdef0123456789|approved",
                "message": {"message_id": 5, "chat": {"id": 42}, "text": "Q?"},
            },
        })
        offset = an.bot_poll_once(cfg, offset=None, poll_timeout=1)
        self.assertEqual(offset, 2)
        self.assertEqual(an.read_tg_answer("abcdef0123456789"), "approved")
        answered = self.tg.last("answerCallbackQuery")
        self.assertEqual(answered["body"]["callback_query_id"], "cq1")
        edited = self.tg.last("editMessageText")
        self.assertIn("Answered: approved", edited["body"]["text"])
        an.remove_tg_answer("abcdef0123456789")
        an.remove_tg_pending("abcdef0123456789")

    def test_expired_callback_not_attributed(self):
        """A button pressed after the ask ended must never leak into a newer ask."""
        cfg = self._tg_cfg()
        an.write_tg_pending("feedface01234567", "Other question?", 60)
        self.tg.queue_update({
            "update_id": 4,
            "callback_query": {
                "id": "cq3",
                "data": "agentbell|aaaaaaaaaaaaaaaa|approved",
            },
        })
        an.bot_poll_once(cfg, poll_timeout=1)
        self.assertIsNone(an.read_tg_answer("aaaaaaaaaaaaaaaa"))
        self.assertIsNone(an.read_tg_answer("feedface01234567"))
        answered = self.tg.last("answerCallbackQuery")
        self.assertEqual(answered["body"]["callback_query_id"], "cq3")
        self.assertIn("expired", answered["body"]["text"])
        an.remove_tg_pending("feedface01234567")

    def test_free_text_attributed_to_newest_pending(self):
        cfg = self._tg_cfg()
        an.write_tg_pending("deadbeef", "Which env?", 60)
        self.tg.queue_update({
            "update_id": 2,
            "message": {"message_id": 6, "chat": {"id": 42}, "text": "staging"},
        })
        an.bot_poll_once(cfg, poll_timeout=1)
        self.assertEqual(an.read_tg_answer("deadbeef"), "staging")
        an.remove_tg_answer("deadbeef")
        an.remove_tg_pending("deadbeef")

    def test_unknown_callback_answered_politely(self):
        cfg = self._tg_cfg()
        self.tg.queue_update({
            "update_id": 3,
            "callback_query": {"id": "cq2", "data": "whatever"},
        })
        an.bot_poll_once(cfg, poll_timeout=1)
        answered = self.tg.last("answerCallbackQuery")
        self.assertEqual(answered["body"]["callback_query_id"], "cq2")
        self.assertEqual(answered["body"]["text"], "unknown action")

    def test_heartbeat_and_lock(self):
        an.write_bot_heartbeat()
        self.assertTrue(an.bot_heartbeat_fresh())
        path = an.acquire_bot_lock()
        self.assertTrue(os.path.exists(path))
        with self.assertRaises(SystemExit):
            an.acquire_bot_lock()  # our own fresh lock -> "already running"
        try:
            os.remove(path)
        except OSError:
            pass


class TestAskParallelChannels(_TelegramFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ntfy = MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()
        super().tearDownClass()

    def _cfg(self):
        return self._tg_cfg(ntfy_url=self.ntfy.url, channels=("ntfy", "telegram"))

    def _run_ask_async(self, cfg, **kwargs):
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.update(
                result=an.run_ask(cfg, print_status=False, **kwargs)
            ),
            daemon=True,
        )
        thread.start()
        return holder, thread

    def _wait_new_request(self, method="sendMessage", timeout=20.0):
        before = len(self.tg.requests)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            new = [r for r in self.tg.requests[before:] if r["method"] == method]
            if new:
                return new[0]
            time.sleep(0.05)
        return None

    def test_ntfy_wins(self):
        cfg = self._cfg()
        tg_before = len(self.tg.requests)
        holder, thread = self._run_ask_async(cfg, message="Deploy?", timeout_seconds=20)
        deadline = time.monotonic() + 5
        while not self.ntfy.posts.get("tgtopic") and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.ntfy.posts.get("tgtopic"))
        # The two channels are published in parallel, so ntfy arriving first
        # says nothing about Telegram having arrived yet - wait for it.
        sent, deadline = None, time.monotonic() + 10
        while sent is None and time.monotonic() < deadline:
            sent = next((r for r in self.tg.requests[tg_before:]
                         if r["method"] == "sendMessage"), None)
            if sent is None:
                time.sleep(0.05)
        self.assertIsNotNone(sent)
        headers = self.ntfy.posts["tgtopic"][-1]["headers"]
        self.assertEqual(headers["Priority"], "4")
        match = an.re.search(r"ID: ([0-9a-f]+)", self.ntfy.posts["tgtopic"][-1]["body"])
        urllib.request.urlopen(
            urllib.request.Request(
                f"{self.ntfy.url}/tgtopic-responses", method="POST",
                data=f"APPROVED {match.group(1)}".encode(),
            )
        ).read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(holder["result"]["approved"])
        self.assertEqual(holder["result"]["channel"], "ntfy")

    def test_telegram_wins(self):
        cfg = self._cfg()
        holder, thread = self._run_ask_async(cfg, message="Deploy?", timeout_seconds=20)
        body = self._wait_new_request("sendMessage")["body"]
        match = an.re.search(r"ID: ([0-9a-f]+)", body["text"])
        an.write_tg_answer(match.group(1), "approved")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(holder["result"]["approved"])
        self.assertEqual(holder["result"]["channel"], "telegram")

    def test_telegram_publish_failure_does_not_block_ntfy(self):
        """A broken telegram channel must not abort an ask that ntfy can answer."""
        old_base = an.TG_API_BASE
        broken = MockTelegram(fail_send=True)
        an.TG_API_BASE = broken.url
        try:
            cfg = self._cfg()
            before = len(self.ntfy.posts.get("tgtopic", []))
            holder, thread = self._run_ask_async(cfg, message="Deploy?", timeout_seconds=20)
            deadline = time.monotonic() + 5
            while len(self.ntfy.posts.get("tgtopic", [])) == before and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertGreater(len(self.ntfy.posts.get("tgtopic", [])), before)
            match = an.re.search(r"ID: ([0-9a-f]+)", self.ntfy.posts["tgtopic"][-1]["body"])
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.ntfy.url}/tgtopic-responses", method="POST",
                    data=f"APPROVED {match.group(1)}".encode(),
                )
            ).read()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertTrue(holder["result"]["approved"])
            self.assertEqual(holder["result"]["channel"], "ntfy")
        finally:
            an.TG_API_BASE = old_base
            broken.stop()


class TestRetryAndQueue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_backoff = an.RETRY_BACKOFF_SECONDS
        an.RETRY_BACKOFF_SECONDS = (0.02, 0.02)

    @classmethod
    def tearDownClass(cls):
        an.RETRY_BACKOFF_SECONDS = cls.old_backoff

    def setUp(self):
        self.ntfy = MockNtfy()
        self.addCleanup(self.ntfy.stop)
        for name in ("queue", "deferred"):
            shutil.rmtree(os.path.join(an.state_dir(), name), ignore_errors=True)

    def test_transient_failure_retried_then_succeeds(self):
        ntfy = MockNtfy(post_503_count=1)
        try:
            cfg = make_config(ntfy.url, topic="retryok")
            result = an.send_notification(cfg, "retry me", priority="normal")
            self.assertTrue(result["ok"])
            self.assertFalse(result.get("queued"))
            self.assertEqual(len(ntfy.posts.get("retryok", [])), 1)
        finally:
            ntfy.stop()

    def test_persistent_failure_queued(self):
        cfg = make_config(self.ntfy.url, topic="down")
        old_url = self.ntfy.url
        self.ntfy.stop()
        try:
            result = an.send_notification(cfg, "while offline", priority="normal")
        finally:
            pass
        self.assertTrue(result["ok"])  # not lost: queued
        self.assertEqual(result.get("queued"), ["ntfy"])
        items = an._read_item_files(an.queue_dir())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][1]["message"], "while offline")
        self.assertIn("queued", an.read_history()[-1]["event"])

    def test_drain_delivers_and_clears(self):
        cfg = make_config(self.ntfy.url, topic="drain")
        an.enqueue_item(cfg, {"message": "late but delivered", "channels": ["ntfy"],
                              "priority": "normal", "event": "notify"})
        stats = an.drain_queue(cfg, limit=None)
        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(an._read_item_files(an.queue_dir()), [])
        posts = self.ntfy.posts["drain"]
        self.assertEqual(posts[-1]["body"], "late but delivered")
        records = an.read_history()
        self.assertTrue(any(r["event"] == "queued_delivered" for r in records))

    def test_drain_permanent_failure_drops(self):
        cfg = make_config(self.ntfy.url, topic="drainperm")
        an.enqueue_item(cfg, {"message": "telegram without license", "channels": ["telegram"],
                              "priority": "normal", "event": "notify"})
        stats = an.drain_queue(cfg, limit=None)
        self.assertEqual(stats["dropped"], 1)
        records = an.read_history()
        self.assertTrue(any(r["event"] == "queue_dropped" for r in records))

    def test_drain_expired_items_dropped(self):
        cfg = make_config(self.ntfy.url, topic="drainexp")
        an.enqueue_item(cfg, {"message": "too old", "channels": ["ntfy"],
                              "priority": "normal", "event": "notify"})
        for _, item in an._read_item_files(an.queue_dir()):
            item["created"] = time.time() - an.QUEUE_MAX_AGE_SECONDS - 60
            with open(os.path.join(an.queue_dir(), f"{item['id']}.json"), "w") as fh:
                json.dump(item, fh)
        stats = an.drain_queue(cfg, limit=None)
        self.assertEqual(stats["dropped"], 1)
        records = an.read_history()
        self.assertTrue(any(r["event"] == "queue_expired" for r in records))

    def test_queue_overflow_drops_oldest(self):
        cfg = make_config(self.ntfy.url, topic="overflow")
        old_max = an.QUEUE_MAX_ITEMS
        an.QUEUE_MAX_ITEMS = 3
        try:
            ids = [an.enqueue_item(cfg, {"message": f"m{i}", "channels": ["ntfy"],
                                         "priority": "normal"}) for i in range(4)]
            items = an._read_item_files(an.queue_dir())
            self.assertEqual(len(items), 3)
            kept = {i["id"] for _, i in items}
            self.assertNotIn(ids[0], kept)
            self.assertIn(ids[3], kept)
            records = an.read_history()
            self.assertTrue(any(r["event"] == "queue_overflow" for r in records))
        finally:
            an.QUEUE_MAX_ITEMS = old_max

    def test_auto_drain_after_successful_send(self):
        cfg = make_config(self.ntfy.url, topic="autodrain")
        an.enqueue_item(cfg, {"message": "queued earlier", "channels": ["ntfy"],
                              "priority": "normal", "event": "notify"})
        result = an.send_notification(cfg, "fresh one", priority="normal")
        self.assertTrue(result["ok"])
        self.assertEqual(an._read_item_files(an.queue_dir()), [])
        bodies = [p["body"] for p in self.ntfy.posts["autodrain"]]
        self.assertIn("fresh one", bodies)
        self.assertIn("queued earlier", bodies)


class TestDeferMode(unittest.TestCase):
    def setUp(self):
        self.ntfy = MockNtfy()
        self.addCleanup(self.ntfy.stop)
        for name in ("queue", "deferred", "ntfy-pending"):
            shutil.rmtree(os.path.join(an.state_dir(), name), ignore_errors=True)

    def _quiet_cfg(self, topic, mode="defer"):
        cfg = make_config(self.ntfy.url, topic=topic)
        cfg.data["quiet_hours"] = [{"start": "00:00", "end": "23:59"}]
        cfg.data["quiet_hours_mode"] = mode
        return cfg

    def test_defer_stores_instead_of_sending(self):
        cfg = self._quiet_cfg("defer1")
        result = an.send_notification(cfg, "night build", priority="low")
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("deferred"))
        self.assertNotIn("defer1", self.ntfy.posts)
        items = an._read_item_files(an.deferred_dir())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][1]["message"], "night build")
        records = an.read_history()
        self.assertTrue(any(r["event"] == "deferred" for r in records))

    def test_suppress_still_default(self):
        cfg = self._quiet_cfg("defer2", mode="suppress")
        result = an.send_notification(cfg, "dropped", priority="low")
        self.assertTrue(result.get("suppressed"))
        self.assertFalse(result.get("deferred"))
        self.assertEqual(an._read_item_files(an.deferred_dir()), [])

    def test_force_bypasses_defer(self):
        cfg = self._quiet_cfg("defer3")
        result = an.send_notification(cfg, "urgent now", priority="low", force=True)
        self.assertFalse(result.get("deferred"))
        self.assertFalse(result.get("suppressed"))
        self.assertEqual(self.ntfy.posts["defer3"][-1]["body"], "urgent now")

    def test_flush_delivers_after_window(self):
        cfg = self._quiet_cfg("defer4")
        an.send_notification(cfg, "morning delivery", priority="low")
        for _, item in an._read_item_files(an.deferred_dir()):
            item["deliver_after"] = time.time() - 1
            with open(os.path.join(an.deferred_dir(), f"{item['id']}.json"), "w") as fh:
                json.dump(item, fh)
        cfg.data["quiet_hours"] = []  # window over
        stats = an.flush_deferred(cfg)
        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(self.ntfy.posts["defer4"][-1]["body"], "morning delivery")
        self.assertEqual(an._read_item_files(an.deferred_dir()), [])
        records = an.read_history()
        self.assertTrue(any(r["event"] == "deferred_delivered" for r in records))

    def test_flush_bundles_many_items(self):
        cfg = self._quiet_cfg("defer5")
        for i in range(4):
            an.defer_item(cfg, f"item {i}", priority="low", channels=["ntfy"])
        for name, item in an._read_item_files(an.deferred_dir()):
            item["deliver_after"] = time.time() - 1
            with open(os.path.join(an.deferred_dir(), name), "w") as fh:
                json.dump(item, fh)
        cfg.data["quiet_hours"] = []
        stats = an.flush_deferred(cfg)
        self.assertEqual(stats["bundled"], 4)
        self.assertEqual(len(self.ntfy.posts.get("defer5", [])), 1)
        body = self.ntfy.posts["defer5"][0]["body"]
        title = self.ntfy.posts["defer5"][0]["headers"]["Title"]
        self.assertIn("4 deferred", title)
        self.assertIn("While you were away", body)
        self.assertIn("item 3", body)

    def test_still_in_quiet_hours_stays_deferred(self):
        cfg = self._quiet_cfg("defer6")
        an.defer_item(cfg, "held", priority="low", channels=["ntfy"])
        for name, item in an._read_item_files(an.deferred_dir()):
            item["deliver_after"] = time.time() - 1
            with open(os.path.join(an.deferred_dir(), name), "w") as fh:
                json.dump(item, fh)
        stats = an.flush_deferred(cfg)  # quiet hours still active
        self.assertEqual(stats["kept"], 1)
        self.assertNotIn("defer6", self.ntfy.posts)
        self.assertEqual(len(an._read_item_files(an.deferred_dir())), 1)

    def test_next_quiet_end(self):
        now = an.datetime.datetime(2026, 8, 14, 23, 0)
        end = an.next_quiet_end([{"start": "22:00", "end": "07:30"}], now)
        end_dt = an.datetime.datetime.fromtimestamp(end)
        self.assertEqual((end_dt.day, end_dt.hour, end_dt.minute), (15, 7, 30))
        now = an.datetime.datetime(2026, 8, 14, 2, 0)
        end = an.next_quiet_end([{"start": "22:00", "end": "07:30"}], now)
        end_dt = an.datetime.datetime.fromtimestamp(end)
        self.assertEqual((end_dt.day, end_dt.hour, end_dt.minute), (14, 7, 30))
        now = an.datetime.datetime(2026, 8, 14, 13, 30)
        end = an.next_quiet_end([{"start": "13:00", "end": "14:00"}], now)
        end_dt = an.datetime.datetime.fromtimestamp(end)
        self.assertEqual((end_dt.day, end_dt.hour, end_dt.minute), (14, 14, 0))
        now = an.datetime.datetime(2026, 8, 14, 12, 0)
        self.assertIsNone(an.next_quiet_end([{"start": "13:00", "end": "14:00"}], now))

    def test_ask_is_never_deferred(self):
        cfg = self._quiet_cfg("deferask")
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.update(
                result=an.run_ask(cfg, "Deploy?", timeout_seconds=3, print_status=False)
            ),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 5
        while not self.ntfy.posts.get("deferask") and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.ntfy.posts.get("deferask"))  # published despite quiet hours
        thread.join(timeout=10)


class TestApprovalHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ntfy = MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def setUp(self):
        shutil.rmtree(os.path.join(an.state_dir(), "ntfy-pending"), ignore_errors=True)
        with contextlib.suppress(OSError):
            os.remove(an._consumed_path("ntfy"))

    def _run_ask_async(self, cfg, **kwargs):
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.update(result=an.run_ask(cfg, print_status=False, **kwargs)),
            daemon=True,
        )
        thread.start()
        return holder, thread

    def _wait_posts(self, topic, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.ntfy.posts.get(topic, [])) >= count:
                return self.ntfy.posts[topic]
            time.sleep(0.05)
        raise AssertionError(f"expected {count} posts on {topic}")

    def test_request_id_is_high_entropy(self):
        cfg = make_config(self.ntfy.url, topic="rid")
        holder, thread = self._run_ask_async(cfg, message="Q?", timeout_seconds=20)
        posts = self._wait_posts("rid", 1)
        match = an.re.search(r"ID: ([0-9a-f]+)", posts[-1]["body"])
        self.assertEqual(len(match.group(1)), 16)
        urllib.request.urlopen(
            urllib.request.Request(f"{self.ntfy.url}/rid-responses", method="POST",
                                   data=f"APPROVED {match.group(1)}".encode())
        ).read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(holder["result"]["approved"])

    def test_parallel_asks_do_not_crosstalk(self):
        cfg = make_config(self.ntfy.url, topic="par")
        holder_a, thread_a = self._run_ask_async(cfg, message="First question?",
                                                 timeout_seconds=30)
        posts = self._wait_posts("par", 1)
        id_a = an.re.search(r"ID: ([0-9a-f]+)", posts[-1]["body"]).group(1)
        holder_b, thread_b = self._run_ask_async(cfg, message="Second question?",
                                                 timeout_seconds=30)
        posts = self._wait_posts("par", 2)
        id_b = an.re.search(r"ID: ([0-9a-f]+)", posts[-1]["body"]).group(1)
        self.assertNotEqual(id_a, id_b)
        # free text goes to the newest open question (B)
        urllib.request.urlopen(
            urllib.request.Request(f"{self.ntfy.url}/par-responses", method="POST",
                                   data=b"staging")
        ).read()
        thread_b.join(timeout=10)
        self.assertFalse(thread_b.is_alive())
        self.assertEqual(holder_b["result"]["answer"], "staging")
        self.assertTrue(thread_a.is_alive())  # A unaffected
        # a button answer for A still reaches A
        urllib.request.urlopen(
            urllib.request.Request(f"{self.ntfy.url}/par-responses", method="POST",
                                   data=f"APPROVED {id_a}".encode())
        ).read()
        thread_a.join(timeout=10)
        self.assertFalse(thread_a.is_alive())
        self.assertTrue(holder_a["result"]["approved"])
        self.assertEqual(holder_a["result"].get("channel"), "ntfy")

    def test_free_text_claimed_by_another_ask_is_not_answered_twice(self):
        """The case the newest-open check alone cannot decide.

        On a slow box the ask that took a free-text reply is already finished
        - and its pending marker deleted - by the time a second ask's poll
        reaches the same message. That ask then finds itself "newest open" and
        answers the very same reply. The claim recorded before the marker
        disappeared is what stops it.
        """
        cfg = make_config(self.ntfy.url, topic="claimed")
        holder, thread = self._run_ask_async(cfg, message="Which env?", timeout_seconds=20)
        posts = self._wait_posts("claimed", 1)
        approval_id = an.re.search(r"ID: ([0-9a-f]+)", posts[-1]["body"]).group(1)
        # The other, newer ask: it takes the reply and shuts down (marker gone)
        # before the message is ever handed to the ask above.
        other_id = "b" * 16
        an.write_ntfy_pending(other_id, "Second question?", 20)
        other = an.ApprovalWaiter(cfg, "claimed-responses", 20, approval_id=other_id)
        other._offer("race-msg-1", "race-claimed-reply")
        self.assertEqual(other.messages.get_nowait(), "race-claimed-reply")
        an.remove_ntfy_pending(other_id)
        self.ntfy.inject("claimed-responses", "race-claimed-reply", "race-msg-1")
        deadline = time.monotonic() + 15
        ignored = False
        while time.monotonic() < deadline and not ignored and "result" not in holder:
            ignored = any(
                r.get("event") == "stale_answer" and r.get("text") == "race-claimed-reply"
                for r in an.read_history(limit=200))
            time.sleep(0.05)
        self.assertNotIn("result", holder,
                         "a reply another ask had already claimed answered this one too")
        self.assertTrue(ignored, "the claimed reply was never processed")
        self.assertTrue(thread.is_alive())        # still waiting, not answered
        # its own button answer still gets through
        urllib.request.urlopen(
            urllib.request.Request(f"{self.ntfy.url}/claimed-responses", method="POST",
                                   data=f"APPROVED {approval_id}".encode())
        ).read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(holder["result"]["approved"])

    def test_claim_consumed_is_once_only_and_bounded(self):
        with contextlib.suppress(OSError):
            os.remove(an._consumed_path("claimtest"))
        self.assertTrue(an.claim_consumed("claimtest", "abc"))
        self.assertFalse(an.claim_consumed("claimtest", "abc"))
        self.assertTrue(an.claim_consumed("claimtest", "def"))
        self.assertTrue(an.claim_consumed("claimtest", None))   # nothing to key on
        for i in range(an.CONSUMED_KEEP_LINES + 20):
            an.claim_consumed("claimtest", f"id-{i}")
        path = an._consumed_path("claimtest")
        self.assertLessEqual(len(an._read_consumed("claimtest")), an.CONSUMED_KEEP_LINES)
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)
        newest = f"id-{an.CONSUMED_KEEP_LINES + 19}"
        self.assertFalse(an.claim_consumed("claimtest", newest))   # newest kept
        os.remove(path)

    def test_stale_button_answer_ignored(self):
        cfg = make_config(self.ntfy.url, topic="staleid")
        holder, thread = self._run_ask_async(cfg, message="Q?", timeout_seconds=20)
        posts = self._wait_posts("staleid", 1)
        approval_id = an.re.search(r"ID: ([0-9a-f]+)", posts[-1]["body"]).group(1)
        urllib.request.urlopen(
            urllib.request.Request(f"{self.ntfy.url}/staleid-responses", method="POST",
                                   data=b"APPROVED aaaaaaaaaaaaaaaa")
        ).read()
        time.sleep(1.0)
        self.assertTrue(thread.is_alive())  # still waiting
        urllib.request.urlopen(
            urllib.request.Request(f"{self.ntfy.url}/staleid-responses", method="POST",
                                   data=f"APPROVED {approval_id}".encode())
        ).read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(holder["result"]["approved"])
        records = an.read_history()
        self.assertTrue(any(r["event"] == "stale_answer" for r in records))

    def test_suggested_topic_is_high_entropy(self):
        topic = an.suggest_topic()
        an.validate_topic(topic)
        self.assertGreaterEqual(len(topic), 16)
        suffix = topic.rsplit("-", 1)[1]
        self.assertGreaterEqual(len(suffix), 32)
        self.assertIsNotNone(an.re.fullmatch(r"[0-9a-f]+", suffix))


class TestQueueList(unittest.TestCase):
    def setUp(self):
        for name in ("queue", "deferred"):
            shutil.rmtree(os.path.join(an.state_dir(), name), ignore_errors=True)

    def test_format_age(self):
        self.assertEqual(an.format_age(30), "30s")
        self.assertEqual(an.format_age(59), "59s")
        self.assertEqual(an.format_age(720), "12m")
        self.assertEqual(an.format_age(7200), "2h")
        self.assertEqual(an.format_age(2 * 86400), "2d")

    def test_queue_list_data(self):
        cfg = make_config("http://x")
        an.enqueue_item(cfg, {"message": "hello", "channels": ["ntfy"],
                              "priority": "normal", "event": "notify"})
        an.defer_item(cfg, "later", priority="low", channels=["ntfy"])
        data = an.queue_list_data()
        self.assertEqual(len(data["queue"]), 1)
        self.assertEqual(data["queue"][0]["message"], "hello")
        self.assertLess(data["queue"][0]["age_seconds"], 60)
        self.assertEqual(data["queue"][0]["priority"], "normal")
        self.assertEqual(data["queue"][0]["attempts"], 0)
        self.assertEqual(len(data["deferred"]), 1)
        self.assertEqual(data["deferred"][0]["message"], "later")
        self.assertGreater(data["deferred"][0]["due_in_seconds"], -60)
        self.assertIn("deliver_after" if False else "due_in_seconds", data["deferred"][0])

    def test_queue_list_data_empty(self):
        data = an.queue_list_data()
        self.assertEqual(data, {"queue": [], "deferred": []})

    def test_print_queue_list(self):
        cfg = make_config("http://x")
        an.enqueue_item(cfg, {"message": "queued thing", "channels": ["ntfy"],
                              "priority": "high", "event": "notify"})
        an.defer_item(cfg, "held thing", priority="low", channels=["ntfy"])
        import io as _io
        from contextlib import redirect_stdout
        buffer = _io.StringIO()
        with redirect_stdout(buffer):
            an.print_queue_list(an.queue_list_data())
        out = buffer.getvalue()
        self.assertIn("queued thing", out)
        self.assertIn("held thing", out)
        self.assertIn("queue:", out)
        self.assertIn("deferred:", out)

    def test_print_queue_list_empty(self):
        import io as _io
        from contextlib import redirect_stdout
        buffer = _io.StringIO()
        with redirect_stdout(buffer):
            an.print_queue_list({"queue": [], "deferred": []})
        self.assertIn("queue:    empty", buffer.getvalue())
        self.assertIn("deferred: empty", buffer.getvalue())

    def test_cli_queue_list(self):
        cfg = make_config("http://x")
        an.enqueue_item(cfg, {"message": "cli visible", "channels": ["ntfy"],
                              "priority": "normal", "event": "notify"})
        parser = an.build_parser()
        import io as _io
        from contextlib import redirect_stdout
        buffer = _io.StringIO()
        with redirect_stdout(buffer):
            parser.parse_args(["queue", "list"]).func(
                parser.parse_args(["queue", "list"])
            )
        self.assertIn("cli visible", buffer.getvalue())


class TestPurge(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.project = tempfile.mkdtemp()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.old_argv0 = sys.argv[0]
        sys.argv[0] = "/usr/local/bin/agentbell"

    def tearDown(self):
        if self.old_home:
            os.environ["HOME"] = self.old_home
        else:
            os.environ.pop("HOME", None)
        sys.argv[0] = self.old_argv0
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.project, ignore_errors=True)
        shutil.rmtree(an.state_dir(), ignore_errors=True)
        shutil.rmtree(an.config_dir(), ignore_errors=True)

    def _write(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_purge_report_and_apply_removes_own_stuff_only(self):
        # config + state (env-based dirs shared by the test suite)
        os.makedirs(an.config_dir(), exist_ok=True)
        self._write(an.config_path(), json.dumps({"ntfy": {"topic": "t"}, "license": "AB1-x"}))
        os.makedirs(an.state_dir(), exist_ok=True)
        self._write(an.history_path(), '{"event": "notify"}\n')
        os.makedirs(an.queue_dir(), exist_ok=True)
        self._write(os.path.join(an.queue_dir(), "x.json"),
                    json.dumps({"id": "x", "created": time.time(), "message": "m"}))
        # claude hooks: ours + a user hook that must survive
        claude = an.claude_settings_path()
        self._write(claude, json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{"type": "command",
                                     "command": "agentbell hook run_completed --agent claude"}]}],
                "PreToolUse": [{"hooks": [{"type": "command", "command": "lint.sh"}]}],
            },
        }))
        # claude MCP: ours + a foreign server that must survive
        claude_json = os.path.join(self.home, ".claude.json")
        self._write(claude_json, json.dumps({
            "mcpServers": {
                "agentbell": {"command": "agentbell", "args": ["mcp"]},
                "other": {"command": "elsewhere"},
            },
        }))
        # opencode project block + MCP entry
        agents_md = os.path.join(self.project, "AGENTS.md")
        self._write(agents_md, f"# Rules\n{an.BLOCK_START}\ninstructions\n{an.BLOCK_END}\n")
        oc_json = os.path.join(self.project, "opencode.json")
        self._write(oc_json, json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "mcp": {"agentbell": {"command": ["agentbell", "mcp"]}},
        }))
        # codex hooks + MCP block
        codex = an.codex_config_path()
        self._write(codex, (
            "[model]\nprovider = \"openai\"\n"
            + an.TOML_START + "\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
            'type = "command"\ncommand = "agentbell hook run_completed --agent codex"\n'
            "async = true\n" + an.TOML_END + "\n"
            "[mcp_servers.agentbell]\ncommand = \"agentbell\"\nargs = [\"mcp\"]\n"
        ))

        report = an.purge_report(project=self.project)
        kinds = {e["kind"] for e in report["entries"]}
        self.assertIn("config", kinds)
        self.assertIn("state", kinds)
        self.assertIn("hooks", kinds)
        self.assertIn("mcp", kinds)
        # apply only entries that touch our temp dirs or the test state/config dirs
        for entry in report["entries"]:
            label = entry["label"]
            if any(path in label for path in (self.home, self.project,
                                              an.config_dir(), an.state_dir())):
                self.assertTrue(entry["apply"](), f"apply failed for {label}")

        self.assertFalse(os.path.exists(an.config_dir()))
        self.assertFalse(os.path.exists(an.state_dir()))
        # user hook preserved, ours removed
        with open(claude) as fh:
            data = json.load(fh)
        self.assertNotIn("Stop", data.get("hooks", {}))
        self.assertIn("PreToolUse", data["hooks"])
        # foreign MCP server preserved, ours removed
        with open(claude_json) as fh:
            data = json.load(fh)
        self.assertNotIn("agentbell", data["mcpServers"])
        self.assertIn("other", data["mcpServers"])
        # opencode: block removed, rest of file kept
        with open(agents_md) as fh:
            text = fh.read()
        self.assertNotIn(an.BLOCK_START, text)
        self.assertIn("# Rules", text)
        with open(oc_json) as fh:
            data = json.load(fh)
        self.assertNotIn("agentbell", data.get("mcp", {}))
        self.assertIn("$schema", data)
        # codex: hook block + MCP block removed, foreign section kept
        with open(codex) as fh:
            text = fh.read()
        self.assertNotIn(an.TOML_START, text)
        self.assertNotIn("[mcp_servers.agentbell]", text)
        self.assertIn("[model]", text)

    def test_purge_second_run_reports_nothing(self):
        report = an.purge_report(project=self.project)
        entries = report["entries"]
        for entry in entries:
            entry["apply"]()
        report2 = an.purge_report(project=self.project)
        self.assertEqual(report2["entries"], [])

    def test_standalone_binary_detected_and_removed(self):
        bin_dir = os.path.join(self.home, ".local", "bin")
        script = os.path.join(bin_dir, "agentbell")
        self._write(script, "#!/usr/bin/env python3\n# agentbell\nprint('hi')\n")
        old_xdg = os.environ.get("XDG_BIN_HOME")
        os.environ["XDG_BIN_HOME"] = bin_dir
        try:
            report = an.purge_report(project=self.project)
            binary = [e for e in report["entries"] if e["kind"] == "binary"]
            self.assertTrue(binary, "standalone binary not detected")
            self.assertTrue(any(script in e["label"] for e in binary))
            for entry in binary:
                if script in entry["label"]:
                    self.assertTrue(entry["apply"]())
            self.assertFalse(os.path.exists(script))
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_BIN_HOME", None)
            else:
                os.environ["XDG_BIN_HOME"] = old_xdg

    def test_foreign_binary_not_removed(self):
        bin_dir = os.path.join(self.home, ".local", "bin")
        script = os.path.join(bin_dir, "agentbell")
        self._write(script, "#!/bin/sh\necho some other tool\n")
        old_xdg = os.environ.get("XDG_BIN_HOME")
        os.environ["XDG_BIN_HOME"] = bin_dir
        try:
            report = an.purge_report(project=self.project)
            self.assertFalse(any(e["kind"] == "binary" for e in report["entries"]))
            self.assertTrue(os.path.exists(script))
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_BIN_HOME", None)
            else:
                os.environ["XDG_BIN_HOME"] = old_xdg

    def test_cursor_rule_removed(self):
        rule = os.path.join(self.project, ".cursor", "rules", "agentbell.mdc")
        self._write(rule, "---\nalwaysApply: true\n---\n# agentbell\nnotify the user\n")
        report = an.purge_report(project=self.project)
        entry = next(e for e in report["entries"] if "agentbell.mdc" in e["label"])
        self.assertTrue(entry["apply"]())
        self.assertFalse(os.path.exists(rule))

    def test_cmd_uninstall_dry_run_deletes_nothing(self):
        import io as _io
        from contextlib import redirect_stdout
        parser = an.build_parser()
        args = parser.parse_args(["uninstall"])
        buffer = _io.StringIO()
        with redirect_stdout(buffer):
            args.func(args)
        out = buffer.getvalue()
        self.assertIn("dry run", out)
        self.assertIn("--yes", out)

    def test_purge_env_override_warning(self):
        old = os.environ.get(an.CONFIG_DIR_ENV)
        os.environ[an.CONFIG_DIR_ENV] = os.path.join(self.home, "cfg")
        try:
            report = an.purge_report(project=self.project)
            self.assertTrue(any("AGENTBELL_*" in w for w in report["warnings"]))
        finally:
            if old is None:
                os.environ.pop(an.CONFIG_DIR_ENV, None)
            else:
                os.environ[an.CONFIG_DIR_ENV] = old


class TestBotStatus(_TelegramFixture):
    def test_status_running(self):
        cfg = self._tg_cfg()
        an.write_bot_heartbeat()
        import io as _io
        from contextlib import redirect_stdout
        buffer = _io.StringIO()
        with redirect_stdout(buffer):
            an.print_bot_status(cfg)
        out = buffer.getvalue()
        self.assertIn("running", out)
        self.assertIn("lock", out)

    def test_status_stale_lock_and_error(self):
        cfg = self._tg_cfg()
        an.write_bot_error("telegram says a webhook is active")
        lock = os.path.join(an.state_dir(), "bot.lock")
        with open(lock, "w") as fh:
            json.dump({"pid": 99999999, "ts": time.time()}, fh)
        import io as _io
        from contextlib import redirect_stdout
        buffer = _io.StringIO()
        with redirect_stdout(buffer):
            an.print_bot_status(cfg)
        out = buffer.getvalue()
        self.assertIn("NOT running", out)
        self.assertIn("webhook", out)
        self.assertIn("stale", out)
        os.remove(lock)


class TestSecrets(unittest.TestCase):
    def test_config_show_redacts_every_credential(self):
        data = {
            "ntfy": {"server": "https://ntfy.example", "topic": "t", "auth": "bob:hunter2"},
            "telegram": {"bot_token": "123456:AAHsuperSecretToken", "chat_id": 42},
            "webhook": {"listen": "0.0.0.0", "port": 8756, "token": "webhook-secret-token"},
            "license": "AB1-PAYLOADPAYLOAD-SIGNATURE",
        }
        safe = json.dumps(an.redacted_config(data))
        for secret in ("hunter2", "AAHsuperSecretToken", "webhook-secret-token",
                       "PAYLOADPAYLOAD", "SIGNATURE"):
            self.assertNotIn(secret, safe, f"{secret} leaked into 'config show'")
        self.assertIn("bob:", safe)          # enough context to recognise it
        self.assertIn("0.0.0.0", safe)       # non-secrets stay visible

    @unittest.skipIf(os.name == "nt", "Unix file permissions not applicable on Windows")
    def test_config_file_is_owner_only(self):
        tmp = tempfile.mkdtemp()
        cfg = an.Config(dict(an.default_config()), path=os.path.join(tmp, "sub", "config.json"))
        cfg.data["license"] = "AB1-secret"
        cfg.save()
        self.assertEqual(os.stat(cfg.path).st_mode & 0o777, 0o600)
        shutil.rmtree(tmp)

    def test_applescript_and_powershell_quoting(self):
        self.assertEqual(an._applescript_string('say "hi" \\ bye'), '"say \\"hi\\" \\\\ bye"')
        self.assertEqual(an._powershell_string("it's"), "'it''s'")

    def test_toml_string_escapes_windows_paths(self):
        self.assertEqual(an.toml_string(r"C:\Users\me\agentbell.exe"),
                         '"C:\\\\Users\\\\me\\\\agentbell.exe"')


class TestCliWiring(unittest.TestCase):
    def test_bare_mcp_command_runs_the_server(self):
        """`mcp add` registers `<binary> mcp` - that exact call must work."""
        args = an.build_parser().parse_args(["mcp"])
        self.assertIs(args.func, an.cmd_mcp)
        self.assertIsNone(getattr(args, "sub", None))

    def test_every_subcommand_dispatches(self):
        parser = an.build_parser()
        for argv in (["notify", "x"], ["hook", "run_completed"], ["ask", "q"], ["watch", "--", "true"],
                     ["test"], ["doctor"], ["hooks"], ["hooks", "status"], ["server"], ["bot"],
                     ["bot", "status"], ["mcp"], ["mcp", "run"], ["mcp", "add"], ["history"],
                     ["queue"], ["queue", "list"], ["queue", "flush"], ["uninstall"],
                     ["config", "show"], ["config", "path"], ["config", "set", "k", "v"],
                     ["bot", "install-service"], ["license", "status"]):
            args = parser.parse_args(argv)
            self.assertTrue(callable(getattr(args, "func", None)), f"no func for {argv}")

    def test_test_command_reports_failure(self):
        """`agentbell test` must exit non-zero when nothing was delivered."""
        cfg = make_config("http://127.0.0.1:1")   # nothing listening
        original = an.Config
        an.Config = lambda *a, **kw: cfg
        try:
            code = an.cmd_test(_Args(no_wait=True))
        finally:
            an.Config = original
        self.assertEqual(code, 1)


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestDoctor(unittest.TestCase):
    def test_doctor_gives_a_powershell_path_fix_on_windows(self):
        cfg = an.Config(an.default_config(), path=os.path.join(tempfile.mkdtemp(), "config.json"))
        original_system = an.platform.system
        original_which = an.shutil.which
        an.platform.system = lambda: "Windows"
        an.shutil.which = lambda command: None
        try:
            install = next(c for c in an.doctor_checks(cfg) if c["name"] == "install")
        finally:
            an.platform.system = original_system
            an.shutil.which = original_which
        self.assertIn("sysconfig.get_path('scripts', scheme='nt_user')", install["fix"])
        self.assertIn("SetEnvironmentVariable", install["fix"])
        self.assertIn('SetEnvironmentVariable("Path", "$userPath;$scripts", "User")', install["fix"])
        self.assertIn("restart PowerShell", install["fix"])
        self.assertNotIn(".bashrc", install["fix"])

    def test_doctor_flags_missing_config_with_a_fix(self):
        tmp = tempfile.mkdtemp()
        cfg = an.Config(an.default_config(), path=os.path.join(tmp, "config.json"))
        checks = an.doctor_checks(cfg)
        by_name = {c["name"]: c for c in checks}
        self.assertEqual(by_name["config"]["status"], an.FAIL)
        self.assertIn("init", by_name["config"]["fix"])
        self.assertEqual(by_name["ntfy topic"]["status"], an.FAIL)
        shutil.rmtree(tmp)

    def test_doctor_warns_while_quiet_hours_are_active(self):
        cfg = make_config("http://127.0.0.1:1")
        now = an.datetime.datetime.now()
        cfg.data["quiet_hours"] = [{
            "start": (now - an.datetime.timedelta(minutes=5)).strftime("%H:%M"),
            "end": (now + an.datetime.timedelta(minutes=30)).strftime("%H:%M"),
        }]
        quiet = [c for c in an.doctor_checks(cfg) if c["name"] == "quiet hours"][0]
        self.assertEqual(quiet["status"], an.WARN)
        self.assertIn("ACTIVE", quiet["detail"])

    def test_doctor_reports_an_invalid_license_key(self):
        cfg = make_config("http://127.0.0.1:1")
        cfg.data["license"] = "AB1-NOTAREALKEY-SIGNATURE"
        license_check = [c for c in an.doctor_checks(cfg) if c["name"] == "license"][0]
        self.assertEqual(license_check["status"], an.FAIL)
        self.assertIsNotNone(license_check["fix"])


class TestMcpClients(unittest.TestCase):
    def test_all_clients_register_and_purge(self):
        tmp = tempfile.mkdtemp()
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        original = os.path.expanduser
        os.path.expanduser = lambda p: p.replace("~", home, 1) if p.startswith("~") else p
        env_keys = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "APPDATA")}
        os.environ["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
        try:
            rows = dict(an.mcp_add_configs("/opt/bin/agentbell",
                                           clients=["claude-desktop", "vscode", "gemini",
                                                    "qwen-code", "cursor", "opencode",
                                                    "chatgpt-desktop", "kimi"]))
            for client, status in rows.items():
                self.assertNotIn("FAILED", status, f"{client}: {status}")
            with open(an.claude_desktop_config_path(), encoding="utf-8") as fh:
                desktop = json.load(fh)
            self.assertEqual(desktop["mcpServers"]["agentbell"]["args"], ["mcp"])
            with open(an.vscode_mcp_path(), encoding="utf-8") as fh:
                vscode = json.load(fh)
            self.assertEqual(vscode["servers"]["agentbell"]["type"], "stdio")
            with open(an.kimi_mcp_path(), encoding="utf-8") as fh:
                kimi = json.load(fh)
            self.assertEqual(kimi["mcpServers"]["agentbell"]["command"], "/opt/bin/agentbell")
            with open(an.qwen_settings_path(), encoding="utf-8") as fh:
                qwen = json.load(fh)
            self.assertEqual(qwen["mcpServers"]["agentbell"]["args"], ["mcp"])
            # ChatGPT Desktop shares the Codex config
            with open(an.codex_config_path()) as fh:
                self.assertIn("[mcp_servers.agentbell]", fh.read())
            labels = [e["label"] for e in an._mcp_entries(tmp)]
            self.assertTrue(any("Claude Desktop" in l for l in labels), labels)
            self.assertTrue(any("VS Code" in l for l in labels), labels)
            self.assertTrue(any("Kimi Code" in l for l in labels), labels)
            self.assertTrue(any("Qwen Code" in l for l in labels), labels)
            for entry in an._mcp_entries(tmp):
                entry["apply"]()
            with open(an.vscode_mcp_path(), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {})
        finally:
            os.path.expanduser = original
            for key, value in env_keys.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(tmp)

    def test_doctor_reports_qwen_mcp(self):
        tmp = tempfile.mkdtemp()
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        original = os.path.expanduser
        os.path.expanduser = lambda p: p.replace("~", home, 1) if p.startswith("~") else p
        env_keys = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "APPDATA")}
        os.environ["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
        try:
            an.mcp_add_configs("/opt/bin/agentbell", clients=["qwen-code"])
            cfg = make_config("http://127.0.0.1:1")
            mcp_check = [c for c in an.doctor_checks(cfg) if c["name"] == "mcp"][0]
            self.assertIn("qwen-code", mcp_check["detail"])
        finally:
            os.path.expanduser = original
            for key, value in env_keys.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(tmp)

    def test_existing_servers_are_kept(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "mcp.json")
        with open(path, "w") as fh:
            json.dump({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}, fh)
        an._mcp_upsert_json(path, "mcpServers", {"command": "an", "args": ["mcp"]})
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("other", data["mcpServers"])
        self.assertEqual(data["theme"], "dark")
        an._remove_mcp_server_key(path, "mcpServers")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["mcpServers"], {"other": {"command": "x"}})
        shutil.rmtree(tmp)

    def test_broken_json_is_not_overwritten(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "mcp.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(RuntimeError):
            an._mcp_upsert_json(path, "mcpServers", {"command": "an"})
        with open(path) as fh:
            self.assertEqual(fh.read(), "{not json")
        shutil.rmtree(tmp)


class TestForeignTelegramAnswer(unittest.TestCase):
    def test_callback_from_another_chat_is_ignored(self):
        cfg = make_config("http://x")
        cfg.data["telegram"] = {"bot_token": "t", "chat_id": 4242}
        approval_id = "abcdef1234567890"
        an.write_tg_pending(approval_id, "Deploy?", 60)
        try:
            an.handle_bot_update(cfg, {"callback_query": {
                "id": "cb", "data": f"agentbell|{approval_id}|approved",
                "from": {"id": 9999}, "message": {"chat": {"id": 9999}, "message_id": 1},
            }})
            self.assertIsNone(an.read_tg_answer(approval_id),
                              "a stranger must not be able to approve")
        finally:
            an.remove_tg_pending(approval_id)
            an.remove_tg_answer(approval_id)


class TestAuditRegressions(unittest.TestCase):
    """One test per confirmed audit finding, so none of them can come back."""

    def test_the_environment_cannot_make_an_invalid_key_verify(self):
        """The env var signs; only LICENSE_PUBLIC_KEY decides what is accepted.

        The old HMAC scheme let one env var pick the *verifier's* secret, which
        unlocked the paid tier without any reverse engineering. Verification
        now reads a hardcoded public key and nothing else, so an attacker's
        key pair - however it is announced to the process - is just noise.
        """
        attacker = os.urandom(32)
        forged = an.make_license_key("mallory", seed=attacker)
        announced = an._ed25519_public_key(attacker).hex()
        planted = {
            "AGENTBELL_LICENSE_SECRET": attacker.hex(),
            "AGENTBELL_LICENSE_PUBLIC_KEY": announced,
            "AGENTBELL_PUBLIC_KEY": announced,
            "LICENSE_PUBLIC_KEY": announced,
        }
        saved = {name: os.environ.get(name) for name in planted}
        os.environ.update(planted)
        an._LICENSE_CACHE.clear()
        try:
            self.assertFalse(an.check_license_key(forged),
                             "a self-signed key must not unlock premium")
            # the key is well formed - only the signature is the wrong one
            with dev_keypair(attacker):
                self.assertTrue(an.check_license_key(forged))
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            an._LICENSE_CACHE.clear()

    def test_license_is_valid_through_the_whole_expiry_day(self):
        today = an.datetime.date.today().isoformat()
        yesterday = (an.datetime.date.today() - an.datetime.timedelta(days=1)).isoformat()
        with dev_keypair() as seed:
            self.assertTrue(an.check_license_key(
                an.make_license_key("c", expiry=today, seed=seed)))
            self.assertFalse(an.check_license_key(
                an.make_license_key("c", expiry=yesterday, seed=seed)))

    @unittest.skipUnless(HAS_TOMLLIB, "tomllib requires 3.11+")
    def test_codex_feature_flag_stays_top_level(self):
        """A bare key after a [table] would belong to that table, not the root."""
        import tomllib
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.toml")
        with open(path, "w") as fh:
            fh.write('model = "gpt-5"\n[model_providers.oss]\nname = "x"\n')
        original = an.codex_config_path
        an.codex_config_path = lambda: path
        try:
            an.install_codex_hooks()
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            self.assertIs(data["features"]["hooks"], True)
            self.assertIn("Stop", data["hooks"])
            self.assertEqual(data["model_providers"]["oss"], {"name": "x"})
            an.uninstall_codex_hooks()
            with open(path, "rb") as fh:
                self.assertEqual(tomllib.load(fh),
                                 {"model": "gpt-5", "model_providers": {"oss": {"name": "x"}}})
        finally:
            an.codex_config_path = original
            shutil.rmtree(tmp)

    @unittest.skipUnless(HAS_TOMLLIB, "tomllib requires 3.11+")
    def test_codex_install_repairs_a_pre_1_3_0_config(self):
        """The old install put the flag where TOML scoped it to the last table."""
        import tomllib
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.toml")
        with open(path, "w") as fh:
            fh.write('model = "gpt-5"\n[model_providers.oss]\nname = "x"\n\n'
                     'features.hooks = true\n\n' + an.TOML_START + "\n"
                     '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
                     'command = "/old/agentbell hook run_completed --agent codex"\n'
                     + an.TOML_END + "\n")
        original = an.codex_config_path
        an.codex_config_path = lambda: path
        try:
            with open(path, "rb") as fh:
                self.assertIsNone(tomllib.load(fh).get("features"))   # broken before
            result = an.install_codex_hooks()
            self.assertTrue(result["changed"])
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            self.assertIs(data["features"]["hooks"], True)
            self.assertEqual(data["model_providers"]["oss"], {"name": "x"})
            self.assertFalse(an.install_codex_hooks()["changed"])     # idempotent
        finally:
            an.codex_config_path = original
            shutil.rmtree(tmp)

    def test_answer_parsing_keeps_instructions_and_fails_closed_on_no(self):
        self.assertEqual(an._parse_answer("yes"), ("approved", ""))
        self.assertEqual(an._parse_answer("APPROVED a1b2c3d4"), ("approved", ""))
        self.assertEqual(an._parse_answer("yes, but use staging"),
                         ("answer", "yes, but use staging"))
        self.assertEqual(an._parse_answer("no, not yet")[0], "denied")
        self.assertEqual(an._parse_answer("DENIED whatever")[0], "denied")
        self.assertEqual(an._parse_answer("")[0], "denied")
        self.assertEqual(an._parse_answer("use the staging cluster"),
                         ("answer", "use the staging cluster"))

    def test_telegram_token_never_reaches_an_error_message(self):
        url = "https://api.telegram.org/bot123456789:AAHsuperSecretToken/sendMessage"
        self.assertNotIn("AAHsuperSecretToken", an.safe_url(url))
        self.assertIn("<redacted>", an.safe_url(url))

    def test_header_with_newline_does_not_crash(self):
        self.assertEqual(an._latin1_header("a\nb\tc"), "a b c")
        self.assertEqual(an._latin1_header(None), "")

    def test_broken_quiet_hours_config_does_not_crash(self):
        for broken in ("22:00-07:30", ["22:00-07:30"], {"start": "22:00", "end": "07:30"},
                       ["nonsense"], [{"start": "25:99", "end": "x"}], None, 42):
            an.in_quiet_hours(broken)          # must not raise
            an.next_quiet_end(broken)
        self.assertEqual(an.normalize_quiet_hours("22:00-07:30"),
                         [{"start": "22:00", "end": "07:30"}])
        self.assertEqual(an.normalize_quiet_hours(["nope"]), [])

    def test_reinstalling_after_the_binary_moved_replaces_the_hook(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "settings.json")
        original = an.claude_settings_path
        an.claude_settings_path = lambda: path
        cmd = an._hook_command
        try:
            an._hook_command = lambda e, a: f"/old/path/agentbell hook {e} --agent {a}"
            an.install_hooks("claude")
            an._hook_command = lambda e, a: f"/new/path/agentbell hook {e} --agent {a}"
            an.install_hooks("claude")
            with open(path) as fh:
                commands = [h["command"] for groups in json.load(fh)["hooks"].values()
                            for g in groups for h in g["hooks"]]
            self.assertFalse([c for c in commands if "/old/path" in c],
                             "stale hook left behind -> two notifications per turn")
            self.assertTrue([c for c in commands if "/new/path" in c])
        finally:
            an._hook_command = cmd
            an.claude_settings_path = original
            shutil.rmtree(tmp)

    def test_a_killed_sender_gives_its_item_back(self):
        directory = an.queue_dir()
        os.makedirs(directory, exist_ok=True)
        for name in os.listdir(directory):
            os.remove(os.path.join(directory, name))
        orphan = os.path.join(directory, "deadbeef.json.sending")
        with open(orphan, "w") as fh:
            json.dump({"id": "deadbeef", "message": "stranded", "created": time.time()}, fh)
        os.utime(orphan, (time.time() - 3600, time.time() - 3600))
        self.assertEqual(an._reclaim_stale(directory), 1)
        self.assertEqual([i["message"] for i in an.queue_list_data()["queue"]], ["stranded"])
        for name in os.listdir(directory):
            os.remove(os.path.join(directory, name))

    def test_topic_leaves_room_for_the_response_topic(self):
        self.assertEqual(an.MAX_TOPIC_LEN, 54)
        an.validate_topic("x" * an.MAX_TOPIC_LEN + an.RESPONSE_SUFFIX)  # must not raise

    def test_cursor_rule_starts_with_its_frontmatter(self):
        tmp = tempfile.mkdtemp()
        try:
            result = an.install_hooks("cursor", project=tmp)
            with open(result["path"]) as fh:
                text = fh.read()
            self.assertTrue(text.startswith("---\n"), "a .mdc rule must start with frontmatter")
            self.assertIn('description: "', text)          # colon must be quoted
            self.assertFalse(an.install_hooks("cursor", project=tmp)["changed"])
            an.install_hooks("cursor", project=tmp, add=False)
            self.assertFalse(os.path.exists(result["path"]))
        finally:
            shutil.rmtree(tmp)

    def test_unlicensed_telegram_in_config_does_not_fail_the_ntfy_send(self):
        ntfy = MockNtfy()
        try:
            cfg = make_config(ntfy.url, topic="premiumfallback")
            cfg.data["channels"] = ["ntfy", "telegram"]
            cfg.data["telegram"] = {"bot_token": "1:x", "chat_id": 7}
            cfg.data["license"] = None
            result = an.send_notification(cfg, "hello")
            self.assertTrue(result["ok"], result)
            self.assertEqual([r["channel"] for r in result["results"]], ["ntfy"])
            self.assertEqual(len(ntfy.posts.get("premiumfallback", [])), 1)
        finally:
            ntfy.stop()

    def test_dead_daemon_means_no_dead_buttons(self):
        an.write_bot_heartbeat()
        self.assertTrue(an.bot_heartbeat_fresh())
        an._update_bot_state(pid=999999)      # fresh timestamp, dead pid
        self.assertFalse(an.bot_heartbeat_fresh())
        try:
            os.remove(an._bot_state_path())
        except OSError:
            pass


class TestMinDuration(unittest.TestCase):
    """'finished' fires every turn - short turns must not buzz the phone."""

    def setUp(self):
        self.ntfy = MockNtfy()
        self.cfg = make_config(self.ntfy.url, topic="mindurationtopic")

    def tearDown(self):
        self.ntfy.stop()

    def _posts(self):
        return self.ntfy.posts.get("mindurationtopic", [])

    def test_short_turn_is_silent(self):
        an.run_hook(self.cfg, "started", "claude", silent=True)
        result = an.run_hook(self.cfg, "run_completed", "claude", min_duration=60)
        self.assertIn("skipped", result)
        self.assertEqual(self._posts(), [])

    def test_long_turn_notifies_with_duration(self):
        an.write_start_marker("claude")
        path = an._run_marker_path("claude")
        with open(path) as fh:
            data = json.load(fh)
        data["started_at"] = time.time() - 300
        with open(path, "w") as fh:
            json.dump(data, fh)
        an.run_hook(self.cfg, "run_completed", "claude", min_duration=60)
        self.assertEqual(len(self._posts()), 1)
        self.assertIn("in 5m00s", self._posts()[0]["body"])

    def test_failure_always_notifies(self):
        an.run_hook(self.cfg, "started", "claude", silent=True)
        an.run_hook(self.cfg, "run_failed", "claude", min_duration=60)
        self.assertEqual(len(self._posts()), 1)

    def test_unknown_duration_always_notifies(self):
        an.run_hook(self.cfg, "run_completed", "opencode", min_duration=60)
        self.assertEqual(len(self._posts()), 1)

    def test_installed_claude_hook_carries_the_threshold(self):
        commands = [h["command"] for groups in an.claude_event_hooks().values()
                    for g in groups for h in g["hooks"]]
        self.assertTrue(any("--min-duration" in c and "run_completed" in c for c in commands))
        self.assertTrue(any("started" in c and "--silent" in c for c in commands))
        self.assertFalse(any("--min-duration" in c and "run_failed" in c for c in commands))


class TestPartialDelivery(unittest.TestCase):
    def test_queue_keeps_the_channel_that_still_fails(self):
        """One channel recovering must not delete the other's pending delivery."""
        ntfy = MockNtfy()
        original_base = an.TG_API_BASE
        an.TG_API_BASE = "http://127.0.0.1:1"      # telegram unreachable
        cfg = make_config(ntfy.url)
        cfg.data["telegram"] = {"bot_token": "1:x", "chat_id": 7}
        for name in os.listdir(an.queue_dir()) if os.path.isdir(an.queue_dir()) else []:
            os.remove(os.path.join(an.queue_dir(), name))
        try:
            with dev_keypair() as seed:
                cfg.data["license"] = an.make_license_key("test", seed=seed)
                an.enqueue_item(cfg, {"message": "two channels", "priority": "normal",
                                      "channels": ["ntfy", "telegram"], "event": "notify"})
                stats = an.drain_queue(cfg, limit=None, timeout=2.0)
                self.assertEqual(stats["delivered"], 1)
                self.assertEqual(stats["kept"], 1)
                left = an.queue_list_data()["queue"]
                self.assertEqual(len(left), 1)
                self.assertEqual(left[0]["channels"], ["telegram"])
        finally:
            an.TG_API_BASE = original_base
            for name in os.listdir(an.queue_dir()):
                os.remove(os.path.join(an.queue_dir(), name))
            ntfy.stop()


class TestHistoryRotation(unittest.TestCase):
    def test_history_stays_bounded(self):
        path = an.history_path()
        os.makedirs(an.state_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(an.HISTORY_KEEP_LINES + 500):
                fh.write(json.dumps({"event": "notify", "message": "x" * 1000, "n": i}) + "\n")
        self.assertGreater(os.path.getsize(path), an.HISTORY_MAX_BYTES)
        an.write_history({"event": "notify", "message": "newest"})
        self.assertLessEqual(os.path.getsize(path), an.HISTORY_MAX_BYTES)
        records = an.read_history(limit=1)
        self.assertEqual(records[-1]["message"], "newest")
        os.remove(path)


class TestFieldTestRegressions(unittest.TestCase):
    """Findings from the first real setup run (2026-08-14)."""

    def test_unreachable_telegram_is_not_reported_as_a_bad_token(self):
        # The wizard called every failure "invalid bot token", so a timeout
        # sent the user to BotFather to create replacement bots.
        original = an.TelegramChannel._call

        def boom(*a, **kw):
            raise an.TransientError("timeout talking to https://api.telegram.org/bot<redacted>/getMe")

        an.TelegramChannel._call = staticmethod(boom)
        try:
            with self.assertRaises(an.TransientError) as ctx:
                an.TelegramChannel.validate_token("123:abc")
            self.assertNotIn("invalid bot token", str(ctx.exception))
        finally:
            an.TelegramChannel._call = staticmethod(original)

    def test_a_rejected_token_is_still_reported_as_invalid(self):
        original = an.TelegramChannel._call

        def rejected(*a, **kw):
            raise an.PermanentError("HTTP 401 from https://api.telegram.org/bot<redacted>/getMe")

        an.TelegramChannel._call = staticmethod(rejected)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                an.TelegramChannel.validate_token("123:abc")
            self.assertIn("invalid bot token", str(ctx.exception))
        finally:
            an.TelegramChannel._call = staticmethod(original)

    def test_network_failure_keeps_the_token_instead_of_losing_setup(self):
        original = an.TelegramChannel._call
        an.TelegramChannel._call = staticmethod(
            lambda *a, **kw: (_ for _ in ()).throw(an.TransientError("timeout")))
        answers = iter(["123456789:AAFAKEtoken", "y"])
        try:
            buf = io.StringIO()
            stdout, sys.stdout = sys.stdout, buf
            try:
                token = an.prompt_bot_token(reader=lambda prompt: next(answers).strip())
            finally:
                sys.stdout = stdout
            self.assertEqual(token, "123456789:AAFAKEtoken")
            self.assertIn("network problem", buf.getvalue())
            self.assertIn("No need to create another bot", buf.getvalue())
        finally:
            an.TelegramChannel._call = staticmethod(original)

    def test_giving_up_on_the_token_skips_telegram_without_aborting(self):
        original = an.TelegramChannel._call
        an.TelegramChannel._call = staticmethod(
            lambda *a, **kw: (_ for _ in ()).throw(an.PermanentError("Unauthorized")))
        answers = iter(["bad1", "bad2", "bad3"])
        try:
            buf = io.StringIO()
            stdout, sys.stdout = sys.stdout, buf
            try:
                # must return, not SystemExit: the license key and topic
                # entered earlier in the wizard would be lost
                token = an.prompt_bot_token(reader=lambda prompt: next(answers).strip())
            finally:
                sys.stdout = stdout
            self.assertIsNone(token)
            self.assertIn("everything else stays configured", buf.getvalue())
        finally:
            an.TelegramChannel._call = staticmethod(original)

    def test_blank_token_skips_telegram(self):
        buf = io.StringIO()
        stdout, sys.stdout = sys.stdout, buf
        try:
            self.assertIsNone(an.prompt_bot_token(reader=lambda prompt: ""))
        finally:
            sys.stdout = stdout
        self.assertIn("Skipping Telegram", buf.getvalue())

    def test_config_set_rotates_the_topic_without_rerunning_init(self):
        tmp = tempfile.mkdtemp()
        cfg = an.Config(an.default_config(), path=os.path.join(tmp, "config.json"))
        cfg.data["ntfy"]["topic"] = "agentbell"
        new = an.suggest_topic()
        self.assertEqual(an.config_set(cfg, "ntfy.topic", new), new)
        with open(os.path.join(tmp, "config.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["ntfy"]["topic"], new)
        shutil.rmtree(tmp)

    def test_config_set_refuses_unknown_keys(self):
        tmp = tempfile.mkdtemp()
        cfg = an.Config(an.default_config(), path=os.path.join(tmp, "config.json"))
        with self.assertRaises(SystemExit) as ctx:
            an.config_set(cfg, "ntfy.tpoic", "x")
        self.assertIn("ntfy.topic", str(ctx.exception))  # lists what IS settable
        self.assertFalse(os.path.exists(os.path.join(tmp, "config.json")))
        shutil.rmtree(tmp)

    def test_config_set_rejects_a_broken_quiet_hours_window(self):
        # normalize_quiet_hours() drops junk; through 'set' that would mean a
        # typo silently disables quiet hours and you find out at 3am.
        tmp = tempfile.mkdtemp()
        cfg = an.Config(an.default_config(), path=os.path.join(tmp, "config.json"))
        with self.assertRaises(SystemExit):
            an.config_set(cfg, "quiet_hours", "22:00-07:30,lunchtime")
        self.assertEqual(
            an.config_set(cfg, "quiet_hours", "22:00-07:30,13:00-14:00"),
            [{"start": "22:00", "end": "07:30"}, {"start": "13:00", "end": "14:00"}])
        shutil.rmtree(tmp)

    def test_config_set_rejects_a_topic_that_breaks_the_response_topic(self):
        tmp = tempfile.mkdtemp()
        cfg = an.Config(an.default_config(), path=os.path.join(tmp, "config.json"))
        with self.assertRaises(SystemExit):
            an.config_set(cfg, "ntfy.topic", "x" * (an.MAX_TOPIC_LEN + 1))
        with self.assertRaises(SystemExit):
            an.config_set(cfg, "ntfy.topic", "not a topic!")
        shutil.rmtree(tmp)

    def test_short_topic_fix_is_a_single_pasteable_command(self):
        cfg = make_config("http://127.0.0.1:1", topic="agentbell")
        topic_check = [c for c in an.doctor_checks(cfg) if c["name"] == "ntfy topic"][0]
        self.assertEqual(topic_check["status"], an.WARN)
        self.assertTrue(topic_check["fix"].startswith("agentbell config set ntfy.topic "))
        self.assertNotIn("\n", topic_check["fix"])
        # the suggested topic must actually be accepted by 'config set'
        suggested = topic_check["fix"].rsplit(" ", 1)[1]
        self.assertGreaterEqual(len(suggested), an.MIN_GUESSABLE_TOPIC_LEN)
        an.validate_topic(suggested)

    def test_bot_service_file_uses_an_absolute_binary_path(self):
        # 'cp examples/agentbell-bot.service ...' only worked from a
        # checkout, and a %h-relative ExecStart missed pipx/venv installs.
        home = tempfile.mkdtemp()
        original_home = os.environ.get("HOME")
        original_run = an.subprocess.run
        os.environ["HOME"] = home
        # never touch the real systemd/launchd of the machine running the tests
        an.subprocess.run = lambda *a, **kw: an.subprocess.CompletedProcess(a[0], 0)
        try:
            path, _started, note = an.install_bot_service()
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            binary = an.agentbell_binary()
            self.assertIn(binary, body)
            self.assertTrue(os.path.isabs(binary))
            if sys.platform == "darwin":
                # launchd plists carry argv as separate <string> elements
                self.assertIn("<string>bot</string><string>run</string>", body)
            else:
                self.assertIn("bot run", body)
            self.assertTrue(note)
        finally:
            an.subprocess.run = original_run
            if original_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = original_home
            shutil.rmtree(home, ignore_errors=True)

    def test_url_in_a_jsonc_config_is_not_mistaken_for_a_comment(self):
        # the stock OpenCode config is exactly this, and "https://" made the
        # naive check refuse a file with nothing to lose
        self.assertFalse(an.jsonc_has_comments(
            '{\n  "$schema": "https://opencode.ai/config.json"\n}\n'))
        self.assertFalse(an.jsonc_has_comments('{"note": "not a // comment"}'))
        self.assertFalse(an.jsonc_has_comments('{"esc": "quote\\" then // text"}'))
        self.assertTrue(an.jsonc_has_comments('{\n  // keep this\n  "a": 1\n}'))
        self.assertTrue(an.jsonc_has_comments('{\n  /* block */\n  "a": 1\n}'))

    def test_opencode_jsonc_without_comments_is_registered_automatically(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "opencode.jsonc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{\n  "$schema": "https://opencode.ai/config.json"\n}\n')
        result = an._mcp_add_opencode("/bin/agentbell", project=tmp)
        self.assertIn("written to", result)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["mcp"]["agentbell"]["command"], ["/bin/agentbell", "mcp"])
        self.assertEqual(data["$schema"], "https://opencode.ai/config.json")
        # no second file that OpenCode would ignore
        self.assertFalse(os.path.exists(os.path.join(tmp, "opencode.json")))
        self.assertEqual(an.opencode_config_path(tmp), path)
        self.assertTrue(an._mcp_has_entry(path, "mcp"))
        shutil.rmtree(tmp)

    def test_opencode_jsonc_with_real_comments_is_left_alone(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "opencode.jsonc")
        original = '{\n  // my settings\n  "$schema": "https://opencode.ai/config.json"\n}\n'
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)
        result = an._mcp_add_opencode("/bin/agentbell", project=tmp)
        self.assertIn("skipped", result)
        self.assertIn('"agentbell"', result)   # the paste-able snippet
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)
        shutil.rmtree(tmp)

    def test_plain_opencode_json_still_wins_over_jsonc(self):
        tmp = tempfile.mkdtemp()
        for name in ("opencode.json", "opencode.jsonc"):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write("{}")
        self.assertEqual(an.opencode_config_path(tmp), os.path.join(tmp, "opencode.json"))
        shutil.rmtree(tmp)

    def test_next_steps_never_puts_a_command_after_a_blocking_one(self):
        # pasting the whole block fed the following lines into 'agentbell
        # bot' stdin, so 'agentbell doctor' silently never ran
        cfg = make_config("http://127.0.0.1:1")
        cfg.data["telegram"] = {"bot_token": "1:x", "chat_id": "42"}
        buf = io.StringIO()
        with dev_keypair() as seed:
            cfg.data["license"] = an.make_license_key("field-test", seed=seed)
            stdout, sys.stdout = sys.stdout, buf
            try:
                an.print_next_steps(cfg)
            finally:
                sys.stdout = stdout
        lines = [line.strip() for line in buf.getvalue().splitlines()]
        self.assertNotIn("agentbell bot", lines)
        self.assertIn("agentbell bot install-service", lines)


class TestWebhookHardening(unittest.TestCase):
    """The local API is reachable by every process on the box - and, without
    these rules, by any web page the user happens to have open."""

    @classmethod
    def setUpClass(cls):
        cls.ntfy = MockNtfy()
        cls.cfg = make_config(cls.ntfy.url, topic="hardened")
        cls.port = _free_port()
        cls.cfg.data["webhook"] = {"listen": "127.0.0.1", "port": cls.port, "token": None}
        _start_webhook(cls.cfg, cls.port)
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def _post(self, path="/notify", data=b'{"message": "x"}', headers=None):
        request = urllib.request.Request(self.url + path, method="POST", data=data)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_a_request_from_a_browser_page_is_refused(self):
        # a text/plain POST needs no CORS preflight, so without this any open
        # tab could drive the API; a browser always sends Origin cross-site
        status, body = self._post(headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn(b"browser requests are not allowed", body)

    def test_a_rebound_host_header_is_refused(self):
        # DNS rebinding: attacker.example resolves to 127.0.0.1, so the socket
        # is local but the request is not
        status, body = self._post(headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertIn(b"Host", body)

    def test_a_body_larger_than_the_cap_is_refused(self):
        status, body = self._post(headers={"Content-Length": str(64 * 1024 + 1)})
        self.assertEqual(status, 413)
        self.assertIn(b"body too large", body)

    def test_a_malformed_content_length_is_a_400_not_a_traceback(self):
        for bogus in ("abc", "-10"):
            status, body = self._post(headers={"Content-Length": bogus})
            self.assertEqual(status, 400, bogus)
            self.assertIn(b"Content-Length", body)

    def test_deeply_nested_json_is_a_400_not_a_traceback(self):
        deep = (b"[" * 20000) + (b"]" * 20000)
        status, _ = self._post(data=deep)
        self.assertIn(status, (400, 413))

    def test_healthz_still_answers(self):
        with urllib.request.urlopen(f"{self.url}/healthz", timeout=5) as resp:
            self.assertIn(b"agentbell", resp.read())


class TestWebhookToken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ntfy = MockNtfy()
        cls.cfg = make_config(cls.ntfy.url, topic="tokened")
        cls.port = _free_port()
        cls.cfg.data["webhook"] = {"listen": "127.0.0.1", "port": cls.port,
                                   "token": "s3cret-webhook-token"}
        _start_webhook(cls.cfg, cls.port)
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def _post(self, headers=None):
        request = urllib.request.Request(self.url + "/notify", method="POST",
                                         data=b'{"message": "x"}')
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_missing_token_is_401(self):
        self.assertEqual(self._post()[0], 401)

    def test_wrong_token_is_401(self):
        self.assertEqual(self._post({"Authorization": "Bearer nope"})[0], 401)

    def test_the_right_token_is_accepted(self):
        status, body = self._post({"Authorization": "Bearer s3cret-webhook-token"})
        self.assertEqual(status, 200)
        self.assertIn(b'"ok": true', body)


class TestSecurityAuditRegressions(unittest.TestCase):
    """One test per finding of the pre-release security audit."""

    def test_a_public_copy_can_verify_but_never_mint(self):
        # what every user has: the public key, no signing seed. It checks keys
        # and cannot produce one - the HMAC scheme it replaced shipped a secret
        # that `pip download` + grep would have handed to anybody.
        original_file = an.LICENSE_SEED_FILE
        old = os.environ.get(an.LICENSE_SECRET_ENV)
        an.LICENSE_SEED_FILE = ".no-such-license-secret"
        os.environ.pop(an.LICENSE_SECRET_ENV, None)
        try:
            self.assertIsNone(an._signing_seed())
            self.assertIsNone(an.make_license_key("mallory"))
            forged = an.make_license_key("mallory", seed=os.urandom(32))
            self.assertFalse(an.check_license_key(forged))
            self.assertFalse(an.premium_enabled(make_config("http://x")))
        finally:
            an.LICENSE_SEED_FILE = original_file
            if old is not None:
                os.environ[an.LICENSE_SECRET_ENV] = old

    def test_action_buttons_never_carry_the_account_credential(self):
        # the headers of an http action are published inside the message, so
        # every subscriber of the topic can read them
        ntfy = {"server": "https://ntfy.example", "topic": "t", "auth": "user:hunter2"}
        buf = io.StringIO()
        stderr, sys.stderr = sys.stderr, buf
        try:
            actions = an.ask_actions("https://ntfy.example", "t-responses", "abcd",
                                     "Approve", "Deny", ntfy)
        finally:
            sys.stderr = stderr
        self.assertIsNone(actions)                       # no buttons instead of a leak
        self.assertIn("action_auth", buf.getvalue())     # and it says how to get them back
        ntfy["action_auth"] = "tk_publish_only"
        blob = json.dumps(an.ask_actions("https://ntfy.example", "t-responses", "abcd",
                                         "Approve", "Deny", ntfy))
        self.assertNotIn("hunter2", blob)
        self.assertNotIn(an.base64.b64encode(b"user:hunter2").decode(), blob)
        self.assertIn("tk_publish_only", blob)

    def test_a_verdict_for_another_ask_is_ignored_whatever_its_case(self):
        # _parse_answer matched case-insensitively while the id check did not,
        # so "approved <someone else's id>" slipped through as free text
        cfg = make_config("http://127.0.0.1:1")
        mine = "a1b2c3d4a1b2c3d4"
        waiter = an.ApprovalWaiter(cfg, "x-responses", 5, approval_id=mine)
        for index, text in enumerate(("approved deadbeefdeadbeef",
                                      "APPROVED deadbeefdeadbeef",
                                      "Denied deadbeefdeadbeef",
                                      "deny deadbeefdeadbeef")):
            waiter._offer(f"m{index}", text)
            self.assertTrue(waiter.messages.empty(), f"{text!r} answered the wrong question")
        waiter._offer("mine", f"approved {mine}")
        self.assertEqual(waiter.messages.get_nowait(), f"approved {mine}")

    def test_an_agent_name_cannot_escape_the_state_directory(self):
        # --agent is interpolated into <state>/runs/<agent>.json, which used to
        # let it write (and later unlink) any *.json on the box
        args = an.build_parser().parse_args(["hook", "run_completed", "--agent", "../evil"])
        buf = io.StringIO()
        stderr, sys.stderr = sys.stderr, buf
        try:
            with self.assertRaises(SystemExit) as ctx:
                an.cmd_hook(args)
            for bad in ("../evil", "a/b", "", "x" * 33):
                with self.assertRaises(SystemExit):
                    an.write_start_marker(bad)
        finally:
            sys.stderr = stderr
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("invalid --agent name", buf.getvalue())
        self.assertTrue(an._run_marker_path("claude").startswith(an.state_dir()))

    def test_config_show_redacts_the_topic(self):
        # the topic IS the credential: it is enough to read every notification
        # and to publish fake approvals
        data = dict(an.default_config())
        data["ntfy"] = {"server": "https://ntfy.sh", "topic": "basti-0123456789abcdef",
                        "auth": None}
        safe = an.redacted_config(data)
        self.assertNotIn("0123456789abcdef", json.dumps(safe))
        self.assertTrue(safe["ntfy"]["topic"].startswith("basti"))
        self.assertIn("redacted", safe["ntfy"]["topic"])

    def test_webhook_token_is_redacted_completely(self):
        safe = an.redacted_config({"webhook": {"token": "abcd1234-token"}})
        self.assertNotIn("abcd", json.dumps(safe))

    @unittest.skipIf(os.name == "nt", "Unix file permissions not applicable on Windows")
    def test_state_dir_and_history_are_owner_only(self):
        an.write_history({"event": "permission-probe", "message": "secret body"})
        self.assertEqual(os.stat(an.state_dir()).st_mode & 0o077, 0)
        self.assertEqual(os.stat(an.history_path()).st_mode & 0o077, 0)

    @unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
    def test_a_rule_file_that_is_a_symlink_is_not_written_through(self):
        # a hostile repo can ship .rules as a symlink to ~/.bashrc
        tmp = tempfile.mkdtemp()
        target = os.path.join(tmp, "precious")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("do not touch\n")
        link = os.path.join(tmp, ".rules")
        os.symlink(target, link)
        buf = io.StringIO()
        stderr, sys.stderr = sys.stderr, buf
        try:
            self.assertFalse(an._install_block_file(link, "payload"))
            self.assertFalse(an._write_owned_rule(link, "payload"))
        finally:
            sys.stderr = stderr
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "do not touch\n")
        self.assertIn("is a symlink", buf.getvalue())
        shutil.rmtree(tmp)

    def test_a_redirect_is_never_followed(self):
        # urllib's default opener replays the Authorization header at the
        # redirect target, so a typo'd server could harvest the credential
        holder = {}

        class Redirector(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                holder["auth"] = self.headers.get("Authorization")
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/topic"
            with self.assertRaises(an.PermanentError) as ctx:
                an.http_request(url, "POST", {"Authorization": "Basic c2VjcmV0"}, "hi")
            self.assertIn("redirect", str(ctx.exception))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(holder["auth"], "Basic c2VjcmV0")   # sent once, never replayed

    def test_only_http_and_https_are_accepted_as_a_server(self):
        self.assertEqual(an.normalize_server("ntfy.example"), "https://ntfy.example")
        self.assertEqual(an.normalize_server("http://ntfy.example/"), "http://ntfy.example")
        for bad in ("file:///etc/passwd", "ftp://ntfy.example", "gopher://x"):
            with self.assertRaises(RuntimeError):
                an.normalize_server(bad)

    @unittest.skipIf(os.name == "nt", "Unix file permissions not applicable on Windows")
    def test_a_config_we_own_is_never_briefly_world_readable(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        an.write_json_atomic(path, {"license": "AB1-x"}, mode=0o600)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        # somebody else's config keeps the mode it already had
        other = os.path.join(tmp, "settings.json")
        an.write_json_atomic(other, {"a": 1})
        os.chmod(other, 0o640)
        an.write_json_atomic(other, {"a": 2})
        self.assertEqual(os.stat(other).st_mode & 0o777, 0o640)
        shutil.rmtree(tmp)

    def test_codex_keeps_a_feature_flag_the_user_wrote(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.toml")
        original = an.codex_config_path
        an.codex_config_path = lambda: path
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('model = "gpt-5"\n')
            an.install_codex_hooks()
            with open(path, encoding="utf-8") as fh:
                self.assertIn(an.CODEX_FLAG_MARKER, fh.read())
            an.uninstall_codex_hooks()
            with open(path, encoding="utf-8") as fh:
                self.assertNotIn("features.hooks", fh.read())
            # a line the user wrote themselves carries no marker and stays
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("features.hooks = true\n" + an.TOML_START + "\n"
                         + an.TOML_END + "\n")
            an.uninstall_codex_hooks()
            with open(path, encoding="utf-8") as fh:
                self.assertIn("features.hooks = true", fh.read())
        finally:
            an.codex_config_path = original
            shutil.rmtree(tmp)


class TestInsecureAskWarning(unittest.TestCase):
    def test_sensitive_approval_heuristic_is_narrow(self):
        self.assertTrue(an.is_sensitive_approval("Deploy to production?"))
        self.assertTrue(an.is_sensitive_approval("Rotate the API credentials?"))
        self.assertTrue(an.is_sensitive_approval("Delete the production database?"))
        self.assertFalse(an.is_sensitive_approval("Deploy to staging?"))
        self.assertFalse(an.is_sensitive_approval("Delete the temporary build file?"))
        self.assertFalse(an.is_sensitive_approval("Update the README heading?"))

    def test_warns_on_public_ntfy_without_auth(self):
        cfg = an.Config({
            "ntfy": {"server": "https://ntfy.sh", "topic": "test-topic-long-enough"},
        })
        # reset the per-process fired flag so the test runs cleanly
        an._warn_insecure_ask._fired = False
        buf = io.StringIO()
        stderr, sys.stderr = sys.stderr, buf
        try:
            an._warn_insecure_ask(cfg, "Deploy to production?")
        finally:
            sys.stderr = stderr
        self.assertIn("does not have ntfy authentication", buf.getvalue())
        self.assertIn("self-hosted ntfy", buf.getvalue())

    def test_silent_for_routine_or_authenticated_approval(self):
        cfg = an.Config({
            "ntfy": {"server": "https://ntfy.example.com", "topic": "test-topic",
                     "auth": "user:pass"},
        })
        an._warn_insecure_ask._fired = False
        buf = io.StringIO()
        stderr, sys.stderr = sys.stderr, buf
        try:
            an._warn_insecure_ask(cfg, "Deploy to production?")
            an._warn_insecure_ask(an.Config({
                "ntfy": {"server": "https://ntfy.sh", "topic": "test-topic"},
            }), "Update the README heading?")
        finally:
            sys.stderr = stderr
        self.assertEqual(buf.getvalue(), "")

    def test_warns_for_self_hosted_server_without_auth(self):
        cfg = an.Config({
            "ntfy": {"server": "https://ntfy.example.com", "topic": "test-topic"},
        })
        an._warn_insecure_ask._fired = False
        buf = io.StringIO()
        stderr, sys.stderr = sys.stderr, buf
        try:
            an._warn_insecure_ask(cfg, "Delete the production database?")
        finally:
            sys.stderr = stderr
        self.assertIn("does not have ntfy authentication", buf.getvalue())

    def test_warns_only_once_per_process(self):
        cfg = an.Config({
            "ntfy": {"server": "https://ntfy.sh", "topic": "test-topic-long-enough"},
        })
        an._warn_insecure_ask._fired = False
        buf = io.StringIO()
        stderr, sys.stderr = sys.stderr, buf
        try:
            an._warn_insecure_ask(cfg, "Deploy to production?")
            buf.truncate(0)
            buf.seek(0)
            an._warn_insecure_ask(cfg, "Deploy to production?")
        finally:
            sys.stderr = stderr
        # second call must be silent: the flag prevents duplicates
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
