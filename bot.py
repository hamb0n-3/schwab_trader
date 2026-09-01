from __future__ import annotations

import logging
import signal
import time

from auth import get_client, resolve_account_hash
from client import SchwabClient
from config import CONFIG, Config
from db import Database
from executor import Executor
from market_calendar import MarketCalendar
from notify import Notifier
from risk import RiskManager
from strategy import Action, SmaCrossStrategy

logger = logging.getLogger("schwab_bot.bot")

# Page the user after this many cycle failures IN A ROW (dead refresh token,
# broken network): a bot that's been failing for 5 minutes straight should
# not only be whispering into its own log file.
FAILURE_ALERT_THRESHOLD = 5


class TradingBot:
    def __init__(self, config: Config):
        self.config = config
        self._stop = False
        self._consecutive_errors = 0

        raw_client = get_client(
            config.api_key, config.app_secret, config.callback_url, config.token_path
        )
        account_hash = resolve_account_hash(raw_client, config.account_index)
        self.client = SchwabClient(raw_client, account_hash)
        self.db = Database(config.db_path) if config.db_path else None
        if self.db is not None:
            self.db.import_legacy(config)        # one-time JSON -> DB migration
        self.notifier = Notifier(config)
        self.risk = RiskManager(config, db=self.db, notifier=self.notifier)
        self.executor = Executor(self.client, config, self.risk, db=self.db,
                                 notifier=self.notifier)
        self.strategy = SmaCrossStrategy(config.sma_fast, config.sma_slow)
        self.calendar = MarketCalendar()

        signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._handle_sigint)

    def _handle_sigint(self, *_):
        if self._stop:
            # Second signal: don't make the user wait out a hung API call.
            # KeyboardInterrupt is not an Exception subclass, so it escapes the
            # cycle's catch-all and unwinds through run()'s finally (DB close).
            logger.warning("Second shutdown signal — forcing immediate exit.")
            raise KeyboardInterrupt
        logger.warning("Shutdown signal received. Finishing current cycle then "
                       "exiting (signal again to force).")
        self._stop = True

    def run(self):
        mode = "LIVE — REAL MONEY" if self.config.live_enabled else "DRY-RUN (no orders placed)"
        logger.warning("=" * 60)
        logger.warning("Schwab bot starting in %s mode.", mode)
        logger.warning("Symbols: %s", ", ".join(self.config.symbols))
        logger.warning("Strategy: SMA(%d/%d) | size $%.0f | TP %.0f%% / SL %.0f%%",
                       self.config.sma_fast, self.config.sma_slow,
                       self.config.position_size_usd,
                       self.config.take_profit_pct * 100,
                       self.config.stop_loss_pct * 100)
        logger.warning("=" * 60)
        self.notifier.info(
            f"Bot started in {mode} mode | symbols: "
            f"{', '.join(self.config.symbols)} | "
            f"SMA({self.config.sma_fast}/{self.config.sma_slow}), "
            f"size ${self.config.position_size_usd:.0f}")

        try:
            snapshot = self.client.snapshot()
            self.risk.begin_session(snapshot)

            while not self._stop:
                try:
                    self._cycle()
                    self._note_cycle_success()
                except Exception as e:
                    logger.exception("Cycle error (continuing).")
                    self._note_cycle_error(e)
                # Sleep in short slices so Ctrl+C / kill switch react quickly.
                slept = 0
                while slept < self.config.poll_interval_seconds and not self._stop:
                    if self.risk.kill_switch_active():
                        break
                    time.sleep(1)
                    slept += 1
        except KeyboardInterrupt:
            logger.warning("Forced exit.")
        finally:
            # Send the stop notification FIRST: Notifier never raises, while
            # db.close() can (sqlite I/O) — a close failure must not eat the
            # lifecycle push the user relies on to notice the bot died.
            self.notifier.info("Bot stopped.")
            # Always close the DB so the WAL is checkpointed — even if startup
            # (snapshot / begin_session) or an unexpected error aborts the loop.
            if self.db is not None:
                try:
                    self.db.close()
                except Exception:
                    logger.exception("DB close failed during shutdown.")
            logger.warning("Bot stopped.")

    def _note_cycle_error(self, err: Exception) -> None:
        self._consecutive_errors += 1
        n = self._consecutive_errors
        if n >= FAILURE_ALERT_THRESHOLD and n % FAILURE_ALERT_THRESHOLD == 0:
            self.notifier.urgent(
                f"Bot failing: {n} consecutive cycle errors "
                f"(latest: {err!r:.200}). Still retrying every cycle — "
                f"check the token, network, and Schwab status.")

    def _note_cycle_success(self) -> None:
        if self._consecutive_errors >= FAILURE_ALERT_THRESHOLD:
            self.notifier.info(
                f"Bot recovered after {self._consecutive_errors} consecutive "
                f"cycle error(s).")
        self._consecutive_errors = 0

    def _cycle(self):
        # 1. kill switch
        if self.risk.kill_switch_active():
            snapshot = self.client.snapshot()
            self.executor.flatten_all(snapshot)
            self._stop = True
            return

        # 2. market hours — checked BEFORE the snapshot so an idle bot doesn't
        # hit the account endpoint and append a DB snapshot row every poll all
        # night and weekend. The daily-loss re-baseline simply happens on the
        # first open-market cycle of the day instead of at midnight.
        if not self.calendar.is_open():
            logger.info("Market %s — idle.", self.calendar.describe())
            return

        # Reset per-cycle risk reservations and the executor's working-order
        # cache before evaluating any symbol.
        self.risk.begin_cycle()
        self.executor.begin_cycle()

        # 3. snapshot
        snapshot = self.client.snapshot()
        if self.db is not None:
            try:
                self.db.record_snapshot(snapshot)
            except Exception:
                logger.exception("Could not record snapshot (continuing).")

        # 4. daily-loss halt
        if self.risk.check_daily_loss(snapshot):
            logger.critical("Daily loss halt active — no new entries. "
                            "Existing bracket stops remain at the broker.")
            return

        held = {p.symbol for p in snapshot.positions if p.quantity > 0}
        logger.info("Cycle | equity $%.2f | exposure $%.2f | holding: %s",
                    snapshot.equity, snapshot.total_exposure,
                    ", ".join(sorted(held)) or "none")

        # 5-6. evaluate & route
        for symbol in self.config.symbols:
            try:
                closes = self.client.get_closes(symbol, lookback=self.config.sma_slow + 5)
                sig = self.strategy.evaluate(symbol, closes, holding=symbol in held)
                if sig.action is Action.HOLD:
                    logger.debug("%s HOLD: %s", symbol, sig.reason)
                    continue
                logger.info("%s SIGNAL %s: %s", symbol, sig.action.value, sig.reason)
                self.notifier.info(
                    f"{symbol} SIGNAL {sig.action.value}: {sig.reason}")
                if self.db is not None:
                    try:
                        self.db.record_signal(symbol, sig.action.value, sig.reason,
                                              holding=symbol in held)
                    except Exception:
                        logger.exception("Could not record signal (continuing).")
                if sig.action is Action.BUY:
                    self.executor.enter(symbol, snapshot)
                elif sig.action is Action.EXIT:
                    self.executor.exit_position(symbol, snapshot)
                # refresh snapshot after an action so subsequent checks are
                # current — including the held set the strategy consumes,
                # which must not drift from the snapshot it came from
                snapshot = self.client.snapshot()
                held = {p.symbol for p in snapshot.positions if p.quantity > 0}
            except Exception:
                logger.exception("Error processing %s (continuing).", symbol)


def main():
    from logging_setup import setup_logging
    setup_logging(CONFIG.log_level, CONFIG.log_file)

    for w in CONFIG.warnings():
        logger.warning("CONFIG: %s", w)
    problems = CONFIG.validate()
    if problems:
        for p in problems:
            logger.error("CONFIG: %s", p)
        # validate() now returns ONLY true errors (advisories go through
        # warnings()), so any non-empty result is a real misconfiguration that
        # must block a real-money bot from starting.
        logger.error("Fix the configuration above and re-run.")
        return

    TradingBot(CONFIG).run()


if __name__ == "__main__":
    main()
