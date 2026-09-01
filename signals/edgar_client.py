from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger("schwab_bot.edgar.client")

SEC_BASE = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"


class RateLimiter:

    def __init__(self, max_per_second: float):
        self._min_interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


class EdgarClient:
    def __init__(self, user_agent: str, max_per_second: float = 5.0, timeout: float = 20.0):
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "EDGAR requires a User-Agent with contact info, e.g. "
                "'MyBot/1.0 (you@example.com)'. SEC blocks requests without it."
            )
        self._ua = user_agent
        self._limiter = RateLimiter(max_per_second)
        # NOTE: do NOT pin a "Host" header here. httpx MERGES per-request
        # headers into client-level headers (it does not replace them), so a
        # client-level Host: www.sec.gov would be sent on EVERY request —
        # including to data.sec.gov (the submissions API), which returns 404 for
        # the wrong Host and silently zeroes out the whole insider feed. httpx
        # derives the correct Host from each URL's authority automatically.
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def get_text(self, url: str) -> str:
        self._limiter.wait()
        # httpx sets Host automatically per-URL; drop our static one for data.sec.gov
        headers = {"User-Agent": self._ua, "Accept-Encoding": "gzip, deflate"}
        resp = self._client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text

    def get_json(self, url: str):
        self._limiter.wait()
        headers = {"User-Agent": self._ua, "Accept-Encoding": "gzip, deflate"}
        resp = self._client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
