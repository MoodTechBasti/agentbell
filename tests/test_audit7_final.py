"""Regression tests for the last review of 1.7.0 (audit 7).

AF-1   a typed reply was judged against the questions open when it was READ:
       read late (a reconnect, a bot that was down), a "yes" typed while two
       questions were open went to the one left. A reply is now judged at
       the time it was sent, and one read more than LATE_REPLY_SECONDS after
       it was sent is refused. Clock skew between the server and this
       machine cancels out; it can only cost a refusal
AF-2   an HTTP 500 counted as proof that the question never reached the
       phone, but ntfy answers 500 after it has delivered the message; only
       a 4xx (not 408) or Telegram ok:false proves that now
DOC-4  the send-time topic error said "max 64 chars" while the documented
       limit is 54; a 55-64 character topic still notifies, `ask` refuses it
       with the reason, doctor and verify explain it
S-1    HTTPError objects were never closed (Python 3.14 ResourceWarning)
"""

import contextlib
import gc
import io
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: its classes do not re-run)
import agentbell as an  # noqa: E402

A, B = "a" * 16, "b" * 16
TWO_OPEN = "2 approval questions are open or just ended"
LATE = "arrived more than"
REAL_TIME = time.time
BACKLOG = "not sent: chat backlog from before the bot started"


def _clean_state():
    for name in ("ntfy-pending", "tg-pending", "tg-answers"):
        shutil.rmtree(os.path.join(an.state_dir(), name), ignore_errors=True)
    with contextlib.suppress(OSError):
        os.remove(an._consumed_path("ntfy"))


def _history_mark():
    return len(an.read_history(limit=0))


def _stale(mark):
    return [r for r in an.read_history(limit=0)[mark:] if r.get("event") == "stale_answer"]


def _wait_for(condition, timeout=20.0, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(message)


def _raw(name, approval_id):
    try:
        with open(an._pending_path(name, approval_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _clock(at):
    """This machine's clock reads `at` (a float) for the block."""
    with unittest.mock.patch.object(an.time, "time", lambda: at):
        yield


class ClockNtfy:
    """ntfy mock whose clock runs `skew` seconds off this machine's.

    Times are whole seconds, as ntfy's are. The response topic can be
    hidden (a lost connection: nothing is delivered, the server keeps it).
    A POST whose body contains `fail_on` is stored and delivered, and then
    answered with `fail_status`, as ntfy does when its cache write fails.
    """

    def __init__(self, skew=0, fail_on=None, fail_status=500):
        self.skew = skew
        self.fail_on = fail_on
        self.fail_status = fail_status
        self.hidden = False
        self.posts = {}
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
                if mock.fail_on and mock.fail_on in body:
                    self.send_response(mock.fail_status)
                    self.send_header("Content-Length", "17")
                    self.end_headers()
                    self.wfile.write(b"cache write fails")
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
                    time.sleep(0.2)                    # no stream: the poller reads
                    return
                if mock.hidden and topic.endswith("-responses"):
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
            record = {"id": os.urandom(6).hex(), "time": int(REAL_TIME() + self.skew),
                      "body": body}
            self.posts.setdefault(topic, []).append(record)
            return record

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class _NtfyEndToEnd(unittest.TestCase):
    TOPIC = "audit7ntfy"

    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)
        for name, value in (("RETRY_BACKOFF_SECONDS", (0.01, 0.01)),):
            patch = unittest.mock.patch.object(an, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        original = an.ApprovalWaiter.__init__

        def fast(waiter, *args, **kwargs):
            kwargs["poll_interval"] = 0.2
            original(waiter, *args, **kwargs)
        patch = unittest.mock.patch.object(an.ApprovalWaiter, "__init__", fast)
        patch.start()
        self.addCleanup(patch.stop)
        self.results = {}

    def mock(self, **kwargs):
        ntfy = ClockNtfy(**kwargs)
        self.addCleanup(ntfy.stop)
        self.cfg = base.make_config(ntfy.url, topic=self.TOPIC)
        return ntfy

    def ask(self, key, question, timeout=30):
        def run():
            try:
                self.results[key] = an.run_ask(self.cfg, question, timeout_seconds=timeout,
                                               print_status=False)
            except RuntimeError as exc:
                self.results[key] = exc
        thread = threading.Thread(target=run, daemon=True)
        with contextlib.redirect_stderr(io.StringIO()):
            thread.start()
        return thread

    def id_of(self, ntfy, question):
        _wait_for(lambda: any(question in p["body"] for p in ntfy.posts.get(self.TOPIC, [])),
                  message=f"{question} was not published")
        body = next(p["body"] for p in ntfy.posts[self.TOPIC] if question in p["body"])
        return re.search(r"ID: ([0-9a-f]+)", body).group(1)

    def published(self, approval_id):
        _wait_for(lambda: "question_time" in _raw("ntfy-pending", approval_id),
                  message="the question's time was not recorded")

    def reply(self, ntfy, text):
        ntfy.add(f"{self.TOPIC}-responses", text)

    def notices(self, ntfy):
        return [p["body"] for p in ntfy.posts.get(self.TOPIC, []) if "was not used" in p["body"]]

    def finish(self, key, thread):
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        return self.results[key]


# ---------------------------------------------------------------------------
# AF-2 - only a rejection proves that nothing reached the phone
# ---------------------------------------------------------------------------

class TestServerErrorsAreNoProof(_NtfyEndToEnd):
    def test_the_status_table(self):
        exc = an.TransientError("HTTP error")
        for code, refused in ((500, False), (502, False), (503, False), (504, False),
                              (520, False), (524, False), (408, False), (301, False),
                              (400, True), (401, True), (403, True), (404, True),
                              (413, True), (429, True)):
            exc.__cause__ = urllib.error.HTTPError("http://x", code, "", {}, io.BytesIO())
            self.addCleanup(exc.__cause__.close)
            self.assertEqual(an._refused(exc), refused, code)
        self.assertTrue(an._refused(an.TelegramRefused("Telegram error: chat not found")))
        self.assertFalse(an._refused(an.TransientError("timeout talking to ntfy")))

    def test_a_telegram_5xx_keeps_its_question(self):
        def failing(code):
            def send():
                exc = an.TransientError(f"HTTP {code}")
                exc.__cause__ = urllib.error.HTTPError("http://x", code, "", {}, io.BytesIO())
                self.addCleanup(exc.__cause__.close)
                raise exc
            return send
        for code, refused in ((500, False), (400, True)):
            with self.assertRaises(RuntimeError) as caught:
                an._publish_question(failing(code))
            self.assertEqual(caught.exception.refused_every_time, refused, code)

    def test_a_500_after_delivery_keeps_the_marker_and_blocks_a_typed_yes(self):
        """The finding's repro: B waits, A's question is delivered three
        times but answered 500 each time. A "yes" typed under A's copies
        must not approve B."""
        ntfy = self.mock(fail_on="DROP")
        second = self.ask("b", "B: run the test suite?")
        b_id = self.id_of(ntfy, "B: run")
        self.published(b_id)
        first = self.ask("a", "A: DROP the production database?")
        failed = self.finish("a", first)
        self.assertIsInstance(failed, RuntimeError)
        self.assertIn("HTTP 500", str(failed))
        copies = [p for p in ntfy.posts[self.TOPIC] if "DROP" in p["body"]]
        self.assertEqual(len(copies), an.RETRY_ATTEMPTS)          # all on the phone
        a_id = re.search(r"ID: ([0-9a-f]+)", copies[0]["body"]).group(1)
        tombstone = _raw("ntfy-pending", a_id)
        self.assertEqual((tombstone.get("closed"), tombstone.get("answered"),
                          tombstone.get("question_time")), (True, False, None))
        mark = _history_mark()
        self.reply(ntfy, "yes")
        # the notice goes out before the record is written
        _wait_for(lambda: self.notices(ntfy) and _stale(mark),
                  message="no notice on the phone")
        self.assertTrue(second.is_alive())
        self.assertEqual([r["reason"] for r in _stale(mark)], [TWO_OPEN])
        self.reply(ntfy, f"DENIED {b_id}")
        self.assertTrue(self.finish("b", second)["denied"])

    def test_a_rejected_question_still_leaves_no_marker(self):
        ntfy = self.mock(fail_on="Deploy", fail_status=400)
        first = self.ask("a", "Deploy?")
        self.assertIsInstance(self.finish("a", first), RuntimeError)
        a_id = self.id_of(ntfy, "Deploy?")
        self.assertEqual(_raw("ntfy-pending", a_id), {})
        self.assertEqual(len([p for p in ntfy.posts[self.TOPIC] if "Deploy" in p["body"]]), 1)


# ---------------------------------------------------------------------------
# AF-1 - the age of a reply, skew-proof
# ---------------------------------------------------------------------------

class TestReplyAgeBound(unittest.TestCase):
    def test_it_never_underestimates_whatever_the_skew(self):
        """Simulated clocks: the server runs `skew` off this machine, times
        are whole seconds, the question's round trip takes up to 2 s."""
        rng = random.Random(7)
        for _ in range(5000):
            skew = rng.uniform(-7200, 7200)
            t0 = rng.uniform(1e9, 2e9)                 # local time before the send
            t1 = t0 + rng.uniform(0, 2)                # the server stamps the question
            t_reply = t1 + rng.uniform(-0.5, 300)      # the reply is sent
            t_read = t_reply + rng.uniform(0, 200)     # and read
            marker = {"question_sent_at": t0, "question_time": int(t1 + skew)}
            bound = an.reply_age_bound(marker, "question_time", int(t_reply + skew), t_read)
            true_age = t_read - t_reply
            self.assertGreaterEqual(bound, true_age)
            self.assertLessEqual(bound, true_age + (t1 - t0) + 2)

    def test_unknown_parts_give_no_bound(self):
        marker = {"question_sent_at": 100.0, "question_time": 5}
        self.assertIsNone(an.reply_age_bound(marker, "question_time", None, 200.0))
        self.assertIsNone(an.reply_age_bound({"question_time": 5}, "question_time", 6, 200.0))
        self.assertIsNone(an.reply_age_bound({"question_sent_at": 1.0, "question_time": None},
                                             "question_time", 6, 200.0))


class TestTelegramLateReplies(base._TelegramFixture):
    """The finding's Telegram repro and its variants, for three skews."""

    SKEWS = (-3600, 0, 3600)

    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)
        self.cfg = self._tg_cfg()
        self.t0 = REAL_TIME()

    def _ask(self, approval_id, message_id, at, skew):
        with _clock(at):
            an.write_tg_pending(approval_id, f"{approval_id[:1]}?", 600)
            an.remember_tg_question_message(approval_id, message_id, int(at + skew), at)

    def _end(self, approval_id, at, answered=False):
        with _clock(at):
            an.close_pending("tg-pending", approval_id, answered=answered)

    def _update(self, message_id, text, typed_at, skew, **extra):
        message = {"message_id": message_id, "chat": {"id": 42}, "text": text,
                   "date": int(typed_at + skew)}
        message.update(extra)
        return {"update_id": message_id, "message": message}

    def _read(self, update, at, backlog=False):
        session = {"backlog": backlog, "notices": {}}
        with _clock(at):
            an.handle_bot_update(self.cfg, update, session)

    def _sent(self, before):
        return [r["body"]["text"] for r in self.tg.requests[before:]
                if r["method"] == "sendMessage"]

    def test_a_backlog_yes_typed_under_two_questions_is_refused(self):
        """A asked, B asked, "yes" typed; B ends unanswered and its grace
        passes while the bot is down; the bot replays the yes."""
        t0 = self.t0
        for skew in self.SKEWS:
            with self.subTest(skew=skew):
                _clean_state()
                self._ask(A, 100, t0, skew)
                self._ask(B, 101, t0 + 1, skew)
                update = self._update(102, "yes", t0 + 2, skew)
                self._end(B, t0 + 5)
                before, mark = len(self.tg.requests), _history_mark()
                self._read(update, t0 + 5 + an.PENDING_TOMBSTONE_GRACE_SECONDS
                           + an.LATE_REPLY_SECONDS + 10, backlog=True)
                self.assertIsNone(an.read_tg_answer(A))
                [entry] = _stale(mark)
                self.assertIn(LATE, entry["reason"])
                self.assertEqual(entry["notice"], BACKLOG)
                self.assertEqual(self._sent(before), [])

    def test_a_late_yes_after_the_backlog_gets_a_notice(self):
        t0 = self.t0
        self._ask(A, 100, t0, 0)
        before, mark = len(self.tg.requests), _history_mark()
        self._read(self._update(101, "yes", t0 + 1, 0), t0 + 1 + an.LATE_REPLY_SECONDS + 5)
        self.assertIsNone(an.read_tg_answer(A))
        [entry] = _stale(mark)
        self.assertEqual(entry["notice"], "sent")
        [notice] = self._sent(before)
        self.assertIn(LATE, notice)

    def test_a_question_in_its_grace_when_the_reply_was_typed_still_counts(self):
        """B ended 42 s before the "yes": its grace covered the moment it was
        typed. Read 25 s later (inside the late window) B's grace is over,
        and the old rule handed the yes to A."""
        t0 = self.t0
        for skew in self.SKEWS:
            with self.subTest(skew=skew):
                _clean_state()
                self._ask(B, 99, t0 - 50, skew)
                self._end(B, t0 - 40)
                self._ask(A, 100, t0, skew)
                mark = _history_mark()
                self._read(self._update(101, "yes", t0 + 2, skew), t0 + 27, backlog=True)
                self.assertIsNone(an.read_tg_answer(A))
                self.assertEqual([r["reason"] for r in _stale(mark)], [TWO_OPEN])

    def test_an_ask_answered_after_the_reply_was_typed_still_counts(self):
        t0 = self.t0
        for skew in self.SKEWS:
            with self.subTest(skew=skew):
                _clean_state()
                self._ask(A, 100, t0, skew)
                self._ask(B, 101, t0 + 1, skew)
                typed = self._update(102, "yes", t0 + 2, skew)
                self._end(B, t0 + 4, answered=True)          # B's button, after the yes
                mark = _history_mark()
                self._read(typed, t0 + 6, backlog=True)
                self.assertIsNone(an.read_tg_answer(A))
                self.assertEqual([r["reason"] for r in _stale(mark)], [TWO_OPEN])
                # typed after B's answer: A is the only question left
                self._read(self._update(103, "yes", t0 + 10, skew), t0 + 11)
                self.assertEqual(an.read_tg_answer(A), "yes")
                an.remove_tg_answer(A)

    def test_a_live_reply_after_the_grace_is_used(self):
        """The retention does not stretch the grace for a live reply."""
        t0 = self.t0
        for skew in self.SKEWS:
            with self.subTest(skew=skew):
                _clean_state()
                self._ask(B, 99, t0 - 70, skew)
                self._end(B, t0 - an.PENDING_TOMBSTONE_GRACE_SECONDS - 5)
                self._ask(A, 100, t0, skew)
                self._read(self._update(101, "yes", t0 + 2, skew), t0 + 3)
                self.assertEqual(an.read_tg_answer(A), "yes")
                self.assertEqual(len(an.pending_markers("tg-pending")), 2)   # B kept
                an.remove_tg_answer(A)

    def test_named_replies_are_not_affected_by_their_age(self):
        t0 = self.t0
        self._ask(A, 100, t0, 0)
        self._ask(B, 101, t0 + 1, 0)
        late = t0 + 2 + an.LATE_REPLY_SECONDS + 60
        self._read(self._update(102, f"APPROVED {A}", t0 + 2, 0), late)
        self.assertEqual(an.read_tg_answer(A), f"APPROVED {A}")
        self._read(self._update(103, "ok", t0 + 3, 0,
                                reply_to_message={"message_id": 101, "text": "B?"}), late)
        self.assertEqual(an.read_tg_answer(B), "ok")

    def test_a_reply_without_a_date_is_not_used(self):
        self._ask(A, 100, self.t0, 0)
        update = self._update(101, "yes", self.t0, 0)
        del update["message"]["date"]
        mark = _history_mark()
        self._read(update, self.t0 + 1, backlog=True)
        self.assertIsNone(an.read_tg_answer(A))
        self.assertEqual([r["reason"] for r in _stale(mark)], ["the time it was sent is unknown"])


class TestNtfyLateReplies(unittest.TestCase):
    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)

    def test_the_route_refuses_a_late_reply(self):
        t0 = REAL_TIME()
        with _clock(t0):
            an.write_ntfy_pending(A, "Q?", 600)
            an.remember_ntfy_question(A, 5000, t0)       # the server's clock: 5000
        with _clock(t0 + 3):
            owner, reason, _ = an.ntfy_reply_route(5002)
        self.assertEqual((owner["approval_id"], reason), (A, None))
        with _clock(t0 + 3 + an.LATE_REPLY_SECONDS + 1):
            owner, reason, about = an.ntfy_reply_route(5002)
        self.assertIsNone(owner)
        self.assertIn(LATE, reason)
        self.assertEqual(about, A)

    def test_a_reply_from_the_future_is_not_trusted(self):
        """A bound below zero is below every true age: a clock was stepped
        between the question and the reply, so the age is unknown."""
        t0 = REAL_TIME()
        with _clock(t0):
            an.write_ntfy_pending(A, "Q?", 600)
            an.remember_ntfy_question(A, 5000, t0)
        with _clock(t0 + 3):
            self.assertEqual(an.ntfy_reply_route(5100),
                             (None, "the time it was sent is unknown", A))

    def test_a_question_without_a_send_time_takes_no_typed_reply(self):
        an.write_ntfy_pending(A, "Q?", 600)
        an.remember_ntfy_question(A, 5000)
        self.assertEqual(an.ntfy_reply_route(5001),
                         (None, "the time it was sent is unknown", A))

    def test_tombstones_are_kept_for_the_late_window(self):
        t0 = REAL_TIME()
        with _clock(t0):
            an.write_ntfy_pending(A, "Q?", 600)
            an.close_pending("ntfy-pending", A, answered=True)
        kept = an.PENDING_TOMBSTONE_GRACE_SECONDS + an.LATE_REPLY_SECONDS
        with _clock(t0 + kept - 1):
            self.assertEqual(len(an.pending_markers("ntfy-pending")), 1)
        with _clock(t0 + kept + 1):
            self.assertEqual(an.pending_markers("ntfy-pending"), [])


class TestNtfyLateRepliesEndToEnd(_NtfyEndToEnd):
    """Real asks against a server whose clock is an hour off ours."""

    def test_a_live_reply_is_used_whatever_the_skew(self):
        for skew in (-3600, 3600):
            with self.subTest(skew=skew):
                _clean_state()
                ntfy = self.mock(skew=skew)
                thread = self.ask("a", "Deploy?")
                self.published(self.id_of(ntfy, "Deploy?"))
                self.reply(ntfy, "yes")
                self.assertTrue(self.finish("a", thread)["approved"])

    def test_a_reply_read_after_a_lost_connection_is_refused(self):
        with unittest.mock.patch.object(an, "LATE_REPLY_SECONDS", 2):
            for skew in (-3600, 3600):
                with self.subTest(skew=skew):
                    _clean_state()
                    ntfy = self.mock(skew=skew)
                    thread = self.ask("a", "Deploy?")
                    a_id = self.id_of(ntfy, "Deploy?")
                    self.published(a_id)
                    ntfy.hidden = True
                    mark = _history_mark()
                    self.reply(ntfy, "yes")
                    time.sleep(an.LATE_REPLY_SECONDS + 2)
                    ntfy.hidden = False
                    _wait_for(lambda: self.notices(ntfy) and _stale(mark),
                              message="no notice on the phone")
                    self.assertTrue(thread.is_alive())
                    self.assertIn(LATE, _stale(mark)[0]["reason"])
                    self.reply(ntfy, f"DENIED {a_id}")
                    self.assertTrue(self.finish("a", thread)["denied"])

    def test_the_finding_repro(self):
        """A (long) and B (short) open; "yes" typed while both are; the
        connection is lost until B has timed out and its grace is over."""
        with unittest.mock.patch.object(an, "PENDING_TOMBSTONE_GRACE_SECONDS", 1), \
                unittest.mock.patch.object(an, "LATE_REPLY_SECONDS", 2):
            ntfy = self.mock()
            first = self.ask("a", "A: delete old backups?", timeout=40)
            a_id = self.id_of(ntfy, "A: delete")
            self.published(a_id)
            second = self.ask("b", "B: merge the PR?", timeout=2)
            self.published(self.id_of(ntfy, "B: merge"))
            ntfy.hidden = True
            mark = _history_mark()
            self.reply(ntfy, "yes")
            self.assertTrue(self.finish("b", second)["timeout"])
            time.sleep(an.PENDING_TOMBSTONE_GRACE_SECONDS + an.LATE_REPLY_SECONDS + 2)
            ntfy.hidden = False
            _wait_for(lambda: _stale(mark), message="the reply was not refused")
            self.assertTrue(first.is_alive())
            self.reply(ntfy, f"DENIED {a_id}")
            self.assertTrue(self.finish("a", first)["denied"])


# ---------------------------------------------------------------------------
# DOC-4 - one topic limit, stated the same way everywhere
# ---------------------------------------------------------------------------

class TestTopicLimits(unittest.TestCase):
    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)
        self.ntfy = base.MockNtfy()
        self.addCleanup(self.ntfy.stop)

    def test_a_60_character_topic_still_notifies(self):
        topic = "t" * 60
        an.validate_topic(topic)
        sent = an.NtfyChannel(base.make_config(self.ntfy.url, topic=topic)).publish(topic, "hi")
        self.assertTrue(sent["ok"])
        self.assertEqual(self.ntfy.posts[topic][0]["body"], "hi")

    def test_the_send_error_names_both_limits(self):
        with self.assertRaises(RuntimeError) as caught:
            an.validate_topic("t" * 65)
        message = str(caught.exception)
        self.assertIn("at most 54 characters", message)
        self.assertIn("'-responses'", message)
        self.assertIn("ntfy allows 64", message)
        self.assertNotIn("max 64", message)

    def test_ask_refuses_a_60_character_topic_with_the_reason(self):
        topic = "t" * 60
        cfg = base.make_config(self.ntfy.url, topic=topic)
        with self.assertRaises(RuntimeError) as caught:
            an.run_ask(cfg, "Deploy?", timeout_seconds=5, print_status=False)
        message = str(caught.exception)
        self.assertIn("60 characters", message)
        self.assertIn("at most 54 characters", message)
        self.assertIn("Notifications still work", message)
        self.assertNotIn(topic, message)
        self.assertEqual(an.pending_markers("ntfy-pending"), [])
        self.assertEqual(self.ntfy.posts, {})

    def test_doctor_and_verify_say_why(self):
        status, problem = an.rate_topic("t" * 60)
        self.assertEqual(status, an.FAIL)
        self.assertIn("too long (60 chars, max 54)", problem)
        self.assertIn("notifications still go out, but 'ask' cannot", problem)
        status, problem = an.rate_topic("t" * 70)
        self.assertEqual(status, an.FAIL)
        self.assertIn("ntfy allows 64", problem)


# ---------------------------------------------------------------------------
# S-1 - HTTP errors let go of their connection
# ---------------------------------------------------------------------------

class TestHttpErrorsAreClosed(unittest.TestCase):
    def _error(self, code, headers=None):
        body = io.BytesIO(b"nope")
        return urllib.error.HTTPError("http://127.0.0.1:9/t", code, "x", headers or {}, body), body

    def test_http_request_closes_the_error_and_keeps_its_status(self):
        for code, kind, refused in ((503, an.TransientError, False),
                                    (500, an.TransientError, False),
                                    (403, an.PermanentError, True)):
            error, body = self._error(code)
            with unittest.mock.patch.object(an.OPENER, "open", side_effect=error):
                with self.assertRaises(kind) as caught:
                    an.http_request("http://127.0.0.1:9/t")
            self.assertTrue(body.closed, code)
            self.assertIn("nope", str(caught.exception))          # read before closing
            self.assertEqual(caught.exception.__cause__.code, code)
            self.assertEqual(an._refused(caught.exception), refused, code)

    def test_a_redirect_is_closed_too(self):
        error, body = self._error(302, {"Location": "https://elsewhere.example/t"})
        with unittest.mock.patch.object(an.OPENER, "open", side_effect=error):
            with self.assertRaises(an.PermanentError) as caught:
                an.http_request("http://127.0.0.1:9/t")
        self.assertTrue(body.closed)
        self.assertIn("elsewhere.example", str(caught.exception))

    def test_a_close_that_fails_does_not_replace_the_error(self):
        error, _ = self._error(500)
        with unittest.mock.patch.object(an.OPENER, "open", side_effect=error), \
                unittest.mock.patch.object(error, "close", side_effect=OSError("gone")):
            with self.assertRaises(an.TransientError) as caught:
                an.http_request("http://127.0.0.1:9/t")
        self.assertEqual(caught.exception.__cause__.code, 500)
        error.close()                     # the real close, for this test's own hygiene

    def test_subscribe_closes_the_error(self):
        error, body = self._error(401)
        cfg = base.make_config("http://127.0.0.1:9", topic="audit7sub")
        with unittest.mock.patch.object(an.OPENER, "open", side_effect=error):
            with self.assertRaises(RuntimeError):
                an.NtfyChannel(cfg).subscribe("audit7sub", since="10s")
        self.assertTrue(body.closed)

    def test_a_server_error_leaves_no_resource_warning(self):
        ntfy = base.MockNtfy(post_503_count=1)
        self.addCleanup(ntfy.stop)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with self.assertRaises(an.TransientError):
                an.http_request(f"{ntfy.url}/audit7warn", "POST", body="x")
            gc.collect()
        self.assertEqual([str(w.message) for w in caught
                          if issubclass(w.category, ResourceWarning)
                          and "HTTPError" in str(w.message)], [])


if __name__ == "__main__":
    unittest.main()
