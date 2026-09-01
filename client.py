from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from schwab.client import Client
from schwab.orders.equities import (
    equity_buy_limit,
    equity_sell_limit,
)
from schwab.orders.generic import OrderBuilder
from schwab.orders.common import (
    Duration,
    EquityInstruction,
    OrderStrategyType,
    OrderType,
    Session,
    first_triggers_second,
    one_cancels_other,
)

logger = logging.getLogger("schwab_bot.client")


def _fmt(price: float) -> str:
    return f"{price:.2f}"


@dataclass
class Position:
    symbol: str
    quantity: float
    average_price: float
    market_value: float


@dataclass
class AccountSnapshot:
    account_hash: str
    cash: float
    equity: float           # total account value (liquidation value)
    buying_power: float
    positions: list[Position]

    def position_for(self, symbol: str) -> Optional[Position]:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None

    @property
    def total_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions)


# ---------- Order builders (return schwab-py OrderBuilder objects) ----------

def build_equity_sell_stop(symbol: str, quantity: int, stop_price: float) -> OrderBuilder:
    return (
        OrderBuilder()
        .set_session(Session.NORMAL)
        .set_duration(Duration.GOOD_TILL_CANCEL)
        .set_order_type(OrderType.STOP)
        .set_stop_price(_fmt(stop_price))
        .set_order_strategy_type(OrderStrategyType.SINGLE)
        .add_equity_leg(EquityInstruction.SELL, symbol, quantity)
    )


def build_equity_sell_limit_gtc(symbol: str, quantity: int, price: float) -> OrderBuilder:
    return equity_sell_limit(symbol, quantity, _fmt(price)).set_duration(
        Duration.GOOD_TILL_CANCEL
    )


def build_bracket_buy(
    symbol: str,
    quantity: int,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
) -> OrderBuilder:
    return first_triggers_second(
        equity_buy_limit(symbol, quantity, _fmt(entry_price)),
        one_cancels_other(
            build_equity_sell_limit_gtc(symbol, quantity, take_profit_price),
            build_equity_sell_stop(symbol, quantity, stop_loss_price),
        ),
    )


class SchwabClient:
    def __init__(self, client: Client, account_hash: str):
        self._c = client
        self.account_hash = account_hash

    # ---------------- Market data ----------------

    def get_last_price(self, symbol: str, *,
                       require_fresh: bool = False) -> Optional[float]:
        resp = self._c.get_quote(symbol)
        resp.raise_for_status()
        data = resp.json()
        # Schwab keys the quote response by the UPPERCASE symbol; tolerate a
        # caller passing mixed case so a casing mismatch doesn't look like a
        # missing quote and silently suppress an otherwise-valid entry.
        node = data.get(symbol) or data.get(symbol.upper()) or {}
        quote = node.get("quote", {})
        keys = (("lastPrice", "mark") if require_fresh
                else ("lastPrice", "mark", "closePrice"))
        for key in keys:
            if quote.get(key):
                return float(quote[key])
        return None

    def get_closes(self, symbol: str, lookback: int) -> list[float]:
        # Request only a window comfortably covering `lookback` TRADING days
        # (2x calendar days + slack for weekends/holidays) — without a start
        # the API returns its multi-year default payload on every cycle.
        start = (
            datetime.now(timezone.utc) - timedelta(days=lookback * 2 + 10)
            if lookback else None
        )
        resp = self._c.get_price_history_every_day(symbol, start_datetime=start)
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        closes = [float(c["close"]) for c in candles if "close" in c]
        return closes[-lookback:] if lookback else closes

    def get_daily_closes(self, symbol: str, start: datetime,
                         end: datetime) -> dict[str, float]:
        resp = self._c.get_price_history_every_day(
            symbol, start_datetime=start, end_datetime=end
        )
        resp.raise_for_status()
        out: dict[str, float] = {}
        for c in resp.json().get("candles", []):
            if "close" not in c:
                continue
            ts = c.get("datetime", 0) / 1000.0
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            out[day] = float(c["close"])
        return out

    # ---------------- Account & positions ----------------

    def snapshot(self) -> AccountSnapshot:
        resp = self._c.get_account(
            self.account_hash, fields=Client.Account.Fields.POSITIONS
        )
        resp.raise_for_status()
        acct = resp.json().get("securitiesAccount", {})
        balances = acct.get("currentBalances", {})

        positions: list[Position] = []
        for p in acct.get("positions", []):
            instrument = p.get("instrument", {})
            symbol = instrument.get("symbol", "")
            long_qty = float(p.get("longQuantity", 0) or 0)
            short_qty = float(p.get("shortQuantity", 0) or 0)
            qty = long_qty - short_qty
            if qty == 0:
                continue
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=qty,
                    average_price=float(p.get("averagePrice", 0) or 0),
                    market_value=float(p.get("marketValue", 0) or 0),
                )
            )

        return AccountSnapshot(
            account_hash=self.account_hash,
            cash=float(balances.get("cashBalance", 0) or 0),
            equity=float(
                balances.get("liquidationValue", balances.get("equity", 0)) or 0
            ),
            buying_power=float(balances.get("buyingPower", 0) or 0),
            positions=positions,
        )

    # ---------------- Orders ----------------

    def preview(self, order: OrderBuilder) -> dict:
        resp = self._c.preview_order(self.account_hash, order)
        try:
            resp.raise_for_status()
        except Exception:
            return {"preview_ok": False, "status": resp.status_code, "body": resp.text}
        # A 2xx preview can still carry an empty or non-JSON body for some order
        # shapes; parsing it must not crash the dry-run path (the whole point of
        # preview is to surface, not raise).
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return {"preview_ok": True, "status": resp.status_code, "body": body}

    def place(self, order: OrderBuilder) -> Optional[int]:
        resp = self._c.place_order(self.account_hash, order)
        resp.raise_for_status()
        # schwab-py exposes a helper to pull the order id from the response.
        try:
            from schwab.utils import Utils
            return Utils(self._c, self.account_hash).extract_order_id(resp)
        except Exception:
            logger.warning("Order placed but could not parse order id.")
            return None

    def get_order(self, order_id: int) -> dict:
        resp = self._c.get_order(order_id, self.account_hash)
        resp.raise_for_status()
        return resp.json()

    def cancel(self, order_id: int) -> bool:
        resp = self._c.cancel_order(order_id, self.account_hash)
        # 200/201 expected; some cancels return an empty body.
        if resp.status_code >= 300:
            logger.warning("Cancel for order %s returned %s: %s",
                           order_id, resp.status_code, resp.text)
            return False
        return True

    # ensure_cancelled() polling: 5 tries x 0.4s covers the normal
    # confirmation latency without stalling an exit for more than ~2s.
    _CONFIRM_TRIES = 5
    _CONFIRM_WAIT_SECONDS = 0.4

    def ensure_cancelled(self, order_id: int) -> bool:
        self.cancel(order_id)
        for attempt in range(self._CONFIRM_TRIES):
            try:
                status = self.get_order(order_id).get("status")
            except Exception:
                logger.exception("Could not poll order %s while confirming "
                                 "cancel.", order_id)
                status = None
            if status in self.TERMINAL_STATUSES:
                return True
            if attempt < self._CONFIRM_TRIES - 1:
                time.sleep(self._CONFIRM_WAIT_SECONDS)
        logger.warning("Order %s did not reach a terminal state after cancel "
                       "(last status: %s).", order_id, status)
        return False

    # Statuses under which an order (or a leg of a strategy) can still execute.
    # Anything here must be treated as live by the stacking guard and must be
    # cancelled before a market flatten. Missing a live status fails DANGEROUS
    # (invisible resting stop -> naked short after a flatten), while including
    # a stale name is harmless — so this list errs broad. PENDING_CANCEL /
    # PENDING_REPLACE are included deliberately: a cancel/replace REQUEST does
    # not stop the original order from filling until the broker confirms it.
    LIVE_STATUSES = frozenset({
        "WORKING", "QUEUED", "ACCEPTED", "NEW",
        "PENDING_ACTIVATION", "PENDING_ACKNOWLEDGEMENT", "PENDING_REVIEW",
        "AWAITING_PARENT_ORDER", "AWAITING_CONDITION",
        "AWAITING_STOP_CONDITION", "AWAITING_RELEASE_TIME",
        "AWAITING_MANUAL_REVIEW", "AWAITING_UR_OUT",
        "PENDING_CANCEL", "PENDING_REPLACE",
    })
    # Statuses from which an order can never execute again.
    TERMINAL_STATUSES = frozenset({
        "CANCELED", "FILLED", "REJECTED", "EXPIRED", "REPLACED",
    })

    # How far back to look for still-working orders, in 60-day pages (60 days
    # is the Schwab Trader API's maximum entered-time span per query). The
    # bracket EXITS this bot builds are GTC, which Schwab keeps alive for up
    # to ~180 calendar days; a single 60-day page would hide an old-but-live
    # stop/take-profit, which both (a) lets a duplicate bracket be stacked on
    # a symbol that still has protection resting, and (b) leaves an orphaned
    # OCO leg uncancelled on exit. 4 pages = 240 days, past any GTC lifetime.
    _ORDER_PAGE_DAYS = 60
    _ORDER_PAGES = 4

    @classmethod
    def _order_is_live(cls, order: dict) -> bool:
        if order.get("status") in cls.LIVE_STATUSES:
            return True
        return any(cls._order_is_live(c)
                   for c in order.get("childOrderStrategies", []))

    @classmethod
    def live_nodes(cls, order: dict):
        if order.get("status") in cls.LIVE_STATUSES:
            yield order
        for child in order.get("childOrderStrategies", []):
            yield from cls.live_nodes(child)

    def open_orders(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        out: list[dict] = []
        seen_ids: set = set()
        for page in range(self._ORDER_PAGES):
            frm = now - timedelta(days=self._ORDER_PAGE_DAYS * (page + 1))
            to = now - timedelta(days=self._ORDER_PAGE_DAYS * page)
            try:
                resp = self._c.get_orders_for_account(
                    self.account_hash,
                    from_entered_datetime=frm,
                    to_entered_datetime=to,
                )
                resp.raise_for_status()
                orders = resp.json()
            except Exception:
                if page == 0:
                    raise
                logger.warning(
                    "Could not fetch working orders %d-%d days back; GTC "
                    "legs entered before then are invisible this cycle.",
                    self._ORDER_PAGE_DAYS * page,
                    self._ORDER_PAGE_DAYS * self._ORDER_PAGES,
                )
                break
            for o in orders:
                oid = o.get("orderId")
                if oid is not None and oid in seen_ids:
                    continue  # window boundaries touch; don't double-count
                if oid is not None:
                    seen_ids.add(oid)
                if self._order_is_live(o):
                    out.append(o)
        return out

    @staticmethod
    def _symbols_in_order(order: dict) -> set[str]:
        found: set[str] = set()
        for leg in order.get("orderLegCollection", []):
            sym = leg.get("instrument", {}).get("symbol")
            if sym:
                found.add(sym)
        for child in order.get("childOrderStrategies", []):
            found |= SchwabClient._symbols_in_order(child)
        return found

    def working_orders_for_symbol(
        self, symbol: str, orders: Optional[list[dict]] = None
    ) -> list[dict]:
        if orders is None:
            orders = self.open_orders()
        return [o for o in orders if symbol in self._symbols_in_order(o)]

    def has_working_order(self, symbol: str) -> bool:
        return len(self.working_orders_for_symbol(symbol)) > 0

    def _cancel_live_tree(self, order: dict) -> int:
        if order.get("status") in self.LIVE_STATUSES:
            oid = order.get("orderId")
            if oid is None:
                logger.warning("Live order node without an orderId; cannot "
                               "cancel it: %.200s", str(order))
                return 1
            return 0 if self.ensure_cancelled(oid) else 1
        return sum(self._cancel_live_tree(c)
                   for c in order.get("childOrderStrategies", []))

    def cancel_working_orders_for_symbol(self, symbol: str) -> int:
        return sum(self._cancel_live_tree(o)
                   for o in self.working_orders_for_symbol(symbol))

    def cancel_all_working_orders(self) -> int:
        return sum(self._cancel_live_tree(o) for o in self.open_orders())
