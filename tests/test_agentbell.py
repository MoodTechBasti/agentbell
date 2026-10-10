"""Tests for agentbell. Run with: python3 -m unittest discover -s tests -v"""

import atexit
import base64
import contextlib
import http.client
import io
import json
import os
import queue
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agentbell as an  # noqa: E402

_TEST_ROOT = tempfile.mkdtemp(prefix="agentbell-tests-")


def _remove_tree(path):
    """Delete a temp tree at exit; say so when part of it stays (a file a
    process still holds open on Windows) instead of failing silently."""
    try:
        shutil.rmtree(path)
    except OSError as exc:
        shutil.rmtree(path, ignore_errors=True)
        sys.stderr.write(f"test_agentbell: could not remove all of {path}: {exc}\n")


# Every temp dir a test makes (mkdtemp, NamedTemporaryFile) lands under
# _TEST_ROOT and goes with it, removed or not by the test itself (WF-3).
tempfile.tempdir = os.path.join(_TEST_ROOT, "tmp")
os.makedirs(tempfile.tempdir)
atexit.register(_remove_tree, _TEST_ROOT)
os.environ["AGENTBELL_STATE_DIR"] = os.path.join(_TEST_ROOT, "state")
os.environ["AGENTBELL_CONFIG_DIR"] = os.path.join(_TEST_ROOT, "config")
os.environ.pop("AGENTBELL_CONFIG", None)

# Many tests fire the same hook push back to back; the identical-push window
# would turn them into `hook.skipped_duplicate`. Off here, on (patched) in
# TestHookDedupe only.
an.HOOK_DEDUPE_WINDOW_SECONDS = 0

# tomllib is 3.11+; the project supports 3.9, so those tests are skipped there
HAS_TOMLLIB = sys.version_info >= (3, 11)


# Env vars that move a location agentbell (or `python -m site`, `pipx`) reads
# away from the home dir. Unset, each one falls back to a path under HOME.
_HOME_OVERRIDES = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_BIN_HOME",
                   "KIMI_CODE_HOME", "QWEN_HOME", "PYTHONUSERBASE", "PIPX_HOME",
                   "PIPX_BIN_DIR")


def _set_home(home):
    """Point every home-dir mechanism at `home`. Windows expanduser ignores
    HOME and reads USERPROFILE, and Windows paths come from APPDATA, so tests
    must move all of them - otherwise they read and write the real profile,
    agent configs and pip --user install of the machine running the tests."""
    old = {key: os.environ.get(key)
           for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA") + _HOME_OVERRIDES}
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home
    os.environ["APPDATA"] = os.path.join(home, "AppData", "Roaming")
    os.environ["LOCALAPPDATA"] = os.path.join(home, "AppData", "Local")
    for key in _HOME_OVERRIDES:
        os.environ.pop(key, None)
    return old


def _restore_home(old):
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# The whole suite runs in a temp home; a test's own _set_home() moves it
# further and _restore_home() comes back here, never to the real one.
os.makedirs(os.path.join(_TEST_ROOT, "home"))
_set_home(os.path.join(_TEST_ROOT, "home"))


def _fake_launcher(tmpdir, name="agentbell"):
    """A stand-in installed launcher for sys.argv[0]: agentbell_binary()
    only trusts an argv[0] that actually exists on disk."""
    path = os.path.join(tmpdir, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n")
    return path


class MockNtfy:
    """Tiny in-process ntfy server: publish + JSON stream/poll subscribe."""

    def __init__(self, stream_enabled=True, post_503_count=0):
        self.posts = {}          # topic -> list of {"id","body","ts","headers"}
        self.subscribers = {}    # topic -> list of queue.Queue
        self.stream_enabled = stream_enabled
        self.post_503_count = post_503_count  # transient failures on the first N POSTs
        self.post_fail_status = 503           # ...answered with this status
        self.get_503_count = 0                # transient failures on the next N polls
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
                    self.send_response(server.post_fail_status)
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
                # the server's clock in whole seconds, like real ntfy
                self.wfile.write(json.dumps({"id": record["id"], "time": int(record["ts"]),
                                             "event": "message"}).encode())

            def _write(self, record):
                line = json.dumps({"event": "message", "id": record["id"],
                                   "time": int(record["ts"]), "message": record["body"]})
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
                since = params.get("since", "")
                window = re.fullmatch(r"(\d+)([smhd])", since)
                if since and since != "latest" and not since.isdigit() and not window:
                    # real ntfy rejects a malformed `since` outright
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"code":40008,"error":"invalid since"}')
                    return
                if params.get("poll") == "1" and server.get_503_count > 0:
                    server.get_503_count -= 1
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"temporarily unavailable")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                self.wfile.write(b'{"event":"open"}\n')
                self.wfile.flush()
                if params.get("poll") == "1":
                    if since == "latest":
                        records = server.posts.get(topic, [])[-1:]
                    elif window:
                        # duration windows resolve against the *server* clock
                        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[window.group(2)]
                        cutoff = time.time() - int(window.group(1)) * unit
                        records = [r for r in server.posts.get(topic, [])
                                   if r["ts"] >= cutoff]
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


def _remove_pending(name, approval_id):
    """Drop an ask's marker outright; an ended ask leaves a tombstone."""
    with contextlib.suppress(OSError):
        os.remove(an._pending_path(name, approval_id))


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


def _clear_state_queue():
    """Empty the offline queue of the test state dir. A failed send queues
    its notification, and the next successful send in the same process would
    replay it into whatever mock server that test runs."""
    directory = an.queue_dir()
    for name in os.listdir(directory) if os.path.isdir(directory) else []:
        os.remove(os.path.join(directory, name))


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

    def test_quiet_hours_include_the_last_minute_of_the_day(self):
        """00:00-23:59 used to go quiet at 23:58. 24:00 cannot be written."""
        window = [{"start": "00:00", "end": "23:59"}]
        late = an.datetime.datetime(2026, 8, 14, 23, 59)
        self.assertTrue(an.in_quiet_hours(window, late))
        end = an.next_quiet_end(window, late)
        end_dt = an.datetime.datetime.fromtimestamp(end)
        self.assertEqual((end_dt.day, end_dt.hour, end_dt.minute), (15, 0, 0))
        # an ordinary end minute stays exclusive
        daytime = [{"start": "13:00", "end": "14:00"}]
        at_end = an.datetime.datetime(2026, 8, 14, 14, 0)
        self.assertFalse(an.in_quiet_hours(daytime, at_end))
        evening = [{"start": "22:00", "end": "23:59"}]
        self.assertTrue(an.in_quiet_hours(evening, late))
        self.assertFalse(an.in_quiet_hours(
            evening, an.datetime.datetime(2026, 8, 15, 0, 0)))

    def test_invalid_window_ignored(self):
        self.assertFalse(an.in_quiet_hours([{"start": "99:00", "end": "10:00"}]))
        self.assertFalse(an.in_quiet_hours([]))


class TestAnswerParsing(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(an._parse_answer("APPROVED abc123")[0], "approved")
        self.assertEqual(an._parse_answer("approve")[0], "approved")
        self.assertEqual(an._parse_answer("YES")[0], "approved")
        self.assertEqual(an._parse_answer("Ja")[0], "approved")
        self.assertEqual(an._parse_answer("DENIED abc123")[0], "denied")
        self.assertEqual(an._parse_answer("no")[0], "denied")
        self.assertEqual(an._parse_answer("Deploy only staging first")[0], "answer")
        self.assertEqual(an._parse_answer("Deploy only staging first")[1], "Deploy only staging first")
        self.assertEqual(an._parse_answer("")[0], "denied")

    def test_negations_and_the_no_button_label_do_not_approve(self):
        """Typing the suggested deny label, or an obvious no, is a denial.

        Free text that is not a negation stays an answer: "staging" is the
        documented exit-0 reply, not a failed approval.
        """
        cases = (
            "Abort", "abort the migration", "Nein", "Nein, warte", "not yet",
            "don't", "don't deploy", "don’t", "do not ship", "never", "nah",
            "👎", "👎🏻", "👎 later",
            "Stopp", "Stopp sofort", "noch nicht", "nö", "nee", "abbrechen",
            "not now", "Not", "please don't", "absolutely not", "wait",
            "warte", "❌", "🛑", "🚫",
            "nicht jetzt", "Nicht jetzt", "Nicht", "halt", "halt, tests are red",
            "hold on", "later", "later tonight", "bloß nicht", "bloss nicht",
            "Moment", "moment", "✋", "✋🏻", "⛔",
        )
        for text in cases:
            self.assertEqual(an._parse_answer(text)[0], "denied", text)
        kind, reason = an._parse_answer("Abort, tests are red", no_label="Abort")
        self.assertEqual(kind, "denied")
        self.assertEqual(reason, "tests are red")
        self.assertEqual(an._parse_answer("Hold", no_label="Hold")[0], "denied")
        # not a built-in negation, and not the button label: still an answer
        self.assertEqual(an._parse_answer("Hold")[0], "answer")
        self.assertEqual(an._parse_answer("note the file")[0], "answer")
        self.assertEqual(an._parse_answer("nevertheless, ship it")[0], "answer")
        self.assertEqual(an._parse_answer("do nothing yet")[0], "answer")
        self.assertEqual(an._parse_answer("yesterday")[0], "answer")
        self.assertEqual(an._parse_answer("use the staging cluster")[0], "answer")
        # a longer instruction is not the standalone refusal "not" / "nicht";
        # "wait" is a postponement, so "wait for CI, then ship" must not let
        # `ask && deploy` ship now
        self.assertEqual(an._parse_answer("wait for CI, then ship")[0], "denied")
        self.assertEqual(an._parse_answer("not staging — use prod")[0], "answer")
        self.assertEqual(an._parse_answer("nicht staging — use prod")[0], "answer")
        # "halt" denies; a longer word that merely starts with it does not
        self.assertEqual(an._parse_answer("halting problem, ship it")[0], "answer")
        self.assertEqual(an._parse_answer("hold onto the release")[0], "answer")
        self.assertEqual(an._parse_answer("latest is fine")[0], "answer")
        self.assertEqual(an._parse_answer("momentum is fine")[0], "answer")
        # a label must not be interpreted as a regular expression
        self.assertEqual(an._parse_answer("a.b", no_label="a.b")[0], "denied")
        self.assertEqual(an._parse_answer("ab", no_label="a.b")[0], "answer")

    def test_yes_button_label_approves_only_when_it_stands_alone(self):
        self.assertEqual(an._parse_answer("Ship it", yes_label="Ship it")[0], "approved")
        self.assertEqual(an._parse_answer("Ship it!", yes_label="Ship it")[0], "approved")
        self.assertEqual(
            an._parse_answer("Ship it, but canary first", yes_label="Ship it")[0],
            "answer")
        self.assertEqual(an._parse_answer("👍🏻")[0], "approved")
        # the hint says to reply with the yes label. "Stop it" is that label
        # here, so it must not lose to the leading denial "stop".
        self.assertEqual(an._parse_answer("Stop it")[0], "denied")
        self.assertEqual(
            an._parse_answer("Stop it", yes_label="Stop it", no_label="Keep")[0],
            "approved")
        self.assertEqual(
            an._parse_answer("Keep", yes_label="Stop it", no_label="Keep")[0],
            "denied")
        self.assertEqual(
            an._parse_answer("Stop it now", yes_label="Stop it", no_label="Keep")[0],
            "denied")


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


class _EmptyBody:
    """A response whose body cannot be read. urllib does not wrap that read."""

    status = 200

    def __init__(self, error):
        self.error = error

    def read(self):
        raise self.error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestConnectionDrops(unittest.TestCase):
    """A reset or a short read is a transient delivery failure, not a crash.

    urllib wraps some socket errors in URLError and leaves others
    (RemoteDisconnected from getresponse, IncompleteRead from read) raw.
    Those used to skip the retry, the offline queue, and the catches in
    ask, watch, the bot and doctor.
    """

    def _url(self):
        return "http://example.test/topic"

    def test_reset_and_short_read_are_transient(self):
        cases = (
            http.client.RemoteDisconnected("closed"),
            ConnectionResetError("reset"),
            http.client.IncompleteRead(b"partial"),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                with unittest.mock.patch.object(
                        an.OPENER, "open", side_effect=error):
                    with self.assertRaises(an.TransientError) as caught:
                        an.http_request(self._url(), "POST", body="hi")
                self.assertIn(type(error).__name__, str(caught.exception))
            with self.subTest(error=type(error).__name__ + "-body"):
                with unittest.mock.patch.object(
                        an.OPENER, "open", return_value=_EmptyBody(error)):
                    with self.assertRaises(an.TransientError) as caught:
                        an.http_request(self._url())
                self.assertIn("dropped while reading", str(caught.exception))

    def test_a_lost_error_body_keeps_the_status_class(self):
        def error(code):
            err = urllib.error.HTTPError(
                self._url(), code, "x", http.client.HTTPMessage(), io.BytesIO(b""))

            def boom(*args, **kwargs):
                raise http.client.IncompleteRead(b"")

            err.read = boom
            return err

        with unittest.mock.patch.object(an.OPENER, "open", side_effect=error(503)):
            with self.assertRaises(an.TransientError) as caught:
                an.http_request(self._url())
        self.assertIn("HTTP 503", str(caught.exception))
        with unittest.mock.patch.object(an.OPENER, "open", side_effect=error(404)):
            with self.assertRaises(an.PermanentError) as caught:
                an.http_request(self._url())
        self.assertIn("HTTP 404", str(caught.exception))

    def test_subscribe_reports_a_reset_as_a_runtime_error(self):
        cfg = make_config("http://example.test")
        with unittest.mock.patch.object(
                an.OPENER, "open",
                side_effect=http.client.RemoteDisconnected("closed")):
            with self.assertRaises(RuntimeError) as caught:
                an.NtfyChannel(cfg).subscribe("topic", since="90s")
        self.assertNotIsInstance(caught.exception, http.client.RemoteDisconnected)
        self.assertIn("cannot subscribe", str(caught.exception))

    def test_a_dropped_publish_is_queued(self):
        cfg = make_config("http://example.test")
        old = an.RETRY_BACKOFF_SECONDS
        an.RETRY_BACKOFF_SECONDS = (0, 0)
        try:
            with unittest.mock.patch.object(
                    an.OPENER, "open",
                    side_effect=http.client.RemoteDisconnected("closed")):
                result = an.send_notification(cfg, "hello", timeout=1, event="notify")
        finally:
            an.RETRY_BACKOFF_SECONDS = old
            shutil.rmtree(an.queue_dir(), ignore_errors=True)
        self.assertFalse(result["ok"] is False and not result.get("queued"))
        self.assertEqual(result.get("queued"), ["ntfy"])
        self.assertTrue(any(
            record.get("event") == "queued" and record.get("message") == "hello"
            for record in an.read_history()))

    def test_watch_keeps_the_command_exit_when_the_push_is_reset(self):
        cfg = make_config("http://example.test")
        old = an.RETRY_BACKOFF_SECONDS
        an.RETRY_BACKOFF_SECONDS = (0, 0)
        try:
            with unittest.mock.patch.object(
                    an.OPENER, "open",
                    side_effect=ConnectionResetError("reset")):
                result = an.run_watch(
                    cfg, [sys.executable, "-c", "import sys; raise SystemExit(7)"])
        finally:
            an.RETRY_BACKOFF_SECONDS = old
            shutil.rmtree(an.queue_dir(), ignore_errors=True)
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["notification"]["queued"], ["ntfy"])

    def test_doctor_reports_a_reset_instead_of_crashing(self):
        cfg = make_config("http://example.test")
        with unittest.mock.patch.object(
                an.OPENER, "open",
                side_effect=http.client.IncompleteRead(b"")):
            checks = an.doctor_checks(cfg)
        server = next(check for check in checks if check["name"] == "ntfy server")
        self.assertEqual(server["status"], an.FAIL)
        self.assertIn("unreachable", server["detail"])

    def test_stream_reader_reconnects_after_a_short_read(self):
        cfg = make_config("http://127.0.0.1:9", topic="streamdrop")
        waiter = an.ApprovalWaiter(
            cfg, "streamdrop-responses", 5, poll_interval=30, approval_id="ab" * 8)
        calls = {"n": 0}

        class Short:
            def __iter__(self):
                raise http.client.IncompleteRead(b"")

            def close(self):
                pass

        def subscribe(self, topic, since=None, timeout=30.0):
            calls["n"] += 1
            if calls["n"] == 1:
                return Short()
            waiter.stop_event.set()
            raise RuntimeError("stop")

        with unittest.mock.patch.object(an.NtfyChannel, "subscribe", subscribe):
            thread = threading.Thread(target=waiter._reader, daemon=True)
            thread.start()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(calls["n"], 2)
        self.assertTrue(any("stop" in error for error in waiter.errors))


class TestApprovalFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ntfy = MockNtfy()

    @classmethod
    def tearDownClass(cls):
        cls.ntfy.stop()

    def setUp(self):
        # an ask that ended unanswered leaves a tombstone that refuses typed
        # replies for a while; every test starts without one
        shutil.rmtree(os.path.join(an.state_dir(), "ntfy-pending"), ignore_errors=True)

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
        headers = self.ntfy.posts["approvals3"][-1]["headers"]
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
        # exit 0 is cmd_ask's job (denied and timeout are both false). The
        # boolean is not a yes: a gate that only reads `approved` must not
        # treat the environment name as permission.
        self.assertFalse(outcome["approved"])
        self.assertFalse(outcome["denied"])
        self.assertFalse(outcome["timeout"])
        self.assertEqual(outcome["answer"], "staging")

    def test_typing_the_no_button_label_denies(self):
        """The hint tells the user to reply with the no button's label.

        That reply used to come back approved, including from ask_approval,
        which returns this same result.
        """
        cfg = make_config(self.ntfy.url, topic="approvals-label")
        holder, thread = self._run_ask_async(
            cfg, message="Deploy?", timeout_seconds=20, print_status=False,
            no_label="Abort")
        deadline = time.monotonic() + 5
        while not self.ntfy.posts.get("approvals-label") and time.monotonic() < deadline:
            time.sleep(0.05)
        urllib.request.urlopen(
            urllib.request.Request(
                f"{self.ntfy.url}/approvals-label-responses", method="POST",
                data="Abort".encode(),
            )
        ).read()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        outcome = holder["result"]
        self.assertTrue(outcome["denied"])
        self.assertFalse(outcome["approved"])

    def test_old_yes_after_a_prime_blip_does_not_approve(self):
        """One failed listing of the response topic must not drop the guard.

        A "yes" already on the topic is the previous question's answer.
        """
        topic = "primeblip"
        self.ntfy.inject(f"{topic}-responses", "yes", "old-yes-id")
        self.ntfy.get_503_count = 1
        cfg = make_config(self.ntfy.url, topic=topic)
        original = an.ApprovalWaiter.__init__

        def fast(self, *args, **kwargs):
            kwargs["poll_interval"] = 0.3
            original(self, *args, **kwargs)

        an.ApprovalWaiter.__init__ = fast
        try:
            holder, thread = self._run_ask_async(
                cfg, message="Deploy?", timeout_seconds=8, print_status=False)
            time.sleep(2.0)
            self.assertTrue(thread.is_alive(), holder.get("result"))
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.ntfy.url}/{topic}-responses", method="POST",
                    data=b"no",
                )
            ).read()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertTrue(holder["result"]["denied"])
            self.assertFalse(holder["result"]["approved"])
        finally:
            an.ApprovalWaiter.__init__ = original
            self.ntfy.get_503_count = 0

    def test_unreadable_response_topic_refuses_to_wait(self):
        topic = "primefail"
        self.ntfy.inject(f"{topic}-responses", "yes", "stale-yes")
        self.ntfy.get_503_count = 10
        cfg = make_config(self.ntfy.url, topic=topic)
        try:
            with self.assertRaises(RuntimeError) as caught:
                an.run_ask(cfg, "Deploy?", timeout_seconds=5, print_status=False)
            self.assertIn("not waiting", str(caught.exception))
        finally:
            self.ntfy.get_503_count = 0

    def test_prime_retries_a_blip_and_ignores_the_old_id(self):
        cfg = make_config("http://127.0.0.1:9", topic="primeretry")
        waiter = an.ApprovalWaiter(
            cfg, "primeretry-responses", 5, poll_interval=30, approval_id="ab" * 8)
        calls = {"n": 0}

        def flaky(self, topic, since, timeout=10.0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise an.TransientError("blip")
            return [{"id": "old-yes", "event": "message", "message": "yes"}]

        with unittest.mock.patch.object(an.NtfyChannel, "poll", flaky), \
             unittest.mock.patch.object(
                 an.NtfyChannel, "subscribe", side_effect=RuntimeError("no stream")):
            waiter.start()
            waiter.stop_event.set()
            for thread in waiter._threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(calls["n"], 2)
        self.assertIn("old-yes", waiter.seen)
        waiter._offer("old-yes", "yes")
        self.assertTrue(waiter.messages.empty())

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
        self.old_home = _set_home(self.home)
        self.old_argv0 = sys.argv[0]
        sys.argv[0] = _fake_launcher(self.tmp)

    def tearDown(self):
        _restore_home(self.old_home)
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
        stop_hooks = [h for g in data["hooks"]["Stop"] for h in g["hooks"]]
        # shape-independent: on Windows the binary is quoted ('C:\...\agentbell')
        self.assertTrue(any(an._is_our_hook_command(h["command"])
                            and "hook run_completed --agent claude" in h["command"]
                            for h in stop_hooks), stop_hooks)
        # idempotent
        result2 = an.install_hooks("claude")
        self.assertFalse(result2["changed"])

    def test_http_hook_with_headers_does_not_crash_install(self):
        """An HTTP hook stores headers as an object. That used to be unhashable
        and hooks install died before writing anything.
        """
        path = self._settings("claude")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{
                "type": "http",
                "url": "https://example.test/hook",
                "headers": {"Authorization": "Bearer keep-me"},
                "args": ["one", "two"],
            }]}]}}, fh)
        result = an.install_hooks("claude")
        self.assertTrue(result["changed"])
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        kept = data["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(kept["headers"]["Authorization"], "Bearer keep-me")
        self.assertEqual(kept["args"], ["one", "two"])
        self.assertIn("Stop", data["hooks"])
        self.assertFalse(an.install_hooks("claude")["changed"])

    def test_claude_uninstall(self):
        an.install_hooks("claude")
        result = an.install_hooks("claude", add=False)
        self.assertTrue(result["changed"])
        with open(self._settings("claude")) as fh:
            data = json.load(fh)
        for event in ("Stop", "StopFailure", "Notification"):
            self.assertNotIn(event, data.get("hooks", {}))

    def test_checkout_py_binary_uninstalls_cleanly(self):
        # dev checkout / test-runner fallback: the binary is agentbell.py.
        # uninstall and self-heal must still recognize the hook as ours
        # (regression: the substring matcher knew only the launcher shape).
        old_argv0, old_which = sys.argv[0], an.shutil.which
        sys.argv[0] = _fake_launcher(self.tmp, "agentbell.py")
        an.shutil.which = lambda prog: None
        try:
            an.install_hooks("claude")
            self.assertTrue(an._file_contains_our_hook(self._settings("claude")))
            result = an.install_hooks("claude", add=False)
        finally:
            sys.argv[0], an.shutil.which = old_argv0, old_which
        self.assertTrue(result["changed"])
        with open(self._settings("claude")) as fh:
            data = json.load(fh)
        for event in ("Stop", "StopFailure", "Notification"):
            self.assertNotIn(event, data.get("hooks", {}))

    def test_our_hook_command_shapes(self):
        ours = [
            "/usr/local/bin/agentbell hook run_completed --agent x",
            "/repo/agentbell.py hook started --agent x --silent",
            "C:\\Users\\Jo\\agentbell.exe hook run_completed --agent x",
            "'C:\\Users\\Jo Do\\agentbell.exe' hook run_completed --agent x",
            "'/opt/a b/agentbell' hook input_required --agent x",
        ]
        foreign = [
            "lint.sh",
            "otherbell hook run_completed --agent x",
            "/usr/local/bin/agentbell-helper hook run_completed --agent x",
            "/usr/local/bin/agentbell notify hi",
            "bash -c 'agentbell hook run_completed --agent x'",
        ]
        for command in ours:
            self.assertTrue(an._is_our_hook_command(command), command)
        for command in foreign:
            self.assertFalse(an._is_our_hook_command(command), command)

    def test_user_wrapped_hook_is_visible_and_never_duplicated(self):
        path = self._settings("claude")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wrapper = "bash -c 'agentbell hook run_completed --agent claude'"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": wrapper}
            ]}]}}, fh)
        status = {agent: value for agent, value, _, _ in an.hooks_status()}
        self.assertEqual(status["claude"], "user wrapper")
        result = an.install_hooks("claude")
        self.assertFalse(result["changed"])
        self.assertTrue(any("did not add a second" in note
                            for note in result["notes"]))
        with open(path, encoding="utf-8") as fh:
            commands = an._json_hook_commands(path)
        self.assertEqual(commands, [wrapper])

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
        self.assertTrue(an._is_our_hook_command(hook["command"]), hook)
        self.assertIn("hook run_completed --agent gemini", hook["command"])
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

    def test_codex_keeps_host_tables_written_inside_the_block(self):
        """Codex writes its own tables between the agentbell markers.

        Reinstall and uninstall must move those tables out, not delete them.
        A hook the user added in the same region stays too.
        """
        path = an.codex_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        foreign = (
            "[hooks.state]\n"
            'trust = "agent-ops-hash"\n'
            "\n"
            "[tui]\n"
            'theme = "dark"\n'
            "\n"
            "[plugins.example]\n"
            "enabled = true\n"
            "\n"
            "[[hooks.Stop]]\n"
            "[[hooks.Stop.hooks]]\n"
            'type = "command"\n'
            'command = "lint.sh"\n'
        )
        block = an.codex_hooks_block()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('model = "gpt-5"\n' + block.replace(
                an.TOML_END, "\n" + foreign + an.TOML_END, 1))
        os.chmod(path, 0o600)
        result = an.install_codex_hooks()
        self.assertTrue(result["changed"])
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertLess(text.index(an.TOML_END), text.index("[hooks.state]"))
        self.assertLess(text.index(an.TOML_END), text.index("[tui]"))
        self.assertLess(text.index(an.TOML_END), text.index("[plugins.example]"))
        self.assertIn('trust = "agent-ops-hash"', text)
        self.assertIn('theme = "dark"', text)
        self.assertIn("enabled = true", text)
        self.assertIn('command = "lint.sh"', text)
        self.assertIn('model = "gpt-5"', text)
        self.assertIn("hook run_completed --agent codex", text)
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        again = an.install_codex_hooks()
        self.assertFalse(again["changed"])
        an.uninstall_codex_hooks()
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(an.TOML_START, text)
        self.assertNotIn("hook run_completed --agent codex", text)
        self.assertIn('trust = "agent-ops-hash"', text)
        self.assertIn('theme = "dark"', text)
        self.assertIn("[plugins.example]", text)
        self.assertIn('command = "lint.sh"', text)
        self.assertIn('model = "gpt-5"', text)
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_codex_flag_is_its_own_line_without_a_trailing_newline(self):
        path = an.codex_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('model = "gpt-5"')
        an.install_codex_hooks()
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('model = "gpt-5"\n', text)
        self.assertNotIn('"gpt-5"features.hooks', text)
        self.assertIn("\nfeatures.hooks = true", text)
        an.uninstall_codex_hooks()
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("features.hooks", text)
        self.assertIn('model = "gpt-5"', text)
        # a flag already glued to the last line is removed, not left behind
        glued = ('model = "gpt-5"features.hooks = true  ' + an.CODEX_FLAG_MARKER
                 + "\n" + an.TOML_START + "\n" + an.TOML_END + "\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(glued)
        an.uninstall_codex_hooks()
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("features.hooks", text)
        self.assertEqual(text.strip(), 'model = "gpt-5"')

    def test_codex_uninstall_without_an_end_marker_leaves_the_file(self):
        path = an.codex_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        original = 'model = "gpt-5"\n' + an.TOML_START + "\n[[hooks.Stop]]\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)
        self.assertFalse(an.uninstall_codex_hooks())
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)

    def test_user_hook_attached_to_our_stop_group_stays_an_array(self):
        """A second [[hooks.Stop.hooks]] under our Stop element must keep its parent.

        Uninstall used to delete [[hooks.Stop]] because the next chunk was
        our own hook, and the user's later hook was left with no parent.
        TOML then reads hooks.Stop as a table instead of an array.
        """
        path = an.codex_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        an.install_codex_hooks()
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        extra = "[[hooks.Stop.hooks]]\n" 'command = "lint.sh"\n'
        self.assertIn(an.TOML_END, text)
        text = text.replace(an.TOML_END, extra + an.TOML_END, 1)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.assertTrue(an.uninstall_codex_hooks())
        with open(path, encoding="utf-8") as fh:
            result = fh.read()
        self.assertNotIn("hook run_completed --agent codex", result)
        self.assertIn('command = "lint.sh"', result)
        lint_at = result.index("lint.sh")
        parent_at = result.rindex("[[hooks.Stop]]", 0, lint_at)
        self.assertLess(parent_at, lint_at)
        if HAS_TOMLLIB:
            import tomllib
            data = tomllib.loads(result)
            self.assertIsInstance(data["hooks"]["Stop"], list)
            commands = [
                hook.get("command")
                for group in data["hooks"]["Stop"]
                for hook in group.get("hooks", [])
            ]
            self.assertEqual(commands, ["lint.sh"])

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

    def test_opencode_stale_plugin_is_update_needed_until_reinstalled(self):
        """An older plugin file (pre-1.6.1 logic, or a moved binary) must be
        visible as 'update needed' - status observes, install repairs."""
        project = os.path.join(self.tmp, "proj_stale")
        path = os.path.join(project, ".opencode", "plugin", "agentbell.js")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("// agentbell: old plugin without idle dedupe\nsession.idle\n")
        status = {a: s for a, s, _, _ in an.hooks_status(project=project)}
        self.assertEqual(status["opencode"], "update needed")
        self.assertTrue(an.install_hooks("opencode", project=project)["changed"])
        status = {a: s for a, s, _, _ in an.hooks_status(project=project)}
        self.assertEqual(status["opencode"], "installed")
        with open(path) as fh:
            text = fh.read()
        self.assertIn("--min-duration", text)
        self.assertIn("IDLE_DEDUPE_MS", text)
        self.assertNotIn("__MIN_DURATION__", text)

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

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_opencode_reinstall_writes_the_v2_loadable_template(self):
        """OpenCode 2.0.16 rejects a plugin module that has no default export
        with `id` + `setup` ("Plugin must export a default definition with an
        id and an effect or setup function"), so a reinstall has to repair an
        install that only carries the 1.x named export. The written file must
        be exactly what `_render_opencode_plugin()` produces - the same bytes
        the node tests replay - and it must parse as an ES module."""
        project = os.path.join(self.tmp, "proj_v2")
        os.makedirs(project)
        path = os.path.join(project, ".opencode", "plugin", "agentbell.js")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("// agentbell 1.x only\nexport const AgentBell = async () => ({});\n")
        self.assertTrue(an.install_hooks("opencode", project=project)["changed"])
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, an._render_opencode_plugin())
        for marker in ("export default {", "setup(ctx)", "payloadOf",
                       "session.execution.succeeded", "session.execution.failed",
                       "session.execution.interrupted", "session.idle",
                       "permission.asked", "permission.updated"):
            self.assertIn(marker, text)
        # a second run changes nothing, and the file still parses
        self.assertFalse(an.install_hooks("opencode", project=project)["changed"])
        self.assertFalse(an.opencode_plugin_stale(project))
        check = os.path.join(self.tmp, "proj_v2_check.mjs")
        with open(check, "w", encoding="utf-8") as fh:
            fh.write(text)
        out = subprocess.run(["node", "--check", check],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_opencode_install_migrates_legacy_agents_md_block(self):
        project = os.path.join(self.tmp, "proj_legacy")
        os.makedirs(project)
        agents_md = os.path.join(project, "AGENTS.md")
        with open(agents_md, "w") as fh:
            fh.write("# Existing rules\n" + an.BLOCK_START
                     + "\nold: run `agentbell hook run_completed --agent opencode`\n"
                     + an.BLOCK_END + "\n")
        an.install_hooks("opencode", project=project)
        with open(agents_md) as fh:
            text = fh.read()
        self.assertEqual(text.strip(), "# Existing rules")
        self.assertTrue(os.path.exists(
            os.path.join(project, ".opencode", "plugin", "agentbell.js")))

    def test_opencode_install_leaves_aider_block_alone(self):
        """v1.6.1 regression: `hooks install opencode` removed *any* agentbell
        block from AGENTS.md as the v1.3rc migration and deleted the file -
        taking the project's Aider block with it. Removal is owner-scoped."""
        project = os.path.join(self.tmp, "proj_aider_keep")
        os.makedirs(project)
        an.install_hooks("aider", project=project)
        agents_md = os.path.join(project, "AGENTS.md")
        with open(agents_md) as fh:
            before = fh.read()
        self.assertIn("--agent aider", before)
        an.install_hooks("opencode", project=project)
        self.assertTrue(os.path.exists(agents_md))
        with open(agents_md) as fh:
            self.assertEqual(fh.read(), before)
        # and the other way round: uninstalling Aider never touches a
        # block that is not Aider's
        with open(agents_md, "w") as fh:
            fh.write(an.BLOCK_START + "\nlegacy --agent opencode\n" + an.BLOCK_END + "\n")
        self.assertFalse(an.install_hooks("aider", project=project, add=False)["changed"])
        self.assertTrue(os.path.exists(agents_md))
        # an unrecognizable block (no owner named) is left alone as well
        with open(agents_md, "w") as fh:
            fh.write(an.BLOCK_START + "\nold\n" + an.BLOCK_END + "\n")
        self.assertFalse(an.install_hooks("opencode", project=project)["changed"]
                         and not os.path.exists(agents_md))
        self.assertTrue(os.path.exists(agents_md))

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
        # the healed block is the current one verbatim (TOML-escapes the
        # binary on Windows, so a raw-path assertIn would never match there)
        self.assertIn(an.kimi_hooks_block(), text)
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

    def test_kimi_install_does_not_duplicate_hooks_without_markers(self):
        """Kimi has removed the marker comments while leaving the hooks live.

        A second install must not append another copy, and status must not
        report the hooks as missing — that is the advice that duplicates them.
        Uninstall still removes those commands: the command string is exactly
        ours, which is not a guess.
        """
        path = an.kimi_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        an.install_kimi_hooks()
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        stripped = text.replace(an.TOML_START + "\n", "").replace(an.TOML_END + "\n", "")
        self.assertNotIn(an.TOML_START, stripped)
        self.assertIn("hook started --agent kimi", stripped)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(stripped)
        self.assertTrue(an.AGENT_SPECS["kimi"]["status"](None))
        result = an.install_kimi_hooks()
        self.assertFalse(result["changed"])
        self.assertTrue(any("markers are gone" in note for note in result["notes"]))
        with open(path, encoding="utf-8") as fh:
            again = fh.read()
        self.assertEqual(again.count("hook started --agent kimi"), 1)
        self.assertEqual(again.count("hook run_completed --agent kimi"), 1)
        self.assertEqual(again.count("hook run_failed --agent kimi"), 1)
        removed = an.uninstall_kimi_hooks()
        self.assertTrue(removed["changed"])
        self.assertTrue(any("markers were already gone" in note for note in removed["notes"]))
        with open(path, encoding="utf-8") as fh:
            cleared = fh.read()
        self.assertNotIn("hook started --agent kimi", cleared)
        self.assertNotIn("hook run_completed --agent kimi", cleared)
        self.assertNotIn("hook run_failed --agent kimi", cleared)

    def test_kimi_keeps_a_foreign_table_inside_the_markers(self):
        path = an.kimi_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        block = an.kimi_hooks_block()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(block.replace(
                an.TOML_END, '\n[model]\nname = "k2"\n' + an.TOML_END, 1))
        os.chmod(path, 0o600)
        result = an.install_kimi_hooks()
        self.assertTrue(result["changed"])
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertLess(text.index(an.TOML_END), text.index("[model]"))
        self.assertIn('name = "k2"', text)
        self.assertIn("hook started --agent kimi", text)
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertFalse(an.install_kimi_hooks()["changed"])
        self.assertTrue(an.uninstall_kimi_hooks()["changed"])
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('name = "k2"', text)
        self.assertNotIn("hook started --agent kimi", text)
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_kimi_uninstall_without_markers_removes_only_our_hooks(self):
        """Kimi strips the marker comments. Uninstall must still delete the
        three hook tables whose command is ours, and must keep a user's own
        [[hooks]] table plus the rest of the file. The removal plan used to
        say the hooks were already gone while Kimi kept calling them.
        """
        path = an.kimi_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        an.install_kimi_hooks()
        with open(path, encoding="utf-8") as fh:
            stripped = (fh.read()
                        .replace(an.TOML_START + "\n", "")
                        .replace(an.TOML_END + "\n", ""))
        user = '[[hooks]]\nevent = "Stop"\ncommand = "echo mine"\n'
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('[model]\nname = "k2"\n\n' + stripped + "\n" + user)
        self.assertTrue(an.AGENT_SPECS["kimi"]["status"](None))
        entries = [entry for entry in an._agent_hook_entries()
                   if entry["label"].startswith("kimi ")]
        self.assertEqual(len(entries), 1)
        self.assertIn("remove the agentbell hooks", entries[0]["action"])
        self.assertTrue(entries[0]["apply"]())
        with open(path, encoding="utf-8") as fh:
            left = fh.read()
        self.assertNotIn("hook started --agent kimi", left)
        self.assertNotIn("hook run_completed --agent kimi", left)
        self.assertNotIn("hook run_failed --agent kimi", left)
        self.assertIn('command = "echo mine"', left)
        self.assertIn('name = "k2"', left)
        self.assertFalse(an.AGENT_SPECS["kimi"]["status"](None))
        self.assertFalse(an.install_hooks("kimi", add=False)["changed"])

    def test_kimi_uninstall_without_markers_leaves_a_wrapper_and_says_so(self):
        path = an.kimi_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wrapped = "bash -c 'agentbell hook run_completed --agent kimi'"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('[[hooks]]\nevent = "Stop"\ncommand = '
                     + an.toml_string(wrapped) + "\n")
        self.assertTrue(an.AGENT_SPECS["kimi"]["status"](None))
        result = an.install_hooks("kimi", add=False)
        self.assertFalse(result["changed"])
        self.assertTrue(any("left in place" in note for note in result["notes"]))
        with open(path, encoding="utf-8") as fh:
            self.assertIn("bash -c", fh.read())
        self.assertTrue(an.AGENT_SPECS["kimi"]["status"](None))

    @unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
    def test_kimi_uninstall_updates_a_symlinked_config(self):
        path = an.kimi_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        real = os.path.join(self.tmp, "dotfiles-kimi.toml")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write('[model]\nname = "k2"\n' + an.kimi_hooks_block())
        os.chmod(real, 0o600)
        os.symlink(real, path)
        removed = an.uninstall_kimi_hooks()
        self.assertTrue(removed["changed"])
        self.assertTrue(os.path.islink(path))
        self.assertEqual(os.path.realpath(path), os.path.realpath(real))
        with open(real, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('name = "k2"', text)
        self.assertNotIn("hook started --agent kimi", text)
        self.assertFalse(os.path.islink(real))
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)

    def test_quoted_hash_in_a_toml_header_is_not_a_comment(self):
        self.assertEqual(
            an._toml_header_key('[projects."/home/u/C#/app"]'),
            '[projects."/home/u/C#/app"]')
        self.assertEqual(
            an._toml_header_key("[projects.'/home/u/C#/app']"),
            "[projects.'/home/u/C#/app']")
        self.assertEqual(
            an._toml_header_key('[projects."/home/u/C#/app"] # note'),
            '[projects."/home/u/C#/app"]')
        self.assertEqual(an._toml_header_key("[hooks.state] # note"), "[hooks.state]")
        self.assertEqual(an._toml_header_key("# [not a header]"), "")

    def test_codex_keeps_a_table_whose_key_contains_a_hash(self):
        path = an.codex_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        foreign = '[projects."/home/u/C#/app"]\nroot = "/home/u/C#/app"\n'
        block = an.codex_hooks_block()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('model = "gpt-5"\n' + block.replace(
                an.TOML_END, "\n" + foreign + an.TOML_END, 1))
        an.install_codex_hooks()
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('[projects."/home/u/C#/app"]', text)
        self.assertIn('root = "/home/u/C#/app"', text)
        self.assertLess(text.index(an.TOML_END), text.index('[projects."/home/u/C#/app"]'))
        an.uninstall_codex_hooks()
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('[projects."/home/u/C#/app"]', text)
        self.assertIn('root = "/home/u/C#/app"', text)
        self.assertNotIn("hook run_completed --agent codex", text)

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
        stop_hooks = [h for g in data["hooks"]["Stop"] for h in g["hooks"]]
        self.assertTrue(any(an._is_our_hook_command(h["command"])
                            and "hook run_completed --agent qwen-code" in h["command"]
                            for h in stop_hooks), stop_hooks)
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

    def test_aider_stale_block_self_heals_without_touching_foreign_content(self):
        project = os.path.join(self.tmp, "proj_aider_stale")
        os.makedirs(project)
        path = os.path.join(project, "AGENTS.md")
        old = (
            "# Project rules\n\n"
            + an.BLOCK_START + "\n"
            + "## Notifications (agentbell)\n\n"
            + "- run `agentbell hook run_completed --agent aider`\n"
            + an.BLOCK_END + "\n\n"
            + "# Keep this section\n"
        )
        with open(path, "w") as fh:
            fh.write(old)

        status = {agent: state for agent, state, _, _ in
                  an.hooks_status(project=project)}
        self.assertEqual(status["aider"], "update needed")

        result = an.install_hooks("aider", project=project)
        self.assertTrue(result["changed"])
        with open(path) as fh:
            healed = fh.read()
        self.assertIn("# Project rules", healed)
        self.assertIn("# Keep this section", healed)
        self.assertIn(an.AIDER_SCOPE_NOTICE, healed)
        self.assertEqual(healed.count(an.BLOCK_START), 1)
        self.assertEqual(healed.count(an.BLOCK_END), 1)
        self.assertNotIn("- run `agentbell hook", healed)

        status = {agent: state for agent, state, _, _ in
                  an.hooks_status(project=project)}
        self.assertEqual(status["aider"], "installed")
        self.assertFalse(an.install_hooks("aider", project=project)["changed"])

    def test_hooks_status_prints_prominent_aider_update_banner(self):
        project = os.path.join(self.tmp, "proj_aider_banner")
        os.makedirs(project)
        with open(os.path.join(project, "AGENTS.md"), "w") as fh:
            fh.write(an.BLOCK_START + "\n--agent aider\n" + an.BLOCK_END + "\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            an.cmd_hooks(_Args(sub="status", project=project))
        out = buf.getvalue()
        self.assertIn("ACTION REQUIRED", out)
        self.assertIn("AGENTS.md", out)
        self.assertIn("agentbell hooks install aider", out)
        self.assertIn("+", out)

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

    def test_protocol_refuses_a_tool_call_that_breaks_its_schema(self):
        """The stdio surface reports a malformed call as a tool error, not a
        push: the user live-tested `notify` without `message` and got a real
        empty notification."""
        _clear_state_queue()
        ntfy = MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = make_config(ntfy.url, topic="stdio-validation")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "notify", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "notify", "arguments": {"message": "stdio ok"}}},
        ]
        payload = "\n".join(json.dumps(r) for r in requests) + "\n"
        original = an.Config
        an.Config = lambda *a, **kw: cfg
        old_stdin, old_stdout = sys.stdin, sys.stdout
        try:
            sys.stdin = io.TextIOWrapper(io.BytesIO(payload.encode()))
            sys.stdout = stdout_buf = io.TextIOWrapper(io.BytesIO())
            an.mcp_loop()
            stdout_buf.flush()
            output = stdout_buf.buffer.getvalue().decode()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
            an.Config = original
        lines = [json.loads(line) for line in output.strip().splitlines()]
        self.assertTrue(lines[0]["result"]["isError"])
        self.assertIn("message", lines[0]["result"]["content"][0]["text"])
        self.assertNotIn("isError", lines[1]["result"])
        self.assertEqual(lines[1]["result"]["content"][0]["text"], "sent")
        posts = ntfy.posts.get("stdio-validation", [])
        self.assertEqual([p["body"] for p in posts], ["stdio ok"])


class TestMCPArgumentValidation(unittest.TestCase):
    """A call that breaks the schema `tools/list` advertised must be refused
    before it can notify, ask or write history - the tools' only job is the
    side effect, so "invalid" has to mean "nothing happened"."""

    def setUp(self):
        _clear_state_queue()
        self.ntfy = MockNtfy()
        self.cfg = make_config(self.ntfy.url, topic="mcpvalidation")
        self._original = an.Config
        an.Config = lambda *a, **kw: self.cfg

    def tearDown(self):
        an.Config = self._original
        self.ntfy.stop()

    def _refused(self, name, arguments):
        """Call a known tool with a malformed payload; both side-effect
        functions are booby-trapped, so reaching either one fails loudly."""
        with unittest.mock.patch.object(
                an, "send_notification",
                side_effect=AssertionError("send_notification was reached")):
            with unittest.mock.patch.object(
                    an, "run_ask",
                    side_effect=AssertionError("run_ask was reached")):
                with self.assertRaises(RuntimeError) as ctx:
                    an.mcp_tool_call(name, arguments)
        return str(ctx.exception)

    def test_notify_requires_message(self):
        self.assertIn("message", self._refused("notify", {}))

    def test_notify_rejects_null_message(self):
        self.assertIn("message", self._refused("notify", {"message": None}))

    def test_notify_rejects_a_non_string_message(self):
        self.assertIn("message", self._refused("notify", {"message": 42}))
        self.assertIn("string", self._refused("notify", {"message": ["hi"]}))

    def test_notify_rejects_a_blank_message(self):
        for blank in ("", "   ", "\n\t "):
            with self.subTest(blank=blank):
                self.assertIn("message", self._refused("notify", {"message": blank}))

    def test_notify_rejects_an_unknown_priority(self):
        self.assertIn("priority",
                      self._refused("notify", {"message": "hi", "priority": "whenever"}))
        self.assertIn("priority",
                      self._refused("notify", {"message": "hi", "priority": "URGENT"}))

    def test_notify_rejects_a_wrongly_typed_optional_field(self):
        self._refused("notify", {"message": "hi", "priority": 4})
        self._refused("notify", {"message": "hi", "title": 7})
        self._refused("notify", {"message": "hi", "tags": ["a", "b"]})
        self._refused("notify", {"message": "hi", "agent": 9})

    def test_ask_requires_a_non_blank_question(self):
        self.assertIn("message", self._refused("ask_approval", {}))
        self.assertIn("message", self._refused("ask_approval", {"message": None}))
        self.assertIn("message", self._refused("ask_approval", {"message": "  "}))

    def test_ask_rejects_a_non_integer_timeout(self):
        self._refused("ask_approval", {"message": "ok?", "timeout_seconds": "60"})
        self._refused("ask_approval", {"message": "ok?", "timeout_seconds": 1.5})
        self._refused("ask_approval", {"message": "ok?", "timeout_seconds": True})

    def test_ask_rejects_a_non_string_label(self):
        self._refused("ask_approval", {"message": "ok?", "yes_label": 1})

    def test_a_refused_call_leaves_no_history_behind(self):
        before = len(an.read_history(limit=0))
        self._refused("notify", {})
        self._refused("ask_approval", {})
        self.assertEqual(len(an.read_history(limit=0)), before)
        self.assertEqual(self.ntfy.posts.get("mcpvalidation", []), [])

    def test_unknown_tool_still_names_itself(self):
        with self.assertRaises(RuntimeError) as ctx:
            an.mcp_tool_call("make_coffee", {"message": "hi"})
        self.assertIn("make_coffee", str(ctx.exception))

    def test_a_valid_notify_keeps_its_defaults(self):
        result = an.mcp_tool_call("notify", {"message": "mcp validation ok"})
        self.assertEqual(result, "sent")
        posts = self.ntfy.posts["mcpvalidation"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"], "mcp validation ok")
        self.assertEqual(posts[0]["headers"]["Priority"], "3")
        recs = [r for r in an.read_history(limit=0)
                if r.get("message") == "mcp validation ok"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "notify")
        self.assertEqual(recs[0]["priority"], "normal")

    def test_explicit_nulls_on_optional_fields_keep_the_defaults(self):
        result = an.mcp_tool_call("notify", {"message": "mcp null options",
                                            "title": None, "priority": None,
                                            "tags": None, "agent": None})
        self.assertEqual(result, "sent")
        recs = [r for r in an.read_history(limit=0)
                if r.get("message") == "mcp null options"]
        self.assertEqual(recs[0]["priority"], "normal")
        self.assertIsNone(recs[0]["title"])
        self.assertNotIn("agent", recs[0])

    def test_an_unknown_key_is_still_tolerated(self):
        result = an.mcp_tool_call("notify", {"message": "mcp extra key", "channel": "ntfy"})
        self.assertEqual(result, "sent")

    def test_a_valid_ask_reaches_run_ask_with_the_advertised_defaults(self):
        seen = {}

        def fake_run_ask(cfg, message, **kwargs):
            seen["message"] = message
            seen.update(kwargs)
            return {"timeout": True}

        with unittest.mock.patch.object(an, "run_ask", fake_run_ask):
            outcome = an.mcp_tool_call("ask_approval", {"message": "ship it?"})
        self.assertEqual(json.loads(outcome), {"timeout": True})
        self.assertEqual(seen["message"], "ship it?")
        self.assertEqual(seen["timeout_seconds"], an.MCP_ASK_DEFAULT_TIMEOUT)
        self.assertEqual(seen["yes_label"], "Approve")
        self.assertEqual(seen["no_label"], "Deny")
        self.assertFalse(seen["print_status"])

    def test_a_valid_ask_clamps_an_out_of_range_timeout(self):
        seen = {}

        def fake_run_ask(cfg, message, **kwargs):
            seen.update(kwargs)
            return {"timeout": True}

        for raw, expected in ((0, an.MCP_ASK_DEFAULT_TIMEOUT),
                              (5, 10),
                              (10 ** 6, an.MCP_ASK_MAX_TIMEOUT)):
            with self.subTest(timeout_seconds=raw):
                with unittest.mock.patch.object(an, "run_ask", fake_run_ask):
                    an.mcp_tool_call("ask_approval",
                                     {"message": "ok?", "timeout_seconds": raw})
                self.assertEqual(seen["timeout_seconds"], expected)


class TestMCPToolSignatures(unittest.TestCase):
    """The advertised schema is the contract the validator enforces: a field
    that shows up as required in `tools/list` must be required here."""

    def test_every_required_field_is_read_by_the_tool(self):
        for tool in an.MCP_TOOLS:
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                self.assertEqual(schema["required"], ["message"])
                self.assertEqual(schema["properties"]["message"]["type"], "string")

    def test_signature_marks_optional_fields(self):
        tools = {tool["name"]: an.mcp_tool_signature(tool) for tool in an.MCP_TOOLS}
        self.assertTrue(tools["notify"].startswith("notify(message,"))
        self.assertIn("title?", tools["notify"])
        self.assertTrue(tools["ask_approval"].startswith("ask_approval(message,"))
        self.assertIn("timeout_seconds?", tools["ask_approval"])



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
                    f"{mock.url}/pollonly-responses", method="POST", data=b"yes"
                )
            ).read()
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertTrue(holder["result"]["approved"])
            self.assertFalse(holder["result"]["timeout"])
        finally:
            mock.stop()

    def test_prime_and_poller_use_server_side_windows(self):
        """Regression (clock drift, DECISIONS §16i): the ask receiver's prime
        and poll requests must use server-relative duration windows, never a
        local epoch cursor - a local clock ahead of the server's blinded the
        poll fallback exactly when the stream was down."""
        mock = MockNtfy(stream_enabled=False)
        seen = []
        original = an.NtfyChannel.poll

        def spy(self, topic, since, timeout=10.0):
            seen.append(since)
            return original(self, topic, since, timeout=timeout)

        an.NtfyChannel.poll = spy
        try:
            cfg = make_config(mock.url, topic="driftwin")
            waiter = an.ApprovalWaiter(cfg, "driftwin-responses", 5, poll_interval=0.05)
            waiter.start()
            time.sleep(0.3)
        finally:
            waiter.stop_event.set()
            an.NtfyChannel.poll = original
            mock.stop()
        self.assertGreaterEqual(len(seen), 2)   # prime + at least one poll
        for since in seen:
            self.assertIsInstance(since, str)
            self.assertRegex(since, r"^\d+s$")
        self.assertGreaterEqual(int(seen[0][:-1]), an.NTFY_LOOKBACK_MARGIN_SECONDS)


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
                        "date": int(time.time()),
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
        # Telegram stamps every message with its server time
        if isinstance(update.get("message"), dict):
            update["message"].setdefault("date", int(time.time()))
        self.updates.append(update)

    def last(self, method):
        return next((r for r in reversed(self.requests) if r["method"] == method), None)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class _TelegramFixture(unittest.TestCase):
    """Base: points the Telegram API at a mock."""

    @classmethod
    def setUpClass(cls):
        cls.tg = MockTelegram()
        cls.old_base = an.TG_API_BASE
        an.TG_API_BASE = cls.tg.url

    @classmethod
    def tearDownClass(cls):
        an.TG_API_BASE = cls.old_base
        cls.tg.stop()

    def setUp(self):
        try:
            os.remove(an._bot_state_path())
        except OSError:
            pass
        # no tombstone of an earlier test's ask may refuse this test's replies
        for name in ("ntfy-pending", "tg-pending"):
            shutil.rmtree(os.path.join(an.state_dir(), name), ignore_errors=True)

    def _tg_cfg(self, ntfy_url="https://ntfy.sh", channels=("telegram",)):
        return an.Config({
            "ntfy": {"server": ntfy_url, "topic": "tgtopic", "auth": None},
            "telegram": {"bot_token": "tok123", "chat_id": "42"},
            "channels": list(channels),
            "quiet_hours": [],
            "quiet_hours_min_priority": 3,
            "approval_timeout": 60,
            "webhook": {"listen": "127.0.0.1", "port": 0, "token": None},
        })


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

    def _interrupt_child(self, signum, exit_code, topic):
        """A child that takes longer than subprocess.run's 0.25s SIGKILL window.

        watch runs as its own process, and the signal comes from this one,
        outside watch's process group: the child sees it only if watch
        forwards it, and it exits with `exit_code` only if it is allowed to
        finish.
        """
        directory = tempfile.mkdtemp()
        ready = os.path.join(directory, "ready")
        script = os.path.join(directory, "child.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(
                "import signal, time, sys\n"
                "def stop(signum, frame):\n"
                "    deadline = time.monotonic() + 0.6\n"
                "    while time.monotonic() < deadline:\n"
                "        time.sleep(0.05)\n"
                f"    raise SystemExit({exit_code})\n"
                "signal.signal(signal.SIGINT, stop)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                f"open({ready!r}, 'w').close()\n"
                "time.sleep(3)\n"
                "raise SystemExit(0)\n"
            )
        config_dir = os.path.join(directory, "config")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"ntfy": {"server": self.ntfy.url, "topic": topic}}, fh)
        # the state dir stays this module's, so read_history() sees the record
        env = dict(os.environ, AGENTBELL_CONFIG_DIR=config_dir, HOME=directory,
                   USERPROFILE=directory, XDG_CONFIG_HOME=directory, XDG_STATE_HOME=directory)
        before = len(self.ntfy.posts.get(topic, []))
        started = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(an.__file__), "watch", "--", sys.executable, script],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not os.path.exists(ready) and time.monotonic() < deadline:
                time.sleep(0.02)
            proc.send_signal(signum)
            out, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            shutil.rmtree(directory, ignore_errors=True)
        result = {"exit_code": proc.returncode, "message": out.decode("utf-8", "replace")}
        return result, time.monotonic() - started, before

    @unittest.skipIf(os.name == "nt", "POSIX process-group signals")
    def test_ctrl_c_lets_the_child_finish_and_still_notifies(self):
        result, elapsed, before = self._interrupt_child(signal.SIGINT, 3, "watch-sigint")
        self.assertEqual(result["exit_code"], 3)
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertIn("failed (exit 3)", result["message"])
        posts = self.ntfy.posts["watch-sigint"]
        self.assertEqual(len(posts), before + 1)
        self.assertIn("exit 3", posts[-1]["body"])
        self.assertEqual(posts[-1]["headers"]["Priority"], "5")
        self.assertTrue(any(
            record.get("event") == "watch" and "exit 3" in (record.get("message") or "")
            for record in an.read_history()))

    @unittest.skipIf(os.name == "nt", "POSIX process-group signals")
    def test_sigterm_lets_the_child_finish_and_still_notifies(self):
        result, elapsed, before = self._interrupt_child(signal.SIGTERM, 4, "watch-sigterm")
        self.assertEqual(result["exit_code"], 4)
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertIn("failed (exit 4)", result["message"])
        posts = self.ntfy.posts["watch-sigterm"]
        self.assertEqual(len(posts), before + 1)
        self.assertIn("exit 4", posts[-1]["body"])


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
    def _wait_new_request(self, method="sendMessage", timeout=20.0, before=None):
        # `before` has to be the count from before the ask thread starts.
        # Counting after start misses a sendMessage that already landed and
        # then waits out the whole timeout.
        if before is None:
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
        before = len(self.tg.requests)
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.update(
                result=an.run_ask(cfg, "Deploy?", timeout_seconds=20, print_status=False)
            ),
            daemon=True,
        )
        thread.start()
        sent = self._wait_new_request("sendMessage", before=before)
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
        self.addCleanup(an.release_bot_lock, an.acquire_bot_lock())    # a running bot
        an.write_bot_heartbeat()
        before = len(self.tg.requests)
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.update(
                result=an.run_ask(cfg, "Deploy?", timeout_seconds=20, print_status=False)
            ),
            daemon=True,
        )
        thread.start()
        body = self._wait_new_request("sendMessage", before=before)["body"]
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

    def test_explicit_telegram_needs_a_configured_bot(self):
        cfg = self._tg_cfg()
        cfg.data["telegram"] = {"bot_token": None, "chat_id": None}
        with self.assertRaises(RuntimeError) as ctx:
            an.run_ask(cfg, "Deploy?", timeout_seconds=5, print_status=False,
                       channels=["telegram"])
        self.assertIn("not configured", str(ctx.exception))

    def test_derived_channels_keep_a_configured_telegram(self):
        cfg = self._tg_cfg(channels=("ntfy", "telegram"))
        self.assertEqual(an.resolve_ask_channels(cfg), ["ntfy", "telegram"])

    def test_derived_channels_drop_an_unconfigured_telegram(self):
        cfg = self._tg_cfg(channels=("ntfy", "telegram"))
        cfg.data["telegram"] = {"bot_token": None, "chat_id": None}
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
        _remove_pending("tg-pending", "abcdef0123456789")

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
        _remove_pending("tg-pending", "feedface01234567")

    def test_free_text_attributed_to_newest_pending(self):
        cfg = self._tg_cfg()
        an.write_tg_pending("deadbeef", "Which env?", 60)
        self._stamp_question("deadbeef", 5)
        self.tg.queue_update({
            "update_id": 2,
            "message": {"message_id": 6, "chat": {"id": 42}, "text": "staging"},
        })
        an.bot_poll_once(cfg, poll_timeout=1)
        self.assertEqual(an.read_tg_answer("deadbeef"), "staging")
        an.remove_tg_answer("deadbeef")
        _remove_pending("tg-pending", "deadbeef")

    def _stamp_question(self, approval_id, message_id, date=None):
        path = an._pending_path("tg-pending", approval_id)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["question_message_id"] = message_id
        data["question_date"] = int(time.time()) if date is None else date
        data["question_sent_at"] = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_replayed_yes_from_before_the_question_is_ignored(self):
        """A restarted bot replays backlog. A lower message id is not this answer.

        Ordering is by message id, not by the local clock. A reply dated in
        the future still predates the question when its id is lower. A
        higher id counts however far Telegram's clock is from the local one
        (here five hours behind it); one that reached the bot long after it
        was sent does not (audit 7, AF-1).
        """
        cfg = self._tg_cfg()
        self.tg.updates.clear()
        an.write_tg_pending("deadbeef", "Deploy?", 60)
        skew = -5 * 3600
        self._stamp_question("deadbeef", 20, date=int(time.time()) + skew)
        self.tg.queue_update({
            "update_id": 11,
            "message": {"message_id": 11, "chat": {"id": 42}, "text": "yes",
                        "date": int(time.time()) + 3600},
        })
        an.bot_poll_once(cfg, poll_timeout=1)
        self.assertIsNone(an.read_tg_answer("deadbeef"))
        self.assertTrue(any(
            r.get("event") == "stale_answer" and "predate" in (r.get("reason") or "")
            for r in an.read_history()))
        self.tg.queue_update({
            "update_id": 12,
            "message": {"message_id": 21, "chat": {"id": 42}, "text": "yes",
                        "date": int(time.time()) + skew - an.LATE_REPLY_SECONDS - 5},
        })
        an.bot_poll_once(cfg, poll_timeout=1)
        self.assertIsNone(an.read_tg_answer("deadbeef"))
        self.tg.queue_update({
            "update_id": 13,
            "message": {"message_id": 22, "chat": {"id": 42}, "text": "yes",
                        "date": int(time.time()) + skew},
        })
        an.bot_poll_once(cfg, poll_timeout=1)
        self.assertEqual(an.read_tg_answer("deadbeef"), "yes")
        an.remove_tg_answer("deadbeef")
        _remove_pending("tg-pending", "deadbeef")

    def test_reply_without_a_message_id_is_not_ordered_as_newer(self):
        cfg = self._tg_cfg()
        self.tg.updates.clear()
        an.write_tg_pending("deadbeef", "Deploy?", 60)
        self.tg.queue_update({
            "update_id": 13,
            "message": {"chat": {"id": 42}, "text": "yes", "date": int(time.time())},
        })
        an.bot_poll_once(cfg, poll_timeout=1)
        self.assertIsNone(an.read_tg_answer("deadbeef"))
        _remove_pending("tg-pending", "deadbeef")

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
        lock = an.acquire_bot_lock()
        an.write_bot_heartbeat()
        self.assertTrue(an.bot_heartbeat_fresh())
        with self.assertRaises(SystemExit):
            an.acquire_bot_lock()  # our own fresh lock -> "already running"
        an.release_bot_lock(lock)
        self.assertFalse(an.bot_heartbeat_fresh())

    def test_a_reused_pid_does_not_keep_the_lock(self):
        """SIGKILL leaves the lock. The pid can belong to something else."""
        path = os.path.join(an.state_dir(), "bot.lock")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "start": "not-this-process"}, fh)
        lock = an.acquire_bot_lock()
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertNotIn("start", data)
        an.release_bot_lock(lock)


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
        holder = {"tg_before": len(self.tg.requests)}
        thread = threading.Thread(
            target=lambda: holder.update(
                result=an.run_ask(cfg, print_status=False, **kwargs)
            ),
            daemon=True,
        )
        thread.start()
        return holder, thread

    def _wait_new_request(self, method="sendMessage", timeout=20.0, before=None):
        # See TestTelegramAsk: the count must be from before the ask starts.
        if before is None:
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
        body = self._wait_new_request("sendMessage", before=holder["tg_before"])["body"]
        match = an.re.search(r"ID: ([0-9a-f]+)", body["text"])
        # the marker learns the message id only after sendMessage has answered
        question_id = None
        deadline = time.monotonic() + 10
        while question_id is None and time.monotonic() < deadline:
            try:
                with open(an._pending_path("tg-pending", match.group(1)), encoding="utf-8") as fh:
                    question_id = json.load(fh).get("question_message_id")
            except (OSError, ValueError):
                pass
            if question_id is None:
                time.sleep(0.02)
        self.assertIsInstance(question_id, int)
        self.assertGreater(question_id, 0)
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

    def test_telegram_is_asked_while_ntfy_prime_is_still_failing(self):
        """A dead ntfy must not swallow the ask before Telegram is contacted.

        Prime used to raise in the calling thread, so the Telegram question
        was never sent. It also must not wait out the prime retries first.
        """
        release = threading.Event()
        started = threading.Event()

        def blocked(waiter):
            started.set()
            if not release.wait(5):
                raise RuntimeError("prime still blocked")
            raise RuntimeError("unreachable")

        cfg = self._cfg()
        before = len(self.ntfy.posts.get("tgtopic", []))
        with unittest.mock.patch.object(an.ApprovalWaiter, "_prime", blocked):
            holder, thread = self._run_ask_async(
                cfg, message="Deploy?", timeout_seconds=20)
            try:
                self.assertTrue(started.wait(2), "ntfy prime did not start")
                sent = self._wait_new_request(
                    "sendMessage", timeout=2, before=holder["tg_before"])
                self.assertIsNotNone(
                    sent, "telegram was not asked while ntfy prime was blocked")
                match = an.re.search(r"ID: ([0-9a-f]+)", sent["body"]["text"])
                an.write_tg_answer(match.group(1), "approved")
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(), holder.get("result"))
                self.assertTrue(holder["result"]["approved"])
                self.assertEqual(holder["result"]["channel"], "telegram")
                self.assertEqual(len(self.ntfy.posts.get("tgtopic", [])), before)
            finally:
                release.set()
                thread.join(timeout=3)
        self.assertEqual(
            len(self.ntfy.posts.get("tgtopic", [])), before,
            "ntfy was published after the ask had already finished")

    def test_ntfy_prime_failure_leaves_telegram_as_the_only_channel(self):
        def broken(waiter):
            raise RuntimeError("unreachable")

        cfg = self._cfg()
        before = len(self.ntfy.posts.get("tgtopic", []))
        with unittest.mock.patch.object(an.ApprovalWaiter, "_prime", broken):
            holder, thread = self._run_ask_async(
                cfg, message="Deploy?", timeout_seconds=8)
            sent = self._wait_new_request(
                "sendMessage", timeout=3, before=holder["tg_before"])
            self.assertIsNotNone(sent)
            match = an.re.search(r"ID: ([0-9a-f]+)", sent["body"]["text"])
            an.write_tg_answer(match.group(1), "no")
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), holder.get("result"))
        self.assertTrue(holder["result"]["denied"])
        self.assertEqual(holder["result"]["channel"], "telegram")
        self.assertEqual(len(self.ntfy.posts.get("tgtopic", [])), before)


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
        # make_config has no bot token: Telegram is "not configured", which is
        # a permanent error, so the item is dropped instead of retried forever
        an.enqueue_item(cfg, {"message": "telegram without a bot", "channels": ["telegram"],
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
        # free text names neither of two open questions: used by none, and
        # the phone is told so once
        urllib.request.urlopen(
            urllib.request.Request(f"{self.ntfy.url}/par-responses", method="POST",
                                   data=b"staging")
        ).read()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(
                "was not used" in p["body"] for p in self.ntfy.posts.get("par", [])):
            time.sleep(0.05)
        notices = [p for p in self.ntfy.posts["par"] if "was not used" in p["body"]]
        self.assertEqual(len(notices), 1)
        self.assertTrue(thread_a.is_alive())
        self.assertTrue(thread_b.is_alive())
        # a button answer reaches each of them
        urllib.request.urlopen(
            urllib.request.Request(f"{self.ntfy.url}/par-responses", method="POST",
                                   data=f"DENIED {id_b}".encode())
        ).read()
        thread_b.join(timeout=10)
        self.assertFalse(thread_b.is_alive())
        self.assertTrue(holder_b["result"]["denied"])
        urllib.request.urlopen(
            urllib.request.Request(f"{self.ntfy.url}/par-responses", method="POST",
                                   data=f"APPROVED {id_a}".encode())
        ).read()
        thread_a.join(timeout=10)
        self.assertFalse(thread_a.is_alive())
        self.assertTrue(holder_a["result"]["approved"])
        self.assertEqual(holder_a["result"].get("channel"), "ntfy")

    def test_free_text_claimed_by_another_ask_is_not_answered_twice(self):
        """The case the markers alone cannot decide.

        On a slow box the ask that took a free-text reply is already finished
        - its marker an answered tombstone that no longer counts - by the time
        a second ask's poll reaches the same message. That ask then finds
        itself the only open question and answers the very same reply. The
        claim recorded before the marker was closed is what stops it.
        """
        cfg = make_config(self.ntfy.url, topic="claimed")
        holder, thread = self._run_ask_async(cfg, message="Which env?", timeout_seconds=20)
        posts = self._wait_posts("claimed", 1)
        approval_id = an.re.search(r"ID: ([0-9a-f]+)", posts[-1]["body"]).group(1)
        # The other ask took the reply (its claim) and ended (an answered
        # tombstone) before the message is ever handed to the ask above.
        other_id = "b" * 16
        an.write_ntfy_pending(other_id, "Second question?", 20)
        self.assertTrue(an.claim_ntfy_message("race-msg-1"))
        # answered a while ago: its tombstone no longer counts for this reply
        # (one that may have been sent before that answer is refused anyway)
        earlier = time.time() - 10
        with unittest.mock.patch.object(an.time, "time", lambda: earlier):
            an.close_pending("ntfy-pending", other_id, answered=True)
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

    def test_claim_consumed_is_once_only_across_processes(self):
        state = tempfile.mkdtemp()
        home = tempfile.mkdtemp()
        gate = os.path.join(state, "go")
        code = (
            "import os,time,agentbell as a; "
            f"gate={gate!r}; "
            "\nwhile not os.path.exists(gate): time.sleep(.005)\n"
            "print(int(a.claim_consumed('process-race','same-id')), flush=True)"
        )
        env = dict(os.environ, AGENTBELL_STATE_DIR=state, HOME=home,
                   USERPROFILE=home)
        workers = [subprocess.Popen(
            [sys.executable, "-c", code], cwd=os.path.dirname(an.__file__),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(8)]
        with open(gate, "w", encoding="utf-8"):
            pass
        results = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0, stderr)
            results.append(int(stdout.strip()))
        self.assertEqual(sum(results), 1)
        shutil.rmtree(state, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)

    def test_claim_failure_is_recorded_and_not_accepted(self):
        cfg = make_config(self.ntfy.url, topic="claimfailure")
        waiter = an.ApprovalWaiter(
            cfg, "claimfailure-responses", 5, approval_id="a" * 16)
        an.write_ntfy_pending("a" * 16, "Q?", 5)          # the one open question
        an.remember_ntfy_question("a" * 16, 1000, time.time())
        original = an.claim_ntfy_message
        an.claim_ntfy_message = lambda message_id: (_ for _ in ()).throw(
            OSError("read only"))
        try:
            waiter._offer("message-id", "free text", 1001)
        finally:
            an.claim_ntfy_message = original
        self.assertTrue(waiter.messages.empty())
        self.assertTrue(any("cannot claim approval answer" in error
                            for error in waiter.errors))
        self.assertTrue(any(rec.get("event") == "answer_claim_failed"
                            for rec in an.read_history(limit=20)))

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
        self.old_home = _set_home(self.home)
        self.old_argv0 = sys.argv[0]
        sys.argv[0] = _fake_launcher(self.home)
        # tests here apply what purge finds: never the real pipx / pip --user install
        self.real_pipx_installed = an._pipx_installed
        for name, value in (("_pipx_installed", None), ("_user_site_dirs", (None, None))):
            patcher = unittest.mock.patch.object(an, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        _restore_home(self.old_home)
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
        self._write(an.config_path(), json.dumps({"ntfy": {"topic": "t"},
                                                   "telegram": {"bot_token": "1:x", "chat_id": "2"}}))
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
        self._write(agents_md, f"# Rules\n{an.BLOCK_START}\n{an.OPENCODE_INSTRUCTIONS}{an.BLOCK_END}\n")
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

    def test_pipx_list_detects_agentbell_package(self):
        completed = an.subprocess.CompletedProcess(
            ["/tools/pipx", "list"], 0,
            stdout="venvs are in /tmp/pipx/venvs\n   package agentbell 1.6.3\n",
            stderr="",
        )
        with unittest.mock.patch.object(
                an.shutil, "which",
                side_effect=lambda name: "/tools/pipx" if name == "pipx" else None), \
                unittest.mock.patch.object(
                    an.subprocess, "run", return_value=completed) as run:
            self.assertEqual(self.real_pipx_installed(), "/tools/pipx")
        run.assert_called_once_with(
            ["/tools/pipx", "list"], capture_output=True, text=True, timeout=30)

    def test_pipx_package_is_removed_via_pipx(self):
        with unittest.mock.patch.object(
                an, "_pipx_installed", return_value="/tools/pipx"), \
                unittest.mock.patch.object(
                    an, "_user_site_dirs", return_value=(None, None)), \
                unittest.mock.patch.object(
                    an, "_pipx_uninstall", return_value=True) as uninstall:
            report = an.purge_report(project=self.project)
            binary = [entry for entry in report["entries"]
                      if entry["kind"] == "binary"]
            self.assertEqual(len(binary), 1)
            self.assertEqual(binary[0]["action"], "pipx uninstall agentbell")
            self.assertTrue(binary[0]["apply"]())
        uninstall.assert_called_once_with("/tools/pipx")

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
        self._write(an.config_path(), "{}")
        parser = an.build_parser()
        args = parser.parse_args(["uninstall", "--project", self.project])
        buffer = _io.StringIO()
        with redirect_stdout(buffer):
            args.func(args)
        out = buffer.getvalue()
        self.assertIn("dry run", out)
        self.assertIn("--yes", out)
        self.assertTrue(os.path.exists(an.config_path()))

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
        self.addCleanup(an.release_bot_lock, an.acquire_bot_lock())    # a running bot
        an.write_bot_heartbeat()
        import io as _io
        from contextlib import redirect_stdout
        buffer = _io.StringIO()
        with redirect_stdout(buffer):
            an.print_bot_status(cfg)
        out = buffer.getvalue()
        self.assertIn("bot:       running (pid", out)
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
            "ntfy": {"server": "https://ntfy.example", "topic": "t", "auth": "bob:hunter2",
                     "action_auth": "tk_publish_only_secret"},
            "telegram": {"bot_token": "123456:AAHsuperSecretToken", "chat_id": 42},
            "webhook": {"listen": "0.0.0.0", "port": 8756, "token": "webhook-secret-token"},
        }
        safe = json.dumps(an.redacted_config(data))
        for secret in ("hunter2", "AAHsuperSecretToken", "webhook-secret-token",
                       "tk_publish_only_secret"):
            self.assertNotIn(secret, safe, f"{secret} leaked into 'config show'")
        self.assertIn("bob:", safe)          # enough context to recognise it
        self.assertIn("0.0.0.0", safe)       # non-secrets stay visible

    @unittest.skipIf(os.name == "nt", "Unix file permissions not applicable on Windows")
    def test_config_file_is_owner_only(self):
        tmp = tempfile.mkdtemp()
        cfg = an.Config(dict(an.default_config()), path=os.path.join(tmp, "sub", "config.json"))
        cfg.data["telegram"]["bot_token"] = "123456:secret"
        cfg.save()
        self.assertEqual(os.stat(cfg.path).st_mode & 0o777, 0o600)
        shutil.rmtree(tmp)

    def test_applescript_and_powershell_quoting(self):
        self.assertEqual(an._applescript_string('say "hi" \\ bye'), '"say \\"hi\\" \\\\ bye"')

    def test_windows_toast_text_is_not_powershell_source(self):
        """The message must not be part of the -Command string.

        Windows PowerShell 5.1 treats ’ (U+2019) as a quote, so quoting the
        text into the script both broke the toast and ran whatever followed.
        """
        title = "it\u2019s done"
        message = "\u2019; calc.exe # $(Remove-Item)"
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0)

        original = an.platform.system
        an.platform.system = lambda: "Windows"
        try:
            with unittest.mock.patch.object(an.subprocess, "run", fake_run):
                result = an.os_notify(title, "a\x00" + message, 3)
        finally:
            an.platform.system = original
        self.assertEqual(result, {"channel": "os", "ok": True})
        script = captured["cmd"][-1]
        self.assertEqual(captured["cmd"][:4],
                         ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertNotIn("calc", script)
        self.assertNotIn("Remove-Item", script)
        self.assertNotIn("\u2019", script)
        self.assertNotIn("it\u2019s", script)
        self.assertIn("$env:AGENTBELL_OS_TITLE", script)
        self.assertIn("$env:AGENTBELL_OS_MESSAGE", script)
        self.assertEqual(captured["env"]["AGENTBELL_OS_TITLE"], title)
        self.assertEqual(captured["env"]["AGENTBELL_OS_MESSAGE"], "a" + message)
        self.assertNotIn("\x00", captured["env"]["AGENTBELL_OS_MESSAGE"])

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
                     ["bot", "install-service"],
                     ["verify"], ["verify", "--agent", "x"], ["integrate"],
                     ["integrate", "--json"], ["hook", "totally_made_up_event"]):
            args = parser.parse_args(argv)
            self.assertTrue(callable(getattr(args, "func", None)), f"no func for {argv}")

    def test_test_command_reports_failure(self):
        """`agentbell test` must exit non-zero when nothing was delivered."""
        self.addCleanup(_clear_state_queue)       # the failed send is queued
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


def _init_args(**overrides):
    values = dict(
        non_interactive=True, server=None, topic=None, ntfy_auth=None,
        telegram_token=None, telegram_chat=None,
        quiet_hours=None, quiet_hours_mode=None,
        no_hooks=True, no_test=False, no_wait=True,
    )
    values.update(overrides)
    return _Args(**values)


class TestReinitKeepsSelfHostedAuth(unittest.TestCase):
    """Re-init is how Telegram gets added. It must not move a self-hosted
    server back to ntfy.sh and then post the saved password there."""

    SECRET = "owner:s3cret-selfhosted"
    TOPIC = "selfhost-topic-0123456789abcdef"
    ACTION = "tk_old_action_token"

    def _basic(self, secret):
        return "Basic " + base64.b64encode(secret.encode()).decode()

    def _config(self, server, action_auth=None):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "config.json")
        cfg = an.Config(an.default_config(), path=path)
        ntfy = {"server": server, "topic": self.TOPIC, "auth": self.SECRET}
        if action_auth is not None:
            ntfy["action_auth"] = action_auth
        cfg.data["ntfy"] = ntfy
        cfg.save()
        return directory, path

    def _run(self, path, args, stdin=None):
        seen = []
        original = an.http_request

        def wrapped(url, method="GET", headers=None, body=None, timeout=10.0):
            seen.append({
                "url": url,
                "auth": (headers or {}).get("Authorization"),
                "body": body if isinstance(body, str) else "",
            })
            if "ntfy.sh" in url:
                return 200, b""
            return original(url, method, headers, body, timeout)

        previous = os.environ.get("AGENTBELL_CONFIG")
        os.environ["AGENTBELL_CONFIG"] = path
        stdout, stderr = io.StringIO(), io.StringIO()
        patches = [unittest.mock.patch.object(an, "http_request", wrapped)]
        if stdin is not None:
            patches.append(unittest.mock.patch.object(
                an.sys.stdin, "isatty", return_value=True))
            patches.append(unittest.mock.patch("builtins.input", side_effect=stdin))
        for patch in patches:
            patch.start()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                an.cmd_init(args)
        finally:
            for patch in patches:
                patch.stop()
            if previous is None:
                os.environ.pop("AGENTBELL_CONFIG", None)
            else:
                os.environ["AGENTBELL_CONFIG"] = previous
        return seen, stdout.getvalue(), stderr.getvalue()

    def _saved(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)["ntfy"]

    def test_pressing_enter_keeps_the_server_and_the_password(self):
        server = MockNtfy()
        directory, path = self._config(server.url)
        try:
            seen, stdout, _stderr = self._run(
                path, _init_args(non_interactive=False),
                stdin=["", "", "", "", ""])
            saved = self._saved(path)
        finally:
            server.stop()
            shutil.rmtree(directory)
        self.assertEqual(saved["server"], server.url)
        self.assertEqual(saved["topic"], self.TOPIC)
        self.assertEqual(saved["auth"], self.SECRET)
        self.assertTrue(any(server.url in call["url"] for call in seen))
        self.assertFalse(any("ntfy.sh" in call["url"] for call in seen))
        self.assertIn(self._basic(self.SECRET), [call["auth"] for call in seen])
        self.assertIn(server.url, stdout)

    def test_noninteractive_reinit_does_not_move_the_server(self):
        server = MockNtfy()
        directory, path = self._config(server.url)
        try:
            seen, _stdout, stderr = self._run(path, _init_args())
            saved = self._saved(path)
        finally:
            server.stop()
            shutil.rmtree(directory)
        self.assertEqual(saved["server"], server.url)
        self.assertEqual(saved["topic"], self.TOPIC)
        self.assertEqual(saved["auth"], self.SECRET)
        self.assertTrue(seen)
        self.assertTrue(all(server.url in call["url"] for call in seen))
        self.assertNotIn("not sending the saved", stderr)

    def test_switching_server_does_not_send_the_old_password(self):
        directory, path = self._config("https://ntfy.example")
        try:
            seen, _stdout, stderr = self._run(
                path, _init_args(server="https://ntfy.sh"))
            saved = self._saved(path)
        finally:
            shutil.rmtree(directory)
        self.assertEqual(saved["server"], "https://ntfy.sh")
        self.assertEqual(saved["topic"], self.TOPIC)
        self.assertIsNone(saved["auth"])
        self.assertTrue(seen)
        self.assertTrue(all("ntfy.sh" in call["url"] for call in seen))
        blob = json.dumps(seen)
        self.assertNotIn(self.SECRET, blob)
        self.assertNotIn(self._basic(self.SECRET), blob)
        self.assertTrue(all(call["auth"] in (None, "") for call in seen))
        self.assertIn("not sending the saved", stderr)

    def test_new_auth_flag_is_what_gets_sent(self):
        directory, path = self._config("https://ntfy.example")
        fresh = "fresh:token"
        try:
            seen, _stdout, _stderr = self._run(
                path, _init_args(server="https://ntfy.sh", ntfy_auth=fresh))
            saved = self._saved(path)
        finally:
            shutil.rmtree(directory)
        self.assertEqual(saved["auth"], fresh)
        blob = json.dumps(seen)
        self.assertNotIn(self.SECRET, blob)
        self.assertNotIn(self._basic(self.SECRET), blob)
        self.assertIn(self._basic(fresh), [call["auth"] for call in seen])

    def test_interactive_server_change_asks_before_sending(self):
        directory, path = self._config("https://ntfy.example")
        try:
            seen, stdout, _stderr = self._run(
                path, _init_args(non_interactive=False),
                stdin=["https://ntfy.sh", "", "", "", "", ""])
            saved = self._saved(path)
        finally:
            shutil.rmtree(directory)
        self.assertEqual(saved["server"], "https://ntfy.sh")
        self.assertIsNone(saved["auth"])
        self.assertIn("will not be sent", stdout)
        blob = json.dumps(seen)
        self.assertNotIn(self.SECRET, blob)
        self.assertNotIn(self._basic(self.SECRET), blob)
        self.assertTrue(all("ntfy.sh" in call["url"] for call in seen))

    def test_server_change_drops_the_action_token(self):
        directory, path = self._config("https://ntfy.example", action_auth=self.ACTION)
        try:
            seen, _stdout, stderr = self._run(
                path, _init_args(server="https://ntfy.sh"))
            saved = self._saved(path)
        finally:
            shutil.rmtree(directory)
        self.assertIsNone(saved["auth"])
        self.assertIsNone(saved.get("action_auth"))
        self.assertNotIn(self.ACTION, json.dumps(saved))
        self.assertNotIn(self.ACTION, json.dumps(seen))
        self.assertIn("action token", stderr)
        actions = an.ask_actions(
            "https://ntfy.sh", self.TOPIC + "-responses", "ab",
            "Approve", "Deny", saved)
        self.assertNotIn(self.ACTION, json.dumps(actions))

    def test_new_password_does_not_keep_the_old_action_token(self):
        directory, path = self._config("https://ntfy.example", action_auth=self.ACTION)
        try:
            seen, _stdout, stderr = self._run(
                path, _init_args(server="https://ntfy.sh", ntfy_auth="fresh:token"))
            saved = self._saved(path)
        finally:
            shutil.rmtree(directory)
        self.assertEqual(saved["auth"], "fresh:token")
        self.assertIsNone(saved.get("action_auth"))
        self.assertNotIn(self.ACTION, json.dumps(saved))
        self.assertNotIn(self.ACTION, json.dumps(seen))
        self.assertIn("action token", stderr)

    def test_same_server_keeps_the_action_token(self):
        server = MockNtfy()
        directory, path = self._config(server.url, action_auth=self.ACTION)
        try:
            _seen, _stdout, stderr = self._run(path, _init_args())
            saved = self._saved(path)
        finally:
            server.stop()
            shutil.rmtree(directory)
        self.assertEqual(saved["action_auth"], self.ACTION)
        self.assertNotIn("action token", stderr)


class TestRunTestConfirmation(unittest.TestCase):
    """`agentbell test` semantics. Field-test regression: publish success and
    confirmation are different facts - a message the server accepted must
    never be reported as 'NOT delivered' just because the read-back failed."""

    def test_confirmed_when_message_is_readable(self):
        server = MockNtfy()
        try:
            cfg = make_config(server.url)
            outcome = an.run_test(cfg, confirm_seconds=6, poll_interval=0.1)
        finally:
            server.stop()
        self.assertEqual(outcome["sent"], ["ntfy"])
        self.assertTrue(outcome["confirmed"])
        self.assertIsNone(outcome["reason"])

    def test_poll_window_is_a_server_side_duration(self):
        """Regression (WSL2 clock drift): the confirmation poll must use a
        server-relative duration window ('90s'), never an epoch cursor from
        the local clock - a clock ahead of the server's filtered out
        messages that were delivered fine."""
        server = MockNtfy()
        seen = []
        original = an.NtfyChannel.poll

        def spy(self, topic, since, timeout=10.0):
            seen.append(since)
            return original(self, topic, since, timeout=timeout)

        an.NtfyChannel.poll = spy
        try:
            cfg = make_config(server.url)
            outcome = an.run_test(cfg, confirm_seconds=6, poll_interval=0.1)
        finally:
            an.NtfyChannel.poll = original
            server.stop()
        self.assertTrue(outcome["confirmed"])
        self.assertTrue(seen)
        for since in seen:
            self.assertIsInstance(since, str)
            self.assertRegex(since, r"^\d+[smhd]$")
            self.assertEqual(since, f"{an.NTFY_LOOKBACK_MARGIN_SECONDS}s")

    def test_poll_error_becomes_the_reason(self):
        """A failing confirmation poll (e.g. HTTP 429) must surface its error
        instead of being silently swallowed."""
        server = MockNtfy()
        original = an.NtfyChannel.poll

        def boom(self, topic, since, timeout=10.0):
            raise an.TransientError("HTTP 429 from server: limit reached")

        an.NtfyChannel.poll = boom
        try:
            cfg = make_config(server.url)
            outcome = an.run_test(cfg, confirm_seconds=0.3, poll_interval=0.1)
        finally:
            an.NtfyChannel.poll = original
            server.stop()
        self.assertEqual(outcome["sent"], ["ntfy"])
        self.assertFalse(outcome["confirmed"])
        self.assertIn("429", outcome["reason"])

    def test_unconfirmed_when_message_never_readable(self):
        server = MockNtfy()
        original = an.NtfyChannel.poll
        an.NtfyChannel.poll = lambda self, topic, since, timeout=10.0: []
        try:
            cfg = make_config(server.url)
            outcome = an.run_test(cfg, confirm_seconds=0.3, poll_interval=0.1)
        finally:
            an.NtfyChannel.poll = original
            server.stop()
        self.assertEqual(outcome["sent"], ["ntfy"])
        self.assertFalse(outcome["confirmed"])
        self.assertIn("read back", outcome["reason"])

    def test_send_failure_reports_nothing_sent(self):
        cfg = make_config("http://127.0.0.1:1")   # nothing listening
        outcome = an.run_test(cfg, wait=False)
        self.assertEqual(outcome["sent"], [])
        self.assertIs(outcome["confirmed"], False)
        self.assertTrue(outcome["reason"])

    def test_no_wait_reports_unchecked(self):
        server = MockNtfy()
        try:
            cfg = make_config(server.url)
            outcome = an.run_test(cfg, wait=False)
        finally:
            server.stop()
        self.assertEqual(outcome["sent"], ["ntfy"])
        self.assertIsNone(outcome["confirmed"])

    def test_poll_rejects_malformed_duration(self):
        cfg = make_config("http://127.0.0.1:1")
        with self.assertRaises(ValueError):
            an.NtfyChannel(cfg).poll("testtopic", "90s&x=1")

    def _cmd_test_with(self, outcome):
        cfg = make_config("http://server.invalid")
        original_config, original_run = an.Config, an.run_test
        an.Config = lambda *a, **kw: cfg
        an.run_test = lambda *a, **kw: outcome
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = an.cmd_test(_Args(no_wait=False))
        finally:
            an.Config, an.run_test = original_config, original_run
        return code, out.getvalue(), err.getvalue()

    def test_cmd_test_confirmed_exits_zero(self):
        code, out, _ = self._cmd_test_with(
            {"sent": ["ntfy"], "confirmed": True, "reason": None})
        self.assertEqual(code, 0)
        self.assertIn("confirmed", out)

    def test_cmd_test_accepted_but_unconfirmed_is_not_called_not_delivered(self):
        code, _, err = self._cmd_test_with(
            {"sent": ["ntfy"], "confirmed": False, "reason": "HTTP 429 from x"})
        self.assertEqual(code, 1)          # fail-closed: unconfirmed != proven
        self.assertIn("NOT confirmed", err)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("NOT delivered", err)

    def test_cmd_test_nothing_sent_says_not_delivered(self):
        code, _, err = self._cmd_test_with(
            {"sent": [], "confirmed": False, "reason": "ntfy publish failed"})
        self.assertEqual(code, 1)
        self.assertIn("NOT delivered", err)

    def _doctor_delivery_with(self, outcome):
        cfg = make_config("http://server.invalid")
        original_run = an.run_test
        an.run_test = lambda *a, **kw: outcome
        try:
            checks = an.doctor_checks(cfg, send=True)
        finally:
            an.run_test = original_run
        return [c for c in checks if c["name"] == "delivery"][-1]

    def test_doctor_send_confirmed_is_ok(self):
        check = self._doctor_delivery_with(
            {"sent": ["ntfy"], "confirmed": True, "reason": None})
        self.assertEqual(check["status"], an.OK)

    def test_doctor_send_accepted_but_unconfirmed_is_warn_not_fail(self):
        """Field-test regression: 'server accepted, read-back failed' must not
        be reported as 'did NOT arrive'."""
        check = self._doctor_delivery_with(
            {"sent": ["ntfy"], "confirmed": False, "reason": "HTTP 429 from x"})
        self.assertEqual(check["status"], an.WARN)
        self.assertIn("429", check["detail"])
        self.assertNotIn("NOT arrive", check["detail"])

    def test_doctor_send_nothing_sent_is_fail(self):
        check = self._doctor_delivery_with(
            {"sent": [], "confirmed": False, "reason": "connection refused"})
        self.assertEqual(check["status"], an.FAIL)


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

    def test_doctor_warns_that_windows_config_acl_is_unverified(self):
        cfg = an.Config(an.default_config(), path=os.path.join(
            tempfile.mkdtemp(), "config.json"))
        cfg.save()
        original_system = an.platform.system
        an.platform.system = lambda: "Windows"
        try:
            check = next(c for c in an.doctor_checks(cfg) if c["name"] == "config")
        finally:
            an.platform.system = original_system
        self.assertEqual(check["status"], an.WARN)
        self.assertIn("cannot verify its Windows ACL", check["detail"])
        self.assertIn("icacls", check["fix"])

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

    def test_doctor_mentions_self_integrated_agents(self):
        an.write_history({"event": "hook.run_completed", "agent": "doc-selfint",
                          "delivered": ["ntfy"]})
        cfg = make_config("http://127.0.0.1:1")
        original_status = an.hooks_status
        original_find = an.find_agents
        an.hooks_status = lambda project=None: []
        an.find_agents = lambda: []
        try:
            hooks = [c for c in an.doctor_checks(cfg) if c["name"] == "agent hooks"][0]
        finally:
            an.hooks_status = original_status
            an.find_agents = original_find
        self.assertEqual(hooks["status"], an.OK)
        self.assertIn("doc-selfint", hooks["detail"])

    def test_doctor_does_not_fail_a_configured_telegram(self):
        cfg = make_config("http://127.0.0.1:1")
        cfg.data["telegram"] = {"bot_token": "1:x", "chat_id": "2"}
        cfg.data["channels"] = ["ntfy", "telegram"]
        telegram = [c for c in an.doctor_checks(cfg)
                    if c["name"] in ("telegram", "telegram bot")]
        self.assertTrue(telegram, "a configured Telegram gets its own row")
        for check in telegram:
            self.assertNotEqual(check["status"], an.FAIL, check)

    def test_doctor_surfaces_answer_claim_failures(self):
        an.write_history({"event": "answer_claim_failed", "error": "OSError"})
        cfg = make_config("http://127.0.0.1:1")
        check = next(c for c in an.doctor_checks(cfg)
                     if c["name"] == "approval answers")
        self.assertEqual(check["status"], an.WARN)
        self.assertIn("state directory", check["fix"])


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

    @unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
    def test_mcp_removal_does_not_follow_a_tmp_symlink(self):
        """uninstall walks project MCP files. mcp.json.tmp as a symlink must not be followed."""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "mcp.json")
        precious = os.path.join(tmp, "precious")
        with open(precious, "w", encoding="utf-8") as fh:
            fh.write("do not touch\n")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "mcpServers": {
                    "agentbell": {"command": "agentbell"},
                    "other": {"command": "x"},
                },
                "theme": "dark",
            }, fh)
        os.chmod(path, 0o600)
        os.symlink(precious, path + ".tmp")
        try:
            self.assertTrue(an._remove_mcp_server_key(path, "mcpServers"))
            with open(precious, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "do not touch\n")
            self.assertFalse(os.path.islink(path))
            self.assertFalse(os.path.lexists(path + ".tmp"))
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["theme"], "dark")
            self.assertEqual(data["mcpServers"], {"other": {"command": "x"}})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        finally:
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
            # the refusal is answered on a closed local port, not the real API
            with unittest.mock.patch.object(an, "TG_API_BASE", "http://127.0.0.1:9"):
                an.handle_bot_update(cfg, {"callback_query": {
                    "id": "cb", "data": f"agentbell|{approval_id}|approved",
                    "from": {"id": 9999}, "message": {"chat": {"id": 9999}, "message_id": 1},
                }})
            self.assertIsNone(an.read_tg_answer(approval_id),
                              "a stranger must not be able to approve")
        finally:
            _remove_pending("tg-pending", approval_id)
            an.remove_tg_answer(approval_id)


class TestAuditRegressions(unittest.TestCase):
    """One test per confirmed audit finding, so none of them can come back."""

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
        """The old install put the flag where TOML scoped it to the last table.
        The top-level flag is added; the unmarked line may be the user's and
        stays (CR3-2)."""
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
            self.assertEqual(data["model_providers"]["oss"],
                             {"name": "x", "features": {"hooks": True}})
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
        dirty = "https://api.telegram.org/bot123456789:AAHsuper Secret\rToken/getMe"
        scrubbed = an.safe_url(dirty)
        self.assertNotIn("super", scrubbed)
        self.assertNotIn("Token", scrubbed)
        secret = "AAHsuperSecretToken"
        with self.assertRaises(an.PermanentError) as caught:
            an.TelegramChannel._call(f"123456:{secret[:6]} {secret[6:]}", "getMe")
        self.assertNotIn(secret[:6], str(caught.exception))
        self.assertNotIn("Secret", str(caught.exception))
        seen = []

        def wrapped(url, method="GET", headers=None, body=None, timeout=10.0):
            seen.append(url)
            return 200, b'{"ok": true, "result": {"username": "probe"}}'

        with unittest.mock.patch.object(an, "http_request", wrapped):
            an.TelegramChannel._call(f"123456:{secret}\n", "getMe")
        self.assertEqual(seen, [f"https://api.telegram.org/bot123456:{secret}/getMe"])

    def test_header_with_newline_does_not_crash(self):
        self.assertEqual(an._ntfy_header("a\nb\tc"), "a b c")
        self.assertEqual(an._ntfy_header(None), "")

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

    def _clear_queue(self):
        for name in os.listdir(an.queue_dir()) if os.path.isdir(an.queue_dir()) else []:
            os.remove(os.path.join(an.queue_dir(), name))

    def test_an_unreachable_telegram_does_not_cost_the_ntfy_send(self):
        """Config-derived channels ntfy + telegram: ntfy is delivered once and
        the unreachable Telegram is queued for later, not lost and not fatal."""
        ntfy = MockNtfy()
        self._clear_queue()
        self.addCleanup(self._clear_queue)
        self.addCleanup(setattr, an, "TG_API_BASE", an.TG_API_BASE)
        an.TG_API_BASE = "http://127.0.0.1:1"      # telegram unreachable
        try:
            cfg = make_config(ntfy.url, topic="tgdownfallback")
            cfg.data["channels"] = ["ntfy", "telegram"]
            cfg.data["telegram"] = {"bot_token": "1:x", "chat_id": 7}
            with unittest.mock.patch.object(an, "RETRY_ATTEMPTS", 1):
                result = an.send_notification(cfg, "hello")
            self.assertTrue(result["ok"], result)
            self.assertEqual([r["channel"] for r in result["results"]], ["ntfy"])
            self.assertEqual(result["queued"], ["telegram"])
            self.assertEqual(len(ntfy.posts.get("tgdownfallback", [])), 1)
        finally:
            ntfy.stop()

    def test_telegram_listed_without_a_bot_is_reported_not_hidden(self):
        """A channel the user listed but never set up is an error for that
        channel; the other channels are still delivered."""
        ntfy = MockNtfy()
        try:
            cfg = make_config(ntfy.url, topic="tgnotsetup")
            cfg.data["channels"] = ["ntfy", "telegram"]
            result = an.send_notification(cfg, "hello")
            self.assertFalse(result["ok"], result)
            self.assertEqual([r["channel"] for r in result["results"]], ["ntfy"])
            self.assertEqual(len(ntfy.posts.get("tgnotsetup", [])), 1)
            self.assertTrue(any(e.startswith("telegram:") and "not configured" in e
                                for e in result["errors"]), result)
        finally:
            ntfy.stop()

    def test_dead_daemon_means_no_dead_buttons(self):
        lock = an.acquire_bot_lock()
        an.write_bot_heartbeat()
        self.assertTrue(an.bot_heartbeat_fresh())
        an.release_bot_lock(lock)             # fresh timestamp, no bot any more
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

    def _backdate(self, agent, seconds, session_id=None, cwd=None):
        path = an._run_marker_path(agent, an._marker_scope(session_id, cwd))
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["started_at"] = time.time() - seconds
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def test_parallel_sessions_keep_their_own_duration(self):
        """Two Claude sessions share one agent name and often one directory.

        The start marker used to be one file per agent. The long session
        then read the short session's fresh start and was dropped, and the
        short session notified because its marker was already gone.
        """
        an.write_start_marker("claude", session_id="session-long")
        long_path = self._backdate("claude", 300, session_id="session-long")
        an.write_start_marker("claude", session_id="session-short")
        short_path = self._backdate("claude", 5, session_id="session-short")
        self.assertNotEqual(long_path, short_path)
        try:
            long_result = an.run_hook(
                self.cfg, "run_completed", "claude", min_duration=60,
                session_id="session-long", cwd="/same/project")
            short_result = an.run_hook(
                self.cfg, "run_completed", "claude", min_duration=60,
                session_id="session-short", cwd="/same/project")
        finally:
            for path in (long_path, short_path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self.assertNotIn("skipped", long_result)
        self.assertEqual(len(self._posts()), 1)
        self.assertIn("in 5m00s", self._posts()[0]["body"])
        self.assertEqual(short_result.get("skipped"), "shorter than min-duration")

    def test_without_a_session_id_the_directory_is_the_fallback(self):
        an.write_start_marker("claude", cwd="/proj/one")
        one = self._backdate("claude", 300, cwd="/proj/one")
        an.write_start_marker("claude", cwd="/proj/two")
        two = an._run_marker_path("claude", an._marker_scope(cwd="/proj/two"))
        self.assertNotEqual(one, two)
        try:
            result = an.run_hook(
                self.cfg, "run_completed", "claude", min_duration=60, cwd="/proj/one")
            self.assertNotIn("skipped", result)
            self.assertTrue(os.path.exists(two))
        finally:
            for path in (one, two):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def test_a_session_marker_older_than_the_window_is_deleted(self):
        """A turn that never reaches Stop must not leave its file forever.

        The dedupe record lives in the same directory and is not a start
        marker, even when it is old.
        """
        an.write_start_marker("claude", session_id="fresh")
        fresh = an._run_marker_path("claude", an._marker_scope(session_id="fresh"))
        stale = an._run_marker_path("claude", an._marker_scope(session_id="old"))
        with open(stale, "w", encoding="utf-8") as fh:
            json.dump({"agent": "claude", "started_at": time.time() - 90 * 86400}, fh)
        keep = an._dedupe_path()
        with open(keep, "w", encoding="utf-8") as fh:
            json.dump({"abc": time.time() - 90 * 86400}, fh)
        old = time.time() - 90 * 86400
        os.utime(keep, (old, old))
        try:
            an.write_start_marker("claude", session_id="newer")
            self.assertTrue(os.path.exists(fresh))
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(keep))
        finally:
            for path in (fresh, stale, keep,
                         an._run_marker_path("claude", an._marker_scope(session_id="newer"))):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def test_hook_payload_session_id_is_applied(self):
        payload = {"session_id": "from-stdin", "cwd": "/work"}
        captured = {}

        def fake_run(cfg, event, agent, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        args = _Args(event="started", agent="claude", cwd=None, duration=None,
                     force=False, silent=True, min_duration=None)
        with unittest.mock.patch.object(an, "read_hook_payload", return_value=payload), \
                unittest.mock.patch.object(an, "run_hook", fake_run), \
                unittest.mock.patch.object(an, "Config", return_value=self.cfg):
            with self.assertRaises(SystemExit) as caught:
                an.cmd_hook(args)
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(captured["session_id"], "from-stdin")
        self.assertEqual(captured["cwd"], "/work")

    def test_ready_stdin_is_read_and_an_empty_pipe_does_not_block(self):
        self.assertEqual(
            an.read_hook_payload(raw='{"session_id": "pipe-session"}')["session_id"],
            "pipe-session")
        self.assertEqual(an.read_hook_payload(raw="not-json"), {})
        self.assertEqual(an.read_hook_payload(raw="[1]"), {})
        self.assertEqual(an._hook_session({"sessionId": "camel"}), "camel")

        class _Pipe:
            def __init__(self, fd):
                self._fd = fd

            def fileno(self):
                return self._fd

            def isatty(self):
                return False

        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'{"session_id": "pipe-session"}')
            os.close(write_fd)
            write_fd = None
            self.assertIn("pipe-session", an._read_ready_text(_Pipe(read_fd)))
        finally:
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)
        empty_read, empty_write = os.pipe()
        try:
            started = time.monotonic()
            self.assertEqual(an._read_ready_text(_Pipe(empty_read)), "")
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            os.close(empty_read)
            os.close(empty_write)

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
                # must return, not SystemExit: the topic entered earlier in
                # the wizard would be lost
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

    @unittest.skipIf(sys.platform.startswith("win"),
                     "no bot service installer on Windows (by design, SystemExit)")
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
            with exc:             # it holds the connection (3.14 warns)
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
            with exc:             # it holds the connection (3.14 warns)
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

    @unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
    def test_atomic_text_write_does_not_follow_a_tmp_symlink(self):
        # hooks install aider writes AGENTS.md via AGENTS.md.tmp. A repo that
        # ships that name as a symlink to ~/.bashrc used to be overwritten.
        tmp = tempfile.mkdtemp()
        precious = os.path.join(tmp, "bashrc")
        with open(precious, "w", encoding="utf-8") as fh:
            fh.write("do not touch\n")
        path = os.path.join(tmp, "AGENTS.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("original\n")
        os.symlink(precious, path + ".tmp")
        try:
            an._write_text_atomic(path, "new content\n")
            with open(precious, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "do not touch\n")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "new content\n")
            self.assertFalse(os.path.islink(path))
            self.assertFalse(os.path.lexists(path + ".tmp"))
        finally:
            shutil.rmtree(tmp)

    @unittest.skipIf(os.name == "nt", "os.symlink requires admin or developer mode on Windows")
    def test_atomic_text_write_updates_through_a_config_symlink(self):
        # A dotfiles checkout points ~/.kimi-code/config.toml at the repo.
        # Replacing the link with a regular file left the repo copy, hooks
        # and all. The temp name beside the real file must still not be
        # followed when that name is itself a symlink.
        tmp = tempfile.mkdtemp()
        real_dir = os.path.join(tmp, "dotfiles")
        os.makedirs(real_dir)
        real = os.path.join(real_dir, "config.toml")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("original\n")
        os.chmod(real, 0o600)
        link = os.path.join(tmp, "config.toml")
        os.symlink(real, link)
        precious = os.path.join(tmp, "bashrc")
        with open(precious, "w", encoding="utf-8") as fh:
            fh.write("do not touch\n")
        os.symlink(precious, real + ".tmp")
        try:
            an._write_text_atomic(link, "updated\n")
            self.assertTrue(os.path.islink(link))
            self.assertEqual(os.path.realpath(link), os.path.realpath(real))
            with open(real, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "updated\n")
            with open(precious, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "do not touch\n")
            self.assertFalse(os.path.lexists(real + ".tmp"))
            self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)
        finally:
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
                # an unread body makes Windows reset the connection, and the
                # client never sees the 302
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
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
        an.write_json_atomic(path, {"telegram": {"bot_token": "1:x"}}, mode=0o600)
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
        self.assertTrue(an.is_sensitive_approval("Deploying to production?"))
        self.assertTrue(an.is_sensitive_approval("Deleting the production database?"))
        self.assertTrue(an.is_sensitive_approval("Publishing the release?"))
        self.assertFalse(an.is_sensitive_approval("Deploy to staging?"))
        self.assertFalse(an.is_sensitive_approval("Delete the temporary build file?"))
        self.assertFalse(an.is_sensitive_approval("Update the README heading?"))

    def test_warns_on_public_ntfy_without_auth(self):
        cfg = an.Config({
            "ntfy": {"server": "https://ntfy.sh", "topic": "test-topic-long-enough"},
        })
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
        buf = io.StringIO()
        stderr, sys.stderr = sys.stderr, buf
        try:
            an._warn_insecure_ask(cfg, "Delete the production database?")
        finally:
            sys.stderr = stderr
        self.assertIn("does not have ntfy authentication", buf.getvalue())

    def test_warns_for_every_sensitive_ask_in_a_long_lived_process(self):
        cfg = an.Config({
            "ntfy": {"server": "https://ntfy.sh", "topic": "test-topic-long-enough"},
        })
        buf = io.StringIO()
        stderr, sys.stderr = sys.stderr, buf
        try:
            an._warn_insecure_ask(cfg, "Deploy to production?")
            an._warn_insecure_ask(cfg, "Deploy to production?")
        finally:
            sys.stderr = stderr
        self.assertEqual(buf.getvalue().count("Sensitive approval detected"), 2)


class TestHookAttribution(unittest.TestCase):
    """History records carry the firing agent, so `verify` can observe
    self-integrated agents. Unique slugs per test: history.jsonl is shared
    suite-wide."""

    def setUp(self):
        self.ntfy = MockNtfy()
        self.cfg = make_config(self.ntfy.url, topic="attributiontopic")

    def tearDown(self):
        self.ntfy.stop()

    def _recs(self, slug):
        return [r for r in an.read_history(limit=0) if r.get("agent") == slug]

    def _posts(self):
        return self.ntfy.posts.get("attributiontopic", [])

    def test_hook_history_carries_agent_and_event(self):
        an.run_hook(self.cfg, "run_completed", "att-basic")
        recs = self._recs("att-basic")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "hook.run_completed")
        self.assertNotIn("source_event", recs[0])
        self.assertNotIn("forced", recs[0])

    def test_plain_notify_has_no_agent_field(self):
        an.send_notification(self.cfg, "att plain notify marker")
        recs = [r for r in an.read_history(limit=0)
                if r.get("message") == "att plain notify marker"]
        self.assertEqual(len(recs), 1)
        self.assertNotIn("agent", recs[0])
        self.assertNotIn("forced", recs[0])

    def test_unknown_slug_appears_as_slug_not_agent(self):
        an.run_hook(self.cfg, "run_completed", "att-roo-code")
        body = self._posts()[-1]["body"]
        self.assertIn("att-roo-code", body)
        self.assertNotIn("Agent finished", body)

    def test_suppressed_keeps_source_event(self):
        self.cfg.data["quiet_hours"] = [{"start": "00:00", "end": "23:59"}]
        self.cfg.data["quiet_hours_min_priority"] = 5
        an.run_hook(self.cfg, "run_completed", "att-supp")
        recs = self._recs("att-supp")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "suppressed")
        self.assertEqual(recs[0]["source_event"], "hook.run_completed")

    def test_force_marks_the_record_as_forced(self):
        self.cfg.data["quiet_hours"] = [{"start": "00:00", "end": "23:59"}]
        self.cfg.data["quiet_hours_min_priority"] = 5
        an.run_hook(self.cfg, "run_completed", "att-forced", force=True)
        recs = self._recs("att-forced")
        self.assertEqual(len(recs), 1)
        self.assertIs(recs[0]["forced"], True)
        self.assertEqual(recs[0]["event"], "hook.run_completed")
        self.assertEqual(len(self._posts()), 1)

    def test_plain_forced_notify_carries_no_forced_field(self):
        an.send_notification(self.cfg, "att plain forced marker", force=True)
        recs = [r for r in an.read_history(limit=0)
                if r.get("message") == "att plain forced marker"]
        self.assertEqual(len(recs), 1)
        self.assertNotIn("forced", recs[0])

    def test_unknown_event_exits_zero_records_and_sends_nothing(self):
        args = an.build_parser().parse_args(
            ["hook", "totally_made_up_event", "--agent", "att-unk-evt"])
        with self.assertRaises(SystemExit) as ctx:
            an.cmd_hook(args)
        self.assertEqual(ctx.exception.code, 0)
        recs = self._recs("att-unk-evt")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "hook.unknown_event")
        self.assertEqual(recs[0]["requested"], "totally_made_up_event")
        self.assertEqual(self._posts(), [])

    def test_unknown_event_with_bad_agent_still_exits_two(self):
        args = an.build_parser().parse_args(
            ["hook", "totally_made_up_event", "--agent", "../../evil"])
        with self.assertRaises(SystemExit) as ctx:
            an.cmd_hook(args)
        self.assertEqual(ctx.exception.code, 2)

    def test_alias_still_normalizes_without_choices(self):
        args = an.build_parser().parse_args(["hook", "done", "--agent", "att-alias"])
        with self.assertRaises(SystemExit) as ctx:
            an.cmd_hook(args)
        self.assertEqual(ctx.exception.code, 0)
        recs = self._recs("att-alias")
        self.assertEqual(len(recs), 1)
        self.assertNotEqual(recs[0]["event"], "hook.unknown_event")

    def test_deferred_run_keeps_agent_and_source_event(self):
        self.cfg.data["quiet_hours"] = [{"start": "00:00", "end": "23:59"}]
        self.cfg.data["quiet_hours_min_priority"] = 5
        self.cfg.data["quiet_hours_mode"] = "defer"
        an.run_hook(self.cfg, "run_completed", "att-defer")
        recs = self._recs("att-defer")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "deferred")
        self.assertEqual(recs[0]["source_event"], "hook.run_completed")
        for name in (os.listdir(an.deferred_dir())
                     if os.path.isdir(an.deferred_dir()) else []):
            os.remove(os.path.join(an.deferred_dir(), name))

    def test_mcp_notify_with_agent_is_attributed(self):
        original = an.Config
        an.Config = lambda *a, **kw: self.cfg
        try:
            result = an.mcp_tool_call("notify", {"message": "mcp attributed",
                                                 "agent": "att-mcp"})
        finally:
            an.Config = original
        self.assertEqual(result, "sent")
        recs = self._recs("att-mcp")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "notify")

    def test_mcp_notify_with_hostile_agent_survives_without_field(self):
        original = an.Config
        an.Config = lambda *a, **kw: self.cfg
        try:
            result = an.mcp_tool_call("notify", {"message": "att hostile mcp marker",
                                                 "agent": "../../etc/passwd"})
        finally:
            an.Config = original
        self.assertEqual(result, "sent")
        recs = [r for r in an.read_history(limit=0)
                if r.get("message") == "att hostile mcp marker"]
        self.assertEqual(len(recs), 1)
        self.assertNotIn("agent", recs[0])

    def test_safe_agent_name(self):
        self.assertEqual(an.safe_agent_name("roo-code"), "roo-code")
        self.assertIsNone(an.safe_agent_name(None))
        self.assertIsNone(an.safe_agent_name("../../x"))
        self.assertIsNone(an.safe_agent_name(""))
        self.assertIsNone(an.safe_agent_name("a" * 33))

    def test_queued_keeps_source_event_and_agent(self):
        cfg = make_config("http://127.0.0.1:1")   # nothing listening -> queued
        an.run_hook(cfg, "run_failed", "att-queued")
        recs = self._recs("att-queued")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "queued")
        self.assertEqual(recs[0]["source_event"], "hook.run_failed")
        for name in os.listdir(an.queue_dir()) if os.path.isdir(an.queue_dir()) else []:
            os.remove(os.path.join(an.queue_dir(), name))


def _iso(epoch):
    return an.datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(
        timespec="seconds")


class TestVerifyPrimitives(unittest.TestCase):
    """_parse_since / _history_ts / hook_observations against hand-written
    records - no config, no network, no state dir."""

    def test_parse_since(self):
        self.assertEqual(an._parse_since("7d"), 7 * 86400)
        self.assertEqual(an._parse_since("12h"), 12 * 3600)
        self.assertEqual(an._parse_since("90m"), 5400)
        self.assertEqual(an._parse_since("45s"), 45)
        self.assertEqual(an._parse_since("45"), 45)
        for bad in ("", None, "x7", "-5", "7 d", "1.5h"):
            with self.assertRaises(SystemExit) as ctx:
                an._parse_since(bad)
            self.assertEqual(ctx.exception.code, 2)

    def test_history_ts(self):
        now = time.time()
        self.assertAlmostEqual(an._history_ts({"ts": _iso(now)}), now, delta=1.5)
        self.assertIsNone(an._history_ts({}))
        self.assertIsNone(an._history_ts({"ts": "not-a-date"}))
        self.assertIsNone(an._history_ts({"ts": 12345}))

    def test_buckets_and_window(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 30), "agent": "vp-a", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 20), "agent": "vp-a", "event": "suppressed",
             "source_event": "hook.run_failed"},
            {"ts": _iso(now - 10), "agent": "vp-a", "event": "hook.skipped_short"},
            {"ts": _iso(now - 5), "agent": "vp-a", "event": "hook.run_completed",
             "delivered": [], "errors": {"ntfy": "down"}},
            {"ts": _iso(now - 9000), "agent": "vp-a", "event": "hook.run_completed",
             "delivered": ["ntfy"]},                      # outside the window
            {"ts": _iso(now - 15), "event": "notify", "delivered": ["ntfy"]},  # no agent
            {"agent": "vp-a", "event": "hook.run_completed"},                  # no ts
        ]
        obs = an.hook_observations(records, since_seconds=3600, now=now)
        self.assertEqual(list(obs), ["vp-a"])
        a = obs["vp-a"]
        self.assertEqual(a["count"], 4)
        self.assertEqual(a["delivered"], 1)
        self.assertEqual(a["held"], 1)
        self.assertEqual(a["skipped_short"], 1)
        self.assertEqual(a["failed"], 1)
        self.assertEqual(a["events"]["run_completed"], 2)
        self.assertEqual(a["events"]["run_failed"], 1)
        self.assertEqual(a["events"]["skipped_short"], 1)
        self.assertNotIn("_last", a)

    def test_queued_with_source_event_counts_as_held(self):
        now = time.time()
        records = [{"ts": _iso(now - 3), "agent": "vp-q", "event": "queued",
                    "source_event": "hook.run_completed"}]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(obs["vp-q"]["held"], 1)
        self.assertEqual(obs["vp-q"]["events"], {"run_completed": 1})

    def test_duplicates_within_five_seconds(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 10), "agent": "vp-d", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 8), "agent": "vp-d", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
        ]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(len(obs["vp-d"]["duplicates"]), 1)
        self.assertEqual(obs["vp-d"]["duplicates"][0]["event"], "run_completed")

    def test_no_duplicate_at_sixty_seconds_or_different_event(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 120), "agent": "vp-n", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 60), "agent": "vp-n", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 58), "agent": "vp-n", "event": "hook.run_failed",
             "delivered": ["ntfy"]},
        ]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(obs["vp-n"]["duplicates"], [])

    def test_skipped_short_never_a_duplicate(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 10), "agent": "vp-s", "event": "hook.skipped_short"},
            {"ts": _iso(now - 9), "agent": "vp-s", "event": "hook.skipped_short"},
        ]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(obs["vp-s"]["duplicates"], [])

    def test_permission_burst_is_not_a_duplicate(self):
        """Field-test regression (GitHub Copilot CLI): several real permission
        prompts within seconds - even the same second - are legitimate
        interaction events, not a double integration."""
        now = time.time()
        records = [
            {"ts": _iso(now - 12 + i), "agent": "vp-p",
             "event": "hook.permission_required", "delivered": ["ntfy"]}
            for i in (0, 0, 1, 1, 3)
        ]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(obs["vp-p"]["duplicates"], [])
        self.assertEqual(obs["vp-p"]["events"]["permission_required"], 5)

    def test_input_required_burst_is_not_a_duplicate(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 10), "agent": "vp-i", "event": "hook.input_required",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 9), "agent": "vp-i", "event": "hook.input_required",
             "delivered": ["ntfy"]},
        ]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(obs["vp-i"]["duplicates"], [])

    def test_interleaved_turn_duplicate_still_detected(self):
        """An interaction event between two rapid run_completed records must
        not reset the tracking - detection is per event label."""
        now = time.time()
        records = [
            {"ts": _iso(now - 10), "agent": "vp-x", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 9), "agent": "vp-x", "event": "hook.permission_required",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 8), "agent": "vp-x", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
        ]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(len(obs["vp-x"]["duplicates"]), 1)
        self.assertEqual(obs["vp-x"]["duplicates"][0]["event"], "run_completed")

    def test_forced_records_never_pair_as_duplicates(self):
        """A --force smoke test next to a real event is a human re-running a
        command, not a second integration."""
        now = time.time()
        records = [
            {"ts": _iso(now - 10), "agent": "vp-fd", "event": "hook.run_completed",
             "delivered": ["ntfy"], "forced": True},
            {"ts": _iso(now - 8), "agent": "vp-fd", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 7), "agent": "vp-fd", "event": "hook.run_completed",
             "delivered": ["ntfy"], "forced": True},
        ]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(obs["vp-fd"]["duplicates"], [])

    def test_forced_and_started_and_unknown_are_tracked(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 30), "agent": "vp-f", "event": "hook.run_completed",
             "delivered": ["ntfy"], "forced": True},
            {"ts": _iso(now - 20), "agent": "vp-f", "event": "hook.started",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 10), "agent": "vp-f", "event": "hook.unknown_event",
             "requested": "task_done"},
            {"ts": _iso(now - 9), "agent": "vp-f", "event": "hook.unknown_event",
             "requested": "task_done"},
        ]
        obs = an.hook_observations(records, 3600, now=now)
        f = obs["vp-f"]
        self.assertEqual(f["forced"], 1)
        self.assertEqual(f["started_delivered"], 1)
        self.assertEqual(f["unknown_events"], {"task_done": 2})
        self.assertEqual(f["count"], 2)   # unknown events are not observations


class TestVerify(unittest.TestCase):
    """`verify` observes, sends nothing, and never leaks the topic/server.
    Unique slugs per test - history.jsonl is shared suite-wide."""

    TOPIC = "leakmarker-topic-98765abc"
    SERVER = "http://leak-server-marker.invalid"

    def _cfg(self, topic=None):
        tmp = tempfile.mkdtemp()
        cfg = an.Config({
            "ntfy": {"server": self.SERVER, "topic": topic or self.TOPIC, "auth": None},
            "channels": ["ntfy"],
        }, path=os.path.join(tmp, "config.json"))
        cfg.save()
        return cfg

    def _run(self, cfg, argv):
        """cmd_verify with a fixed Config; returns (exit_code, stdout)."""
        args = an.build_parser().parse_args(["verify"] + argv)
        original = an.Config
        an.Config = lambda *a, **kw: cfg
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = an.cmd_verify(args)
        finally:
            an.Config = original
        return code, buf.getvalue()

    def test_no_events_is_unverified_exit_one(self):
        code, out = self._run(self._cfg(), ["--agent", "vf-none"])
        self.assertEqual(code, 1)
        self.assertIn("nothing known", out)

    def test_two_events_verify_exit_zero(self):
        for _ in range(2):
            an.write_history({"event": "hook.run_completed", "agent": "vf-two",
                              "delivered": ["ntfy"]})
        code, out = self._run(self._cfg(), ["--agent", "vf-two"])
        self.assertEqual(code, 0)
        self.assertIn("2 event(s)", out)
        self.assertIn("self-integrated", out)

    def test_window_excludes_old_events(self):
        an.write_history({"event": "hook.run_completed", "agent": "vf-old",
                          "delivered": ["ntfy"],
                          "ts": _iso(time.time() - 3600)})
        code, _ = self._run(self._cfg(), ["--agent", "vf-old", "--since", "10m"])
        self.assertEqual(code, 1)
        code, _ = self._run(self._cfg(), ["--agent", "vf-old", "--since", "2h"])
        self.assertEqual(code, 0)

    def test_duplicate_warns_but_never_fails(self):
        now = time.time()
        for offset in (6, 3):
            an.write_history({"event": "hook.run_completed", "agent": "vf-dup",
                              "delivered": ["ntfy"], "ts": _iso(now - offset)})
        code, out = self._run(self._cfg(), ["--agent", "vf-dup"])
        self.assertEqual(code, 0)
        self.assertIn("double integration", out)
        self.assertIn("ONE lifecycle mechanism", out)

    def test_held_and_forced_are_reported_honestly(self):
        an.write_history({"event": "suppressed", "source_event": "hook.run_completed",
                          "agent": "vf-held"})
        an.write_history({"event": "hook.run_completed", "agent": "vf-held",
                          "delivered": ["ntfy"], "forced": True,
                          "ts": _iso(time.time() - 30)})
        code, out = self._run(self._cfg(), ["--agent", "vf-held"])
        self.assertEqual(code, 0)
        self.assertIn("1 held", out)
        self.assertIn("1 forced smoke test", out)

    def test_broken_topic_fails_despite_events(self):
        an.write_history({"event": "hook.run_completed", "agent": "vf-badcfg",
                          "delivered": ["ntfy"]})
        code, out = self._run(self._cfg(topic="bad topic!"), ["--agent", "vf-badcfg"])
        self.assertEqual(code, 1)
        self.assertIn("not valid", out)

    def test_no_topic_or_server_in_text_or_json(self):
        an.write_history({"event": "hook.run_completed", "agent": "vf-leak",
                          "delivered": ["ntfy"]})
        for argv in (["--agent", "vf-leak"], ["--agent", "vf-leak", "--json"]):
            _, out = self._run(self._cfg(), argv)
            self.assertNotIn(self.TOPIC, out)
            self.assertNotIn("leak-server-marker", out)
            self.assertNotIn("leakmarker", out)

    def test_read_only(self):
        cfg = self._cfg()
        an.write_history({"event": "hook.run_completed", "agent": "vf-ro",
                          "delivered": ["ntfy"]})
        before_cfg = os.stat(cfg.path).st_mtime_ns
        state = an.state_dir()
        before_state = sorted((n, os.path.getsize(os.path.join(state, n)))
                              for n in os.listdir(state)
                              if os.path.isfile(os.path.join(state, n)))
        self._run(cfg, ["--agent", "vf-ro", "--json"])
        self.assertEqual(os.stat(cfg.path).st_mtime_ns, before_cfg)
        after_state = sorted((n, os.path.getsize(os.path.join(state, n)))
                             for n in os.listdir(state)
                             if os.path.isfile(os.path.join(state, n)))
        self.assertEqual(before_state, after_state)

    def test_bad_agent_name_exits_two(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(self._cfg(), ["--agent", "../../x"])
        self.assertEqual(ctx.exception.code, 2)

    def test_bad_since_exits_two(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(self._cfg(), ["--since", "soon"])
        self.assertEqual(ctx.exception.code, 2)

    def test_json_shape(self):
        an.write_history({"event": "hook.run_completed", "agent": "vf-shape",
                          "delivered": ["ntfy"]})
        _, out = self._run(self._cfg(), ["--agent", "vf-shape", "--json"])
        data = json.loads(out)
        for key in ("verified", "agent", "since", "window_seconds", "checks", "agents"):
            self.assertIn(key, data)
        self.assertIs(data["verified"], True)
        row = data["agents"][0]
        for key in ("agent", "known", "installed", "reliability", "count", "delivered",
                    "held", "skipped_short", "forced", "events", "last_ts",
                    "last_age_seconds", "duplicates"):
            self.assertIn(key, row)
        self.assertEqual(row["reliability"], "self-integrated")
        self.assertIs(row["known"], False)

    def test_outdated_aider_block_is_banner_in_text_and_structured_in_json(self):
        project = tempfile.mkdtemp()
        with open(os.path.join(project, "AGENTS.md"), "w") as fh:
            fh.write(an.BLOCK_START + "\n--agent aider\n" + an.BLOCK_END + "\n")

        code, out = self._run(self._cfg(),
                              ["--agent", "aider", "--project", project])
        self.assertEqual(code, 1)
        self.assertIn("ACTION REQUIRED", out)
        self.assertIn("agentbell hooks install aider", out)

        code, out = self._run(
            self._cfg(), ["--agent", "aider", "--project", project, "--json"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertNotIn("+====", out)  # JSON stays machine-readable; no text banner
        self.assertEqual(data["repair_notices"][0]["code"],
                         "aider_agents_block_outdated")
        self.assertEqual(data["repair_notices"][0]["command"],
                         "agentbell hooks install aider")
        self.assertTrue(data["agents"][0]["installed"])

        an.install_hooks("aider", project=project)
        _, out = self._run(self._cfg(),
                           ["--agent", "aider", "--project", project])
        self.assertNotIn("ACTION REQUIRED", out)
        _, out = self._run(
            self._cfg(), ["--agent", "aider", "--project", project, "--json"])
        self.assertEqual(json.loads(out)["repair_notices"], [])

    def test_delivered_started_warns_about_silent(self):
        an.write_history({"event": "hook.started", "agent": "vf-started",
                          "delivered": ["ntfy"]})
        code, out = self._run(self._cfg(), ["--agent", "vf-started"])
        self.assertEqual(code, 0)
        self.assertIn("--silent", out)

    def test_unknown_event_record_warns_with_valid_list(self):
        an.write_history({"event": "hook.unknown_event", "agent": "vf-unke",
                          "requested": "task_done"})
        an.write_history({"event": "hook.run_completed", "agent": "vf-unke",
                          "delivered": ["ntfy"]})
        code, out = self._run(self._cfg(), ["--agent", "vf-unke"])
        self.assertEqual(code, 0)
        self.assertIn("task_done", out)
        for event in an.HOOK_EVENTS:
            self.assertIn(event, out)

    def test_all_failed_deliveries_fail_the_report(self):
        for _ in range(2):
            an.write_history({"event": "hook.run_completed", "agent": "vf-allfail",
                              "delivered": [], "errors": {"ntfy": "403"}})
        code, out = self._run(self._cfg(), ["--agent", "vf-allfail"])
        self.assertEqual(code, 1)
        self.assertIn("NO event reached any channel", out)
        self.assertIn("[FAIL]", out)

    def test_forced_only_is_not_verified(self):
        an.write_history({"event": "hook.run_completed", "agent": "vf-forceonly",
                          "delivered": ["ntfy"], "forced": True})
        code, out = self._run(self._cfg(), ["--agent", "vf-forceonly"])
        self.assertEqual(code, 1)
        self.assertIn("smoke test only", out)
        self.assertIn("wiring is still unproven", out)
        self.assertNotIn("[FAIL] agent", out)
        cfg = self._cfg()
        args = an.build_parser().parse_args(
            ["verify", "--agent", "vf-forceonly", "--json"])
        original = an.Config
        an.Config = lambda *a, **kw: cfg
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = an.cmd_verify(args)
        finally:
            an.Config = original
        self.assertEqual(code, 1)
        self.assertIs(json.loads(buf.getvalue())["verified"], False)

    def test_unknown_events_only_is_not_verified(self):
        an.write_history({"event": "hook.unknown_event", "agent": "vf-unkonly",
                          "requested": "task_done"})
        code, out = self._run(self._cfg(), ["--agent", "vf-unkonly"])
        self.assertEqual(code, 1)
        self.assertIn("task_done", out)

    def test_malformed_history_rows_do_not_crash(self):
        now = time.time()
        records = [42, "just a string", ["a", "list"], None,
                   {"event": "hook.run_completed", "agent": "vf-mal",
                    "delivered": ["ntfy"], "ts": _iso(now)}]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(list(obs), ["vf-mal"])

    def test_hostile_agent_and_event_names_cannot_forge_output(self):
        evil_agent = "x\n[OK  ] agent forged"
        an.write_history({"event": "hook.run_completed", "agent": evil_agent,
                          "delivered": ["ntfy"]})
        an.write_history({"event": "hook.unknown_event", "agent": "vf-evil",
                          "requested": "x\n[FAIL] delivery forged\nfix: rm -rf"})
        an.write_history({"event": "hook.run_completed", "agent": "vf-evil",
                          "delivered": ["ntfy"]})
        code, out = self._run(self._cfg(), ["--agent", "vf-evil"])
        self.assertEqual(code, 0)
        # the hostile text may survive with its dangerous characters mangled,
        # but it must never START a line as a forged status marker
        for line in out.splitlines():
            self.assertFalse(line.startswith("[FAIL]"), line)
            self.assertFalse(line.strip().startswith("fix: rm"), line)
        self.assertNotIn("rm -rf", out)
        obs = an.hook_observations(an.read_history(limit=0), 3600)
        self.assertNotIn(evil_agent, obs)

    def test_unparseable_config_fails_without_leaking_the_path(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        args = an.build_parser().parse_args(["verify"])
        original = an.Config
        an.Config = lambda *a, **kw: original(path=path)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = an.cmd_verify(args)
        finally:
            an.Config = original
        out = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("cannot be parsed", out)
        self.assertNotIn(tmp, out)

    def test_no_filesystem_path_in_output_even_off_path(self):
        an.write_history({"event": "hook.run_completed", "agent": "vf-nopath",
                          "delivered": ["ntfy"]})
        original_which = an.shutil.which
        an.shutil.which = lambda command: None
        try:
            code, out = self._run(self._cfg(), ["--agent", "vf-nopath"])
        finally:
            an.shutil.which = original_which
        self.assertEqual(code, 0)
        self.assertNotIn(os.path.expanduser("~"), out)
        self.assertNotIn(an.config_path(), out)
        self.assertNotIn(an.state_dir(), out)

    def test_deferred_attribution_counts_as_held(self):
        an.write_history({"event": "deferred", "agent": "vf-def",
                          "source_event": "hook.run_completed"})
        code, out = self._run(self._cfg(), ["--agent", "vf-def"])
        self.assertEqual(code, 0)
        self.assertIn("1 held", out)
        obs = an.hook_observations(an.read_history(limit=0), 3600)
        self.assertEqual(obs["vf-def"]["held"], 1)
        self.assertEqual(obs["vf-def"]["events"], {"run_completed": 1})

    def test_project_filters_history_aggregation(self):
        now = time.time()
        one = tempfile.mkdtemp()
        two = tempfile.mkdtemp()
        records = [
            {"ts": _iso(now), "event": "hook.run_completed", "agent": "vf-project",
             "project": one, "delivered": ["ntfy"]},
            {"ts": _iso(now), "event": "hook.run_completed", "agent": "vf-project",
             "project": two, "delivered": ["ntfy"]},
            {"ts": _iso(now), "event": "hook.run_completed", "agent": "vf-project",
             "delivered": ["ntfy"]},
        ]
        with unittest.mock.patch.object(an, "read_history", return_value=records), \
                unittest.mock.patch.object(an, "hooks_status", return_value=[]):
            report = an.verify_report(
                self._cfg(), agent="vf-project", since_seconds=3600,
                project=one, now=now)
        self.assertEqual(report["agents"][0]["count"], 1)

    def test_hook_history_records_normalized_project(self):
        project = tempfile.mkdtemp()
        cfg = self._cfg()
        with unittest.mock.patch.object(an, "send_notification") as send:
            send.return_value = {"ok": True}
            an.run_hook(cfg, "run_completed", "vf-project-record", cwd=project)
        self.assertEqual(send.call_args.kwargs["project"], an._normalized_project(project))

    def test_verify_reads_agents_md_once(self):
        project = tempfile.mkdtemp()
        with open(os.path.join(project, "AGENTS.md"), "w", encoding="utf-8") as fh:
            fh.write(an.BLOCK_START + "\n--agent aider\n" + an.BLOCK_END + "\n")
        original = an.aider_block_state
        calls = []

        def counted(value=None):
            calls.append(value)
            return original(value)

        with unittest.mock.patch.object(an, "aider_block_state", side_effect=counted):
            an.verify_report(self._cfg(), agent="aider", project=project)
        self.assertEqual(calls, [project])

    def test_known_agent_without_events_suggests_native_installer(self):
        original = an.hooks_status
        an.hooks_status = lambda project=None: [
            (a, "not installed", "x", "hook") for a in an.AGENTS]
        try:
            with unittest.mock.patch.object(an, "read_history", return_value=[]):
                code, out = self._run(self._cfg(), ["--agent", "claude"])
        finally:
            an.hooks_status = original
        self.assertEqual(code, 1)
        self.assertIn("hooks install claude", out)

    def test_installed_without_events_warns_finish_a_turn(self):
        original = an.hooks_status
        an.hooks_status = lambda project=None: [
            ("claude", "installed", "x", "hook")]
        try:
            with unittest.mock.patch.object(an, "read_history", return_value=[]):
                code, out = self._run(self._cfg(), [])
        finally:
            an.hooks_status = original
        self.assertEqual(code, 1)
        self.assertIn("finish one real agent turn", out)

    def test_user_wrapper_is_installed_but_warned(self):
        original = an.hooks_status
        an.hooks_status = lambda project=None: [
            ("claude", "user wrapper", "x", "hook")]
        try:
            with unittest.mock.patch.object(an, "read_history", return_value=[]):
                report = an.verify_report(self._cfg(), agent="claude")
        finally:
            an.hooks_status = original
        self.assertTrue(report["agents"][0]["installed"])
        self.assertTrue(any(c["status"] == an.WARN and "user-owned" in c["detail"]
                            for c in report["checks"]))


class TestAgentbellBinary(unittest.TestCase):
    """agentbell_binary() feeds every published contract, hook command and
    service file. Regression: `python -m unittest` rewrites sys.argv[0] to
    the literal string "python -m unittest" (stdlib unittest/__main__.py);
    with agentbell not on PATH the old argv[0] fallback published
    "<cwd>/python -m unittest" as the executable - every CI job, and any
    embedder with a foreign argv[0]."""

    def _binary(self, argv0, which=None):
        original_argv, original_which = sys.argv, an.shutil.which
        sys.argv = [argv0] + sys.argv[1:]
        an.shutil.which = lambda prog: which
        try:
            return an.agentbell_binary()
        finally:
            sys.argv, an.shutil.which = original_argv, original_which

    def test_path_install_wins(self):
        installed = os.path.abspath(os.path.join(os.sep, "somewhere", "agentbell"))
        self.assertEqual(self._binary("python -m unittest", which=installed), installed)

    def test_test_runner_argv_never_leaks(self):
        binary = self._binary("python -m unittest")
        self.assertEqual(binary, os.path.abspath(an.__file__))
        self.assertNotIn("unittest", binary)

    def test_foreign_existing_script_rejected(self):
        # a test runner's own script exists on disk but is not agentbell
        binary = self._binary(os.path.abspath(__file__))
        self.assertEqual(binary, os.path.abspath(an.__file__))

    def test_launcher_off_path_accepted(self):
        # installed launcher invoked by absolute path, its dir not on PATH
        tmp = tempfile.mkdtemp()
        for name in ("agentbell", "agentbell.exe", "agentbell.py"):
            launcher = os.path.join(tmp, name)
            with open(launcher, "w") as fh:
                fh.write("#!/bin/sh\n")
            self.assertEqual(self._binary(launcher), launcher, name)

    def test_empty_argv_falls_back_to_module(self):
        # embedded interpreters can leave argv[0] empty
        self.assertEqual(self._binary(""), os.path.abspath(an.__file__))

    def test_contract_binary_survives_test_runner(self):
        # the exact CI failure: manifest built under `python -m unittest`
        original_argv, original_which = sys.argv, an.shutil.which
        sys.argv = ["python -m unittest"] + sys.argv[1:]
        an.shutil.which = lambda prog: None
        try:
            manifest = an.integration_manifest(agent="roo-code")
        finally:
            sys.argv, an.shutil.which = original_argv, original_which
        self.assertEqual(manifest["binary"], os.path.abspath(an.__file__))
        # agentbell.py runs through the interpreter, as generated hooks do
        smoke = [part.replace("\\", "/") for part in shlex.split(manifest["commands"]["smoke"])]
        self.assertEqual(smoke[:2], [part.replace("\\", "/")
                                     for part in (sys.executable, manifest["binary"])])


class TestIntegrationGuide(unittest.TestCase):
    """`integrate` publishes the contract. The anti-drift test is the load-
    bearing one: every command the guide hands to an agent must parse against
    the real CLI, forever."""

    def _manifest(self, agent=None):
        return an.integration_manifest(agent=agent)

    def _guide(self, agent=None):
        return an.integration_guide(self._manifest(agent=agent))

    def test_every_advertised_command_parses(self):
        manifest = self._manifest(agent="roo-code")
        parser = an.build_parser()
        commands = list(manifest["commands"].values())
        commands += [e["command"] for e in manifest["events"]]
        for step in manifest["verification"]["steps"]:
            commands += step["commands"]
        self.assertGreaterEqual(len(commands), 12)
        prefix = manifest["command_prefix"] + " "   # the binary, or python + agentbell.py
        for command in commands:
            self.assertTrue(command.startswith(prefix), command)
            try:
                args = parser.parse_args(shlex.split(command[len(prefix):]))
            except SystemExit as exc:   # pragma: no cover - the assertion message matters
                self.fail(f"guide command does not parse: {command} ({exc})")
            self.assertTrue(callable(getattr(args, "func", None)), command)

    def test_appendix_rules_block_commands_parse_too(self):
        """The rules block is executed by agents verbatim - its backticked
        commands must parse against the real CLI, same as the manifest's."""
        manifest = self._manifest(agent="roo-code")
        block = manifest["rules_block"]["text"]
        commands = re.findall(r"`(agentbell [^`]+)`", block)
        self.assertGreaterEqual(len(commands), 4)
        parser = an.build_parser()
        for command in commands:
            parts = shlex.split(command)
            try:
                args = parser.parse_args(parts[1:])
            except SystemExit as exc:
                self.fail(f"appendix command does not parse: {command} ({exc})")
            self.assertTrue(callable(getattr(args, "func", None)), command)

    def test_all_hook_events_and_binary_in_guide(self):
        guide = self._guide()
        for event in an.HOOK_EVENTS:
            self.assertIn(event, guide)
        self.assertIn(an.agentbell_binary(), guide)
        self.assertIn(an.INTEGRATE_PLACEHOLDER, guide)

    def test_known_slug_redirects_to_native_installer(self):
        manifest = self._manifest(agent="claude")
        self.assertTrue(manifest["agent_slug"]["is_known"])
        guide = an.integration_guide(manifest)
        self.assertIn("hooks install claude", guide)
        self.assertIn("STOP", guide)

    def test_unknown_slug_is_not_known(self):
        manifest = self._manifest(agent="roo-code")
        self.assertFalse(manifest["agent_slug"]["is_known"])
        self.assertEqual(manifest["agent_slug"]["value"], "roo-code")
        self.assertIn("roo-code", an.integration_guide(manifest))

    def test_ask_exit_codes_include_runtime_error(self):
        ask_codes = self._manifest(agent="roo-code")["exit_codes"]["ask"]
        self.assertIn("3", ask_codes)
        self.assertIn("error", ask_codes["3"])

    def test_no_credential_can_leak(self):
        """integrate never reads Config - even a populated config file with
        marker values must leave no trace in text or JSON output."""
        path = an.config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existed = os.path.exists(path)
        self.assertFalse(existed, "sandbox config unexpectedly exists")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ntfy": {"server": "http://secret-server-marker",
                                "topic": "secret-topic-marker"},
                       "telegram": {"bot_token": "secret-token-marker"},
                       "webhook": {"token": "secret-webhook-marker"}}, fh)
        try:
            guide = self._guide()
            blob = json.dumps(self._manifest())
            for marker in ("secret-server-marker", "secret-topic-marker",
                           "secret-token-marker", "secret-webhook-marker"):
                self.assertNotIn(marker, guide)
                self.assertNotIn(marker, blob)
        finally:
            os.remove(path)

    def test_placeholder_is_shell_safe_and_regex_valid(self):
        self.assertTrue(an.AGENT_NAME_RE.fullmatch(an.INTEGRATE_PLACEHOLDER))
        self.assertEqual(shlex.quote(an.INTEGRATE_PLACEHOLDER), an.INTEGRATE_PLACEHOLDER)

    def test_silent_and_min_duration_are_coupled_in_one_section(self):
        guide = self._guide()
        manifest = self._manifest()
        started = guide.index(manifest["commands"]["started_silent"])
        completed = guide.index(manifest["commands"]["completed_min_duration"])
        self.assertLess(started, completed)
        self.assertEqual(guide.count("\n", started, completed), 1)   # adjacent lines
        self.assertIn("BOTH", guide)

    def test_rules_block_uses_slug_scoped_markers_only(self):
        manifest = self._manifest(agent="roo-code")
        guide = an.integration_guide(manifest)
        self.assertIn("<!-- agentbell:roo-code:start -->", guide)
        self.assertIn("<!-- agentbell:roo-code:end -->", guide)
        self.assertIn(an._instructions_text("roo-code"), guide)
        self.assertNotIn(an.BLOCK_START, guide)     # generic marker would
        self.assertNotIn(an.BLOCK_END, guide)       # collide with hooks install

    def test_windows_variant(self):
        original_system = an.platform.system
        original_which = an.shutil.which
        an.platform.system = lambda: "Windows"
        an.shutil.which = lambda command: None
        try:
            guide = self._guide()
        finally:
            an.platform.system = original_system
            an.shutil.which = original_which
        self.assertIn("py -m agentbell", guide)
        self.assertIn("not on the user's PATH", guide)

    def test_json_roundtrip_and_required_keys(self):
        blob = json.dumps(self._manifest())
        data = json.loads(blob)
        for key in ("contract_version", "agentbell_version", "binary",
                    "binary_on_path", "platform", "agent_slug", "mechanisms",
                    "events", "commands", "duration", "exit_codes", "mcp",
                    "rules_block", "known_agents", "policy", "safety",
                    "verification", "report_template"):
            self.assertIn(key, data)
        self.assertIsInstance(data["contract_version"], int)
        self.assertEqual(len(data["events"]), len(an.HOOK_EVENTS))
        self.assertEqual(len(data["known_agents"]), len(an.AGENTS))

    def test_guide_stays_short(self):
        for agent in (None, "claude", "roo-code"):
            guide = self._guide(agent=agent)
            self.assertLessEqual(len(guide.splitlines()), 170,
                                 f"guide too long for agent={agent}")

    def test_read_only(self):
        state = an.state_dir()
        os.makedirs(state, exist_ok=True)
        before = sorted(os.listdir(state))
        args = an.build_parser().parse_args(["integrate", "--json"])
        with contextlib.redirect_stdout(io.StringIO()):
            an.cmd_integrate(args)
        self.assertEqual(sorted(os.listdir(state)), before)
        self.assertFalse(os.path.exists(an.config_path()))

    def test_first_line_says_changed_nothing(self):
        first = self._guide().splitlines()[0].lower()
        self.assertIn("changed nothing", first)



class TestHookDedupe(unittest.TestCase):
    """An identical hook push within the window is one piece of news: the
    repeat is suppressed and recorded as hook.skipped_duplicate (v1.6.1)."""

    def setUp(self):
        self.ntfy = MockNtfy()
        self.cfg = make_config(self.ntfy.url, topic="deduptopic")
        self._old_window = an.HOOK_DEDUPE_WINDOW_SECONDS
        an.HOOK_DEDUPE_WINDOW_SECONDS = 5.0
        self._clear()

    def tearDown(self):
        an.HOOK_DEDUPE_WINDOW_SECONDS = self._old_window
        self._clear()
        self.ntfy.stop()

    def _clear(self):
        for path in (an._dedupe_path(), an._run_marker_path("dd-claude")):
            try:
                os.remove(path)
            except OSError:
                pass

    def _posts(self):
        return self.ntfy.posts.get("deduptopic", [])

    def _skipped(self):
        return [r for r in an.read_history(limit=0)
                if r.get("event") == "hook.skipped_duplicate" and r.get("agent") == "dd-claude"]

    def test_identical_failure_burst_is_one_push(self):
        before = len(self._skipped())
        for _ in range(6):
            an.run_hook(self.cfg, "run_failed", "dd-claude", cwd="/repo/agent-ops")
        self.assertEqual(len(self._posts()), 1)
        skipped = self._skipped()[before:]
        self.assertEqual(len(skipped), 5)
        self.assertEqual(skipped[0]["source_event"], "hook.run_failed")
        self.assertIn("/repo/agent-ops", skipped[0]["message"])
        self.assertEqual(skipped[0]["window"], 5.0)

    def test_different_text_is_never_a_repeat(self):
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/a")
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/b")
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/a", duration=300)
        self.assertEqual(len(self._posts()), 3)

    def test_repeat_after_the_window_is_delivered(self):
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/a")
        with open(an._dedupe_path()) as fh:
            seen = json.load(fh)
        seen = {k: v - 6 for k, v in seen.items()}
        with open(an._dedupe_path(), "w") as fh:
            json.dump(seen, fh)
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/a")
        self.assertEqual(len(self._posts()), 2)
        with open(an._dedupe_path()) as fh:
            self.assertEqual(len(json.load(fh)), 1)   # pruned to the window

    def test_force_bypasses_the_window(self):
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/a")
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/a", force=True)
        self.assertEqual(len(self._posts()), 2)

    def test_window_zero_disables_it(self):
        an.HOOK_DEDUPE_WINDOW_SECONDS = 0
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/a")
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/a")
        self.assertEqual(len(self._posts()), 2)

    def test_corrupt_bookkeeping_never_blocks_a_push(self):
        an.ensure_state_dir(os.path.dirname(an._dedupe_path()))
        with open(an._dedupe_path(), "w") as fh:
            fh.write("{not json")
        an.run_hook(self.cfg, "run_completed", "dd-claude", cwd="/repo/a")
        self.assertEqual(len(self._posts()), 1)


class TestVerifyDedupe(unittest.TestCase):
    """verify: failure bursts are not a double integration; suppressed
    repeats are observed, counted and (for turn events) the duplicate evidence."""

    def test_failure_burst_is_not_a_duplicate(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 12 + i), "agent": "vp-fb", "event": "hook.run_failed",
             "delivered": ["ntfy"]}
            for i in (0, 4, 4, 5, 5, 7)
        ]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(obs["vp-fb"]["duplicates"], [])
        self.assertEqual(obs["vp-fb"]["events"]["run_failed"], 6)

    def test_suppressed_turn_repeat_is_the_duplicate_evidence(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 10), "agent": "vp-sd", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 9), "agent": "vp-sd", "event": "hook.skipped_duplicate",
             "source_event": "hook.run_completed", "message": "x"},
        ]
        obs = an.hook_observations(records, 3600, now=now)
        o = obs["vp-sd"]
        self.assertEqual(o["count"], 2)
        self.assertEqual(o["delivered"], 1)
        self.assertEqual(o["skipped_duplicate"], 1)
        self.assertEqual(o["events"], {"run_completed": 2})
        self.assertEqual(len(o["duplicates"]), 1)
        self.assertTrue(o["duplicates"][0]["suppressed"])
        self.assertEqual(o["duplicates"][0]["gap_seconds"], 1.0)
        self.assertIn("1 suppressed (identical push)", an._obs_sentence(o, now=now))

    def test_suppressed_failure_repeat_counts_but_does_not_warn(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 10), "agent": "vp-sf", "event": "hook.run_failed",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 9), "agent": "vp-sf", "event": "hook.skipped_duplicate",
             "source_event": "hook.run_failed", "message": "x"},
        ]
        obs = an.hook_observations(records, 3600, now=now)
        self.assertEqual(obs["vp-sf"]["skipped_duplicate"], 1)
        self.assertEqual(obs["vp-sf"]["duplicates"], [])

    def test_report_row_carries_the_bucket_and_warns(self):
        now = time.time()
        records = [
            {"ts": _iso(now - 10), "agent": "vp-rr", "event": "hook.run_completed",
             "delivered": ["ntfy"]},
            {"ts": _iso(now - 9), "agent": "vp-rr", "event": "hook.skipped_duplicate",
             "source_event": "hook.run_completed", "message": "x"},
        ]
        with unittest.mock.patch.object(an, "read_history", return_value=records):
            report = an.verify_report(an.Config(), agent="vp-rr", since_seconds=3600, now=now)
        row = [r for r in report["agents"] if r["agent"] == "vp-rr"][0]
        self.assertEqual(row["skipped_duplicate"], 1)
        warns = [c for c in report["checks"] if c["status"] == an.WARN
                 and "double integration" in c["detail"]]
        self.assertEqual(len(warns), 1)
        self.assertIn("suppressed", warns[0]["detail"])


@unittest.skipUnless(shutil.which("node"), "node not available")
class TestOpenCodePluginBehaviour(unittest.TestCase):
    """Replays OpenCode bus events through the real plugin file under node
    with a recording `$` - the plugin logic itself, not just its text."""

    HARNESS = r"""
import { pathToFileURL } from "node:url";
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
const script = JSON.parse(process.argv[3]);
for (const step of script) {
  if (step.sleep) { await new Promise(r => setTimeout(r, step.sleep)); continue; }
  await hooks.event({ event: step });
}
console.log(JSON.stringify(calls));
"""

    def _run(self, script):
        tmp = tempfile.mkdtemp(prefix="agentbell-plugin-")
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
        calls = json.loads(out.stdout.strip().splitlines()[-1])
        return [c[1:] for c in calls]          # drop the binary

    def test_double_idle_is_one_turn_end_with_duration(self):
        calls = self._run([
            {"type": "message.updated", "properties": {"info": {"role": "user", "sessionID": "s1"}}},
            {"type": "message.updated", "properties": {"info": {"role": "assistant", "sessionID": "s1"}}},
            {"sleep": 1100},
            {"type": "session.idle", "properties": {"sessionID": "s1"}},
            {"type": "session.idle", "properties": {"sessionID": "s1"}},
        ])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["hook", "run_completed", "--agent", "opencode"])
        self.assertIn("--min-duration", calls[0])
        self.assertEqual(calls[0][calls[0].index("--min-duration") + 1], str(an.HOOK_MIN_DURATION))
        self.assertEqual(calls[0][calls[0].index("--duration") + 1], "1")

    def test_unknown_duration_sends_without_threshold(self):
        calls = self._run([{"type": "session.idle", "properties": {"sessionID": "s2"}}])
        self.assertEqual(calls, [["hook", "run_completed", "--agent", "opencode"]])

    def test_two_sessions_are_two_turn_ends(self):
        calls = self._run([
            {"type": "session.idle", "properties": {"sessionID": "a"}},
            {"type": "session.idle", "properties": {"sessionID": "b"}},
        ])
        self.assertEqual(len(calls), 2)

    def test_child_session_events_are_ignored(self):
        calls = self._run([
            {"type": "session.created", "properties": {"info": {"id": "child", "parentID": "root"}}},
            {"type": "message.updated", "properties": {"info": {"role": "user", "sessionID": "child"}}},
            {"type": "session.idle", "properties": {"sessionID": "child"}},
            {"type": "session.error", "properties": {"sessionID": "child"}},
            {"type": "session.idle", "properties": {"sessionID": "root"}},
        ])
        self.assertEqual(calls, [["hook", "run_completed", "--agent", "opencode"]])

    def test_error_resets_the_turn_and_notifies(self):
        calls = self._run([
            {"type": "message.updated", "properties": {"info": {"role": "user", "sessionID": "e"}}},
            {"type": "session.error", "properties": {"sessionID": "e"}},
            {"type": "session.idle", "properties": {"sessionID": "e"}},
        ])
        self.assertEqual(calls[0], ["hook", "run_failed", "--agent", "opencode"])
        self.assertEqual(calls[1], ["hook", "run_completed", "--agent", "opencode"])

    def test_legacy_entry_also_accepts_the_v2_payload(self):
        """The payload key is normalised in one place, so the 1.x entry point
        answers a `{type, data}` event too - that keeps a mixed install from
        going silent during an upgrade."""
        calls = self._run([
            {"type": "message.updated",
             "data": {"sessionID": "v2s", "info": {"role": "user", "time": {"created": 1}}}},
            {"type": "session.execution.succeeded", "data": {"sessionID": "v2s"}},
        ])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["hook", "run_completed", "--agent", "opencode"])
        self.assertEqual(calls[0][calls[0].index("--min-duration") + 1],
                         str(an.HOOK_MIN_DURATION))


def _render_opencode_plugin_for(command=("agentbell",)):
    """The rendered OpenCode plugin with `command` pinned as agentbell's argv.

    agentbell_command() depends on where agentbell lives on the machine: one
    element for an installed launcher, [python, agentbell.py] for a checkout.
    A test that records the plugin's argv must not inherit that.
    """
    with unittest.mock.patch.object(an, "agentbell_command", return_value=list(command)):
        return an._render_opencode_plugin()


@unittest.skipUnless(shutil.which("node"), "node not available")
class TestOpenCodePluginV2Behaviour(unittest.TestCase):
    """Replays OpenCode 2.x bus events through the real plugin file's default
    export. Event names and payload shapes come from the installed 2.0.16
    binary; the evidence is summarised in DECISIONS.md §38.

    The clock is faked so the 10 s turn-end dedupe can be exercised without
    the test taking ten seconds."""

    HARNESS = r"""
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const calls = [];
globalThis.Bun = { spawn(args) {
  calls.push(args);
  return { exited: Promise.resolve(0) };
} };
let now = 1000000;
Date.now = () => now;
const plugin = (await import(pathToFileURL(process.argv[1]).href)).default;
assert.equal(plugin.id, "agentbell");
assert.equal(typeof plugin.setup, "function");
const script = JSON.parse(process.argv[2]);
let subscribeOptions = null;
let closed = false;
const cleanup = plugin.setup({ event: { async *subscribe(options) {
  subscribeOptions = options;
  const signal = options && options.signal;
  try {
    for (const step of script) {
      if (step.clock) { now += step.clock; continue; }
      yield step;
    }
    await new Promise((resolve) => {
      if (!signal) return;
      if (signal.aborted) return resolve();
      signal.addEventListener("abort", resolve, { once: true });
    });
  } finally { closed = true; }
} } });
assert.equal(typeof cleanup, "function");
await new Promise((resolve) => setTimeout(resolve, 25));
cleanup();
for (let i = 0; i < 50 && !closed; i++) await new Promise((r) => setTimeout(r, 5));
assert.equal(closed, true, "cleanup did not stop the subscription");
console.log(JSON.stringify({
  calls,
  aborted: Boolean(subscribeOptions && subscribeOptions.signal
                   && subscribeOptions.signal.aborted),
}));
"""

    def _run(self, script, command=("agentbell",)):
        tmp = tempfile.mkdtemp(prefix="agentbell-plugin-v2-")
        plugin = os.path.join(tmp, "agentbell.mjs")
        with open(plugin, "w", encoding="utf-8") as fh:
            fh.write(_render_opencode_plugin_for(command))
        out = subprocess.run(["node", "--input-type=module", "-e", self.HARNESS,
                              plugin, json.dumps(script)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        meta = json.loads(out.stdout.strip().splitlines()[-1])
        # Bun.spawn gets [...command, ...args]; check the command, hand the
        # tests only the arguments they are about
        for call in meta["calls"]:
            self.assertEqual(call[:len(command)], list(command))
        meta["calls"] = [call[len(command):] for call in meta["calls"]]
        return meta

    def test_a_checkout_install_spawns_the_interpreter_and_the_script(self):
        """agentbell.py is not executable, so agentbell_command() is
        [python, script] and the rendered BIN is an array; both elements have
        to reach Bun.spawn ahead of the hook arguments."""
        meta = self._run([{"type": "session.execution.succeeded",
                           "data": {"sessionID": "s10"}}],
                         command=("/usr/bin/python3", "/opt/agentbell/agentbell.py"))
        self.assertEqual(meta["calls"],
                         [["hook", "run_completed", "--agent", "opencode"]])

    def test_success_reports_the_prompt_duration_and_cleans_up(self):
        meta = self._run([
            {"type": "message.updated",
             "data": {"sessionID": "s1", "info": {"role": "user", "time": {"created": 1000}}}},
            {"clock": 1100},
            {"type": "session.execution.succeeded", "data": {"sessionID": "s1"}},
            {"type": "session.execution.succeeded", "data": {"sessionID": "s1"}},
        ])
        # one push for the turn, not one per completion report
        self.assertEqual(len(meta["calls"]), 1)
        call = meta["calls"][0]
        self.assertEqual(call[:4], ["hook", "run_completed", "--agent", "opencode"])
        self.assertEqual(call[call.index("--duration") + 1], "1")
        self.assertEqual(call[call.index("--min-duration") + 1], str(an.HOOK_MIN_DURATION))
        self.assertTrue(meta["aborted"], "the abort signal was not passed to subscribe")

    def test_unknown_duration_still_notifies(self):
        meta = self._run([{"type": "session.execution.succeeded",
                           "data": {"sessionID": "s2"}}])
        self.assertEqual(meta["calls"],
                         [["hook", "run_completed", "--agent", "opencode"]])

    def test_failure_notifies_and_does_not_report_a_duration(self):
        meta = self._run([
            {"type": "message.updated",
             "data": {"sessionID": "s3", "info": {"role": "user", "time": {"created": 1000}}}},
            {"type": "session.execution.failed",
             "data": {"sessionID": "s3", "error": {"name": "APIError", "message": "boom"}}},
        ])
        self.assertEqual(meta["calls"], [["hook", "run_failed", "--agent", "opencode"]])

    def test_permission_asked_is_throttled_to_one_ping(self):
        meta = self._run([
            {"type": "permission.asked", "data": {"sessionID": "s4", "id": "per_1"}},
            {"type": "permission.asked", "data": {"sessionID": "s4", "id": "per_2"}},
        ])
        self.assertEqual(meta["calls"],
                         [["hook", "permission_required", "--agent", "opencode"]])

    def test_child_sessions_are_ignored(self):
        meta = self._run([
            {"type": "session.created", "data": {"sessionID": "child", "parentID": "root"}},
            {"type": "message.updated",
             "data": {"sessionID": "child", "info": {"role": "user", "time": {"created": 1}}}},
            {"type": "session.execution.succeeded", "data": {"sessionID": "child"}},
            {"type": "session.execution.failed", "data": {"sessionID": "child"}},
            {"type": "session.execution.succeeded", "data": {"sessionID": "root"}},
        ])
        self.assertEqual(meta["calls"],
                         [["hook", "run_completed", "--agent", "opencode"]])

    def test_resent_prompt_does_not_start_a_turn(self):
        meta = self._run([
            {"type": "message.updated",
             "data": {"sessionID": "s5", "info": {"role": "user", "time": {"created": 1000}}}},
            {"clock": 1100},
            {"type": "session.execution.succeeded", "data": {"sessionID": "s5"}},
            # 2.x re-sends the prompt after the turn (background diff summary);
            # the old message must not be read as the start of a new one
            {"type": "message.updated",
             "data": {"sessionID": "s5", "info": {"role": "user", "time": {"created": 1000}}}},
            {"clock": 10500},
            {"type": "session.execution.succeeded", "data": {"sessionID": "s5"}},
            # the real next prompt does start a turn
            {"type": "message.updated",
             "data": {"sessionID": "s5", "info": {"role": "user", "time": {"created": 2000000}}}},
            {"clock": 11000},
            {"type": "session.execution.succeeded", "data": {"sessionID": "s5"}},
        ])
        self.assertEqual(len(meta["calls"]), 3)
        self.assertEqual(meta["calls"][0][meta["calls"][0].index("--duration") + 1], "1")
        self.assertNotIn("--duration", meta["calls"][1])
        self.assertEqual(meta["calls"][2][meta["calls"][2].index("--duration") + 1], "11")

    def test_interrupt_clears_the_turn_without_notifying(self):
        meta = self._run([
            {"type": "message.updated",
             "data": {"sessionID": "s6", "info": {"role": "user", "time": {"created": 1000}}}},
            {"clock": 30000},
            {"type": "session.execution.interrupted",
             "data": {"sessionID": "s6", "reason": "user"}},
            # a genuinely newer prompt (time.created after the interrupt) still
            # starts the next turn; only a replay of the old one must not
            {"type": "message.updated",
             "data": {"sessionID": "s6", "info": {"role": "user", "time": {"created": 2000000}}}},
            {"clock": 2000},
            {"type": "session.execution.succeeded", "data": {"sessionID": "s6"}},
        ])
        self.assertEqual(len(meta["calls"]), 1)
        self.assertEqual(meta["calls"][0][meta["calls"][0].index("--duration") + 1], "2")

    def test_interrupted_turn_cannot_be_rearmed_by_a_replayed_prompt(self):
        """The interrupt is silent, but it still has to be *terminal*: OpenCode
        re-sends the previous prompt after a turn, so a clear-only interrupt
        would let that old prompt start a new turn and then report the gap as
        the next turn's duration. The recorded end timestamp blocks it."""
        meta = self._run([
            {"type": "message.updated",
             "data": {"sessionID": "s8", "info": {"role": "user", "time": {"created": 1000}}}},
            {"clock": 5000},
            {"type": "session.execution.interrupted",
             "data": {"sessionID": "s8", "reason": "shutdown"}},
            # same prompt, same created stamp: a replay, not a new turn
            {"type": "message.updated",
             "data": {"sessionID": "s8", "info": {"role": "user", "time": {"created": 1000}}}},
            {"clock": 2000},
            {"type": "session.execution.succeeded", "data": {"sessionID": "s8"}},
        ])
        self.assertEqual(len(meta["calls"]), 1)
        self.assertNotIn("--duration", meta["calls"][0])

    def test_failed_turn_cannot_be_rearmed_by_a_replayed_prompt(self):
        meta = self._run([
            {"type": "message.updated",
             "data": {"sessionID": "s9", "info": {"role": "user", "time": {"created": 1000}}}},
            {"clock": 5000},
            {"type": "session.execution.failed",
             "data": {"sessionID": "s9", "error": {"name": "APIError", "message": "boom"}}},
            {"type": "message.updated",
             "data": {"sessionID": "s9", "info": {"role": "user", "time": {"created": 1000}}}},
            {"clock": 2000},
            {"type": "session.execution.succeeded", "data": {"sessionID": "s9"}},
        ])
        self.assertEqual(meta["calls"][0], ["hook", "run_failed", "--agent", "opencode"])
        self.assertEqual(len(meta["calls"]), 2)
        self.assertNotIn("--duration", meta["calls"][1])

    def test_unknown_events_are_harmless(self):
        meta = self._run([
            {"type": "session.step.started",
             "data": {"sessionID": "s7", "assistantMessageID": "msg_1"}},
            {"type": "server.connected", "data": {}},
            {"type": "whiteboard.changed", "data": {"whatever": True}},
        ])
        self.assertEqual(meta["calls"], [])

    def test_setup_without_an_event_context_is_a_noop(self):
        tmp = tempfile.mkdtemp(prefix="agentbell-plugin-v2-noctx-")
        plugin = os.path.join(tmp, "agentbell.mjs")
        with open(plugin, "w", encoding="utf-8") as fh:
            fh.write(an._render_opencode_plugin())
        harness = (
            "import assert from 'node:assert/strict';\n"
            "import { pathToFileURL } from 'node:url';\n"
            "globalThis.Bun = { spawn() { throw new Error('unexpected spawn'); } };\n"
            "const plugin = (await import(pathToFileURL(process.argv[1]).href)).default;\n"
            "assert.equal(plugin.setup({}), undefined);\n"
            "console.log('ok');\n"
        )
        out = subprocess.run(["node", "--input-type=module", "-e", harness, plugin],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "ok")

    def test_v1_loader_replay_registers_one_hook_and_delivers_once(self):
        """Behavioral replay of the real 1.x loader, not a shape guess.

        `readV1Plugin`, `getServerPlugin`, `getLegacyPlugins` and
        `applyPlugin` below are transcribed from
        https://raw.githubusercontent.com/anomalyco/opencode/v1.18.18/packages/opencode/src/plugin/index.ts
        and `.../plugin/shared.ts` (the same reader is at v1.18.12).

        What that source says, and what this asserts:
        - `applyPlugin` reads `mod.default` through `readV1Plugin(..., "detect")`.
          In detect mode an object with none of `id`/`server`/`tui` is not a
          1.x plugin, but an object that *has* `id` and no callable `server`
          **throws** - which is exactly what the default export did before
          `server: AgentBell` was added. The first assertion pins that.
        - When it does return, `applyPlugin` pushes one hook and returns, so
          the legacy `getLegacyPlugins` walk over `Object.values(mod)` (which
          resolves functions or objects with a server function) is never
          reached. The assertions pin one hook; the fallback would register
          the same function twice on this namespace.
        - The 2.x rule is mirrored by `v2Accepts`: a plain-object `default`
          with a string `id` and a callable `setup`, extra keys ignored. That
          is the behaviour of the schema the 2.0.16 binary bundles, replayed
          against the same `effect` build (`{id, setup, server}` is accepted
          there too; DECISIONS.md §38).
        """
        tmp = tempfile.mkdtemp(prefix="agentbell-plugin-loader-")
        plugin = os.path.join(tmp, "agentbell.mjs")
        with open(plugin, "w", encoding="utf-8") as fh:
            fh.write(_render_opencode_plugin_for())
        harness = r"""
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

// --- transcribed from opencode v1.18.18 plugin/index.ts + shared.ts -------
const isRecord = (v) => !!v && typeof v === "object" && !Array.isArray(v);
const isServerPlugin = (v) => typeof v === "function";
function getServerPlugin(value) {
  if (isServerPlugin(value)) return value;
  if (!value || typeof value !== "object" || !("server" in value)) return;
  if (!isServerPlugin(value.server)) return;
  return value.server;
}
function getLegacyPlugins(mod) {
  const seen = new Set();
  const result = [];
  for (const entry of Object.values(mod)) {
    if (seen.has(entry)) continue;
    seen.add(entry);
    const plugin = getServerPlugin(entry);
    if (!plugin) throw new TypeError("Plugin export is not a function");
    result.push(plugin);
  }
  return result;
}
function readV1Plugin(mod, spec, kind, mode = "strict") {
  const value = mod.default;
  if (!isRecord(value)) {
    if (mode === "detect") return;
    throw new TypeError(`Plugin ${spec} must default export an object with ${kind}()`);
  }
  if (mode === "detect" && !("id" in value) && !("server" in value) && !("tui" in value)) return;
  const server = "server" in value ? value.server : undefined;
  const tui = "tui" in value ? value.tui : undefined;
  if (server !== undefined && typeof server !== "function") {
    throw new TypeError(`Plugin ${spec} has invalid server export`);
  }
  if (tui !== undefined && typeof tui !== "function") {
    throw new TypeError(`Plugin ${spec} has invalid tui export`);
  }
  if (server !== undefined && tui !== undefined) {
    throw new TypeError(`Plugin ${spec} must default export either server() or tui(), not both`);
  }
  if (kind === "server" && server === undefined) {
    throw new TypeError(`Plugin ${spec} must default export an object with server()`);
  }
  return value;
}
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
async function applyPlugin(mod, input) {
  const hooks = [];
  const plugin = readV1Plugin(mod, "file:///plugin", "server", "detect");
  if (plugin) {
    hooks.push(await plugin.server(input, undefined));
    return hooks;
  }
  for (const server of getLegacyPlugins(mod)) hooks.push(await server(input, undefined));
  return hooks;
}
// --- the 2.x rule, mirroring the schema the 2.0.16 binary bundles ---------
function v2Accepts(mod) {
  const v = mod.default;
  if (!isRecord(v)) return false;
  if (typeof v.id !== "string") return false;
  return typeof v.setup === "function" || typeof v.effect === "function";
}

// the shape that shipped and was rejected: an `id` with no `server`
let preFix = "";
try { readV1Plugin({ default: { id: "agentbell", setup() {} } }, "spec", "server", "detect"); }
catch (e) { preFix = String(e.message); }
assert.match(preFix, /must default export an object with server\(\)/,
  "the pre-fix default must still be rejected by the 1.x reader");

const ns = await import(pathToFileURL(process.argv[1]).href);
assert.equal(v2Accepts(ns), true, "2.x needs a plain-object default with id + setup");

// The legacy walk is what a default without id/server/tui would fall into. On
// this namespace it does not throw - both `AgentBell` and `default` resolve to
// the same callable - but it counts them as two entries, because `seen` tracks
// the raw export values, not the resolved plugins. So the detect path
// returning early is what keeps this at exactly one hook, and this number is
// the pin for that.
const legacy = getLegacyPlugins(ns);
assert.equal(legacy.length, 2);
assert.equal(legacy[0], legacy[1]);

const hooks = await applyPlugin(ns, { $ });
assert.equal(hooks.length, 1, "the 1.x loader must register exactly one hook");
assert.equal(typeof hooks[0].event, "function");
await hooks[0].event({ event: { type: "session.execution.succeeded",
                                data: { sessionID: "s1" } } });
console.log(JSON.stringify({ hookCount: hooks.length,
                             calls: calls.map((a) => a.slice(1)) }));
"""
        out = subprocess.run(["node", "--input-type=module", "-e", harness, plugin],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(result["hookCount"], 1)
        self.assertEqual(result["calls"],
                         [["hook", "run_completed", "--agent", "opencode"]])


if __name__ == "__main__":
    unittest.main()
