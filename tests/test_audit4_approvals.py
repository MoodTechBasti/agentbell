"""Regression tests for the approvals findings of the fourth audit round.

APR2-1  an ask that failed or ended used to drop its marker, so a "yes"
        typed under its question approved an older open ask
APR2-2  an older ask's retried or failed copy could land after a newer
        question and hand the newer ask its "yes"
        Both: a typed reply that names no question is used only when exactly
        one question can be on the phone. Ended asks leave tombstones; an
        unused reply is recorded and announced on its channel.
APR2-3  typographic punctuation ("yes… wait", "ok — later") reads like ASCII
APR2-4  quiet_hours_min_priority accepts a name ("high") and never crashes
WIN-1   a marker caught mid-rewrite is read again, and one that stays
        unreadable counts as an open question (fail closed)
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: its classes do not re-run)
import agentbell as an  # noqa: E402

A, B, C = "a" * 16, "b" * 16, "c" * 16
TWO_OPEN = "2 approval questions are open or just ended"


def _clean_state():
    for name in ("queue", "deferred", "ntfy-pending", "tg-pending", "tg-answers"):
        shutil.rmtree(os.path.join(an.state_dir(), name), ignore_errors=True)
    with contextlib.suppress(OSError):
        os.remove(an._consumed_path("ntfy"))


def _history_mark():
    return len(an.read_history(limit=0))


def _stale(mark):
    return [r for r in an.read_history(limit=0)[mark:] if r.get("event") == "stale_answer"]


def _set_pending(name, approval_id, **fields):
    path = an._pending_path(name, approval_id)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _marker(name, approval_id):
    """One read of a marker; {} when it is missing or caught mid-write."""
    try:
        with open(an._pending_path(name, approval_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _wait_for(condition, timeout=10.0, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(message)


# ---------------------------------------------------------------------------
# APR2-1 / APR2-2 - ntfy, end to end
# ---------------------------------------------------------------------------

class TestNtfyOneCandidateRule(unittest.TestCase):
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
        self.cfg = base.make_config(self.ntfy.url, topic="apr4ntfy")
        self.results = {}
        self.threads = []

    def _ask(self, key, question, timeout=20):
        def run():
            try:
                self.results[key] = an.run_ask(self.cfg, question, timeout_seconds=timeout,
                                               print_status=False)
            except RuntimeError as exc:
                self.results[key] = exc
        count = len(self._questions())
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread, count

    def _questions(self):
        return [p for p in self.ntfy.posts.get("apr4ntfy", []) if "ID: " in p["body"]]

    def _id_of(self, question):
        _wait_for(lambda: any(question in p["body"] for p in self._questions()),
                  message=f"{question} was not published")
        body = next(p["body"] for p in self._questions() if question in p["body"])
        approval_id = re.search(r"ID: ([0-9a-f]+)", body).group(1)
        _wait_for(lambda: _marker("ntfy-pending", approval_id).get("question_time"),
                  message="the question's time was not recorded")
        return approval_id

    def _reply(self, text):
        urllib.request.urlopen(urllib.request.Request(
            f"{self.ntfy.url}/apr4ntfy-responses", method="POST",
            data=text.encode("utf-8"))).read()

    def _notices(self):
        return [p["body"] for p in self.ntfy.posts.get("apr4ntfy", [])
                if "was not used" in p["body"]]

    def _finish(self, key, thread):
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        return self.results[key]

    def test_two_open_asks_refuse_a_typed_yes_and_take_their_buttons(self):
        first, _ = self._ask("a", "First: migrate prod?")
        a_id = self._id_of("First:")
        second, _ = self._ask("b", "Second: restart staging?")
        b_id = self._id_of("Second:")
        mark = _history_mark()
        self._reply("yes")
        _wait_for(lambda: self._notices(), message="no notice on the phone")
        self.assertIn(f'Your reply "yes" was not used: {TWO_OPEN}', self._notices()[0])
        self.assertTrue(first.is_alive())
        self.assertTrue(second.is_alive())
        self._reply(f"APPROVED {a_id}")
        self._reply(f"DENIED {b_id}")
        self.assertTrue(self._finish("a", first)["approved"])
        self.assertTrue(self._finish("b", second)["denied"])
        # both asks saw the "yes" before their button (one stream, in order):
        # told once, recorded once (each ask also logs the other's button)
        self.assertEqual(len(self._notices()), 1)
        self.assertEqual([(r["channel"], r["reason"], r["notice"]) for r in _stale(mark)
                          if r["text"] == "yes"],
                         [("ntfy", TWO_OPEN, "sent")])

    def test_after_an_answered_ask_a_typed_yes_reaches_the_next_one(self):
        first, _ = self._ask("a", "First: migrate prod?")
        a_id = self._id_of("First:")
        self._reply(f"APPROVED {a_id}")
        self.assertTrue(self._finish("a", first)["approved"])
        self.assertEqual(_marker("ntfy-pending", a_id)["answered"], True)
        second, _ = self._ask("b", "Second: restart staging?")
        self._id_of("Second:")
        self._reply("yes")
        self.assertTrue(self._finish("b", second)["approved"])
        self.assertEqual(self._notices(), [])

    def test_an_unanswered_ask_blocks_typed_replies_until_its_tombstone_expires(self):
        first, _ = self._ask("a", "First: migrate prod?", timeout=1)
        a_id = self._id_of("First:")
        self.assertTrue(self._finish("a", first)["timeout"])
        tombstone = _marker("ntfy-pending", a_id)
        self.assertEqual((tombstone["closed"], tombstone["answered"]), (True, False))
        self.assertGreaterEqual(tombstone["expires"],
                                time.time() + an.PENDING_TOMBSTONE_GRACE_SECONDS - 5)
        second, _ = self._ask("b", "Second: restart staging?")
        self._id_of("Second:")
        self._reply("yes")                    # may be typed under A's question
        _wait_for(lambda: self._notices(), message="no notice on the phone")
        self.assertTrue(second.is_alive())
        _set_pending("ntfy-pending", a_id, expires=time.time() - 1)
        self._reply("yes")
        self.assertTrue(self._finish("b", second)["approved"])
        self.assertFalse(os.path.exists(an._pending_path("ntfy-pending", a_id)))

    def test_a_failed_single_channel_publish_does_not_hand_its_yes_to_an_older_ask(self):
        """APR2-1: the failed question may be on the phone three times. Every
        attempt timed out, which proves nothing (a refusal would: AS3-2)."""
        older, _ = self._ask("b", "First: migrate prod?")
        self._id_of("First:")
        real = an.NtfyChannel.publish

        def timed_out(channel, topic, message, *args, **kwargs):
            if "Second:" in message:
                raise an.TransientError("timeout talking to ntfy")
            return real(channel, topic, message, *args, **kwargs)
        patch = unittest.mock.patch.object(an.NtfyChannel, "publish", timed_out)
        patch.start()
        self.addCleanup(patch.stop)
        newer, _ = self._ask("a", "Second: restart staging?")
        failed = self._finish("a", newer)
        self.assertIsInstance(failed, RuntimeError)
        [tombstone] = [m for m in an.pending_markers("ntfy-pending") if m.get("closed")]
        self.assertEqual((tombstone["answered"], tombstone["question_time"]), (False, None))
        self._reply("yes")
        _wait_for(lambda: self._notices(), message="no notice on the phone")
        self.assertTrue(older.is_alive())
        body = next(p["body"] for p in self._questions() if "First:" in p["body"])
        self._reply(f"DENIED {re.search(r'ID: ([0-9a-f]+)', body).group(1)}")
        self.assertTrue(self._finish("b", older)["denied"])


# ---------------------------------------------------------------------------
# APR2-1 / APR2-2 - ntfy, the rule itself
# ---------------------------------------------------------------------------

class TestNtfyRoute(unittest.TestCase):
    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)

    def _open(self, approval_id, question_time="unset"):
        an.write_ntfy_pending(approval_id, "Q?", 60)
        if question_time != "unset":
            an.remember_ntfy_question(approval_id, question_time)

    def test_single_open_ask_takes_a_newer_reply(self):
        self._open(A, 1000)
        owner, reason, _ = an.ntfy_reply_route(1000)
        self.assertEqual((owner["approval_id"], reason), (A, None))

    def test_a_retried_older_ask_blocks_a_newer_one(self):
        """APR2-2, q1: A's copies land around B's question; any order."""
        self._open(A, 1005)
        _set_pending("ntfy-pending", A, created=time.time() - 30)
        self._open(B, 1002)
        for reply_time in (1003, 1004, 1006):
            self.assertEqual(an.ntfy_reply_route(reply_time), (None, TWO_OPEN, None))

    def test_ended_questions(self):
        self._open(A, 1001)
        self._open(B, 1002)
        an.close_pending("ntfy-pending", B, answered=False)
        self.assertEqual(an.ntfy_reply_route(1003)[1], TWO_OPEN)
        an.close_pending("ntfy-pending", A, answered=True)
        self.assertEqual(an.ntfy_reply_route(1003),
                         (None, "that question is no longer open", B))
        _set_pending("ntfy-pending", B, expires=time.time() - 1)      # its grace is over
        self.assertEqual(an.ntfy_reply_route(1003), (None, "no approval question is open", None))

    def test_a_reply_older_than_every_question_is_only_stale(self):
        self._open(A, 1001)
        self._open(B, 1002)
        self.assertEqual(an.ntfy_reply_route(1000), (None, an.REPLY_PREDATES, None))

    def test_the_one_ask_still_publishing_holds_the_reply(self):
        self._open(A)
        self.assertIsNone(an.ntfy_reply_route(1000))
        an._let_go(an._pending_path("ntfy-pending", A))        # the ask was killed
        self.assertEqual(an.ntfy_reply_route(1000),
                         (None, "that question is no longer open", A))

    def test_a_refused_reply_is_claimed_so_it_is_announced_once(self):
        cfg = base.make_config("http://127.0.0.1:9", topic="apr4once")
        self._open(A, 1001)
        self._open(B, 1002)
        waiters = [an.ApprovalWaiter(cfg, "apr4once-responses", 60, approval_id=i)
                   for i in (A, B)]
        mark = _history_mark()
        for waiter in waiters + waiters:
            waiter._offer("r1", "yes", 1003)
        [entry] = _stale(mark)
        self.assertEqual(entry["reason"], TWO_OPEN)
        self.assertTrue(entry["notice"].startswith("failed: "), entry)   # port 9: visible
        self.assertFalse(an.claim_ntfy_message("r1"))
        self.assertTrue(all(w.messages.empty() for w in waiters))


# ---------------------------------------------------------------------------
# APR2-1 / APR2-2 - Telegram
# ---------------------------------------------------------------------------

class TestTelegramOneCandidateRule(base._TelegramFixture):
    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)
        self.cfg = self._tg_cfg()

    def _open(self, approval_id, message_id=None):
        an.write_tg_pending(approval_id, f"{approval_id[:1]}?", 60)
        if message_id is not None:
            an.remember_tg_question_message(approval_id, message_id)

    def _reply(self, message_id, text, reply_to=None):
        message = {"message_id": message_id, "chat": {"id": 42}, "text": text}
        if reply_to is not None:
            message["reply_to_message"] = reply_to
        an.handle_bot_update(self.cfg, {"update_id": message_id, "message": message})

    def _notices(self, before):
        return [r["body"] for r in self.tg.requests[before:] if r["method"] == "sendMessage"]

    def test_two_open_asks_refuse_a_typed_yes_with_one_notice(self):
        self._open(A, 10)
        self._open(B, 12)
        before, mark = len(self.tg.requests), _history_mark()
        self._reply(13, "yes")
        self.assertIsNone(an.read_tg_answer(A))
        self.assertIsNone(an.read_tg_answer(B))
        [notice] = self._notices(before)
        self.assertEqual(notice["reply_to_message_id"], 13)
        self.assertIn(TWO_OPEN, notice["text"])
        self.assertEqual([(r["channel"], r["reason"], r["notice"]) for r in _stale(mark)],
                         [("telegram", TWO_OPEN, "sent")])

    def test_buttons_still_answer_both(self):
        self._open(A, 10)
        self._open(B, 12)
        for approval_id, answer in ((A, "approved"), (B, "denied")):
            an.handle_bot_update(self.cfg, {"update_id": 1, "callback_query": {
                "id": "cq", "data": f"agentbell|{approval_id}|{answer}",
                "message": {"message_id": 10, "chat": {"id": 42}, "text": "Q"}}})
            self.assertEqual(an.read_tg_answer(approval_id), answer)

    def test_a_button_of_an_ended_ask_has_expired(self):
        self._open(A, 10)
        an.close_pending("tg-pending", A, answered=False)
        before = len(self.tg.requests)
        an.handle_bot_update(self.cfg, {"update_id": 1, "callback_query": {
            "id": "cq", "data": f"agentbell|{A}|approved"}})
        self.assertIsNone(an.read_tg_answer(A))
        self.assertIn("expired", self.tg.requests[before:][-1]["body"]["text"])

    def test_reply_to_always_routes(self):
        self._open(A, 10)
        self._open(B, 12)
        self._open(C)                                  # being sent
        self._reply(20, "use eu-west", reply_to={"message_id": 10, "text": "Q"})
        self.assertEqual(an.read_tg_answer(A), "use eu-west")
        quoted = f"Approval requested\nDeploy?\n\nID: {C}"
        self._reply(21, "go", reply_to={"message_id": 99, "text": quoted})
        self.assertEqual(an.read_tg_answer(C), "go")
        self.assertIsNone(an.read_tg_answer(B))

    def test_reply_to_an_ended_question_is_refused_with_a_notice(self):
        self._open(A, 10)
        self._open(B, 12)
        an.close_pending("tg-pending", A, answered=False)
        before, mark = len(self.tg.requests), _history_mark()
        self._reply(13, "yes", reply_to={"message_id": 10, "text": "Q"})
        self.assertIsNone(an.read_tg_answer(A))
        self.assertIsNone(an.read_tg_answer(B))
        self.assertEqual([(r["approval_id"], r["reason"]) for r in _stale(mark)],
                         [(A, "reply to a question that is no longer open")])
        self.assertEqual(len(self._notices(before)), 1)

    def test_a_failed_single_channel_send_does_not_hand_its_yes_to_an_older_ask(self):
        """APR2-1 on Telegram: the send failed, but a copy may be in the chat."""
        self._open(B, 1)

        def send_ask(channel, *args, **kwargs):
            raise an.TransientError("read timeout")

        with unittest.mock.patch.object(an.TelegramChannel, "send_ask", send_ask), \
                unittest.mock.patch.object(an, "RETRY_BACKOFF_SECONDS", (0.01, 0.01)), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                an.run_ask(self.cfg, "Restart staging?", timeout_seconds=20,
                           print_status=False)
        mark = _history_mark()
        self._reply(5, "yes")
        self.assertIsNone(an.read_tg_answer(B))
        self.assertEqual([r["reason"] for r in _stale(mark)], [TWO_OPEN])

    def test_backlog_older_than_every_question_is_not_announced(self):
        self._open(A, 10)
        self._open(B, 12)
        before, mark = len(self.tg.requests), _history_mark()
        self._reply(9, "yes")                         # replayed by a restarted bot
        self.assertEqual(self._notices(before), [])
        self.assertEqual([r["reason"] for r in _stale(mark)], [an.REPLY_PREDATES])

    def test_bot_status_counts_open_questions_only(self):
        self._open(A, 10)
        self._open(B, 12)
        an.close_pending("tg-pending", A, answered=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            an.print_bot_status(self.cfg)
        self.assertIn("pending:   1 open approval question(s)", out.getvalue())


# ---------------------------------------------------------------------------
# WIN-1 - a marker caught mid-rewrite
# ---------------------------------------------------------------------------

class TestUnreadableMarkers(unittest.TestCase):
    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)

    def _open(self, name, approval_id, **fields):
        an.write_pending(name, approval_id, "Q?", 60)
        if fields:
            _set_pending(name, approval_id, **fields)

    def _tear(self, name, approval_id):
        with open(an._pending_path(name, approval_id), "w", encoding="utf-8"):
            pass                                        # truncated, not yet refilled

    def test_a_torn_read_is_read_again(self):
        self._open("ntfy-pending", A, question_time=1001)
        real, calls = an._read_json_object, []

        def torn_once(path):
            calls.append(path)
            if len(calls) == 1:
                raise json.JSONDecodeError("Expecting value", "", 0)
            return real(path)

        with unittest.mock.patch.object(an, "_read_json_object", torn_once), \
                unittest.mock.patch.object(an, "PENDING_REREAD_SECONDS", 0):
            markers = an.pending_markers("ntfy-pending")
        self.assertEqual([(m["approval_id"], m["question_time"]) for m in markers],
                         [(A, 1001)])
        self.assertEqual(len(calls), 2)

    def test_an_unreadable_marker_counts_as_an_open_question(self):
        self._open("ntfy-pending", A, question_time=1001)
        self._open("ntfy-pending", B)
        self._tear("ntfy-pending", B)
        with unittest.mock.patch.object(an, "PENDING_REREAD_SECONDS", 0):
            markers = {m["approval_id"]: m for m in an.pending_markers("ntfy-pending")}
            self.assertTrue(markers[B]["unreadable"])
            self.assertEqual(an.ntfy_reply_route(1002), (None, TWO_OPEN, None))
            # the only one: a question still going out, so the reply waits
            os.remove(an._pending_path("ntfy-pending", A))
            self.assertIsNone(an.ntfy_reply_route(1002))

    def test_an_unreadable_telegram_marker_blocks_typed_replies(self):
        self._open("tg-pending", A, question_message_id=10)
        self._open("tg-pending", B)
        self._tear("tg-pending", B)
        with unittest.mock.patch.object(an, "PENDING_REREAD_SECONDS", 0):
            markers = an.pending_markers("tg-pending")
            owner, reason, _ = an._tg_reply_owner(
                markers, {"message_id": 11, "chat": {"id": 42}, "text": "yes"})
        self.assertIsNone(owner)
        self.assertEqual(reason, TWO_OPEN)

    def test_an_unreadable_marker_ends_a_grace_after_its_ask(self):
        """Held, it is open however old it is; nobody holds it, it counts
        until a grace after its last write (audit 5)."""
        self._open("ntfy-pending", A)
        self._tear("ntfy-pending", A)
        path = an._pending_path("ntfy-pending", A)
        old = time.time() - an.PENDING_TOMBSTONE_GRACE_SECONDS - 5
        os.utime(path, (old, old))
        with unittest.mock.patch.object(an, "PENDING_REREAD_SECONDS", 0):
            [held] = an.pending_markers("ntfy-pending")
            self.assertEqual((held["unreadable"], held.get("closed")), (True, None))
            an._let_go(path)
            self.assertEqual(an.pending_markers("ntfy-pending"), [])
        self.assertFalse(os.path.exists(path))

    def test_closing_an_unreadable_marker_leaves_a_tombstone(self):
        self._open("ntfy-pending", A)
        self._tear("ntfy-pending", A)
        with unittest.mock.patch.object(an, "PENDING_REREAD_SECONDS", 0):
            an.close_pending("ntfy-pending", A, answered=False)
        tombstone = _marker("ntfy-pending", A)
        self.assertEqual((tombstone["approval_id"], tombstone["closed"],
                          tombstone["answered"]), (A, True, False))
        # never written: stays absent
        an.close_pending("ntfy-pending", B, answered=False)
        self.assertFalse(os.path.exists(an._pending_path("ntfy-pending", B)))


# ---------------------------------------------------------------------------
# APR2-3
# ---------------------------------------------------------------------------

class TestTypographicPunctuation(unittest.TestCase):
    def test_smart_punctuation_reads_like_ascii(self):
        for text in ("yes… wait", "ok… later", "ok…later", "yes — hold on", "yes – wait",
                     "ja … später", "👍… not yet", "yes—wait", "yes? wait", "yes (wait)",
                     "okay → later", "yes ➡️ not now", "ok, “wait”", "👍🏻 — later"):
            with self.subTest(text=text):
                self.assertEqual(an._parse_answer(text), ("denied", text))
        for text, kind in (("yes…", "approved"), ("yes — use staging", "answer"),
                           ("ok… go ahead", "answer"), ("“yes”", "answer"),
                           ("❌ — tests are red", "denied"), ("no… later maybe", "denied")):
            with self.subTest(text=text):
                self.assertEqual(an._parse_answer(text)[0], kind)
        self.assertEqual(an._parse_answer("wait… CI is red"), ("denied", "CI is red"))
        self.assertEqual(an._parse_answer("Don’t — not now", no_label="Don’t")[0], "denied")

    def test_ask_exits_nonzero_on_a_smart_yes_but_wait(self):
        _clean_state()
        self.addCleanup(_clean_state)
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = base.make_config(ntfy.url, topic="apr4smart")
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(
            result=an.run_ask(cfg, "Deploy?", timeout_seconds=20, print_status=False)),
            daemon=True)
        thread.start()
        _wait_for(lambda: ntfy.posts.get("apr4smart"), message="question not published")
        urllib.request.urlopen(urllib.request.Request(
            f"{ntfy.url}/apr4smart-responses", method="POST",
            data="yes… wait".encode("utf-8"))).read()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertTrue(holder["result"]["denied"])
        self.assertFalse(holder["result"]["approved"])


# ---------------------------------------------------------------------------
# APR2-4
# ---------------------------------------------------------------------------

class TestQuietHoursPriorityName(unittest.TestCase):
    ALL_DAY = [{"start": "00:00", "end": "23:59"}]

    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)

    def test_a_name_is_read_like_its_number(self):
        cfg = base.make_config("http://127.0.0.1:9")
        cfg.data.update(quiet_hours=self.ALL_DAY, quiet_hours_min_priority="high")
        self.assertTrue(an.suppressed_by_quiet_hours(cfg, 3, False))
        self.assertFalse(an.suppressed_by_quiet_hours(cfg, 4, False))
        for junk in ("bogus", None, ["high"], 9):
            with self.subTest(value=junk):
                cfg.data["quiet_hours_min_priority"] = junk     # read as normal (3)
                self.assertTrue(an.suppressed_by_quiet_hours(cfg, 2, False))
                self.assertFalse(an.suppressed_by_quiet_hours(cfg, 3, False))

    def test_notify_does_not_crash_outside_quiet_hours(self):
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = base.make_config(ntfy.url, topic="apr4quiet")
        cfg.data.update(quiet_hours=[], quiet_hours_min_priority="high")
        self.assertTrue(an.send_notification(cfg, "hi")["ok"])
        self.assertEqual([p["body"] for p in ntfy.posts["apr4quiet"]], ["hi"])

    def test_doctor_names_the_threshold(self):
        cfg = base.make_config("http://127.0.0.1:9")
        cfg.data.update(quiet_hours=self.ALL_DAY, quiet_hours_min_priority="high")
        [check] = [c for c in an.doctor_checks(cfg) if c["name"] == "quiet hours"]
        self.assertIn("below priority 'high' (4)", check["detail"])

    def test_config_set_accepts_a_name(self):
        cfg = base.make_config("http://127.0.0.1:9")
        with unittest.mock.patch.object(cfg, "save"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(an.config_set(cfg, "quiet_hours_min_priority", "High"), 4)
            self.assertEqual(an.config_set(cfg, "quiet_hours_min_priority", "5"), 5)
            with self.assertRaises(SystemExit):
                an.config_set(cfg, "quiet_hours_min_priority", "bogus")


if __name__ == "__main__":
    unittest.main()
