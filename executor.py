from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Optional

from schwab.orders.equities import equity_sell_market

from client import SchwabClient, build_bracket_buy
from config import Config
from risk import RiskManager

if TYPE_CHECKING:
    from db import Database
    from notify import Notifier

logger = logging.getLogger("schwab_bot.executor")


def _exit_quantity(held: float):
    return int(held) if float(held).is_integer() else held


class Executor:
    def __init__(self, client: SchwabClient, config: Config, risk: RiskManager,
                 db: Optional["Database"] = None,
                 notifier: Optional["Notifier"] = None):
        self.client = client
        self.config = config
        self.risk = risk
        self.db = db
        self.notifier = notifier
        # Per-cycle working-order cache for enter()'s stacking guard. Fetched
        # lazily on the first entry attempt of a cycle (so HOLD-only cycles cost
        # zero extra API calls) and reused for every symbol that cycle.
        self._cycle_orders: Optional[list[dict]] = None
        self._cycle_orders_fetched = False

    def begin_cycle(self) -> None:
        self._cycle_orders = None
        self._cycle_orders_fetched = False

    def _working_orders(self, symbol: str) -> Optional[list[dict]]:
        if not self._cycle_orders_fetched:
            try:
                self._cycle_orders = self.client.open_orders()
                self._register_inflight(self._cycle_orders)
            except Exception:
                logger.exception("Could not fetch working orders.")
                self._cycle_orders = None
            self._cycle_orders_fetched = True
        if self._cycle_orders is None:
            return None
        return self.client.working_orders_for_symbol(symbol, self._cycle_orders)

    def _register_inflight(self, orders: list[dict]) -> None:
        symbols: set[str] = set()
        notional = 0.0
        for o in orders:
            for node in SchwabClient.live_nodes(o):
                for leg in node.get("orderLegCollection", []):
                    # Exact match: BUY_TO_COVER closes a short and must not be
                    # counted as long-entry exposure.
                    if str(leg.get("instruction", "")).upper() != "BUY":
                        continue
                    sym = leg.get("instrument", {}).get("symbol")
                    if not sym:
                        continue
                    symbols.add(sym)
                    try:
                        # Prefer the UNFILLED remainder: the filled part of a
                        # partial fill is already in snapshot.positions, and
                        # counting the original quantity would double it.
                        rem = node.get("remainingQuantity")
                        qty = float(rem if rem is not None
                                    else leg.get("quantity", 0) or 0)
                        px = float(node.get("price", 0) or 0)
                        notional += qty * px
                    except (TypeError, ValueError):
                        pass
        self.risk.set_inflight(symbols, notional)

    @property
    def _mode(self) -> str:
        return "LIVE" if self.config.live_enabled else "DRY_RUN"

    def _notify_info(self, message: str) -> None:
        if self.notifier is not None:
            self.notifier.info(message)

    def _notify_urgent(self, message: str) -> None:
        if self.notifier is not None:
            self.notifier.urgent(message)

    def _record_order(self, **kw) -> None:
        if self.db is None:
            return
        try:
            self.db.record_order(**kw)
        except Exception:
            logger.exception("Could not record order to DB (continuing).")

    def _size_position(self, price: float) -> int:
        if price <= 0:
            return 0
        return int(math.floor(self.config.position_size_usd / price))

    def enter(self, symbol: str, snapshot) -> None:
        # Guard against stacking: if there's already a working order touching
        # this symbol (e.g. an unfilled entry limit or a resting exit bracket),
        # do not place another. The risk manager only sees filled POSITIONS, so
        # an in-flight entry that hasn't filled is invisible to it — without this
        # check a slow-to-fill limit could be re-ordered every cooldown window.
        existing = self._working_orders(symbol)
        if existing is None:
            # If we can't confirm the order state, fail safe: skip the entry
            # rather than risk a duplicate.
            logger.warning(
                "%s: could not check working orders; skipping entry to avoid "
                "duplicates.", symbol,
            )
            return
        if existing:
            logger.info(
                "ENTRY SKIPPED %s: %d working order(s) already open for this "
                "symbol.", symbol, len(existing),
            )
            self._record_order(
                symbol=symbol, side="BUY", order_kind="BRACKET_ENTRY", quantity=0,
                mode=self._mode, status="SKIPPED",
                reason=f"{len(existing)} working order(s) already open",
            )
            return

        # require_fresh: without it a quote with no current-session price
        # falls back to YESTERDAY'S close, and on a gap day the entire bracket
        # plus the fat-finger guard's baseline would be priced off that same
        # stale number — the deviation check cannot catch it.
        live_price = self.client.get_last_price(symbol, require_fresh=True)
        if not live_price:
            logger.warning("No fresh live price for %s; skipping entry.", symbol)
            self._record_order(
                symbol=symbol, side="BUY", order_kind="BRACKET_ENTRY", quantity=0,
                mode=self._mode, status="SKIPPED",
                reason="no fresh live price (lastPrice/mark unavailable)",
            )
            return

        quantity = self._size_position(live_price)
        if quantity <= 0:
            logger.info(
                "%s: position size $%.2f < 1 share at $%.2f; skipping.",
                symbol, self.config.position_size_usd, live_price,
            )
            self._record_order(
                symbol=symbol, side="BUY", order_kind="BRACKET_ENTRY", quantity=0,
                live_price=live_price, mode=self._mode, status="SKIPPED",
                reason=f"size ${self.config.position_size_usd:.2f} < 1 share",
            )
            return

        # Entry slightly through the offer so the limit is marketable but capped.
        entry_price = round(live_price * 1.001, 2)
        take_profit = round(entry_price * (1 + self.config.take_profit_pct), 2)
        stop_loss = round(entry_price * (1 - self.config.stop_loss_pct), 2)
        notional = quantity * entry_price

        # Degenerate-bracket guard: on very low-priced stocks the percentage
        # legs round to the SAME cent as the entry (round($0.10*1.05,2) ==
        # $0.10) — a "bracket" whose stop/target sit at or through the entry
        # is not protection at all. Refuse rather than place it.
        if not (stop_loss < entry_price < take_profit):
            logger.warning(
                "ENTRY BLOCKED %s: degenerate bracket at $%.2f "
                "(SL $%.2f / TP $%.2f collapse into the entry — price too "
                "low for the configured percentages).",
                symbol, entry_price, stop_loss, take_profit)
            self._record_order(
                symbol=symbol, side="BUY", order_kind="BRACKET_ENTRY",
                quantity=quantity, limit_price=entry_price,
                take_profit=take_profit, stop_loss=stop_loss,
                live_price=live_price, notional=notional,
                mode=self._mode, status="BLOCKED",
                reason="degenerate bracket: SL/TP round into the entry",
            )
            return

        is_new = snapshot.position_for(symbol) is None
        decision = self.risk.approve_entry(
            symbol, quantity, entry_price, live_price, snapshot
        )
        if not decision.allowed:
            logger.info("ENTRY BLOCKED %s: %s", symbol, decision.reason)
            self._notify_info(f"Entry blocked {symbol}: {decision.reason}")
            self._record_order(
                symbol=symbol, side="BUY", order_kind="BRACKET_ENTRY",
                quantity=quantity, limit_price=entry_price, take_profit=take_profit,
                stop_loss=stop_loss, live_price=live_price, notional=notional,
                mode=self._mode, status="BLOCKED", reason=decision.reason,
            )
            return

        order = build_bracket_buy(symbol, quantity, entry_price, take_profit, stop_loss)

        summary = (
            f"{symbol}: BUY {quantity} @ ${entry_price:.2f} "
            f"| TP ${take_profit:.2f} (+{self.config.take_profit_pct:.0%}) "
            f"| SL ${stop_loss:.2f} (-{self.config.stop_loss_pct:.0%}) "
            f"| notional ${notional:.2f}"
        )

        if not self.config.live_enabled:
            preview = self.client.preview(order)
            ok = preview.get("preview_ok")
            logger.info("[DRY-RUN] WOULD PLACE BRACKET -> %s", summary)
            logger.info("[DRY-RUN] preview_order ok=%s status=%s",
                        ok, preview.get("status"))
            if not ok:
                logger.warning("[DRY-RUN] preview rejected: %s",
                               str(preview.get("body"))[:500])
            self._record_order(
                symbol=symbol, side="BUY", order_kind="BRACKET_ENTRY",
                quantity=quantity, limit_price=entry_price, take_profit=take_profit,
                stop_loss=stop_loss, live_price=live_price, notional=notional,
                mode="DRY_RUN", status="PREVIEW_OK" if ok else "PREVIEW_REJECTED",
                detail=None if ok else str(preview.get("body"))[:500],
            )
            # Stamp the per-symbol cooldown in dry-run too, so the anti-runaway
            # guard fires identically to live. Otherwise _last_order_time stays
            # empty in the default DRY_RUN mode and the cooldown never engages —
            # a fast poll would re-preview the same symbol every cycle, and the
            # dry-run a user relies on to validate safety would not exercise it.
            self.risk.record_order(symbol)
            # Reserve against the aggregate caps so a later symbol THIS cycle
            # sees this (un-acted-on in dry-run) entry's notional/slot.
            self.risk.reserve_entry(symbol, notional, is_new=is_new)
            self._notify_info(f"[DRY-RUN] would place bracket — {summary} "
                              f"(preview ok={ok})")
            return

        # ---- LIVE ----
        logger.warning("[LIVE] PLACING BRACKET -> %s", summary)
        try:
            order_id = self.client.place(order)
        except Exception:
            # The order MAY have reached Schwab before the error (a timeout
            # after acceptance). Treat the placement as ambiguous-but-possibly
            # live: stamp the cooldown and reserve the caps so we don't
            # immediately re-stack a duplicate this run, record an ERROR row,
            # and page — the broker-truth stacking guard reconciles next cycle.
            logger.exception("[LIVE] %s: bracket placement FAILED (order state "
                             "unknown — may or may not be live at Schwab).",
                             symbol)
            self.risk.record_order(symbol)
            self.risk.reserve_entry(symbol, notional, is_new=is_new)
            self._notify_urgent(
                f"ENTRY UNKNOWN {symbol}: bracket placement errored — the "
                f"order may or may not be live at Schwab. Check the broker "
                f"before the next session. ({summary})")
            self._record_order(
                symbol=symbol, side="BUY", order_kind="BRACKET_ENTRY",
                quantity=quantity, limit_price=entry_price,
                take_profit=take_profit, stop_loss=stop_loss,
                live_price=live_price, notional=notional,
                mode="LIVE", status="ERROR",
                reason="placement errored; order state unknown",
            )
            return
        self.risk.record_order(symbol)
        # Reserve against the aggregate caps: this entry is an UNFILLED limit, so
        # the next snapshot won't show it; without this the exposure / open-count
        # caps under-count in-flight entries placed earlier this cycle.
        self.risk.reserve_entry(symbol, notional, is_new=is_new)
        logger.warning("[LIVE] order id=%s placed for %s", order_id, symbol)
        self._notify_info(f"[LIVE] bracket placed — {summary} (id={order_id})")
        self._record_order(
            symbol=symbol, side="BUY", order_kind="BRACKET_ENTRY",
            quantity=quantity, limit_price=entry_price, take_profit=take_profit,
            stop_loss=stop_loss, live_price=live_price, notional=notional,
            mode="LIVE", status="PLACED",
            broker_order_id=str(order_id) if order_id is not None else None,
        )

    def exit_position(self, symbol: str, snapshot, *,
                      notify_abort: bool = True) -> bool:
        pos = snapshot.position_for(symbol)
        if not pos or pos.quantity <= 0:
            logger.info("%s: no long position to exit.", symbol)
            return False

        # Sell the FULL held quantity — never silently floor a fractional
        # position, or an exit/flatten would leave an unprotected residual.
        # Keep an integer when the holding is whole (the common case the bot
        # itself creates); otherwise pass the exact fractional amount.
        qty = _exit_quantity(pos.quantity)
        if pos.quantity != int(pos.quantity):
            logger.warning("%s: flattening FRACTIONAL position of %s shares.",
                           symbol, pos.quantity)
        order = equity_sell_market(symbol, qty)

        summary = f"{symbol}: SELL {qty} @ MARKET (flatten)"

        if not self.config.live_enabled:
            preview = self.client.preview(order)
            ok = preview.get("preview_ok")
            logger.info("[DRY-RUN] WOULD EXIT -> %s (preview ok=%s)", summary, ok)
            self._record_order(
                symbol=symbol, side="SELL", order_kind="MARKET_EXIT", quantity=qty,
                mode="DRY_RUN", status="PREVIEW_OK" if ok else "PREVIEW_REJECTED",
                detail=None if ok else str(preview.get("body"))[:500],
            )
            # Mirror live: stamp the cooldown so dry-run and live behave alike.
            self.risk.record_order(symbol)
            self._notify_info(f"[DRY-RUN] would exit — {summary} "
                              f"(preview ok={ok})")
            return False

        logger.warning("[LIVE] EXITING -> %s", summary)
        # Cancel resting OCO legs first so they can't fire after we flatten.
        # cancel() now reports failure; if any leg won't confirm cancelled,
        # ABORT the market sell — selling while a stop/TP is still live risks
        # an oversell/short when that leg later triggers.
        failed = self.client.cancel_working_orders_for_symbol(symbol)
        if failed:
            logger.error(
                "[LIVE] %d resting order(s) for %s did not confirm cancelled; "
                "ABORTING market exit to avoid an oversell/short.", failed, symbol)
            if notify_abort:
                self._notify_urgent(
                    f"EXIT ABORTED {symbol}: {failed} resting order(s) failed "
                    f"to cancel — market sell NOT placed. Check the broker.")
            self._record_order(
                symbol=symbol, side="SELL", order_kind="MARKET_EXIT", quantity=qty,
                mode="LIVE", status="BLOCKED",
                reason=f"{failed} resting order(s) failed to cancel",
            )
            return True

        # Re-read the position AFTER cancelling: a resting leg may have filled
        # between our snapshot and now, changing (or clearing) the holding.
        try:
            fresh = self.client.snapshot().position_for(symbol)
        except Exception:
            # The resting protection is ALREADY CANCELLED at this point, so
            # skipping the sell leaves the position live with no stop — that
            # is an unprotected-position failure, not a safe no-op. Selling a
            # possibly-stale quantity would be worse (oversell risk), so we
            # still skip the sell, but we page and report failure.
            logger.exception("[LIVE] %s: could not refresh position after "
                             "cancelling resting orders — market exit skipped; "
                             "POSITION UNPROTECTED until re-protected or "
                             "closed.", symbol)
            if notify_abort:
                self._notify_urgent(
                    f"POSITION UNPROTECTED {symbol}: resting orders were "
                    f"cancelled but the position could not be re-read, so no "
                    f"market sell was placed. Re-protect or close manually.")
            self._record_order(
                symbol=symbol, side="SELL", order_kind="MARKET_EXIT",
                quantity=qty, mode="LIVE", status="ERROR",
                reason="post-cancel position refresh failed; sell not placed",
            )
            return True
        if not fresh or fresh.quantity <= 0:
            logger.warning("[LIVE] %s already flat after cancelling legs; "
                           "no market sell needed.", symbol)
            return False
        qty = _exit_quantity(fresh.quantity)
        order = equity_sell_market(symbol, qty)

        # The resting stops/TPs are ALREADY CANCELLED at this point — if the
        # market sell fails to place, the position is live with NO protection.
        # That must page a human, not vanish into the cycle's catch-all log.
        try:
            order_id = self.client.place(order)
        except Exception:
            logger.exception("[LIVE] %s: market sell FAILED TO PLACE after "
                             "cancelling resting orders — POSITION UNPROTECTED.",
                             symbol)
            if notify_abort:
                self._notify_urgent(
                    f"POSITION UNPROTECTED {symbol}: resting orders were "
                    f"cancelled but the market sell failed to place. "
                    f"Re-protect or close manually.")
            self._record_order(
                symbol=symbol, side="SELL", order_kind="MARKET_EXIT",
                quantity=qty, mode="LIVE", status="ERROR",
                reason="market sell failed to place after cancels",
            )
            return True
        self.risk.record_order(symbol)
        logger.warning("[LIVE] exit order id=%s placed for %s (%s shares)",
                       order_id, symbol, qty)
        self._notify_info(f"[LIVE] exit placed — {symbol}: SELL {qty} @ MARKET "
                          f"(id={order_id})")
        self._record_order(
            symbol=symbol, side="SELL", order_kind="MARKET_EXIT", quantity=qty,
            mode="LIVE", status="PLACED",
            broker_order_id=str(order_id) if order_id is not None else None,
        )
        return False

    def flatten_all(self, snapshot) -> None:
        logger.critical("FLATTEN ALL requested.")
        longs = sum(1 for p in snapshot.positions if p.quantity > 0)
        self._notify_urgent(
            f"FLATTEN ALL ({self._mode}): cancelling working orders and "
            f"selling {longs} long position(s).")
        if self.config.live_enabled:
            try:
                # Tree-aware: cancels the topmost LIVE node of each strategy
                # (a filled bracket's parent is FILLED — its resting exit legs
                # are the live nodes), and confirms each reaches a terminal
                # state rather than trusting the cancel request's HTTP status.
                unconfirmed = self.client.cancel_all_working_orders()
                if unconfirmed:
                    logger.error("[FLATTEN] %d order(s) did not confirm "
                                 "cancelled; per-symbol exits will re-check "
                                 "and abort their sells if still live.",
                                 unconfirmed)
            except Exception:
                logger.exception("Could not enumerate/cancel working orders "
                                 "during flatten (continuing to sell positions).")
        # Suppress per-symbol abort pages and AGGREGATE them: flattening many
        # positions during a broker outage must produce ONE urgent summary,
        # not a page per symbol at the worst possible moment. Each exit is
        # ISOLATED in its own try: one symbol raising (a network error mid-
        # exit) must not abandon the symbols after it — covering as many
        # positions as possible is the whole point of a kill-switch flatten.
        aborted: list[str] = []
        for pos in snapshot.positions:
            if pos.quantity <= 0:
                continue
            try:
                if self.exit_position(pos.symbol, snapshot, notify_abort=False):
                    aborted.append(pos.symbol)
            except Exception:
                logger.exception("[FLATTEN] %s: exit raised; continuing with "
                                 "the remaining positions.", pos.symbol)
                aborted.append(pos.symbol)
        if aborted:
            self._notify_urgent(
                f"FLATTEN INCOMPLETE: exits failed for "
                f"{', '.join(sorted(aborted))} (cancel or sell did not go "
                f"through) — positions may be unprotected. Check the broker.")
