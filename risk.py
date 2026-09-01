from __future__ import annotations

import logging
import os
import time
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

from client import AccountSnapshot
from config import Config

if TYPE_CHECKING:
    from db import Database
    from notify import Notifier

logger = logging.getLogger("schwab_bot.risk")
EASTERN = ZoneInfo("America/New_York")


def _today_et() -> str:
    return datetime.now(EASTERN).strftime("%Y-%m-%d")


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""


@dataclass
class RiskManager:
    config: Config
    # Optional SQLite persistence; when None we use the JSON state file instead.
    db: Optional["Database"] = None
    # Optional notifications; a halt TRANSITION pushes an urgent alert.
    notifier: Optional["Notifier"] = None
    # session state
    starting_equity: float = 0.0
    baseline_date: str = ""
    halted: bool = False
    halt_reason: str = ""
    _last_order_time: dict[str, float] = field(default_factory=dict)
    # In-cycle reservations: orders committed earlier in the CURRENT cycle that
    # the account snapshot does not yet reflect (a dry-run never moves the
    # account; a live bracket entry is an unfilled limit, so snapshot.positions
    # still shows nothing). Without these, the aggregate caps re-approve every
    # symbol against the same pre-cycle baseline and can be breached in one pass.
    _pending_notional: float = 0.0
    _pending_symbols: set[str] = field(default_factory=set)
    # In-flight entries from PRIOR cycles: working BUY orders at the broker
    # that haven't filled, so they're in neither snapshot.positions nor the
    # same-cycle reservations. Without these the open-position/exposure caps
    # under-count whenever entries fill slowly across cycles. Refreshed by the
    # executor from its per-cycle working-order fetch.
    _inflight_notional: float = 0.0
    _inflight_symbols: set[str] = field(default_factory=set)
    # Committed entries per symbol for the current ET day (dry-run and live
    # alike). In-memory: a restart resets the count — acceptable because the
    # cap's job is bounding same-session re-entry churn, and the daily-loss
    # halt (persisted) remains the hard backstop.
    _entries_today: dict[str, int] = field(default_factory=dict)
    _entries_day: str = ""

    def begin_session(self, snapshot: AccountSnapshot) -> None:
        self.halted = False
        self.halt_reason = ""
        today = _today_et()
        stored = self._load_daily_state()

        if stored and stored.get("date") == today:
            self.starting_equity = float(stored["starting_equity"])
            self.baseline_date = today
            logger.info(
                "Risk session resumed for %s. Reloaded equity baseline: $%.2f "
                "(current equity $%.2f).",
                today, self.starting_equity, snapshot.equity,
            )
        else:
            self.starting_equity = snapshot.equity
            self.baseline_date = today
            self._save_daily_state(today, self.starting_equity)
            logger.info(
                "Risk session start for %s. New equity baseline: $%.2f",
                today, self.starting_equity,
            )

        # A non-positive baseline silently disables the daily-loss halt
        # (check_daily_loss returns False when starting_equity <= 0). That can
        # happen if the account-balance parse returns 0 (e.g. missing balance
        # keys in the API response). Surface it loudly rather than trading with
        # the halt disarmed.
        if self.starting_equity <= 0:
            logger.critical(
                "Equity baseline is non-positive ($%.2f) — the daily-loss halt "
                "will NOT arm this session. Check account-balance parsing.",
                self.starting_equity,
            )

    # ---- daily-state persistence ----

    def _load_daily_state(self) -> dict | None:
        if self.db is not None:
            return self.db.get_daily_state(_today_et())
        path = self.config.daily_state_file
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.warning("Could not read daily state file %s; ignoring.", path)
            return None

    def _save_daily_state(self, date: str, starting_equity: float) -> None:
        if self.db is not None:
            self.db.set_daily_state(date, starting_equity)
            return
        path = self.config.daily_state_file
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump({"date": date, "starting_equity": starting_equity}, f)
        except Exception:
            logger.warning("Could not write daily state file %s.", path)

    # ---- global halts ----

    def kill_switch_active(self) -> bool:
        path = self.config.kill_switch_file
        if path and os.path.exists(path):
            if not self.halted:
                self._halt(f"Kill switch file present: {path}")
            return True
        return False

    def check_daily_loss(self, snapshot: AccountSnapshot) -> bool:
        today = _today_et()
        if self.baseline_date and today != self.baseline_date:
            logger.info(
                "New trading day %s — re-baselining equity to $%.2f and "
                "clearing any prior daily-loss halt.", today, snapshot.equity,
            )
            self.starting_equity = snapshot.equity
            self.baseline_date = today
            self.halted = False
            self.halt_reason = ""
            self._save_daily_state(today, self.starting_equity)

        if self.starting_equity <= 0:
            return False
        drawdown = self.starting_equity - snapshot.equity
        if drawdown >= self.config.max_daily_loss_usd:
            self._halt(
                f"Daily loss limit hit: down ${drawdown:.2f} "
                f">= ${self.config.max_daily_loss_usd:.2f}"
            )
            return True
        return False

    def _halt(self, reason: str) -> None:
        # Alert only on the TRANSITION into the halt: check_daily_loss re-calls
        # this every cycle while drawn down (with a changing drawdown figure),
        # which must not re-page the user each poll.
        if not self.halted and self.notifier is not None:
            self.notifier.urgent(f"TRADING HALTED: {reason}")
        self.halted = True
        self.halt_reason = reason
        logger.critical("TRADING HALTED: %s", reason)

    # ---- per-order checks ----

    def approve_entry(
        self,
        symbol: str,
        quantity: int,
        entry_price: float,
        live_price: float,
        snapshot: AccountSnapshot,
    ) -> RiskDecision:
        if self.halted:
            return RiskDecision(False, f"Bot halted: {self.halt_reason}")

        if quantity <= 0:
            return RiskDecision(False, "Quantity must be positive.")

        notional = quantity * entry_price

        # 1. per-order dollar cap
        if notional > self.config.max_position_usd:
            return RiskDecision(
                False,
                f"Order notional ${notional:.2f} exceeds "
                f"MAX_POSITION_USD ${self.config.max_position_usd:.2f}.",
            )

        # 2. per-symbol share cap (including any existing position)
        existing = snapshot.position_for(symbol)
        existing_qty = existing.quantity if existing else 0
        if existing_qty + quantity > self.config.max_shares_per_symbol:
            return RiskDecision(
                False,
                f"Would hold {existing_qty + quantity} shares of {symbol} "
                f"> MAX_SHARES_PER_SYMBOL {self.config.max_shares_per_symbol}.",
            )

        # 3. total exposure cap (orders committed THIS cycle + unfilled entries
        # still working at the broker from PRIOR cycles)
        projected_exposure = (
            snapshot.total_exposure + self._pending_notional
            + self._inflight_notional + notional
        )
        if projected_exposure > self.config.max_total_exposure_usd:
            return RiskDecision(
                False,
                f"Total exposure would be ${projected_exposure:.2f} "
                f"> MAX_TOTAL_EXPOSURE_USD ${self.config.max_total_exposure_usd:.2f}.",
            )

        # 4. max open positions (counts NEW symbols: opened this cycle AND
        # in-flight from prior cycles)
        committed = self._pending_symbols | self._inflight_symbols
        pending_new = committed - {p.symbol for p in snapshot.positions}
        projected_open = len(snapshot.positions) + len(pending_new)
        if (existing is None and symbol not in committed
                and projected_open >= self.config.max_open_positions):
            return RiskDecision(
                False,
                f"Already at MAX_OPEN_POSITIONS ({self.config.max_open_positions}).",
            )

        # 5. price sanity vs live quote (fat-finger / stale-data guard)
        if live_price > 0:
            deviation = abs(entry_price - live_price) / live_price
            if deviation > self.config.max_price_deviation_pct:
                return RiskDecision(
                    False,
                    f"Entry price ${entry_price:.2f} deviates "
                    f"{deviation:.1%} from live ${live_price:.2f} "
                    f"(> {self.config.max_price_deviation_pct:.1%}).",
                )

        # 6. per-symbol cooldown (anti-runaway)
        last = self._last_order_time.get(symbol, 0)
        elapsed = time.time() - last
        if elapsed < self.config.per_symbol_cooldown_seconds:
            wait = self.config.per_symbol_cooldown_seconds - elapsed
            return RiskDecision(
                False,
                f"Cooldown active for {symbol}: wait {wait:.0f}s.",
            )

        # 7. per-symbol daily entry cap. The SMA cross stays true all day, so
        # after an intraday stop-out the same signal re-fires every cooldown
        # window — without this cap, a choppy day churns entries until the
        # daily-loss halt trips.
        today = _today_et()
        if today != self._entries_day:
            self._entries_day = today
            self._entries_today = {}
        cap = self.config.max_entries_per_symbol_per_day
        if self._entries_today.get(symbol, 0) >= cap:
            return RiskDecision(
                False,
                f"Daily entry cap reached for {symbol} "
                f"({cap}/day; resets at the next ET day).",
            )

        return RiskDecision(True, "OK")

    def record_order(self, symbol: str) -> None:
        self._last_order_time[symbol] = time.time()

    def begin_cycle(self) -> None:
        self._pending_notional = 0.0
        self._pending_symbols = set()
        self._inflight_notional = 0.0
        self._inflight_symbols = set()

    def set_inflight(self, symbols: set[str], notional: float) -> None:
        self._inflight_symbols = set(symbols)
        self._inflight_notional = notional

    def reserve_entry(self, symbol: str, notional: float, is_new: bool) -> None:
        self._pending_notional += notional
        if is_new:
            self._pending_symbols.add(symbol)
        self._entries_today[symbol] = self._entries_today.get(symbol, 0) + 1
