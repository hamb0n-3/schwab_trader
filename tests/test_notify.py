import os
import unittest

os.environ.setdefault("SCHWAB_API_KEY", "k")
os.environ.setdefault("SCHWAB_APP_SECRET", "s")
os.environ.setdefault("SCHWAB_DB_PATH", "")

import httpx  # noqa: E402
from config import Config  # noqa: E402
from notify import Notifier  # noqa: E402


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.text = "x"


def make_notifier(**env):
    base = {
        "SIGNAL_WEBHOOK_URL": "http://x/send",
        "PUSHOVER_TOKEN": "tok", "PUSHOVER_USER": "usr",
        "NOTIFY_COOLDOWN_SECONDS": "300",
    }
    base.update(env)
    for k, v in base.items():
        os.environ[k] = v
    return Notifier(Config())


class TestStampOnSuccess(unittest.TestCase):
    def patch(self, statuses):
        self._q = list(statuses)
        self.calls = 0

        def fake_post(_self, url, **kw):
            self.calls += 1
            s = self._q.pop(0) if self._q else 200
            if s == "raise":
                raise httpx.ConnectError("down")
            return _Resp(s)
        self._orig = httpx.Client.post
        httpx.Client.post = fake_post

    def tearDown(self):
        if hasattr(self, "_orig"):
            httpx.Client.post = self._orig

    def test_failed_send_is_retried_not_suppressed(self):
        self.patch(["raise", 200])      # first send fails, second succeeds
        n = make_notifier(PUSHOVER_TOKEN="", PUSHOVER_USER="")  # Signal only
        n.info("hello")                 # fails to send -> not stamped
        n.info("hello")                 # SAME message retried -> attempted again
        self.assertEqual(self.calls, 2)

    def test_successful_send_suppresses_identical_retry(self):
        self.patch([200, 200])
        n = make_notifier(PUSHOVER_TOKEN="", PUSHOVER_USER="")
        n.info("hello")                 # succeeds -> stamped
        n.info("hello")                 # within cooldown -> suppressed
        self.assertEqual(self.calls, 1)

    def test_non_2xx_counts_as_failure(self):
        self.patch([500, 200])
        n = make_notifier(PUSHOVER_TOKEN="", PUSHOVER_USER="")
        n.info("hello")                 # 500 -> not delivered, not stamped
        n.info("hello")                 # retried
        self.assertEqual(self.calls, 2)

    def test_urgent_stamps_if_any_channel_delivers(self):
        # Pushover fails, Signal mirror succeeds -> delivered -> stamped.
        self.patch(["raise", 200, 200, 200])
        n = make_notifier()
        n.urgent("halt")                # pushover raise, signal 200 -> stamped
        n.urgent("halt")                # suppressed (delivered last time)
        self.assertEqual(self.calls, 2)  # only the first urgent's two sends

    def test_cooldown_zero_disables_dedup(self):
        self.patch([200, 200])
        n = make_notifier(PUSHOVER_TOKEN="", PUSHOVER_USER="",
                          NOTIFY_COOLDOWN_SECONDS="0")
        n.info("hello")
        n.info("hello")
        self.assertEqual(self.calls, 2)


if __name__ == "__main__":
    unittest.main()
