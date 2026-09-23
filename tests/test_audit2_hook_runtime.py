"""Audit 2026-09-22, cluster hook-runtime: M14, M16, M18 and the LOW items
config-bom, surrogates, nul, getuser, clamp, latin1-title.

Run with: python3 -m unittest discover -s tests
"""

import base64
import email.header
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: sets up the sandbox dirs)

an = base.an
AGENTBELL_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "agentbell.py")


def _records(**match):
    return [r for r in an.read_history(limit=0)
            if all(r.get(k) == v for k, v in match.items())]


def _clear_queues():
    for name in ("queue", "deferred"):
        shutil.rmtree(os.path.join(an.state_dir(), name), ignore_errors=True)


class _FakeClock:
    """agentbell's `time` module with a clock that only moves when told to.

    A hung server is simulated by a channel that burns its whole timeout;
    backoff sleeps advance the clock instead of waiting. Only agentbell sees
    it (the module attribute is patched), so the suite never really waits.
    """

    def __init__(self):
        self.now = time.time()
        self.module = types.SimpleNamespace(
            **{name: getattr(time, name) for name in dir(time) if not name.startswith("__")})
        self.module.time = lambda: self.now
        self.module.time_ns = lambda: int(self.now * 1_000_000_000)
        self.module.sleep = self.advance

    def advance(self, seconds):
        self.now += float(seconds)

    def patch(self):
        return unittest.mock.patch.object(an, "time", self.module)


# Kimi kills a hook after 10s (the shortest host timeout; Gemini: 15s).
# Interpreter start, history and the queue write need the last 2 seconds.
SEND_LIMIT = 10 - 2


class TestHookSendBudget(unittest.TestCase):
    """M14: a hook's sending never outlives the host's hook timeout."""

    def setUp(self):
        _clear_queues()
        self.clock = _FakeClock()
        self.cfg = base.make_config("http://127.0.0.1:9", topic="budgettopic")
        self.calls = []

    def test_the_budget_leaves_headroom_below_the_host_timeout(self):
        # the per-try timeout never drops below 0.5s, so that is the overshoot
        self.assertLessEqual(an.HOOK_SEND_BUDGET_SECONDS + 0.5, SEND_LIMIT)

    def _hanging(self, cfg, channel, item, timeout=10.0):
        # a stalled server: every try takes the full timeout, then fails
        self.calls.append((channel, round(timeout, 2)))
        self.clock.advance(timeout)
        raise an.TransientError(f"timeout talking to {channel}")

    def test_hung_server_is_queued_within_the_budget(self):
        start = self.clock.now
        with self.clock.patch(), \
                unittest.mock.patch.object(an, "_publish_channel", self._hanging):
            result = an.run_hook(self.cfg, "run_completed", "hr-budget")
        elapsed = self.clock.now - start
        # before the fix: 5+1+5+2+5 = 18s, and the push died with the process
        self.assertLessEqual(elapsed, SEND_LIMIT)
        self.assertEqual(result.get("queued"), ["ntfy"])
        items = [item for _, item in an._read_item_files(an.queue_dir())]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["channels"], ["ntfy"])
        self.assertIn("timeout", items[0]["last_error"]["ntfy"])
        recs = _records(agent="hr-budget")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "queued")
        self.assertEqual(recs[0]["source_event"], "hook.run_completed")
        self.assertEqual(recs[0]["queued_channels"], ["ntfy"])

    def test_a_channel_the_budget_cannot_reach_is_queued_untried(self):
        self.cfg.data["channels"] = ["ntfy", "os"]
        with self.clock.patch(), \
                unittest.mock.patch.object(an, "_publish_channel", self._hanging):
            result = an.run_hook(self.cfg, "run_failed", "hr-budget-two")
        self.assertEqual(result.get("queued"), ["ntfy", "os"])
        # every try got at most what was left of the budget
        self.assertLessEqual(sum(t for _, t in self.calls), SEND_LIMIT)
        items = [item for _, item in an._read_item_files(an.queue_dir())]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["channels"], ["ntfy", "os"])

    def test_queue_drain_after_a_hook_send_stays_inside_the_budget(self):
        an.enqueue_item(self.cfg, {"message": "hr old backlog", "channels": ["ntfy"],
                                   "priority": "normal", "event": "notify"})
        start = self.clock.now

        def publish(cfg, channel, item, timeout=10.0):
            if item.get("message") == "hr old backlog":
                return self._hanging(cfg, channel, item, timeout)
            self.clock.advance(0.1)
            return {"channel": channel, "ok": True}

        with self.clock.patch(), unittest.mock.patch.object(an, "_publish_channel", publish):
            result = an.run_hook(self.cfg, "input_required", "hr-drain")
        self.assertEqual([r["channel"] for r in result["results"]], ["ntfy"])
        self.assertLessEqual(self.clock.now - start, SEND_LIMIT)
        items = [item for _, item in an._read_item_files(an.queue_dir())]
        self.assertEqual([i["message"] for i in items], ["hr old backlog"])
        self.assertEqual(items[0]["attempts"], 1)

    def test_the_os_channel_is_capped_too(self):
        timeouts = []

        def run(argv, **kwargs):
            timeouts.append(kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 0)

        self.cfg.data["channels"] = ["os"]
        with unittest.mock.patch.object(an.platform, "system", return_value="Windows"), \
                unittest.mock.patch.object(an.subprocess, "run", run):
            result = an.run_hook(self.cfg, "run_failed", "hr-budget-os")
        self.assertEqual([r["channel"] for r in result["results"]], ["os"])
        # the PowerShell toast had a fixed 15s timeout of its own
        self.assertLessEqual(timeouts[0], 5.0)

    def test_notify_without_a_budget_keeps_every_retry(self):
        with self.clock.patch(), \
                unittest.mock.patch.object(an, "_publish_channel", self._hanging):
            result = an.send_notification(self.cfg, "hr no budget", timeout=5.0)
        self.assertEqual(len(self.calls), an.RETRY_ATTEMPTS)
        self.assertEqual(result.get("queued"), ["ntfy"])


class TestHookErrorTrace(unittest.TestCase):
    """M16: a failing hook exits 0 but is never invisible."""

    def _run(self, argv):
        args = an.build_parser().parse_args(argv)
        err = io.StringIO()
        with unittest.mock.patch.object(sys, "stderr", err), \
                unittest.mock.patch.object(an, "read_hook_payload", return_value={}):
            with self.assertRaises(SystemExit) as caught:
                an.cmd_hook(args)
        return caught.exception.code, err.getvalue()

    def test_an_exception_is_recorded_and_reported_once(self):
        with unittest.mock.patch.object(an, "run_hook", side_effect=ValueError("hr boom")):
            code, err = self._run(["hook", "run_completed", "--agent", "hr-err", "--force"])
        self.assertEqual(code, 0)
        recs = _records(agent="hr-err")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "hook.error")
        self.assertEqual(recs[0]["source_event"], "hook.run_completed")
        self.assertEqual(recs[0]["error"], "ValueError: hr boom")
        self.assertIs(recs[0]["forced"], True)
        self.assertTrue(recs[0]["project"])
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("hook run_completed failed: ValueError: hr boom", err)
        self.assertIn("agentbell history", err)

    def test_unwritable_history_still_leaves_the_stderr_line(self):
        with unittest.mock.patch.object(an, "run_hook", side_effect=OSError("hr disk")), \
                unittest.mock.patch.object(an, "write_history",
                                           side_effect=PermissionError("read-only")):
            code, err = self._run(["hook", "run_failed", "--agent", "hr-err-ro"])
        self.assertEqual(code, 0)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("OSError: hr disk", err)
        self.assertIn("history not writable either: PermissionError", err)

    def test_verify_counts_a_hook_error_as_an_event_that_reached_nobody(self):
        with unittest.mock.patch.object(an, "run_hook", side_effect=RuntimeError("hr x")):
            self._run(["hook", "run_completed", "--agent", "hr-err-verify"])
        obs = an.hook_observations(an.read_history(limit=0), 3600)["hr-err-verify"]
        self.assertEqual(obs["count"], 1)
        self.assertEqual(obs["failed"], 1)
        self.assertEqual(obs["events"], {"run_completed": 1})


class TestBrokenConfig(unittest.TestCase):
    """L-config-bom: a BOM is fine; a broken config never costs the agent's
    turn (hook) or the user's command (watch)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hr-config-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "config.json")

    def _write(self, raw):
        with open(self.path, "wb") as fh:
            fh.write(raw)

    def test_a_utf8_bom_is_accepted(self):
        self._write(b"\xef\xbb\xbf" + json.dumps({"ntfy": {"topic": "hr-bom-topic"}}).encode())
        cfg = an.Config(path=self.path)
        self.assertEqual(cfg.data["ntfy"]["topic"], "hr-bom-topic")
        self.assertEqual(cfg.data["channels"], ["ntfy"])     # defaults still merged

    def test_valid_json_that_is_not_an_object_is_a_config_error(self):
        for raw in (b"[]", b"null", b'"x"'):
            self._write(raw)
            with self.assertRaises(SystemExit) as caught:
                an.Config(path=self.path)
            self.assertIn("not a JSON object", str(caught.exception.code))

    def test_hook_with_a_broken_config_exits_zero_and_records_it(self):
        self._write(b'{"ntfy": {"topic": "x",}')
        args = an.build_parser().parse_args(["hook", "run_completed", "--agent", "hr-broken"])
        err = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {an.CONFIG_FILE_ENV: self.path}), \
                unittest.mock.patch.object(sys, "stderr", err), \
                unittest.mock.patch.object(an, "read_hook_payload", return_value={}):
            with self.assertRaises(SystemExit) as caught:
                an.cmd_hook(args)
        self.assertEqual(caught.exception.code, 0)
        recs = _records(agent="hr-broken")
        self.assertEqual([r["event"] for r in recs], ["hook.error"])
        self.assertIn("cannot read config", recs[0]["error"])
        self.assertIn("cannot read config", err.getvalue())

    def test_watch_with_a_broken_config_still_runs_the_command(self):
        self._write(b"{broken")
        marker = os.path.join(self.tmp, "ran")
        args = an.build_parser().parse_args(
            ["watch", "--", sys.executable, "-c",
             f"open({marker!r}, 'w').close(); raise SystemExit(3)"])
        err, out = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, {an.CONFIG_FILE_ENV: self.path}), \
                unittest.mock.patch.object(sys, "stderr", err), \
                unittest.mock.patch.object(sys, "stdout", out), \
                unittest.mock.patch.object(an, "send_notification") as send:
            with self.assertRaises(SystemExit) as caught:
                an.cmd_watch(args)
        self.assertEqual(caught.exception.code, 3)
        self.assertTrue(os.path.exists(marker))
        send.assert_not_called()
        self.assertIn("cannot read config", err.getvalue())
        self.assertIn("running the command without a notification", err.getvalue())
        self.assertIn("failed (exit 3)", out.getvalue())


class TestUnsendableText(unittest.TestCase):
    """L-surrogates and L-nul: odd bytes are delivered with replacements,
    never lost."""

    def setUp(self):
        _clear_queues()
        self.ntfy = base.MockNtfy()
        self.addCleanup(self.ntfy.stop)
        self.cfg = base.make_config(self.ntfy.url, topic="hrtext")

    def _bodies(self):
        return [p["body"] for p in self.ntfy.posts.get("hrtext", [])]

    def test_a_surrogate_in_the_cwd_is_delivered_with_a_replacement(self):
        with unittest.mock.patch.object(an, "HOOK_DEDUPE_WINDOW_SECONDS", 5.0):
            an.run_hook(self.cfg, "started", "hr-sur", cwd="/tmp/caf\udce9",
                        silent=True)                                    # marker scope
            an.run_hook(self.cfg, "run_completed", "hr-sur", cwd="/tmp/caf\udce9")
        self.assertEqual(len(self._bodies()), 1)
        self.assertIn("/tmp/caf�", self._bodies()[0])
        recs = _records(agent="hr-sur")
        self.assertEqual([r["event"] for r in recs], ["hook.run_completed"])
        self.assertEqual(recs[0]["delivered"], ["ntfy"])
        self.assertIn("caf\udce9", recs[0]["message"])   # history keeps the original

    def test_a_surrogate_in_message_and_title_is_delivered(self):
        result = an.send_notification(self.cfg, "build caf\udce9 done", title="t\udcff")
        self.assertTrue(result["ok"])
        post = self.ntfy.posts["hrtext"][-1]
        self.assertEqual(post["body"], "build caf� done")
        self.assertEqual(email.header.decode_header(post["headers"]["Title"]),
                         [("t�".encode("utf-8"), "utf-8")])

    def test_a_surrogate_reaches_telegram_as_valid_text(self):
        texts = []

        def fake_call(token, method, body=None, timeout=10.0, url_params=""):
            body["text"].encode("utf-8")        # Telegram only takes valid UTF-8
            texts.append(body["text"])
            return {"message_id": 1}

        channel = an.TelegramChannel(an.Config({"telegram": {"bot_token": "1:x",
                                                             "chat_id": "2"}}))
        with unittest.mock.patch.object(an.TelegramChannel, "_call", staticmethod(fake_call)):
            channel.send("m\udce9\x00", title="t\udcff")
            channel.send_ask("q\udce9", "abcd", "Yes", "No")
        self.assertEqual(texts[0], "<b>t�</b>\nm�")
        self.assertIn("q�", texts[1])

    def test_a_nul_does_not_abort_the_other_channels(self):
        self.cfg.data["channels"] = ["os", "ntfy"]
        argv_seen = []

        def run(argv, **kwargs):
            # CPython refuses a NUL in any argument before exec'ing anything
            if any("\x00" in str(arg) for arg in argv):
                raise ValueError("embedded null byte")
            argv_seen.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with unittest.mock.patch.object(an.platform, "system", return_value="Linux"), \
                unittest.mock.patch.object(an.shutil, "which", return_value="/usr/bin/notify-send"), \
                unittest.mock.patch.object(an.subprocess, "run", run):
            first = an.send_notification(self.cfg, "a\x00b", title="t\x00t")
            second = an.run_hook(self.cfg, "run_completed", "hr-nul", cwd="/work/a\x00b")
        self.assertTrue(first["ok"], first)
        self.assertEqual([r["channel"] for r in first["results"]], ["os", "ntfy"])
        self.assertEqual([r["channel"] for r in second["results"]], ["os", "ntfy"])
        self.assertEqual(argv_seen[0][-2:], ["tt", "ab"])
        self.assertEqual(self._bodies()[0], "ab")
        self.assertIn("/work/ab", self._bodies()[1])
        rec = _records(agent="hr-nul")[0]
        # POSIX keeps the text; Windows resolves it to c:\work\a\x00b
        self.assertTrue(rec["project"].endswith(os.path.normcase(os.path.join("work", "a\x00b"))),
                        rec["project"])

    def test_one_crashing_channel_is_an_error_not_a_lost_push(self):
        self.cfg.data["channels"] = ["os", "ntfy"]
        real = an._publish_channel

        def publish(cfg, channel, item, timeout=10.0):
            if channel == "os":
                raise KeyError("hr bug")
            return real(cfg, channel, item, timeout)

        with unittest.mock.patch.object(an, "_publish_channel", publish):
            result = an.send_notification(self.cfg, "hr isolated")
        self.assertEqual([r["channel"] for r in result["results"]], ["ntfy"])
        self.assertEqual(result["errors"], ["os: KeyError: 'hr bug'"])
        rec = _records(message="hr isolated")[0]
        self.assertEqual(rec["errors"], {"os": "KeyError: 'hr bug'"})

    def test_history_survives_a_surrogate(self):
        an.write_history({"event": "hr-history", "message": "x\udcff"})
        rec = _records(event="hr-history")[-1]
        self.assertEqual(rec["message"], "x\udcff")


class TestNtfyHeaderEncoding(unittest.TestCase):
    """L-latin1-title: non-ASCII titles/tags travel as RFC 2047 encoded-words."""

    def setUp(self):
        _clear_queues()         # an auto-drained leftover would be posts[-1]
        self.ntfy = base.MockNtfy()
        self.addCleanup(self.ntfy.stop)
        self.cfg = base.make_config(self.ntfy.url, topic="hrtitle")

    @staticmethod
    def _decode(value):
        parts = email.header.decode_header(value)
        return "".join(p.decode(c or "ascii") if isinstance(p, bytes) else p for p, c in parts)

    def test_emoji_cjk_and_umlauts_arrive_intact(self):
        title = "✅ Build fertig – Grüße 完了"
        an.send_notification(self.cfg, "body", title=title, tags=["bär", "done"])
        headers = self.ntfy.posts["hrtitle"][-1]["headers"]
        for name in ("Title", "Tags"):
            headers[name].encode("ascii")                       # nothing non-ASCII on the wire
            self.assertTrue(headers[name].startswith("=?UTF-8?B?"))
        self.assertEqual(self._decode(headers["Title"]), title)
        self.assertEqual(self._decode(headers["Tags"]), "bär,done")

    def test_the_approval_title_keeps_its_emoji(self):
        encoded = an._latin1_header("❓ Approval requested")
        self.assertEqual(base64.b64decode(encoded[10:-2]).decode("utf-8"),
                         "❓ Approval requested")

    def test_ascii_stays_byte_identical(self):
        an.send_notification(self.cfg, "body", title="Build done", tags=["build"])
        headers = self.ntfy.posts["hrtitle"][-1]["headers"]
        self.assertEqual(headers["Title"], "Build done")
        self.assertEqual(headers["Tags"], "build")
        self.assertEqual(an._latin1_header(" a\r\nb\x00\x7fc "), "a bc")


class TestClampMessage(unittest.TestCase):
    """L-clamp: linear time, identical output."""

    @staticmethod
    def _old(text, limit=3900):
        text = text or ""
        if len(text.encode("utf-8")) <= limit:
            return text
        out = text
        while len(out.encode("utf-8")) > limit and out:
            out = out[:-1]
        return out + "…"

    def test_same_output_as_the_old_algorithm(self):
        rng = random.Random(20260922)
        alphabet = "ab \néß✅完\U0001f600"
        for _ in range(400):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            limit = rng.randint(0, 80)
            self.assertEqual(an.clamp_message(text, limit), self._old(text, limit),
                             (text, limit))
        self.assertEqual(an.clamp_message(None), "")
        self.assertEqual(an.clamp_message("x" * 3900), "x" * 3900)

    def test_large_input_is_fast(self):
        text = "é" * 300_000          # the old loop took ~40s here
        start = time.perf_counter()
        out = an.clamp_message(text)
        self.assertLess(time.perf_counter() - start, 2.0)
        self.assertEqual(out, "é" * 1950 + "…")


class TestSuggestTopicWithoutUser(unittest.TestCase):
    """L-getuser: no passwd entry and no USER/LOGNAME must not crash init."""

    def test_getuser_failures_fall_back_to_a_generic_prefix(self):
        for exc in (KeyError("getpwuid(): uid not found: 1000"), OSError("No username"),
                    ImportError("No module named 'pwd'")):
            with unittest.mock.patch.object(an.getpass, "getuser", side_effect=exc):
                topic = an.suggest_topic()
            self.assertRegex(topic, r"^agent-[0-9a-f]{32}$")
            self.assertLessEqual(len(topic), an.MAX_TOPIC_LEN)


def _sandbox_env(root):
    env = dict(os.environ)
    for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "APPDATA",
                "LOCALAPPDATA", an.CONFIG_DIR_ENV, an.STATE_DIR_ENV):
        env[key] = os.path.join(root, key.lower())
        os.makedirs(env[key], exist_ok=True)
    env.pop(an.CONFIG_FILE_ENV, None)
    env.pop(an.LICENSE_ENV, None)
    env["PYTHONIOENCODING"] = "cp1252"     # what a Windows pipe/redirect gets
    return env


class TestConsoleEncoding(unittest.TestCase):
    """M18: a console that cannot show an emoji gets a '?', not a crash."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hr-console-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env = _sandbox_env(self.root)
        self.ntfy = base.MockNtfy()
        self.addCleanup(self.ntfy.stop)
        with open(os.path.join(self.env[an.CONFIG_DIR_ENV], "config.json"), "w") as fh:
            json.dump({"ntfy": {"server": self.ntfy.url, "topic": "hrconsole"}}, fh)

    def _cli(self, *argv):
        return subprocess.run([sys.executable, AGENTBELL_PY] + list(argv), env=self.env,
                              capture_output=True, timeout=60)

    def test_cp1252_stdout_prints_emoji_output(self):
        proc = self._cli("watch", "--", sys.executable, "-c", "pass")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"? ", proc.stdout)
        self.assertIn(b"succeeded (exit 0)", proc.stdout)
        proc = self._cli("history")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, rb"\bwatch +normal +ntfy +\? ")

    def test_json_output_is_unchanged(self):
        proc = self._cli("watch", "--json", "--", sys.executable, "-c", "pass")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["message"].startswith("✅ "))

    def test_only_streams_that_can_raise_are_changed(self):
        def wrapper(errors):
            return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors=errors,
                                    newline="\n")

        # Windows Python 3.14 behind a pipe reported cp1252 + surrogateescape
        for errors in ("strict", "surrogateescape"):
            stream = wrapper(errors)
            with unittest.mock.patch.object(sys, "stdout", stream), \
                    unittest.mock.patch.object(sys, "stderr", wrapper(errors)):
                an._safe_console()
                print("✅ ok \udcff")
            self.assertEqual(stream.errors, "replace")
            stream.flush()
            self.assertEqual(stream.buffer.getvalue(), b"? ok ?\n")
        lenient = wrapper("backslashreplace")
        with unittest.mock.patch.object(sys, "stderr", lenient):
            an._safe_console()
        self.assertEqual(lenient.errors, "backslashreplace")
        odd = types.SimpleNamespace(errors="strict")        # no reconfigure()
        with unittest.mock.patch.object(sys, "stdout", odd), \
                unittest.mock.patch.object(sys, "stderr", None):
            an._safe_console()                              # left alone, no crash


if __name__ == "__main__":
    unittest.main()
