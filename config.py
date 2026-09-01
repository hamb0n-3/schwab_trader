from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is optional; env vars still work without it.
    pass


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


# The exact string a user must set in LIVE_CONFIRM to enable real-money trading.
# This is intentionally awkward to type so it can't be set by accident.
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"


@dataclass
class Config:
    # ---- Schwab API credentials (from the developer portal) ----
    # repr=False so a stray repr(CONFIG) / dataclass dump in a log or traceback
    # can't leak the credentials.
    api_key: str = field(
        default_factory=lambda: _get("SCHWAB_API_KEY", "") or "", repr=False)
    app_secret: str = field(
        default_factory=lambda: _get("SCHWAB_APP_SECRET", "") or "", repr=False)
    callback_url: str = field(
        default_factory=lambda: _get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182") or ""
    )
    token_path: str = field(
        default_factory=lambda: _get("SCHWAB_TOKEN_PATH", "./schwab_token.json") or ""
    )
    # Which linked account to trade. 0 = first account returned by the API.
    account_index: int = field(default_factory=lambda: _get_int("SCHWAB_ACCOUNT_INDEX", 0))

    # ---- Safety / mode ----
    # DRY_RUN True  -> validate via preview_order, log intent, DO NOT place orders.
    # DRY_RUN False -> place real orders (requires live_confirm to match the phrase).
    dry_run: bool = field(default_factory=lambda: _get_bool("DRY_RUN", True))
    live_confirm: str = field(default_factory=lambda: _get("LIVE_CONFIRM", "") or "")
    kill_switch_file: str = field(
        default_factory=lambda: _get("KILL_SWITCH_FILE", "./KILL_SWITCH") or ""
    )
    # Persists the calendar-day equity baseline so the daily-loss halt survives a
    # mid-day restart instead of resetting to a lower (already drawn-down) value.
    daily_state_file: str = field(
        default_factory=lambda: _get("DAILY_STATE_FILE", "./daily_state.json") or ""
    )

    # ---- Persistence (SQLite) ----
    # Local database for order/trade history, equity snapshots, signals,
    # backtests, and insider filings. Leave empty to disable DB persistence
    # (the bot then falls back to the JSON state files above).
    db_path: str = field(
        default_factory=lambda: _get("SCHWAB_DB_PATH", "./schwab_trader.db") or ""
    )

    # ---- Universe & strategy ----
    symbols: List[str] = field(
        default_factory=lambda: [
            s.strip().upper()
            for s in (_get("SYMBOLS", "AAPL,MSFT") or "").split(",")
            if s.strip()
        ]
    )
    poll_interval_seconds: int = field(
        default_factory=lambda: _get_int("POLL_INTERVAL_SECONDS", 60)
    )
    sma_fast: int = field(default_factory=lambda: _get_int("SMA_FAST", 20))
    sma_slow: int = field(default_factory=lambda: _get_int("SMA_SLOW", 50))

    # ---- Position sizing ----
    # Dollar amount to allocate per new position (notional). Quantity is derived
    # from the latest price. Whole shares only by default.
    position_size_usd: float = field(
        default_factory=lambda: _get_float("POSITION_SIZE_USD", 1000.0)
    )

    # ---- Bracket exits (applied to every entry) ----
    take_profit_pct: float = field(
        default_factory=lambda: _get_float("TAKE_PROFIT_PCT", 0.05)  # +5%
    )
    stop_loss_pct: float = field(
        default_factory=lambda: _get_float("STOP_LOSS_PCT", 0.03)    # -3%
    )

    # ---- Hard risk limits (the bot refuses to exceed these) ----
    max_position_usd: float = field(
        default_factory=lambda: _get_float("MAX_POSITION_USD", 2500.0)
    )
    max_shares_per_symbol: int = field(
        default_factory=lambda: _get_int("MAX_SHARES_PER_SYMBOL", 1000)
    )
    max_total_exposure_usd: float = field(
        default_factory=lambda: _get_float("MAX_TOTAL_EXPOSURE_USD", 10000.0)
    )
    max_open_positions: int = field(
        default_factory=lambda: _get_int("MAX_OPEN_POSITIONS", 5)
    )
    # Halt all new entries for the day if realized+unrealized loss exceeds this.
    max_daily_loss_usd: float = field(
        default_factory=lambda: _get_float("MAX_DAILY_LOSS_USD", 500.0)
    )
    # Reject an order whose limit price deviates more than this from the live quote.
    max_price_deviation_pct: float = field(
        default_factory=lambda: _get_float("MAX_PRICE_DEVIATION_PCT", 0.02)
    )
    # Minimum seconds between two orders on the same symbol (anti-runaway).
    per_symbol_cooldown_seconds: int = field(
        default_factory=lambda: _get_int("PER_SYMBOL_COOLDOWN_SECONDS", 300)
    )
    # Max committed entries per symbol per ET day. An SMA cross stays true for
    # the rest of the day, so after an intraday stop-out the signal would
    # otherwise re-enter every cooldown window until the daily-loss halt
    # finally trips. Default 2 = the original entry plus one retry.
    max_entries_per_symbol_per_day: int = field(
        default_factory=lambda: _get_int("MAX_ENTRIES_PER_SYMBOL_PER_DAY", 2)
    )

    # ---- Notifications (see notify.py; all optional) ----
    # Signal messenger webhook for INFORMATIONAL events (strategy signals,
    # order outcomes, start/stop) — e.g. signal-cli-rest-api's /v2/send.
    signal_webhook_url: str = field(
        default_factory=lambda: _get("SIGNAL_WEBHOOK_URL", "") or ""
    )
    signal_number: str = field(
        default_factory=lambda: _get("SIGNAL_NUMBER", "") or ""
    )
    signal_recipients: List[str] = field(
        default_factory=lambda: [
            r.strip()
            for r in (_get("SIGNAL_RECIPIENTS", "") or "").split(",")
            if r.strip()
        ]
    )
    # Pushover for URGENT/CRITICAL events (halts, kill switch, aborted exits).
    # repr=False — these are secrets (see api_key/app_secret above).
    pushover_token: str = field(
        default_factory=lambda: _get("PUSHOVER_TOKEN", "") or "", repr=False
    )
    pushover_user: str = field(
        default_factory=lambda: _get("PUSHOVER_USER", "") or "", repr=False
    )
    pushover_priority: int = field(
        default_factory=lambda: _get_int("PUSHOVER_PRIORITY", 1)
    )
    # Suppress an identical notification repeating within this window.
    notify_cooldown_seconds: int = field(
        default_factory=lambda: _get_int("NOTIFY_COOLDOWN_SECONDS", 300)
    )

    # ---- Logging ----
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO") or "INFO")
    log_file: str = field(default_factory=lambda: _get("LOG_FILE", "./schwab_bot.log") or "")

    # ---- Derived / validation ----
    @property
    def live_enabled(self) -> bool:
        return (not self.dry_run) and (self.live_confirm == LIVE_CONFIRM_PHRASE)

    def validate(self) -> list[str]:
        problems = []
        if not self.api_key:
            problems.append("SCHWAB_API_KEY is not set.")
        if not self.app_secret:
            problems.append("SCHWAB_APP_SECRET is not set.")
        if not self.callback_url:
            problems.append("SCHWAB_CALLBACK_URL is not set.")
        if not self.symbols:
            problems.append("SYMBOLS is empty — nothing to trade.")
        if self.sma_fast >= self.sma_slow:
            problems.append("SMA_FAST must be smaller than SMA_SLOW.")
        if self.position_size_usd > self.max_position_usd:
            problems.append(
                f"POSITION_SIZE_USD ({self.position_size_usd}) exceeds "
                f"MAX_POSITION_USD ({self.max_position_usd})."
            )
        if not (0 < self.stop_loss_pct < 1):
            problems.append("STOP_LOSS_PCT must be between 0 and 1.")
        if not (0 < self.take_profit_pct < 1):
            problems.append("TAKE_PROFIT_PCT must be between 0 and 1.")
        # Positivity checks: validate() is the place to catch footguns that
        # would otherwise surface as a silent no-trade (size 0) or a busy-loop
        # (poll interval 0) at runtime.
        for _name, _val in (
            ("POSITION_SIZE_USD", self.position_size_usd),
            ("MAX_POSITION_USD", self.max_position_usd),
            ("MAX_TOTAL_EXPOSURE_USD", self.max_total_exposure_usd),
            ("MAX_DAILY_LOSS_USD", self.max_daily_loss_usd),
            ("MAX_SHARES_PER_SYMBOL", self.max_shares_per_symbol),
            ("MAX_OPEN_POSITIONS", self.max_open_positions),
        ):
            if _val <= 0:
                problems.append(f"{_name} must be positive (got {_val}).")
        if self.sma_fast < 1:
            problems.append("SMA_FAST must be >= 1.")
        if self.poll_interval_seconds < 1:
            problems.append("POLL_INTERVAL_SECONDS must be >= 1.")
        # A negative index would silently wrap to the LAST linked account
        # (Python list indexing) and trade the wrong account.
        if self.account_index < 0:
            problems.append(
                f"SCHWAB_ACCOUNT_INDEX must be >= 0 (got {self.account_index})."
            )
        # Entries are priced 0.1% through the quote, so a zero deviation
        # tolerance would silently block EVERY order.
        if self.max_price_deviation_pct <= 0:
            problems.append(
                f"MAX_PRICE_DEVIATION_PCT must be positive "
                f"(got {self.max_price_deviation_pct})."
            )
        if self.per_symbol_cooldown_seconds < 0:
            problems.append("PER_SYMBOL_COOLDOWN_SECONDS must be >= 0.")
        if self.max_entries_per_symbol_per_day < 1:
            problems.append(
                f"MAX_ENTRIES_PER_SYMBOL_PER_DAY must be >= 1 "
                f"(got {self.max_entries_per_symbol_per_day})."
            )
        if not (-2 <= self.pushover_priority <= 2):
            problems.append(
                f"PUSHOVER_PRIORITY must be between -2 and 2 "
                f"(got {self.pushover_priority})."
            )
        # NOTE: the DRY_RUN-off-without-LIVE_CONFIRM case is a non-blocking
        # advisory, not an error — it lives in warnings() so a safe-but-not-live
        # config doesn't look like a startup-blocking misconfiguration.
        # Catch an unwritable DB path at startup rather than mid-session on the
        # first persist. A missing parent dir is fine — Database() creates it.
        if self.db_path:
            if os.path.isdir(self.db_path):
                problems.append(
                    f"SCHWAB_DB_PATH points at a directory, not a file: {self.db_path}"
                )
            else:
                db_dir = os.path.dirname(os.path.abspath(self.db_path))
                if os.path.isdir(db_dir) and not os.access(db_dir, os.W_OK):
                    problems.append(
                        f"SCHWAB_DB_PATH directory is not writable: {db_dir}"
                    )
        return problems

    def warnings(self) -> list[str]:
        notes = []
        if (not self.dry_run) and not self.live_enabled:
            notes.append(
                "DRY_RUN is off but LIVE_CONFIRM does not match the required "
                "phrase. Real trading will stay DISABLED until it does."
            )
        # A half-configured channel silently sends nothing — call it out so a
        # typo'd key doesn't read as "no news is good news".
        if bool(self.pushover_token) != bool(self.pushover_user):
            notes.append(
                "Pushover is partially configured (need BOTH PUSHOVER_TOKEN and "
                "PUSHOVER_USER). Urgent notifications are DISABLED."
            )
        if (self.signal_number or self.signal_recipients) \
                and not self.signal_webhook_url:
            notes.append(
                "SIGNAL_NUMBER/SIGNAL_RECIPIENTS are set but SIGNAL_WEBHOOK_URL "
                "is empty. Signal notifications are DISABLED."
            )
        return notes


CONFIG = Config()
