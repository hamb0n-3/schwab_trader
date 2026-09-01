from __future__ import annotations

import logging
import threading
import time

import httpx

from config import Config

logger = logging.getLogger("schwab_bot.notify")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_TIMEOUT = 5.0


class Notifier:

    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        # message -> monotonic time of last send, for the dedup cooldown.
        self._last_sent: dict[str, float] = {}
        # Shared client: connection reuse avoids a fresh TCP+TLS handshake per
        # notification. Thread-safe; no connection is opened until first use.
        self._client = httpx.Client(timeout=_TIMEOUT)

    # ---- public API ----

    @property
    def signal_enabled(self) -> bool:
        return bool(self.config.signal_webhook_url)

    @property
    def pushover_enabled(self) -> bool:
        return bool(self.config.pushover_token and self.config.pushover_user)

    def info(self, message: str) -> None:
        key = f"I:{message}"
        if self._recently_sent(key):
            return
        if self._send_signal(message):
            self._stamp(key)

    def urgent(self, message: str) -> None:
        key = f"U:{message}"
        if self._recently_sent(key):
            return
        # Stamp only when SOMETHING actually reached the user — otherwise an
        # outage (the very thing urgent() exists to report) would suppress
        # the retry once it stamped a failed send.
        delivered = self._send_pushover(message)
        delivered = self._send_signal(f"🚨 {message}") or delivered
        if delivered:
            self._stamp(key)

    # ---- internals ----

    def _recently_sent(self, key: str) -> bool:
        cooldown = self.config.notify_cooldown_seconds
        if cooldown <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < cooldown:
                logger.debug("Notification suppressed (cooldown): %s", key)
                return True
        return False

    def _stamp(self, key: str) -> None:
        if self.config.notify_cooldown_seconds <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._last_sent[key] = now
            # Keep the dedup map from growing unbounded on a long run.
            if len(self._last_sent) > 500:
                for k, ts in list(self._last_sent.items()):
                    if now - ts >= self.config.notify_cooldown_seconds:
                        del self._last_sent[k]

    def _send_signal(self, message: str) -> bool:
        if not self.signal_enabled:
            return False
        payload: dict = {"message": message}
        # signal-cli-rest-api's /v2/send needs the sender + recipients; a
        # generic relay webhook usually wants just {"message": ...}.
        if self.config.signal_number:
            payload["number"] = self.config.signal_number
        if self.config.signal_recipients:
            payload["recipients"] = self.config.signal_recipients
        try:
            resp = self._client.post(self.config.signal_webhook_url,
                                     json=payload)
            if resp.status_code >= 300:
                logger.warning("Signal webhook returned %s: %s",
                               resp.status_code, resp.text[:300])
                return False
            return True
        except Exception as e:
            logger.warning("Signal webhook send failed: %s", e)
            return False

    def _send_pushover(self, message: str) -> bool:
        if not self.pushover_enabled:
            return False
        data = {
            "token": self.config.pushover_token,
            "user": self.config.pushover_user,
            "title": "schwab_trader",
            "message": message,
            "priority": self.config.pushover_priority,
        }
        # Emergency priority requires retry/expire (Pushover API contract).
        if self.config.pushover_priority == 2:
            data["retry"] = 60
            data["expire"] = 3600
        try:
            resp = self._client.post(PUSHOVER_URL, data=data)
            if resp.status_code >= 300:
                logger.warning("Pushover returned %s: %s",
                               resp.status_code, resp.text[:300])
                return False
            return True
        except Exception as e:
            logger.warning("Pushover send failed: %s", e)
            return False
