"""Regression tests for the approvals findings of the fifth audit round.

An ask's pending marker is open exactly while the ask holds a kernel lock on
it. A marker nobody holds has ended; the wall clock only prunes those.

AS3-1  a clock jump (suspend/resume, a slow publish) deleted a waiting ask's
       marker: its button expired, and a typed "yes" went to a newer ask
AS3-2  a failed or killed ask blocked typed replies for its whole timeout,
       even when its question never reached the phone
AS3-3  a restarted bot sent a "Reply not used" notice for every replayed
       backlog message
AS3-4  an ask answered on Telegram stopped counting on ntfy, where its
       question was still on the phone
AS3-5  a button on a killed ask was accepted and the question edited to
       "Answered"
"""

import contextlib
import errno
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: its classes do not re-run)
import agentbell as an  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A, B, C, D = "a" * 16, "b" * 16, "c" * 16, "d" * 16
TWO_OPEN = "2 approval questions are open or just ended"
GRACE = an.PENDING_TOMBSTONE_GRACE_SECONDS
# a tombstone is kept this long after its ask ended (audit 7, AF-1)
KEPT = GRACE + an.LATE_REPLY_SECONDS


def _clean_state():
    for name in ("ntfy-pending", "tg-pending", "tg-answers"):
        path = os.path.join(an.state_dir(), name)
        for entry in os.listdir(path) if os.path.isdir(path) else ():
            an._let_go(os.path.join(path, entry))
        shutil.rmtree(path, ignore_errors=True)
    with contextlib.suppress(OSError):
        os.remove(an._consumed_path("ntfy"))


def _raw(name, approval_id):
    """The marker's file as it is; {} when missing or caught mid-write."""
    try:
        with open(an._pending_path(name, approval_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _wait_for(condition, timeout=15.0, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(message)


def _history_mark():
    return len(an.read_history(limit=0))


def _stale(mark):
    return [r for r in an.read_history(limit=0)[mark:] if r.get("event") == "stale_answer"]


# A process that registers an ask on Telegram and then waits to be killed.
_ASK_CHILD = """\
import sys, time
sys.path.insert(0, sys.argv[1])
import agentbell as an
an.write_tg_pending(sys.argv[2], "Deploy?", 300)
an.remember_tg_question_message(sys.argv[2], 5)
print("ready", flush=True)
time.sleep(120)
"""


def _killed_ask(test, approval_id):
    """Run _ASK_CHILD in this test's state dir and SIGKILL it."""
    child = subprocess.Popen([sys.executable, "-c", _ASK_CHILD, ROOT, approval_id],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    test.addCleanup(lambda: child.poll() is None and child.kill())
    if child.stdout.readline().strip() != "ready":
        child.kill()
        test.fail(child.communicate(timeout=30)[1])
    test.assertTrue(an.pending_is_open("tg-pending", approval_id))     # it waits
    child.kill()
    child.communicate(timeout=30)


# ---------------------------------------------------------------------------
# AS3-1 - the lock decides, not the clock
# ---------------------------------------------------------------------------

class TestHeldMarkerIgnoresTheClock(base._TelegramFixture):
    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)
        self.cfg = self._tg_cfg()

    def test_a_waiting_ask_stays_open_after_a_clock_jump(self):
        an.write_tg_pending(A, "Drop prod DB?", 30)
        an.remember_tg_question_message(A, 10)
        later = time.time() + 600                  # the lid was closed for 10 minutes
        with unittest.mock.patch.object(an.time, "time", lambda: later):
            self.assertTrue(an.pending_is_open("tg-pending", A))
            before = len(self.tg.requests)
            an.handle_bot_update(self.cfg, {"update_id": 1, "callback_query": {
                "id": "cq", "data": f"agentbell|{A}|approved",
                "message": {"message_id": 10, "chat": {"id": 42}, "text": "Q?"}}})
        self.assertEqual(an.read_tg_answer(A), "approved")
        methods = [r["method"] for r in self.tg.requests[before:]]
        self.assertEqual(methods, ["answerCallbackQuery", "editMessageText"])
        self.assertNotIn("text", self.tg.requests[before]["body"])        # not "expired"

    def test_a_typed_yes_after_resume_does_not_approve_a_newer_ask(self):
        """The verify repro: A waits across a suspend, C is asked, "yes"."""
        offset = [0.0]
        real = time.time
        results = {}

        def ask(key, question):
            results[key] = an.run_ask(self.cfg, question, timeout_seconds=30,
                                      print_status=False)

        def sent():
            return [r for r in self.tg.requests if r["method"] == "sendMessage"]
        with unittest.mock.patch.object(an.time, "time", lambda: real() + offset[0]), \
                contextlib.redirect_stderr(io.StringIO()):
            first = threading.Thread(target=ask, args=("a", "Drop prod DB?"), daemon=True)
            before = len(sent())
            first.start()
            _wait_for(lambda: len(sent()) > before and _raw(
                "tg-pending", re.search(r"ID: ([0-9a-f]+)", sent()[-1]["body"]["text"])
                .group(1)).get("question_message_id"))
            a_id = re.search(r"ID: ([0-9a-f]+)", sent()[-1]["body"]["text"]).group(1)
            offset[0] = 600
            second = threading.Thread(target=ask, args=("c", "Show git log?"), daemon=True)
            second.start()
            _wait_for(lambda: len(sent()) > before + 1 and _raw(
                "tg-pending", re.search(r"ID: ([0-9a-f]+)", sent()[-1]["body"]["text"])
                .group(1)).get("question_message_id"))
            c_id = re.search(r"ID: ([0-9a-f]+)", sent()[-1]["body"]["text"]).group(1)
            self.assertEqual(sorted(m["approval_id"] for m in an.pending_markers("tg-pending")
                                    if not m.get("closed")), sorted([a_id, c_id]))
            last = self.tg._message_id
            an.handle_bot_update(self.cfg, {"update_id": 2, "message": {
                "message_id": last + 1, "chat": {"id": 42}, "text": "yes"}})
            self.assertIsNone(an.read_tg_answer(c_id))
            self.assertIn(TWO_OPEN, sent()[-1]["body"]["text"])
            an.write_tg_answer(a_id, "denied")
            an.write_tg_answer(c_id, "denied")
            first.join(timeout=15)
            second.join(timeout=15)
        self.assertTrue(results["a"]["denied"])
        self.assertTrue(results["c"]["denied"])

    def test_a_killed_ask_ends_at_once(self):
        _killed_ask(self, D)
        self.assertFalse(an.pending_is_open("tg-pending", D))
        [tombstone] = an.pending_markers("tg-pending")
        self.assertEqual((tombstone["closed"], tombstone["answered"]), (True, False))
        self.assertLessEqual(tombstone["ended"], time.time())
        self.assertEqual(tombstone["expires"], tombstone["ended"] + KEPT)
        self.assertEqual(_raw("tg-pending", D)["closed"], True)     # written for all


class TestMarkerLockDetails(unittest.TestCase):
    def setUp(self):
        _clean_state()
        self.addCleanup(_clean_state)

    def test_a_probe_in_the_way_only_delays_the_ask(self):
        real, calls = an._lock_bot_fd, []

        def busy_once(fd, unlock=False, shared=False):
            calls.append(unlock)
            if len(calls) == 1:
                raise OSError(errno.EAGAIN, "busy")
            return real(fd, unlock=unlock, shared=shared)
        with unittest.mock.patch.object(an, "_lock_bot_fd", busy_once):
            an.write_ntfy_pending(A, "Q?", 60)
        self.assertTrue(an.pending_is_open("ntfy-pending", A))

    def test_a_marker_that_cannot_be_locked_fails_the_ask_visibly(self):
        with unittest.mock.patch.object(an, "_lock_bot_fd",
                                        side_effect=OSError(errno.ENOLCK, "no locks")):
            with self.assertRaisesRegex(RuntimeError, "cannot lock the approval marker"):
                an.write_ntfy_pending(A, "Q?", 60)
        self.assertFalse(os.path.exists(an._pending_path("ntfy-pending", A)))

    def test_an_ask_that_takes_its_lock_while_it_is_read_is_left_alone(self):
        """The lock was free before the content was read, and taken since:
        a live marker is never replaced or deleted."""
        an.write_ntfy_pending(A, "Q?", 60)
        path = an._pending_path("ntfy-pending", A)
        an._write_held_marker(path, {"approval_id": A, "created": time.time()})  # no expiry
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        with unittest.mock.patch.object(an, "_lock_held", side_effect=[False, True]):
            [marker] = an.pending_markers("ntfy-pending")
        self.assertIsNone(marker.get("closed"))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)

    def test_a_tombstone_that_cannot_be_written_is_reported(self):
        an.write_ntfy_pending(A, "Q?", 60)
        an._let_go(an._pending_path("ntfy-pending", A))            # killed
        err = io.StringIO()
        with unittest.mock.patch.object(an.os, "replace", side_effect=OSError(errno.EIO, "io")), \
                contextlib.redirect_stderr(err):
            [marker] = an.pending_markers("ntfy-pending")
        self.assertEqual((marker["closed"], marker["answered"]), (True, False))
        self.assertIn("cannot close the question of an ask that ended (OSError)",
                      err.getvalue())
        self.assertEqual([e for e in os.listdir(an._pending_dir("ntfy-pending"))
                          if e.endswith(".tmp")], [])


# ---------------------------------------------------------------------------
# AS3-2 - a short grace; nothing at all when nothing reached the phone
# ---------------------------------------------------------------------------

class TestEndedAskGrace(unittest.TestCase):
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
        self.cfg = base.make_config(self.ntfy.url, topic="as5grace")

    def _questions(self):
        return [p for p in self.ntfy.posts.get("as5grace", []) if "ID: " in p["body"]]

    def _reply(self, text):
        urllib.request.urlopen(urllib.request.Request(
            f"{self.ntfy.url}/as5grace-responses", method="POST",
            data=text.encode("utf-8"))).read()

    def test_an_unanswered_tombstone_counts_for_the_grace_not_the_timeout(self):
        an.write_ntfy_pending(A, "Q?", 3600)
        an.close_pending("ntfy-pending", A, answered=False)
        tombstone = _raw("ntfy-pending", A)
        self.assertLessEqual(tombstone["ended"], time.time())
        self.assertGreater(tombstone["ended"], time.time() - 10)
        self.assertEqual(tombstone["expires"], tombstone["ended"] + KEPT)

    def test_a_refused_question_leaves_no_marker(self):
        """The server rejected the question (HTTP 400, not retried): nothing
        is on the phone, and the retry's typed "yes" is its answer. A 5xx
        proves nothing (audit 7, AF-2)."""
        self.ntfy.post_fail_status = 400
        self.ntfy.post_503_count = 1
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                an.run_ask(self.cfg, "Deploy prod?", timeout_seconds=3600, print_status=False)
        self.assertEqual(an.pending_markers("ntfy-pending"), [])
        results = {}
        thread = threading.Thread(target=lambda: results.update(r=an.run_ask(
            self.cfg, "Deploy prod?", timeout_seconds=20, print_status=False)), daemon=True)
        thread.start()
        _wait_for(lambda: self._questions())
        approval_id = re.search(r"ID: ([0-9a-f]+)", self._questions()[0]["body"]).group(1)
        _wait_for(lambda: _raw("ntfy-pending", approval_id).get("question_time"))
        self._reply("yes")
        thread.join(timeout=15)
        self.assertTrue(results["r"]["approved"], results)

    def test_a_gateway_error_is_no_proof(self):
        exc = an.TransientError("HTTP 504")
        for code, refused in ((504, False), (502, False), (503, False), (500, False),
                              (520, False), (408, False), (302, False),
                              (400, True), (403, True), (413, True), (429, True)):
            exc.__cause__ = urllib.error.HTTPError("http://x", code, "", {}, io.BytesIO())
            self.addCleanup(exc.__cause__.close)
            self.assertEqual(an._refused(exc), refused, code)
        self.assertFalse(an._refused(an.TransientError("timeout talking to ntfy")))
        self.assertTrue(an._refused(an.TelegramRefused("Telegram error: boom")))

    def test_one_ambiguous_attempt_keeps_the_marker(self):
        """A timeout, then 503s: the first copy may be stored."""
        attempts = []

        def publish(*_args, **_kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise an.TransientError("timeout talking to ntfy")
            exc = an.TransientError("HTTP 503")
            exc.__cause__ = urllib.error.HTTPError("http://x", 503, "", {}, io.BytesIO())
            self.addCleanup(exc.__cause__.close)
            raise exc
        with unittest.mock.patch.object(an.NtfyChannel, "publish", publish), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                an.run_ask(self.cfg, "Deploy prod?", timeout_seconds=20, print_status=False)
        [tombstone] = an.pending_markers("ntfy-pending")
        self.assertEqual((tombstone["closed"], tombstone["answered"],
                          tombstone["question_time"]), (True, False, None))

    @unittest.skipIf(os.name == "nt", "SIGTERM cannot be caught on Windows")
    def test_sigterm_ends_the_ask_through_its_cleanup(self):
        sandbox = tempfile.mkdtemp(prefix="agentbell-as5-")
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        env = dict(os.environ)
        for key in ("AGENTBELL_CONFIG", an.LICENSE_ENV):
            env.pop(key, None)
        env[an.CONFIG_DIR_ENV] = os.path.join(sandbox, "config")
        env[an.STATE_DIR_ENV] = state = os.path.join(sandbox, "state")
        os.makedirs(env[an.CONFIG_DIR_ENV])
        config = os.path.join(env[an.CONFIG_DIR_ENV], "config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump(self.cfg.data, fh)
        os.chmod(config, 0o600)
        child = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "agentbell.py"), "ask", "Deploy prod?",
             "--timeout", "300", "--json"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        _wait_for(lambda: self._questions() or child.poll() is not None)
        approval_id = re.search(r"ID: ([0-9a-f]+)", self._questions()[0]["body"]).group(1)
        marker = os.path.join(state, "ntfy-pending", f"{approval_id}.json")

        def published():
            try:
                with open(marker, encoding="utf-8") as fh:
                    return "question_time" in json.load(fh)
            except (OSError, ValueError):
                return False
        _wait_for(published)
        child.send_signal(signal.SIGTERM)
        child.communicate(timeout=30)
        self.assertEqual(child.returncode, 128 + signal.SIGTERM)
        with open(marker, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual((data["closed"], data["answered"]), (True, False))
        self.assertLessEqual(data["ended"], time.time())
        self.assertEqual(data["expires"], data["ended"] + KEPT)


class TestTelegramRefusal(base._TelegramFixture):
    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)
        self.old_backoff = an.RETRY_BACKOFF_SECONDS
        an.RETRY_BACKOFF_SECONDS = (0.01, 0.01)
        self.addCleanup(setattr, an, "RETRY_BACKOFF_SECONDS", self.old_backoff)

    def test_telegram_ok_false_leaves_no_telegram_marker(self):
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        broken = base.MockTelegram(fail_send=True)
        self.addCleanup(broken.stop)
        cfg = self._tg_cfg(ntfy_url=ntfy.url, channels=("ntfy", "telegram"))
        results = {}
        with unittest.mock.patch.object(an, "TG_API_BASE", broken.url), \
                contextlib.redirect_stderr(io.StringIO()):
            thread = threading.Thread(target=lambda: results.update(r=an.run_ask(
                cfg, "Deploy?", timeout_seconds=20, print_status=False)), daemon=True)
            thread.start()
            _wait_for(lambda: any(r["method"] == "sendMessage" for r in broken.requests))
            _wait_for(lambda: any("ID: " in p["body"] for p in ntfy.posts.get("tgtopic", [])))
            body = next(p["body"] for p in ntfy.posts["tgtopic"] if "ID: " in p["body"])
            approval_id = re.search(r"ID: ([0-9a-f]+)", body).group(1)
            _wait_for(lambda: not os.path.exists(an._pending_path("tg-pending", approval_id)),
                      message="the refused Telegram question kept its marker")
            self.assertTrue(thread.is_alive())                       # ntfy carries it
            urllib.request.urlopen(urllib.request.Request(
                f"{ntfy.url}/tgtopic-responses", method="POST",
                data=f"APPROVED {approval_id}".encode())).read()
            thread.join(timeout=15)
        self.assertTrue(results["r"]["approved"])
        self.assertEqual(an.pending_markers("tg-pending"), [])


# ---------------------------------------------------------------------------
# AS3-3 - backlog and bursts
# ---------------------------------------------------------------------------

class TestBotRefusalNotices(base._TelegramFixture):
    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)
        self.cfg = self._tg_cfg()
        self.tg.updates.clear()

    def _message(self, message_id, text="old chat message"):
        return {"update_id": message_id, "message": {
            "message_id": message_id, "chat": {"id": 42}, "text": f"{text} {message_id}"}}

    def _notices(self, before):
        return [r["body"]["text"] for r in self.tg.requests[before:]
                if r["method"] == "sendMessage"]

    def test_the_backlog_after_a_start_is_only_logged(self):
        an.write_tg_pending(A, "A?", 300)          # its send failed: no message id
        for message_id in range(100, 110):
            self.tg.queue_update(self._message(message_id))
        session = {"backlog": True, "notices": {}}
        timeouts = []
        real = an.TelegramChannel.get_updates

        def get_updates(token, offset=None, timeout=25):
            timeouts.append(timeout)
            return real(token, offset=offset, timeout=timeout)
        before, mark = len(self.tg.requests), _history_mark()
        with unittest.mock.patch.object(an.TelegramChannel, "get_updates", get_updates):
            offset = an.bot_poll_once(self.cfg, poll_timeout=1, session=session)
            self.assertTrue(session["backlog"])
            an.bot_poll_once(self.cfg, offset=offset, poll_timeout=1, session=session)
        self.assertFalse(session["backlog"])       # the first empty answer ends it
        self.assertEqual(timeouts, [0, 0])         # a backlog poll does not wait
        self.assertEqual(self._notices(before), [])
        stale = _stale(mark)
        self.assertEqual(len(stale), 10)
        self.assertTrue(all(r["notice"].startswith("not sent: chat backlog") for r in stale))
        # a live reply after the backlog is announced again
        self.tg.queue_update(self._message(111, "yes"))
        an.bot_poll_once(self.cfg, offset=offset, poll_timeout=1, session=session)
        self.assertEqual(len(self._notices(before)), 1)

    def test_refusal_notices_are_rate_limited_per_reason(self):
        an.write_tg_pending(A, "A?", 300)
        an.remember_tg_question_message(A, 10)
        an.write_tg_pending(B, "B?", 300)
        an.remember_tg_question_message(B, 11)
        session = {"backlog": False, "notices": {}}
        before, mark = len(self.tg.requests), _history_mark()
        for message_id in (20, 21, 22):
            an.handle_bot_update(self.cfg, self._message(message_id, "yes"), session)
        self.assertEqual(len(self._notices(before)), 1)
        self.assertEqual([r.get("notice") for r in _stale(mark)],
                         ["sent", "not sent: one notice per reason a minute",
                          "not sent: one notice per reason a minute"])
        # another reason is told at once
        an.close_pending("tg-pending", A, answered=True)
        an.close_pending("tg-pending", B, answered=True)
        an.handle_bot_update(self.cfg, self._message(23, "yes"), session)
        self.assertIn("no approval question is open", self._notices(before)[-1])
        self.assertEqual(len(self._notices(before)), 2)
        # and the first one again after a minute
        session["notices"][TWO_OPEN] -= an.BOT_NOTICE_INTERVAL_SECONDS + 1
        an.write_tg_pending(C, "C?", 300)
        an.remember_tg_question_message(C, 24)
        an.write_tg_pending(D, "D?", 300)
        an.remember_tg_question_message(D, 25)
        an.handle_bot_update(self.cfg, self._message(26, "yes"), session)
        self.assertEqual(len(self._notices(before)), 3)

    def test_the_bot_starts_in_its_backlog(self):
        sessions = []

        def poll(cfg, offset=None, poll_timeout=25, session=None):
            sessions.append(dict(session))
            raise KeyboardInterrupt
        with unittest.mock.patch.object(an, "bot_poll_once", poll), \
                contextlib.redirect_stdout(io.StringIO()):
            an.run_bot(self.cfg, poll_timeout=1)
        self.assertEqual(sessions, [{"backlog": True, "notices": {}}])
        with contextlib.suppress(OSError):
            os.remove(an._bot_state_path())


# ---------------------------------------------------------------------------
# AS3-4 - an answer on one channel settles only that channel
# ---------------------------------------------------------------------------

class TestAnswerOnTheOtherChannel(base._TelegramFixture):
    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)

    def test_a_telegram_answer_leaves_the_ntfy_question_counting(self):
        ntfy = base.MockNtfy()
        self.addCleanup(ntfy.stop)
        cfg = self._tg_cfg(ntfy_url=ntfy.url, channels=("ntfy", "telegram"))
        cfg.data["ntfy"]["topic"] = "as5xch"
        results = {}

        def ask(key, question):
            results[key] = an.run_ask(cfg, question, timeout_seconds=20, print_status=False)

        def question_id(text):
            _wait_for(lambda: any(text in p["body"] for p in ntfy.posts.get("as5xch", [])))
            body = next(p["body"] for p in ntfy.posts["as5xch"] if text in p["body"])
            approval_id = re.search(r"ID: ([0-9a-f]+)", body).group(1)
            _wait_for(lambda: _raw("ntfy-pending", approval_id).get("question_time"))
            return approval_id

        def reply(text):
            urllib.request.urlopen(urllib.request.Request(
                f"{ntfy.url}/as5xch-responses", method="POST", data=text.encode())).read()
        with unittest.mock.patch.object(an, "bot_heartbeat_fresh", lambda *a: True), \
                contextlib.redirect_stderr(io.StringIO()):
            first = threading.Thread(target=ask, args=("a", "Drop prod DB?"), daemon=True)
            first.start()
            a_id = question_id("Drop prod DB?")
            an.write_tg_answer(a_id, "denied")
            first.join(timeout=15)
            self.assertEqual((results["a"]["denied"], results["a"]["channel"]),
                             (True, "telegram"))
            self.assertEqual(_raw("tg-pending", a_id)["answered"], True)
            self.assertEqual(_raw("ntfy-pending", a_id)["answered"], False)
            second = threading.Thread(target=ask, args=("b", "Show git log?"), daemon=True)
            second.start()
            b_id = question_id("Show git log?")
            reply("yes")                          # may be meant for A's ntfy question
            _wait_for(lambda: any("was not used" in p["body"]
                                  for p in ntfy.posts["as5xch"]))
            self.assertTrue(second.is_alive())
            reply(f"APPROVED {b_id}")
            second.join(timeout=15)
        self.assertTrue(results["b"]["approved"])


# ---------------------------------------------------------------------------
# AS3-5 - a button on a killed ask
# ---------------------------------------------------------------------------

class TestButtonOnAKilledAsk(base._TelegramFixture):
    def setUp(self):
        super().setUp()
        _clean_state()
        self.addCleanup(_clean_state)

    def test_the_button_says_expired_and_edits_nothing(self):
        _killed_ask(self, D)
        before, mark = len(self.tg.requests), _history_mark()
        an.handle_bot_update(self._tg_cfg(), {"update_id": 1, "callback_query": {
            "id": "cq", "data": f"agentbell|{D}|approved",
            "message": {"message_id": 5, "chat": {"id": 42}, "text": "Deploy?"}}})
        self.assertEqual([(r["method"], r["body"].get("text"))
                          for r in self.tg.requests[before:]],
                         [("answerCallbackQuery", "This question has expired.")])
        self.assertIsNone(an.read_tg_answer(D))
        self.assertEqual([r.get("answer") for r in _stale(mark)], ["approved"])


# ---------------------------------------------------------------------------
# `ask` leaves through its cleanup on SIGTERM / SIGHUP
# ---------------------------------------------------------------------------

class TestAskSignals(unittest.TestCase):
    def _run(self, **outcome):
        seen = {}

        def run_ask(*_args, **_kwargs):
            seen["term"] = signal.getsignal(signal.SIGTERM)
            if hasattr(signal, "SIGHUP"):
                seen["hup"] = signal.getsignal(signal.SIGHUP)
            if "raise_signal" in outcome:
                seen["term"](signal.SIGTERM, None)
            return {"approved": True, "answer": None, "denied": False, "timeout": False}
        args = unittest.mock.Mock(message="Q?", timeout=5, yes_label=None, no_label=None,
                                  no_buttons=False, json=True, channel=None)
        with unittest.mock.patch.object(an, "run_ask", run_ask), \
                unittest.mock.patch.object(an, "Config", lambda: None), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                an.cmd_ask(args)
        return seen, caught.exception.code

    def test_sigterm_exits_through_the_cleanup_and_the_handler_is_restored(self):
        previous = signal.getsignal(signal.SIGTERM)
        seen, code = self._run()
        self.assertEqual(code, 0)
        self.assertNotIn(seen["term"], (previous, signal.SIG_DFL, signal.SIG_IGN))
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        _, code = self._run(raise_signal=True)
        self.assertEqual(code, 128 + signal.SIGTERM)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "no SIGHUP")
    def test_an_ignored_sighup_stays_ignored(self):
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)       # nohup
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        seen, _ = self._run()
        self.assertEqual(seen["hup"], signal.SIG_IGN)


if __name__ == "__main__":
    unittest.main()
