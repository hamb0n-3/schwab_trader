from __future__ import annotations

import json
import logging
import os
import re

import httpx

from signals.edgar_client import RateLimiter

logger = logging.getLogger("schwab_bot.congress.client")

HOUSE_BASE = "https://disclosures-clerk.house.gov"
SENATE_BASE = "https://efdsearch.senate.gov"

DEFAULT_USER_AGENT = "schwab-trader-congress/1.0 (personal research bot)"

_CSRF_FORM_RE = re.compile(
    r'name="csrfmiddlewaretoken"\s+value="([^"]+)"'
)


class CongressClient:
    def __init__(self, user_agent: str = DEFAULT_USER_AGENT,
                 max_per_second: float = 2.0, timeout: float = 30.0):
        self._limiter = RateLimiter(max_per_second)
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent or DEFAULT_USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        self._senate_ready = False

    # ---------------- generic fetches (House + Senate detail pages) ----------------

    def get_bytes(self, url: str) -> bytes:
        self._limiter.wait()
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.content

    def get_bytes_cached(self, url: str, cache_path: str) -> bytes:
        meta_path = cache_path + ".meta"
        cached: bytes | None = None
        headers: dict[str, str] = {}
        try:
            if os.path.exists(cache_path) and os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                if meta.get("url") == url:
                    if meta.get("etag"):
                        headers["If-None-Match"] = meta["etag"]
                    if meta.get("last_modified"):
                        headers["If-Modified-Since"] = meta["last_modified"]
                    if headers:
                        with open(cache_path, "rb") as f:
                            cached = f.read()
        except Exception:
            logger.warning("Unreadable HTTP cache %s; refetching.", cache_path)
            cached, headers = None, {}

        self._limiter.wait()
        resp = self._client.get(url, headers=headers)
        if resp.status_code == 304 and cached is not None:
            logger.info("Not modified: %s (using cached copy).", url)
            return cached
        resp.raise_for_status()
        try:
            with open(cache_path, "wb") as f:
                f.write(resp.content)
            with open(meta_path, "w") as f:
                json.dump({"url": url,
                           "etag": resp.headers.get("ETag", ""),
                           "last_modified": resp.headers.get("Last-Modified", "")},
                          f)
        except Exception:
            logger.warning("Could not write HTTP cache %s (continuing).",
                           cache_path)
        return resp.content

    def get_text(self, url: str) -> str:
        self._limiter.wait()
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.text

    # ---------------- Senate session ----------------

    def _ensure_senate_session(self) -> None:
        if self._senate_ready:
            return
        self._limiter.wait()
        home = self._client.get(f"{SENATE_BASE}/search/home/")
        home.raise_for_status()
        m = _CSRF_FORM_RE.search(home.text)
        if not m:
            raise RuntimeError(
                "Senate eFD home page had no csrfmiddlewaretoken — the "
                "handshake flow may have changed."
            )
        self._limiter.wait()
        agree = self._client.post(
            f"{SENATE_BASE}/search/home/",
            data={"prohibition_agreement": "1",
                  "csrfmiddlewaretoken": m.group(1)},
            headers={"Referer": f"{SENATE_BASE}/search/home/"},
        )
        agree.raise_for_status()
        if "csrftoken" not in self._client.cookies:
            raise RuntimeError("Senate eFD handshake did not yield a csrftoken "
                               "cookie.")
        self._senate_ready = True
        logger.info("Senate eFD session established.")

    def senate_report_data(self, start: int, length: int,
                           submitted_start_date: str) -> dict:
        for attempt in (1, 2):
            self._ensure_senate_session()
            csrf = self._client.cookies.get("csrftoken", "")
            self._limiter.wait()
            resp = self._client.post(
                f"{SENATE_BASE}/search/report/data/",
                data={
                    "start": str(start),
                    "length": str(length),
                    "report_types": "[11]",     # Periodic Transaction Reports
                    "filter_types": "[]",
                    "submitted_start_date": submitted_start_date,
                    "submitted_end_date": "",
                    "candidate_state": "",
                    "senator_state": "",
                    "office_id": "",
                    "first_name": "",
                    "last_name": "",
                    "csrfmiddlewaretoken": csrf,
                },
                headers={
                    "Referer": f"{SENATE_BASE}/search/",
                    "X-CSRFToken": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            if resp.status_code == 403 and attempt == 1:
                logger.warning("Senate eFD returned 403 — re-establishing the "
                               "session and retrying once.")
                self._senate_ready = False
                self._client.cookies.clear()
                continue
            resp.raise_for_status()
            try:
                return resp.json()
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"Senate eFD search returned non-JSON: {resp.text[:200]}"
                ) from e
        raise RuntimeError("unreachable")  # pragma: no cover

    # ---------------- lifecycle ----------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
