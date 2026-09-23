"""Regression tests for the ask/queue findings of the 2026-09-22 audit.

A7   postponements ("später", "wait, tests are red") deny instead of exit 0
M13  a typed reply sent between two parallel asks reaches exactly one of them
     (audit 4, APR2-1/APR2-2: now none of them, with a notice; only one
     candidate question takes typed text)
L    draining the offline queue respects quiet hours
L    a channel that fails for good on a queued/deferred item leaves a trace
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import unittest
import unittest.mock
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: its classes do not re-run)
import agentbell as an  # noqa: E402

# 00:00-23:59 covers every minute of the day, so these tests do not depend
# on the time they run at (see test_quiet_hours_include_the_last_minute...).
ALL_DAY = [{"start": "00:00", "end": "23:59"}]


def _clean_state():
    for name in ("queue", "deferred", "ntfy-pending", "tg-pending", "tg-answers"):
        shutil.rmtree(os.path.join(an.state_dir(), name), ignore_errors=True)
    with contextlib.suppress(OSError):
        os.remove(an._consumed_path("ntfy"))


def _history_mark():
    return len(an.read_history(limit=0))


def _history_since(mark):
    return an.read_history(limit=0)[mark:]


def _pending_path(name, approval_id):
    return os.path.join(an.state_dir(), name, f"{approval_id}.json")


def _set_pending(name, approval_id, **fields):
    path = _pending_path(name, approval_id)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class TimedNtfy:
    """ntfy mock whose messages carry a server `time`, like the real one.

    The time is a counter, so the order of question and reply is exact.
    Streams deliver nothing (a buffering proxy): answers arrive by poll only.
    """

    def __init__(self):
        self.posts = {}
        self.clock = 1000
        self.lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                topic = self.path.strip("/").split("/")[0]
                length = int(self.headers.get("Content-Length") or 0)
                record = mock.add(topic, self.rfile.read(length).decode("utf-8"))
                body = json.dumps({"id": record["id"], "time": record["time"],
                                   "event": "message"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                topic = path.strip("/").split("/")[0]
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                if "poll=1" not in query:
                    time.sleep(0.2)
                    return
                for record in list(mock.posts.get(topic, [])):
                    line = json.dumps({"event": "message", "id": record["id"],
                                       "time": record["time"], "message": record["body"]})
                    self.wfile.write((line + "\n").encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def add(self, topic, body):
        with self.lock:
            self.clock += 1
            record = {"id": f"msg{self.clock}", "time": self.clock, "body": body}
            self.posts.setdefault(topic, []).append(record)
            return record

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _wait_for(condition, timeout=10.0, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(message)


# ---------------------------------------------------------------------------
# A7
# ---------------------------------------------------------------------------

class TestPostponementsDeny(unittest.TestCase):
    def test_postponements_and_holds_deny(self):
        cases = (
            "später", "Später", "SPÄTER", "später bitte", "spaeter",
            "wait", "Wait!", "wait, tests are red", "wait for CI, then ship",
            "warte", "warte, noch nicht", "wart mal", "hang on", "hold off",
            "hold it", "one moment", "one sec", "just a moment", "just a sec",
            "not so fast", "jetzt nicht", "pause", "⏸️", "⏸", "⏳", "⌛ CI läuft",
            # already denials before; still denials
            "later", "not yet", "noch nicht", "hold on", "moment", "not now",
            "nicht jetzt", "halt", "✋",
        )
        for text in cases:
            self.assertEqual(an._parse_answer(text)[0], "denied", text)
        self.assertEqual(an._parse_answer("wait, tests are red"),
                         ("denied", "tests are red"))
        self.assertEqual(an._parse_answer("später, erst nach dem Review"),
                         ("denied", "erst nach dem Review"))

    def test_real_yeses_and_lookalike_words_are_not_denied(self):
        self.assertEqual(an._parse_answer("yes")[0], "approved")
        self.assertEqual(an._parse_answer("ja")[0], "approved")
        self.assertEqual(an._parse_answer("Approve")[0], "approved")
        # not bare yeses: free text, exit 0 - but never a denial
        for text in ("yes, go ahead", "ja bitte", "waiting room is fine",
                     "pausenlos weiter", "hang onto the old build", "holdout set",
                     "latest is fine", "spätestens morgen"):
            self.assertEqual(an._parse_answer(text)[0], "answer", text)

    def test_custom_labels_win_over_the_postponement_list(self):
        # the hint tells the user to reply with the yes label
        self.assertEqual(an._parse_answer("Wait", yes_label="Wait")[0], "approved")
        self.assertEqual(an._parse_answer("Later", yes_label="Later")[0], "approved")
        self.assertEqual(an._parse_answer("Pause!", yes_label="Pause")[0], "approved")
        # a German no-label that is itself a postponement
        self.assertEqual(
            an._parse_answer("Jetzt", yes_label="Jetzt", no_label="Später")[0], "approved")
        self.assertEqual(
            an._parse_answer("Später", yes_label="Jetzt", no_label="Später")[0], "denied")
        # more than the label is not the label
        self.assertEqual(an._parse_answer("Wait for CI", yes_label="Wait")[0], "denied")

    def test_ask_exits_nonzero_for_a_postponement(self):
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        _clean_state()
        cfg = base.make_config(ntfy.url, topic="a7later")
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(
            result=an.run_ask(cfg, "Deploy?", timeout_seconds=20, print_status=False)),
            daemon=True)
        thread.start()
        _wait_for(lambda: ntfy.posts.get("a7later"), message="question not published")
        urllib.request.urlopen(urllib.request.Request(
            f"{ntfy.url}/a7later-responses", method="POST",
            data="später".encode("utf-8"))).read()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        result = holder["result"]
        self.assertTrue(result["denied"])
        self.assertFalse(result["approved"])
        args = base._Args(message="Deploy?", timeout=5, yes_label=None, no_label=None,
                          no_buttons=False, json=True, channel=None)
        with unittest.mock.patch.object(an, "run_ask", return_value=result), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                an.cmd_ask(args)
        self.assertEqual(raised.exception.code, 1)


# ---------------------------------------------------------------------------
# M13 - Telegram
# ---------------------------------------------------------------------------

class TestTelegramReplyRouting(base._TelegramFixture):
    A, B, C = "a" * 16, "b" * 16, "c" * 16

    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)
        self.cfg = self._tg_cfg()

    def _ask(self, approval_id, question_message_id=None):
        an.write_tg_pending(approval_id, f"{approval_id[:1]}?", 60)
        if question_message_id is not None:
            an.remember_tg_question_message(approval_id, question_message_id)

    def _reply(self, message_id, text, reply_to=None):
        message = {"message_id": message_id, "chat": {"id": 42}, "text": text}
        if reply_to is not None:
            message["reply_to_message"] = reply_to
        an.handle_bot_update(self.cfg, {"update_id": message_id, "message": message})

    def test_reply_sent_between_two_questions_is_not_used(self):
        """Two questions open: typed text names neither (audit 4)."""
        self._ask(self.A, 100)
        self._ask(self.B, 102)
        mark = _history_mark()
        self._reply(101, "staging")          # written before B's question existed
        self.assertIsNone(an.read_tg_answer(self.A))
        self.assertIsNone(an.read_tg_answer(self.B))
        stale = [r for r in _history_since(mark) if r.get("event") == "stale_answer"]
        self.assertEqual([r["reason"] for r in stale],
                         ["2 approval questions are open or just ended"])

    def test_reply_after_both_questions_is_not_used(self):
        self._ask(self.A, 100)
        self._ask(self.B, 102)
        self._reply(103, "prod")
        self.assertIsNone(an.read_tg_answer(self.B))
        self.assertIsNone(an.read_tg_answer(self.A))

    def test_question_whose_send_failed_makes_replies_stale(self):
        """A failed send may still have reached the chat (audit 3, APR-2).

        Its marker has no message id for the whole ask. The reply may be
        its answer: not used, never handed to the older ask.
        """
        self._ask(self.A, 100)
        self._ask(self.C)                    # newer, send "failed"
        mark = _history_mark()
        self._reply(103, "prod")
        self.assertIsNone(an.read_tg_answer(self.A))
        self.assertIsNone(an.read_tg_answer(self.C))
        stale = [r for r in _history_since(mark) if r.get("event") == "stale_answer"]
        self.assertEqual([r["reason"] for r in stale],
                         ["2 approval questions are open or just ended"])

    def test_telegram_reply_to_a_question_answers_that_question(self):
        self._ask(self.A, 100)
        self._ask(self.B, 104)
        self._reply(105, "use eu-west", reply_to={"message_id": 100, "text": "Q"})
        self.assertEqual(an.read_tg_answer(self.A), "use eu-west")
        self.assertIsNone(an.read_tg_answer(self.B))
        an.remove_tg_answer(self.A)
        # the quoted "ID: <id>" line identifies it too (id not recorded yet)
        self._ask(self.C)
        quoted = f"\U0001f534 Approval requested\nDeploy?\nID: {self.B}\n\nID: {self.C}"
        self._reply(106, "go", reply_to={"message_id": 999, "text": quoted})
        self.assertEqual(an.read_tg_answer(self.C), "go")
        self.assertIsNone(an.read_tg_answer(self.A))
        self.assertIsNone(an.read_tg_answer(self.B))

    def test_reply_to_a_closed_question_is_stale_not_rerouted(self):
        self._ask(self.A, 100)
        closed = "d" * 16
        mark = _history_mark()
        self._reply(105, "yes", reply_to={"message_id": 50,
                                          "text": f"Approval requested\nOld?\n\nID: {closed}"})
        self.assertIsNone(an.read_tg_answer(self.A))
        stale = [r for r in _history_since(mark) if r.get("event") == "stale_answer"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["approval_id"], closed)
        self.assertIn("no longer open", stale[0]["reason"])

    def test_reply_older_than_every_open_question_is_stale(self):
        self._ask(self.A, 100)
        mark = _history_mark()
        self._reply(99, "yes")
        self.assertIsNone(an.read_tg_answer(self.A))
        stale = [r for r in _history_since(mark) if r.get("event") == "stale_answer"]
        self.assertEqual([r["reason"] for r in stale], ["reply predates the question"])


# ---------------------------------------------------------------------------
# M13 - ntfy
# ---------------------------------------------------------------------------

class TestNtfyReplyRouting(unittest.TestCase):
    A, B = "a" * 16, "b" * 16

    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)
        self.cfg = base.make_config("http://127.0.0.1:9", topic="m13unit")

    def _waiter(self, approval_id, question_time="unset"):
        an.write_ntfy_pending(approval_id, "Q?", 60)
        if question_time != "unset":
            an.remember_ntfy_question(approval_id, question_time)
        return an.ApprovalWaiter(self.cfg, "m13unit-responses", 60, approval_id=approval_id)

    def _got(self, waiter):
        items = []
        while not waiter.messages.empty():
            items.append(waiter.messages.get_nowait())
        return items

    def test_reply_with_two_open_questions_is_used_by_neither_in_any_order(self):
        """Audit 4: between the two questions or after both, typed text
        names neither; one record and one notice, whoever sees it first."""
        for reply_time in (1001, 1003):
            for first in ("A", "B"):
                with self.subTest(reply_time=reply_time, first_to_see_the_reply=first):
                    _clean_state()
                    a = self._waiter(self.A, 1000)
                    b = self._waiter(self.B, 1002)
                    order = (b, a) if first == "B" else (a, b)
                    mark = _history_mark()
                    for waiter in order:
                        waiter._offer("r1", "staging", reply_time)
                    self.assertEqual(self._got(a) + self._got(b), [])
                    stale = [r for r in _history_since(mark)
                             if r.get("event") == "stale_answer"]
                    self.assertEqual([r["reason"] for r in stale],
                                     ["2 approval questions are open or just ended"])

    def test_reply_older_than_every_question_is_stale(self):
        a = self._waiter(self.A, 1000)
        mark = _history_mark()
        a._offer("r3", "yes", 999)
        self.assertEqual(self._got(a), [])
        self.assertTrue(any(r.get("event") == "stale_answer" and r.get("text") == "yes"
                            for r in _history_since(mark)))

    def test_reply_while_another_question_is_being_published_is_not_used(self):
        """Two candidates already: nothing to wait for (audit 4)."""
        a = self._waiter(self.A, 1000)
        b = self._waiter(self.B)                  # publishing: no time yet
        mark = _history_mark()
        for waiter in (a, b):
            waiter._offer("r4", "staging", 1001)
        self.assertEqual(self._got(a) + self._got(b), [])
        self.assertIn("r4", a.seen)               # decided, not offered again
        self.assertEqual(len([r for r in _history_since(mark)
                              if r.get("event") == "stale_answer"]), 1)

    def test_instant_reply_to_a_question_being_published_is_kept(self):
        """A responder can answer before `ask` has stored its question's time."""
        b = self._waiter(self.B)
        b._offer("r5", "staging", 1003)
        self.assertEqual(self._got(b), [])
        an.remember_ntfy_question(self.B, 1003)
        b._offer("r5", "staging", 1003)
        self.assertEqual(self._got(b), ["staging"])

    def test_a_killed_ask_stuck_publishing_blocks_typed_replies(self):
        """Its question may be on the phone: for its grace, typed text is not
        used (audit 4); the buttons still work."""
        a = self._waiter(self.A, 1000)
        an.write_ntfy_pending(self.B, "Q?", 60)
        an._let_go(_pending_path("ntfy-pending", self.B))   # killed: its lock is gone
        a._offer("r6", "staging", 1001)
        self.assertEqual(self._got(a), [])
        a._offer("r6b", f"APPROVED {self.A}", 1002)
        self.assertEqual(self._got(a), [f"APPROVED {self.A}"])

    def test_a_finished_waiter_does_not_claim_a_reply(self):
        a = self._waiter(self.A, 1000)
        a.stop_event.set()
        a._offer("r7", "staging", 1001)
        self.assertEqual(self._got(a), [])
        self.assertNotIn("r7", an._read_consumed("ntfy"))

    def test_server_without_times_uses_no_typed_reply(self):
        """Without times a reply cannot be placed after its question (audit 4)."""
        b = self._waiter(self.B, None)
        mark = _history_mark()
        b._offer("r8", "staging", None)
        self.assertEqual(self._got(b), [])
        self.assertEqual([r["reason"] for r in _history_since(mark)
                          if r.get("event") == "stale_answer"],
                         ["that question's place in the chat is unknown"])

    def test_publish_reports_the_server_time(self):
        ntfy = TimedNtfy()
        self.addCleanup(ntfy.stop)
        cfg = base.make_config(ntfy.url, topic="m13time")
        sent = an.NtfyChannel(cfg).publish("m13time", "hello")
        self.assertEqual(sent["time"], ntfy.posts["m13time"][0]["time"])
        self.assertTrue(sent["ok"])
        # a server that does not answer with JSON still publishes fine
        with unittest.mock.patch.object(an, "http_request", return_value=(200, b"<html>")):
            self.assertEqual(an.NtfyChannel(cfg).publish("m13time", "hi"),
                             {"channel": "ntfy", "ok": True})

    def test_a_question_position_that_cannot_be_stored_is_reported(self):
        an.write_ntfy_pending(self.A, "Q?", 60)
        with open(_pending_path("ntfy-pending", self.A), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            an.remember_ntfy_question(self.A, 1000)
        self.assertIn("cannot record the question", err.getvalue())
        # a marker that is gone: nothing to record, nothing to say
        os.remove(_pending_path("ntfy-pending", self.A))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            an.remember_ntfy_question(self.A, 1000)
            an.remember_tg_question_message(self.A, 7)
        self.assertEqual(err.getvalue(), "")
        self.assertFalse(os.path.exists(_pending_path("ntfy-pending", self.A)))


class TestNtfyReplyBetweenParallelAsks(unittest.TestCase):
    """End to end: the reply that used to be dropped by both asks silently.

    Audit 4: with two questions open it is still used by neither, but the
    phone is told once; the buttons answer both.
    """

    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)
        self.ntfy = TimedNtfy()
        self.addCleanup(self.ntfy.stop)

    def test_reply_sent_before_the_second_ask_reaches_the_first(self):
        cfg = base.make_config(self.ntfy.url, topic="m13e2e")
        gate = threading.Event()
        self.addCleanup(gate.set)
        created = []
        original = an.ApprovalWaiter

        class GatedWaiter(original):
            """The first ask's poller looks only when the test says so."""

            def __init__(self, *args, **kwargs):
                kwargs["poll_interval"] = 0.05
                super().__init__(*args, **kwargs)
                created.append(self)
                self.gated = len(created) == 1

            def _poller(self):
                if self.gated:
                    gate.wait()
                super()._poller()

        results = {}

        def ask(key, question):
            results[key] = an.run_ask(cfg, question, timeout_seconds=15, print_status=False)

        with unittest.mock.patch.object(an, "ApprovalWaiter", GatedWaiter):
            first = threading.Thread(target=ask, args=("a", "First?"), daemon=True)
            first.start()
            _wait_for(lambda: len(self.ntfy.posts.get("m13e2e", [])) == 1)
            self.ntfy.add("m13e2e-responses", "staging")      # meant for the first
            second = threading.Thread(target=ask, args=("b", "Second?"), daemon=True)
            second.start()
            _wait_for(lambda: len(self.ntfy.posts.get("m13e2e", [])) == 2)
            mark = _history_mark()
            gate.set()                                      # the first ask looks now
            _wait_for(lambda: [r for r in _history_since(mark)
                               if r.get("event") == "stale_answer"],
                      message="the reply was neither used nor refused")
            self.assertTrue(first.is_alive())
            self.assertTrue(second.is_alive())
            notices = [r["body"] for r in self.ntfy.posts["m13e2e"]
                       if "was not used" in r["body"]]
            self.assertEqual(len(notices), 1, notices)
            first_id, second_id = (re.search(r"ID: ([0-9a-f]+)", r["body"]).group(1)
                                   for r in self.ntfy.posts["m13e2e"][:2])
            self.ntfy.add("m13e2e-responses", f"APPROVED {first_id}")
            self.ntfy.add("m13e2e-responses", f"APPROVED {second_id}")
            first.join(timeout=15)
            second.join(timeout=15)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(results["a"]["approved"], results["a"])
        self.assertTrue(results["b"]["approved"], results["b"])


class TestNtfyFailureInTwoChannelAsk(base._TelegramFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.old_backoff = an.RETRY_BACKOFF_SECONDS
        an.RETRY_BACKOFF_SECONDS = (0.01, 0.01)

    @classmethod
    def tearDownClass(cls):
        an.RETRY_BACKOFF_SECONDS = cls.old_backoff
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)

    def test_failed_ntfy_question_keeps_a_marker_of_unknown_place(self):
        """A failed publish may still have been stored (audit 3, APR-1): its
        marker stays, without a time, so no reply goes to another ask. A
        dropped connection proves nothing; a refusal would (audit 5, AS3-2)."""
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = self._tg_cfg(ntfy_url=ntfy.url, channels=("ntfy", "telegram"))
        before = len(self.tg.requests)
        holder = {}
        attempts = []

        def dropped(*_args, **_kwargs):
            attempts.append(1)
            raise an.TransientError("connection to ntfy failed (ConnectionResetError)")
        with contextlib.redirect_stderr(io.StringIO()), \
                unittest.mock.patch.object(an.NtfyChannel, "publish", dropped):
            thread = threading.Thread(target=lambda: holder.update(
                result=an.run_ask(cfg, "Deploy?", timeout_seconds=20, print_status=False)),
                daemon=True)
            thread.start()
            _wait_for(lambda: any(r["method"] == "sendMessage"
                                  for r in self.tg.requests[before:]))
            sent = next(r for r in self.tg.requests[before:] if r["method"] == "sendMessage")
            approval_id = re.search(r"ID: ([0-9a-f]+)", sent["body"]["text"]).group(1)
            # every publish attempt has failed ...
            _wait_for(lambda: len(attempts) >= an.RETRY_ATTEMPTS)
            # ... and the marker written before the first one says so
            marker = _pending_path("ntfy-pending", approval_id)

            def unplaced():
                try:
                    with open(marker, encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):       # caught mid-write
                    return False
                return "question_time" in data and data["question_time"] is None
            _wait_for(unplaced, message="the failed ntfy question is not marked unplaced")
            self.assertTrue(thread.is_alive())               # Telegram still carries it
            an.write_tg_answer(approval_id, "approved")
            thread.join(timeout=10)
        self.assertTrue(holder["result"]["approved"])
        self.assertEqual(holder["result"]["channel"], "telegram")
        # answered on Telegram: on ntfy the question may still be on the
        # phone, so its tombstone keeps counting for its grace (AS3-4)
        with open(marker, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual((data["closed"], data["answered"]), (True, False))


# ---------------------------------------------------------------------------
# L-drain-quiet
# ---------------------------------------------------------------------------

class TestDrainRespectsQuietHours(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_backoff = an.RETRY_BACKOFF_SECONDS
        an.RETRY_BACKOFF_SECONDS = (0.01, 0.01)

    @classmethod
    def tearDownClass(cls):
        an.RETRY_BACKOFF_SECONDS = cls.old_backoff

    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)
        self.ntfy = base.MockNtfy()
        self.addCleanup(self.ntfy.stop)

    def _cfg(self, topic, mode="defer"):
        cfg = base.make_config(self.ntfy.url, topic=topic)
        cfg.data["quiet_hours"] = ALL_DAY
        cfg.data["quiet_hours_mode"] = mode
        return cfg

    def test_low_priority_item_is_deferred_not_pushed(self):
        for mode in ("defer", "suppress"):
            with self.subTest(mode=mode):
                _clean_state()
                topic = f"dq{mode}"
                cfg = self._cfg(topic, mode)
                an.enqueue_item(cfg, {"message": "built while offline", "title": "CI",
                                      "channels": ["ntfy"], "priority": "low",
                                      "tags": ["ci"], "event": "run_completed"})
                mark = _history_mark()
                stats = an.drain_queue(cfg, limit=None)
                self.assertEqual(stats["deferred"], 1)
                self.assertEqual(stats["delivered"], 0)
                self.assertNotIn(topic, self.ntfy.posts)
                self.assertEqual(an._read_item_files(an.queue_dir()), [])
                deferred = [item for _, item in an._read_item_files(an.deferred_dir())]
                self.assertEqual(len(deferred), 1)
                held = deferred[0]
                self.assertEqual((held["message"], held["title"], held["priority"],
                                  held["channels"], held["tags"], held["event"]),
                                 ("built while offline", "CI", "low", ["ntfy"], ["ci"],
                                  "run_completed"))
                self.assertGreater(held["deliver_after"], time.time())
                events = [r for r in _history_since(mark) if r.get("event") == "queue_deferred"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["deferred_id"], held["id"])
                # and it stays held: the flush that follows every drain keeps it
                self.assertEqual(an.flush_deferred(cfg)["kept"], 1)
                self.assertNotIn(topic, self.ntfy.posts)

    def test_urgent_and_forced_items_still_go_out(self):
        cfg = self._cfg("dqloud")
        an.enqueue_item(cfg, {"message": "normal is above the quiet threshold",
                              "channels": ["ntfy"], "priority": "normal"})
        an.enqueue_item(cfg, {"message": "forced", "channels": ["ntfy"],
                              "priority": "low", "force": True})
        stats = an.drain_queue(cfg, limit=None)
        self.assertEqual((stats["delivered"], stats["deferred"]), (2, 0))
        bodies = [p["body"] for p in self.ntfy.posts["dqloud"]]
        self.assertEqual(sorted(bodies), ["forced", "normal is above the quiet threshold"])

    def test_forced_notification_keeps_force_when_it_is_queued(self):
        cfg = self._cfg("dqforce")
        cfg.data["ntfy"]["server"] = f"http://127.0.0.1:{base._free_port()}"
        result = an.send_notification(cfg, "forced while offline", priority="low", force=True)
        self.assertEqual(result.get("queued"), ["ntfy"])
        items = [item for _, item in an._read_item_files(an.queue_dir())]
        self.assertTrue(items[0].get("force"))
        cfg.data["ntfy"]["server"] = self.ntfy.url           # network is back
        self.assertEqual(an.drain_queue(cfg, limit=None)["delivered"], 1)
        self.assertEqual(self.ntfy.posts["dqforce"][-1]["body"], "forced while offline")

    def test_expired_item_still_expires_during_quiet_hours(self):
        cfg = self._cfg("dqold")
        an.enqueue_item(cfg, {"message": "a day old", "channels": ["ntfy"],
                              "priority": "low", "created": time.time()
                              - an.QUEUE_MAX_AGE_SECONDS - 60})
        stats = an.drain_queue(cfg, limit=None)
        self.assertEqual((stats["dropped"], stats["deferred"]), (1, 0))
        self.assertEqual(an._read_item_files(an.deferred_dir()), [])

    def test_queue_flush_reports_the_deferred_items(self):
        cfg = self._cfg("dqcli")
        an.enqueue_item(cfg, {"message": "held", "channels": ["ntfy"], "priority": "low"})
        out = io.StringIO()
        with unittest.mock.patch.object(an, "Config", return_value=cfg), \
                contextlib.redirect_stdout(out):
            an.cmd_queue(base._Args(sub="flush"))
        self.assertIn("1 deferred (quiet hours)", out.getvalue())
        self.assertIn("1 still held", out.getvalue())


# ---------------------------------------------------------------------------
# L-perm-channel
# ---------------------------------------------------------------------------

class TestPermanentChannelFailureIsRecorded(base._TelegramFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.old_backoff = an.RETRY_BACKOFF_SECONDS
        an.RETRY_BACKOFF_SECONDS = (0.01, 0.01)
        cls.broken_tg = base.MockTelegram(fail_send=True)   # Telegram says no, for good
        cls.ntfy = base.MockNtfy()

    @classmethod
    def tearDownClass(cls):
        an.RETRY_BACKOFF_SECONDS = cls.old_backoff
        cls.broken_tg.stop()
        cls.ntfy.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)
        old_base = an.TG_API_BASE
        an.TG_API_BASE = self.broken_tg.url
        self.addCleanup(setattr, an, "TG_API_BASE", old_base)

    def _cfg(self, ntfy_up):
        url = self.ntfy.url if ntfy_up else f"http://127.0.0.1:{base._free_port()}"
        return self._tg_cfg(ntfy_url=url, channels=("ntfy", "telegram"))

    def _dropped(self, records, event):
        return [r for r in records if r.get("event") == event]

    def test_queued_item_kept_for_ntfy_records_the_dropped_telegram(self):
        cfg = self._cfg(ntfy_up=False)
        an.enqueue_item(cfg, {"message": "perm queued", "channels": ["ntfy", "telegram"],
                              "priority": "normal", "event": "notify"})
        mark = _history_mark()
        stats = an.drain_queue(cfg, limit=None)
        self.assertEqual(stats["kept"], 1)
        items = [item for _, item in an._read_item_files(an.queue_dir())]
        self.assertEqual(items[0]["channels"], ["ntfy"])
        dropped = self._dropped(_history_since(mark), "queue_dropped")
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["channels"], ["telegram"])
        self.assertIn("telegram", dropped[0]["error"])
        self.assertEqual(dropped[0]["message"], "perm queued")

    def test_queued_item_delivered_on_ntfy_records_the_dropped_telegram(self):
        cfg = self._cfg(ntfy_up=True)
        an.enqueue_item(cfg, {"message": "perm delivered", "channels": ["ntfy", "telegram"],
                              "priority": "normal", "event": "notify"})
        mark = _history_mark()
        self.assertEqual(an.drain_queue(cfg, limit=None)["delivered"], 1)
        records = _history_since(mark)
        self.assertEqual(len(self._dropped(records, "queued_delivered")), 1)
        dropped = self._dropped(records, "queue_dropped")
        self.assertEqual([r["channels"] for r in dropped], [["telegram"]])

    def _due_deferred(self, cfg, message):
        an.defer_item(cfg, message, priority="low", channels=["ntfy", "telegram"])
        for name, item in an._read_item_files(an.deferred_dir()):
            item["deliver_after"] = time.time() - 1
            with open(os.path.join(an.deferred_dir(), name), "w", encoding="utf-8") as fh:
                json.dump(item, fh)

    def test_deferred_item_delivered_on_ntfy_records_the_dropped_telegram(self):
        cfg = self._cfg(ntfy_up=True)
        self._due_deferred(cfg, "perm deferred")
        mark = _history_mark()
        self.assertEqual(an.flush_deferred(cfg)["delivered"], 1)
        records = _history_since(mark)
        self.assertEqual([r["channels"] for r in self._dropped(records, "deferred_delivered")],
                         [["ntfy"]])
        dropped = self._dropped(records, "deferred_dropped")
        self.assertEqual([r["channels"] for r in dropped], [["telegram"]])
        self.assertEqual(dropped[0]["message"], "perm deferred")

    def test_deferred_item_queued_for_ntfy_records_the_dropped_telegram(self):
        cfg = self._cfg(ntfy_up=False)
        self._due_deferred(cfg, "perm deferred, ntfy down")
        mark = _history_mark()
        an.flush_deferred(cfg)
        records = _history_since(mark)
        self.assertEqual(len(self._dropped(records, "deferred_queued")), 1)
        self.assertEqual([r["channels"] for r in self._dropped(records, "deferred_dropped")],
                         [["telegram"]])
        items = [item for _, item in an._read_item_files(an.queue_dir())]
        self.assertEqual(items[0]["channels"], ["ntfy"])

    def test_item_dropped_entirely_is_still_one_line(self):
        cfg = self._tg_cfg(ntfy_url=self.ntfy.url, channels=("telegram",))
        an.enqueue_item(cfg, {"message": "telegram only", "channels": ["telegram"],
                              "priority": "normal", "event": "notify"})
        mark = _history_mark()
        self.assertEqual(an.drain_queue(cfg, limit=None)["dropped"], 1)
        self.assertEqual(len(self._dropped(_history_since(mark), "queue_dropped")), 1)


if __name__ == "__main__":
    unittest.main()
