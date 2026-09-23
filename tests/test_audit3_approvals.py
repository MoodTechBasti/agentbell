"""Regression tests for the approvals findings of the third audit round.

APR-1  ntfy: a question whose publish raised or was retried may already be
       on the phone; a reply is not used, never handed to another ask
APR-2  the same on Telegram (a send that errored, or needed a retry)
       (both now under the one-candidate rule of audit 4, APR2-1/APR2-2)
APR-3  a yes followed by a postponement or refusal denies
APR-4  the quiet-hours drain keeps the queue time, so the 24 h expiry holds
APR-5  a typed "APPROVED <id>" on Telegram goes to the ask it names
APR-6  (BOT-3, IW-11) "approve 2" / "deny bad idea" on ntfy is an answer,
       not another ask's button
APR-7  a reply in the same second as the newest question is ambiguous
APR-8  an ntfy marker that never got a time (older agentbell, killed ask)
       does not hand its reply to another ask
S2     (D3, IW-6) one priority -> number helper for every site
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


def _stale(mark):
    return [r for r in _history_since(mark) if r.get("event") == "stale_answer"]


def _set_pending(name, approval_id, **fields):
    path = os.path.join(an.state_dir(), name, f"{approval_id}.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _wait_for(condition, timeout=10.0, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(message)


class ResettingNtfy:
    """ntfy mock with server times. A POST whose body contains `reset_on`
    is stored and then answered with a dropped connection, once: the
    question is on the phone while the client sees an error."""

    def __init__(self, reset_on=None):
        self.posts = {}
        self.clock = 1000
        self.reset_on = reset_on
        self.lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                topic = self.path.strip("/").split("/")[0]
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8")
                record = mock.add(topic, body)
                with mock.lock:
                    reset = mock.reset_on is not None and mock.reset_on in body
                    if reset:
                        mock.reset_on = None
                if reset:
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                out = json.dumps({"id": record["id"], "time": record["time"],
                                  "event": "message"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

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

    def add(self, topic, body, at=None):
        with self.lock:
            self.clock += 1
            record = {"id": f"msg{self.clock}", "time": self.clock if at is None else at,
                      "body": body}
            self.posts.setdefault(topic, []).append(record)
            return record

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# ntfy routing: APR-1, APR-6, APR-7, APR-8 (under the one-candidate rule)
# ---------------------------------------------------------------------------

def _marker(name, approval_id):
    """One read of a marker; {} when it is missing or caught mid-write."""
    try:
        with open(os.path.join(an.state_dir(), name, f"{approval_id}.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


class TestNtfyUnplacedQuestions(unittest.TestCase):
    A, B = "a" * 16, "b" * 16
    TWO_OPEN = "2 approval questions are open or just ended"

    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)
        self.cfg = base.make_config("http://127.0.0.1:9", topic="apr1unit")

    def _waiter(self, approval_id, question_time="unset"):
        an.write_ntfy_pending(approval_id, "Q?", 60)
        if question_time != "unset":
            an.remember_ntfy_question(approval_id, question_time)
        return an.ApprovalWaiter(self.cfg, "apr1unit-responses", 60, approval_id=approval_id)

    def _got(self, waiter):
        items = []
        while not waiter.messages.empty():
            items.append(waiter.messages.get_nowait())
        return items

    def _offer_all(self, waiters, message_id, text, reply_time):
        for waiter in waiters:
            waiter._offer(message_id, text, reply_time)

    def test_reply_before_a_retried_question_is_not_used(self):
        """APR-1: the recorded time is the copy that got through; a reply
        typed under an earlier copy is older than that and is not used."""
        b = self._waiter(self.B, 1004)                   # first copy stored at 1002
        mark = _history_mark()
        b._offer("r1", "yes", 1003)
        self.assertEqual(self._got(b), [])
        self.assertIn("r1", b.seen)                       # decided, not held
        self.assertEqual([(r["approval_id"], r["text"]) for r in _stale(mark)],
                         [(self.B, "yes")])
        b._offer("r2", "prod", 1005)
        self.assertEqual(self._got(b), ["prod"])

    def test_failed_publish_blocks_typed_replies_for_other_asks(self):
        a = self._waiter(self.A, 1001)
        self._waiter(self.B, None)                        # every attempt "failed"
        mark = _history_mark()
        a._offer("r3", "yes", 1005)
        self.assertEqual(self._got(a), [])
        self.assertEqual([r["reason"] for r in _stale(mark)], [self.TWO_OPEN])

    def test_failed_publish_of_an_older_ask_blocks_a_newer_one(self):
        """APR2-2: an older ask's copies can land after a newer question."""
        self._waiter(self.B, None)
        _set_pending("ntfy-pending", self.B, created=time.time() - 30)
        a = self._waiter(self.A, 1001)
        mark = _history_mark()
        a._offer("r4", "staging", 1002)
        self.assertEqual(self._got(a), [])
        self.assertEqual([r["reason"] for r in _stale(mark)], [self.TWO_OPEN])

    def test_same_second_as_the_newest_question_is_ambiguous(self):
        """APR-7: two open questions: no typed reply is used, in any order."""
        a = self._waiter(self.A, 1001)
        b = self._waiter(self.B, 1010)
        mark = _history_mark()
        self._offer_all((a, b), "r6", "yes", 1010)
        self._offer_all((b, a), "r7", "yes", 1011)
        self.assertEqual(self._got(a) + self._got(b), [])
        # one record per reply, from the ask that claimed it first
        self.assertEqual([r["text"] for r in _stale(mark)], ["yes", "yes"])

    def test_same_second_with_a_single_ask_still_answers_it(self):
        b = self._waiter(self.B, 1010)
        b._offer("r7", "staging", 1010)
        self.assertEqual(self._got(b), ["staging"])

    def test_marker_that_never_got_a_time_blocks_other_asks(self):
        """APR-8: an older agentbell (or a killed ask) never records the time."""
        a = self._waiter(self.A, 1001)
        an.write_ntfy_pending(self.B, "Q?", 60)            # no question_time, ever
        mark = _history_mark()
        a._offer("r8", "yes", 1005)
        self.assertEqual(self._got(a), [])
        self.assertEqual([r["reason"] for r in _stale(mark)], [self.TWO_OPEN])

    def test_typed_verdicts_without_a_full_id_are_answers(self):
        """APR-6 / BOT-3 / IW-11: ntfy reads them like Telegram does."""
        for text, kind in (("approve 2", "answer"), ("approve add", "answer"),
                           ("deny 2", "denied"), ("deny bad idea", "denied"),
                           ("denied 2 of them", "denied"),
                           (f"approve {self.A.upper()}", "approved")):
            with self.subTest(text=text):
                _clean_state()
                a = self._waiter(self.A, 1001)
                a._offer("r9", text, 1002)
                self.assertEqual(self._got(a), [text])
                self.assertEqual(an._parse_answer(text, approval_id=self.A)[0], kind)

    def test_full_id_verdict_for_another_ask_is_stale(self):
        a = self._waiter(self.A, 1001)
        mark = _history_mark()
        a._offer("r10", f"APPROVED {'c' * 16}", 1002)
        a._offer("r11", f"denied {'C' * 16}", 1003)
        self.assertEqual(self._got(a), [])
        self.assertEqual(len(_stale(mark)), 2)


class TestNtfyRetriedPublishEndToEnd(unittest.TestCase):
    """APR-1: the first copy reached the phone, the client saw a reset."""

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
        self.ntfy = ResettingNtfy(reset_on="Second?")
        self.addCleanup(self.ntfy.stop)

    def _ids(self):
        return [re.search(r"ID: ([0-9a-f]+)", r["body"]).group(1)
                for r in self.ntfy.posts.get("apr1e2e", []) if "ID: " in r["body"]]

    def test_reply_to_the_first_copy_does_not_approve_the_older_ask(self):
        cfg = base.make_config(self.ntfy.url, topic="apr1e2e")
        results = {}

        def ask(key, question):
            results[key] = an.run_ask(cfg, question, timeout_seconds=20, print_status=False)

        first = threading.Thread(target=ask, args=("a", "First?"), daemon=True)
        first.start()
        _wait_for(lambda: len(self._ids()) == 1)
        a_id = self._ids()[0]
        # one read per check: the ask rewrites the marker meanwhile (WIN-1)
        _wait_for(lambda: _marker("ntfy-pending", a_id).get("question_time") is not None)
        second = threading.Thread(target=ask, args=("b", "Second?"), daemon=True)
        second.start()
        _wait_for(lambda: len(self._ids()) == 3)          # the reset copy and the retry
        b_id = self._ids()[1]
        first_copy, retry = self.ntfy.posts["apr1e2e"][1:3]
        _wait_for(lambda: _marker("ntfy-pending", b_id).get("question_time") == retry["time"])
        mark = _history_mark()
        # typed on the first copy: after it, before the recorded retry
        self.ntfy.add("apr1e2e-responses", "yes", at=first_copy["time"])
        _wait_for(lambda: _stale(mark), message="the reply was not refused")
        [entry] = _stale(mark)
        self.assertEqual(entry["reason"], "2 approval questions are open or just ended")
        self.assertEqual(entry["notice"], "sent")
        notices = [r["body"] for r in self.ntfy.posts["apr1e2e"] if "was not used" in r["body"]]
        self.assertEqual(len(notices), 1, notices)
        self.assertTrue(first.is_alive())
        self.assertTrue(second.is_alive())
        self.ntfy.add("apr1e2e-responses", f"DENIED {a_id}")
        self.ntfy.add("apr1e2e-responses", f"DENIED {b_id}")
        first.join(timeout=15)
        second.join(timeout=15)
        self.assertTrue(results["a"]["denied"], results["a"])
        self.assertTrue(results["b"]["denied"], results["b"])


# ---------------------------------------------------------------------------
# Telegram routing: APR-2, APR-5
# ---------------------------------------------------------------------------

class TestTelegramUnplacedQuestions(base._TelegramFixture):
    A, B = "a" * 16, "b" * 16

    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)
        self.cfg = self._tg_cfg()

    def _ask(self, approval_id, question_message_id=None):
        an.write_tg_pending(approval_id, f"{approval_id[:1]}?", 60)
        if question_message_id is not None:
            an.remember_tg_question_message(approval_id, question_message_id)

    def _reply(self, message_id, text):
        an.handle_bot_update(self.cfg, {"update_id": message_id, "message": {
            "message_id": message_id, "chat": {"id": 42}, "text": text}})

    def test_reply_while_a_newer_question_is_being_sent_is_not_used(self):
        self._ask(self.A, 1)
        self._ask(self.B)                              # sendMessage still running
        mark = _history_mark()
        before = len(self.tg.requests)
        self._reply(3, "yes")
        self._reply(5, "prod")                         # after both: still two open
        self.assertIsNone(an.read_tg_answer(self.A))
        self.assertIsNone(an.read_tg_answer(self.B))
        self.assertEqual([(r["text"], r["reason"], r["notice"]) for r in _stale(mark)],
                         [("yes", "2 approval questions are open or just ended", "sent"),
                          ("prod", "2 approval questions are open or just ended", "sent")])
        notices = [r["body"] for r in self.tg.requests[before:] if r["method"] == "sendMessage"]
        self.assertEqual([n["reply_to_message_id"] for n in notices], [3, 5])
        self.assertIn("Reply on the question", notices[0]["text"])

    def test_retried_send_records_the_copy_that_got_through(self):
        calls = []

        def send_ask(channel, *args, **kwargs):
            calls.append(None)
            if len(calls) == 1:
                raise an.TransientError("read timeout")
            return {"message_id": 4}

        results = {}
        with unittest.mock.patch.object(an.TelegramChannel, "send_ask", send_ask), \
                unittest.mock.patch.object(an, "RETRY_BACKOFF_SECONDS", (0.01, 0.01)), \
                contextlib.redirect_stderr(io.StringIO()):
            thread = threading.Thread(target=lambda: results.update(
                r=an.run_ask(self.cfg, "Drop prod DB?", timeout_seconds=20,
                             print_status=False)), daemon=True)
            thread.start()
            markers = []
            _wait_for(lambda: markers.extend(an.pending_markers("tg-pending")) or any(
                m.get("question_message_id") for m in markers))
            marker = next(m for m in markers if m.get("question_message_id"))
            self.assertEqual(marker["question_message_id"], 4)
            self._reply(3, "yes")                     # under the first copy: older
            self.assertIsNone(an.read_tg_answer(marker["approval_id"]))
            an.write_tg_answer(marker["approval_id"], "no")
            thread.join(timeout=10)
        self.assertTrue(results["r"]["denied"])

    def test_typed_verdict_goes_to_the_ask_it_names(self):
        """APR-5: not to whichever ask is newest."""
        self._ask(self.A, 1)
        self._ask(self.B, 2)
        self._reply(3, f"APPROVED {self.A.upper()}")
        self.assertEqual(an.read_tg_answer(self.A), f"APPROVED {self.A.upper()}")
        self.assertIsNone(an.read_tg_answer(self.B))
        self.assertEqual(an._parse_answer(an.read_tg_answer(self.A),
                                          approval_id=self.A)[0], "approved")
        an.remove_tg_answer(self.A)
        closed = "c" * 16
        mark = _history_mark()
        self._reply(4, f"DENIED {closed}")
        self.assertIsNone(an.read_tg_answer(self.A))
        self.assertIsNone(an.read_tg_answer(self.B))
        self.assertEqual([(r["approval_id"], r["reason"]) for r in _stale(mark)],
                         [(closed, "verdict for a question that is no longer open")])
        an.close_pending("tg-pending", self.A, answered=True)
        self._reply(5, "approve 2")                  # no full id: free text for B
        self.assertEqual(an.read_tg_answer(self.B), "approve 2")


# ---------------------------------------------------------------------------
# APR-3
# ---------------------------------------------------------------------------

class TestYesThenNotNow(unittest.TestCase):
    def test_a_yes_followed_by_a_postponement_or_refusal_denies(self):
        for text in ("yes, but wait", "ok, later", "okay, warte", "ja, aber später",
                     "👍 but hold on", "👍⏳", "👍🏻 ⏳", "yes, not yet", "Yes! Wait.",
                     "approved, abort", "ja, nein", "ok, stop", "yes, ok, wait",
                     "Ship it, but hold on"):
            with self.subTest(text=text):
                kind, answer = an._parse_answer(text, yes_label="Ship it")
                self.assertEqual(kind, "denied")
                self.assertEqual(answer, text)      # the whole reply, nothing lost

    def test_a_yes_with_an_instruction_stays_free_text(self):
        for text in ("yes, but use staging", "yes, go ahead", "ja bitte", "okay, noted",
                     "approve option 2", "yes, nevertheless", "ok, do it now",
                     "Ship it, but canary first", "yesterday", "okay"):
            with self.subTest(text=text):
                kind = an._parse_answer(text, yes_label="Ship it")[0]
                self.assertEqual(kind, "approved" if text == "okay" else "answer")

    def test_a_long_run_of_yeses_does_not_exhaust_the_stack(self):
        self.assertEqual(an._parse_answer("y " * 3000)[0], "answer")
        self.assertEqual(an._parse_answer("yes " * 1500 + "wait")[0], "denied")

    def test_ask_exits_nonzero(self):
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        _clean_state()
        self.addCleanup(_clean_state)
        cfg = base.make_config(ntfy.url, topic="apr3")
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(
            result=an.run_ask(cfg, "Deploy?", timeout_seconds=20, print_status=False)),
            daemon=True)
        thread.start()
        _wait_for(lambda: ntfy.posts.get("apr3"), message="question not published")
        urllib.request.urlopen(urllib.request.Request(
            f"{ntfy.url}/apr3-responses", method="POST",
            data="yes, but wait".encode("utf-8"))).read()
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
# APR-4, S2
# ---------------------------------------------------------------------------

class TestQueueAgeAndPriorities(unittest.TestCase):
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

    def _queue(self):
        return [item for _, item in an._read_item_files(an.queue_dir())]

    def _deferred(self):
        return [item for _, item in an._read_item_files(an.deferred_dir())]

    def test_quiet_hours_cycle_keeps_the_queue_time(self):
        """APR-4: the drain/flush cycle used to restart the 24 h clock nightly."""
        cfg = base.make_config(f"http://127.0.0.1:{base._free_port()}", topic="apr4")
        queued_at = time.time() - 20 * 3600
        an.enqueue_item(cfg, {"message": "build finished", "priority": "low",
                              "channels": ["ntfy"], "created": queued_at})
        cfg.data["quiet_hours"] = ALL_DAY               # night: the drain defers it
        self.assertEqual(an.drain_queue(cfg, timeout=0.5)["deferred"], 1)
        self.assertEqual(self._deferred()[0]["queued_at"], queued_at)
        cfg.data["quiet_hours"] = []                    # morning: server still down
        for name, item in an._read_item_files(an.deferred_dir()):
            item["deliver_after"] = 0
            with open(os.path.join(an.deferred_dir(), name), "w", encoding="utf-8") as fh:
                json.dump(item, fh)
        self.assertEqual(an.flush_deferred(cfg, timeout=0.5)["kept"], 1)
        self.assertEqual([item["created"] for item in self._queue()], [queued_at])
        # four more hours: past the queue's age limit, counted from the start
        mark = _history_mark()
        with unittest.mock.patch.object(an, "QUEUE_MAX_AGE_SECONDS", 19 * 3600):
            self.assertEqual(an.drain_queue(cfg, timeout=0.5)["dropped"], 1)
        self.assertEqual(self._queue(), [])
        self.assertEqual([r["event"] for r in _history_since(mark)], ["queue_expired"])

    def test_plain_deferred_item_is_queued_with_a_fresh_time(self):
        cfg = base.make_config(f"http://127.0.0.1:{base._free_port()}", topic="apr4b")
        an.defer_item(cfg, "held", priority="low", channels=["ntfy"])
        item = self._deferred()[0]
        self.assertNotIn("queued_at", item)
        item["deliver_after"] = 0
        with open(os.path.join(an.deferred_dir(), f"{item['id']}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(item, fh)
        before = time.time()
        an.flush_deferred(cfg, timeout=0.5)
        self.assertGreaterEqual(self._queue()[0]["created"], before)

    def test_priority_number(self):
        for value, number in (("urgent", 5), (5, 5), ("5", 5), (4, 4), ("low", 2),
                              (None, 3), ("", 3), ("bogus", 3), (["high"], 3),
                              ({"p": 1}, 3), (float("inf"), 3), (9, 3)):
            with self.subTest(value=value):
                self.assertEqual(an.priority_number(value), number)

    def test_quiet_hours_read_numeric_priorities_like_the_send_path(self):
        """S2 / D3 / IW-6: `queue list` said urgent, the drain held it as normal."""
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = base.make_config(ntfy.url, topic="s2prio")
        cfg.data.update(quiet_hours=ALL_DAY, quiet_hours_min_priority=4,
                        quiet_hours_mode="defer")
        for priority in (5, "5", 4, "urgent", 2):
            an.enqueue_item(cfg, {"message": f"p {priority!r}", "channels": ["ntfy"],
                                  "priority": priority})
        stats = an.drain_queue(cfg)
        self.assertEqual((stats["delivered"], stats["deferred"]), (4, 1))
        self.assertEqual([item["priority"] for item in self._deferred()], ["low"])
        # a due deferred item with a legacy number goes out too
        an.defer_item(cfg, "legacy urgent", priority=5, channels=["ntfy"])
        for name, item in an._read_item_files(an.deferred_dir()):
            item["deliver_after"] = 0
            with open(os.path.join(an.deferred_dir(), name), "w", encoding="utf-8") as fh:
                json.dump(item, fh)
        an.flush_deferred(cfg)
        bodies = [p["body"] for p in ntfy.posts["s2prio"]]
        self.assertIn("legacy urgent", bodies)
        self.assertEqual([item["message"] for item in self._deferred()], ["p 2"])

    def test_odd_priority_does_not_crash_the_drain(self):
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = base.make_config(ntfy.url, topic="s2odd")
        an.enqueue_item(cfg, {"message": "list priority", "channels": ["ntfy"],
                              "priority": ["high"]})
        self.assertEqual(an.drain_queue(cfg)["delivered"], 1)
        self.assertEqual(self._queue(), [])


if __name__ == "__main__":
    unittest.main()
