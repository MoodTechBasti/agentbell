"""Audit round 3, cluster hook-runtime: HR-1, HR-2, HR-4, HR-5, HR-6 and the
S8 leftover (clamp_message sanitizes on its own).

Run with: python3 -m unittest discover -s tests
"""

import io
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_agentbell as base  # noqa: E402  (module import: sets up the sandbox dirs)

an = base.an


def _clear_queues():
    for name in ("queue", "deferred"):
        shutil.rmtree(os.path.join(an.state_dir(), name), ignore_errors=True)


def _queued_items():
    return [item for _, item in an._read_item_files(an.queue_dir())]


class _FakeClock:
    """agentbell's `time` with a clock that only moves when told to."""

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


def _records(**match):
    return [r for r in an.read_history(limit=0)
            if all(r.get(k) == v for k, v in match.items())]


class TestBudgetHoldsOnStalls(unittest.TestCase):
    """HR-1: a stall no socket timeout sees still ends in the queue, in time."""

    BUDGET = 1.0

    def setUp(self):
        _clear_queues()
        self.release = threading.Event()
        # the abandoned try is still blocked when the test ends: let it go
        self.addCleanup(self.release.set)
        patcher = unittest.mock.patch.object(an, "HOOK_SEND_BUDGET_SECONDS", self.BUDGET)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_worst_case_still_fits_below_the_host_timeout(self):
        # Kimi kills a hook after 10s; start-up and the queue write need 2s.
        # Worst case: a 0.5s minimum try plus 0.5s grace for its own timeouts.
        self.assertLessEqual(an.HOOK_SEND_BUDGET_SECONDS + 0.5 + 0.5, 10 - 2)

    def _stall(self, *args, **kwargs):
        # without the budget the hook waits here until the host kills it
        self.release.wait(10)
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    def _hook(self, cfg, agent):
        start = time.monotonic()
        result = an.run_hook(cfg, "run_completed", agent)
        return result, time.monotonic() - start

    def test_a_stalled_dns_lookup_is_queued_within_the_budget(self):
        cfg = base.make_config("http://hr3-stalled-dns.invalid", topic="hr3dnstopic")
        with unittest.mock.patch.object(socket, "getaddrinfo", self._stall):
            result, elapsed = self._hook(cfg, "hr3-dns")
        self.assertLess(elapsed, self.BUDGET + 1.5)
        self.assertEqual(result.get("queued"), ["ntfy"])
        items = _queued_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["channels"], ["ntfy"])
        self.assertIn("send time budget", items[0]["last_error"]["ntfy"])
        rec = _records(agent="hr3-dns")[0]
        self.assertEqual(rec["event"], "queued")
        self.assertEqual(rec["queued_channels"], ["ntfy"])

    def test_a_send_that_never_answers_is_queued_within_the_budget(self):
        # a server trickling one byte per read timeout looks like this
        cfg = base.make_config("http://127.0.0.1:9", topic="hr3tricktopic")

        def publish(cfg, channel, item, timeout=10.0):
            self._stall()

        with unittest.mock.patch.object(an, "_publish_channel", publish):
            result, elapsed = self._hook(cfg, "hr3-trickle")
        self.assertLess(elapsed, self.BUDGET + 1.5)
        self.assertEqual(result.get("queued"), ["ntfy"])
        self.assertEqual(len(_queued_items()), 1)


class TestOsToastUnderTheBudget(unittest.TestCase):
    """HR-2 / HR-6: the toast gets what is left of the budget, and a toast
    the budget cuts short is queued - it is not "unavailable"."""

    def setUp(self):
        _clear_queues()
        self.clock = _FakeClock()
        self.cfg = base.make_config("http://127.0.0.1:9", topic="hr3ostopic")
        self.timeouts = []

    def _slow_helper(self, argv, **kwargs):
        self.timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    def _linux_toast(self):
        return (unittest.mock.patch.object(an.platform, "system", return_value="Linux"),
                unittest.mock.patch.object(an.shutil, "which", return_value="/bin/notify-send"),
                unittest.mock.patch.object(an.subprocess, "run", self._slow_helper))

    def test_a_toast_after_a_slow_push_gets_the_rest_and_is_queued_when_cut(self):
        self.cfg.data["channels"] = ["ntfy", "os"]
        real = an._publish_channel

        def publish(cfg, channel, item, timeout=10.0):
            if channel == "ntfy":
                self.clock.advance(5.5)       # slow, but it arrives
                return {"channel": "ntfy", "ok": True}
            return real(cfg, channel, item, timeout)

        system, which, run = self._linux_toast()
        with self.clock.patch(), system, which, run, \
                unittest.mock.patch.object(an, "_publish_channel", publish):
            result = an.run_hook(self.cfg, "run_failed", "hr3-os")
        self.assertEqual([r["channel"] for r in result["results"]], ["ntfy"])
        self.assertEqual(result.get("queued"), ["os"])
        # only the 0.5s left of the budget, not the hook's 5s per try
        self.assertTrue(self.timeouts)
        self.assertTrue(all(t <= 0.5 for t in self.timeouts), self.timeouts)
        items = _queued_items()
        self.assertEqual([i["channels"] for i in items], [["os"]])
        self.assertIn("did not finish", items[0]["last_error"]["os"])
        rec = _records(agent="hr3-os")[0]
        self.assertNotIn("errors", rec)
        self.assertEqual(rec["queued_channels"], ["os"])

    def test_a_helper_that_times_out_without_a_budget_is_not_retried(self):
        self.cfg.data["channels"] = ["os"]
        system, which, run = self._linux_toast()
        with system, which, run:
            result = an.send_notification(self.cfg, "hr3 no budget toast")
        self.assertEqual(len(self.timeouts), 1)
        self.assertEqual(result["errors"], ["os: OS notification helper did not finish within 10s"])
        self.assertNotIn("queued", result)
        self.assertEqual(_queued_items(), [])


class TestHookErrorInHistoryTable(unittest.TestCase):
    """HR-4: the table the stderr line points to shows who failed and why."""

    def test_the_history_table_shows_agent_and_reason(self):
        args = an.build_parser().parse_args(["hook", "run_completed", "--agent", "hr3-err"])
        with unittest.mock.patch.object(an, "run_hook", side_effect=ValueError("hr3 boom")), \
                unittest.mock.patch.object(an, "read_hook_payload", return_value={}), \
                unittest.mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                an.cmd_hook(args)
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "stdout", out):
            an.cmd_history(an.build_parser().parse_args(["history", "--limit", "0"]))
        rows = [line for line in out.getvalue().splitlines() if "hook.error" in line]
        self.assertIn("hr3-err run_completed: ValueError: hr3 boom", rows[-1])


class TestVerifyAfterQueuedDelivery(unittest.TestCase):
    """HR-5: a queued hook push the queue delivered counts as delivered."""

    def setUp(self):
        _clear_queues()
        self.cfg = base.make_config("http://127.0.0.1:9", topic="hr3verifytopic")

    def _hook(self, agent, event, publish):
        with unittest.mock.patch.object(an, "RETRY_ATTEMPTS", 1), \
                unittest.mock.patch.object(an, "_publish_channel", publish):
            return an.run_hook(self.cfg, event, agent)

    @staticmethod
    def _down(cfg, channel, item, timeout=10.0):
        raise an.TransientError("hr3 offline")

    @staticmethod
    def _up(cfg, channel, item, timeout=10.0):
        return {"channel": channel, "ok": True}

    def test_a_push_the_queue_delivered_is_no_longer_held(self):
        self._hook("hr3-held", "run_completed", self._down)
        self._hook("hr3-held", "input_required", self._down)
        obs = an.hook_observations(an.read_history(limit=0), 3600)["hr3-held"]
        self.assertEqual((obs["held"], obs["delivered"]), (2, 0))
        with unittest.mock.patch.object(an, "_publish_channel", self._up):
            an.drain_queue(self.cfg)
        obs = an.hook_observations(an.read_history(limit=0), 3600)["hr3-held"]
        self.assertEqual((obs["count"], obs["held"], obs["delivered"]), (2, 0, 2))
        self.assertIn("2 delivered", an._obs_sentence(obs))

    def test_a_partial_delivery_is_not_counted_twice(self):
        # ntfy arrived, the toast was queued: already "delivered", and the
        # queue delivering the toast later must not re-count it
        self.cfg.data["channels"] = ["ntfy", "os"]

        def half(cfg, channel, item, timeout=10.0):
            if channel == "os":
                raise an.TransientError("hr3 toast busy")
            return {"channel": channel, "ok": True}

        with unittest.mock.patch.object(an, "auto_drain"):
            self._hook("hr3-half", "run_failed", half)
        self._hook("hr3-half", "input_required", self._down)
        with unittest.mock.patch.object(an, "_publish_channel", self._up):
            an.drain_queue(self.cfg)
        obs = an.hook_observations(an.read_history(limit=0), 3600)["hr3-half"]
        self.assertEqual((obs["count"], obs["held"], obs["delivered"]), (2, 0, 2))


class TestClampSanitizes(unittest.TestCase):
    """S8: every channel body goes through clamp_message, which now makes it
    sendable itself - no caller can forget the second step."""

    def test_nul_and_surrogates_are_handled_by_clamp_message(self):
        self.assertEqual(an.clamp_message("a\x00b\udcff"), "ab�")
        self.assertEqual(an.clamp_message("\x00" * 5, 3), "")


if __name__ == "__main__":
    unittest.main()
