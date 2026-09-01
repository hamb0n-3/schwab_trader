from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Protocol

from signals.congress_parsers import CongressTransaction
from strategy import Action, Signal

logger = logging.getLogger("schwab_bot.congress.strategy")


# --------------------------- price access ---------------------------

class PriceProvider(Protocol):
    def daily_closes(self, symbol: str, start: date, end: date) -> dict[str, float]:
        ...

    def last_price(self, symbol: str) -> Optional[float]:
        ...


class SchwabPriceProvider:

    def __init__(self, sc):
        self._sc = sc

    def daily_closes(self, symbol: str, start: date, end: date) -> dict[str, float]:
        try:
            return self._sc.get_daily_closes(
                symbol,
                datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
                datetime(end.year, end.month, end.day, 23, 59,
                         tzinfo=timezone.utc),
            )
        except Exception as e:
            logger.warning("No price history for %s: %s", symbol, e)
            return {}

    def last_price(self, symbol: str) -> Optional[float]:
        try:
            return self._sc.get_last_price(symbol)
        except Exception as e:
            logger.warning("No live quote for %s: %s", symbol, e)
            return None


def _tx_date(t: CongressTransaction) -> Optional[date]:
    try:
        return date.fromisoformat(t.transaction_date)
    except ValueError:
        return None


def close_on_or_before(closes: dict[str, float], tx_date: date,
                       max_back_days: int = 7) -> Optional[tuple[str, float]]:
    for back in range(max_back_days + 1):
        day = (tx_date - timedelta(days=back)).isoformat()
        if day in closes:
            return day, closes[day]
    return None


# --------------------------- the strategy ---------------------------

@dataclass
class CongressPtrStrategy:
    # Surface sells (as INFO)? Off by default — see module docstring.
    include_sells: bool = False
    # Minimum size, compared against the range's LOWER bound (the only number
    # the filing guarantees). A tx whose amount failed to parse cannot confirm
    # its size and is rejected whenever a minimum is set.
    min_amount: float = 15_000.0
    # Maximum age of the TRANSACTION (not the filing): with the 30-45 day
    # legal lag, 60 keeps roughly "filed recently about a recent-ish trade".
    max_age_days: int = 60
    # Case-insensitive substring match on the politician name; empty = all.
    politicians: set[str] = field(default_factory=set)
    # Ticker watchlist; empty = all.
    tickers: set[str] = field(default_factory=set)
    # Only common-stock rows (House [ST] / Senate "Stock"), ticker required.
    stock_only: bool = True
    # Require N DISTINCT politicians on the same (ticker, action).
    cluster_min: int = 1
    # |today/then - 1| <= this  ->  "hasn't run yet"  ->  BUY.
    proximity_threshold: float = 0.05
    # None -> no price checks; every qualifying group becomes INFO.
    prices: Optional[PriceProvider] = None

    # ---- filters (mirrors insider_strategy) ----

    def _passes_filters(self, t: CongressTransaction, today: date) -> bool:
        if t.tx_type == "P":
            pass
        elif t.tx_type == "S" and self.include_sells:
            pass
        else:
            return False  # exchanges and (by default) sells
        if self.stock_only and not (t.is_stock and t.ticker):
            return False
        if not t.ticker and self.tickers:
            return False
        if self.tickers and t.ticker not in self.tickers:
            return False
        if self.min_amount > 0:
            # Unknown size cannot be confirmed >= minimum: reject.
            if t.amount_min is None or t.amount_min < self.min_amount:
                return False
        if self.politicians:
            name = t.politician.lower()
            if not any(p in name for p in self.politicians):
                return False
        # Age gate on the TRANSACTION date. Same rule as the insider feed:
        # when an age limit is active, a missing/unparseable date cannot be
        # confirmed recent — reject it.
        if self.max_age_days:
            if not t.transaction_date:
                return False
            try:
                tx_d = date.fromisoformat(t.transaction_date)
            except ValueError:
                return False
            if (today - tx_d).days > self.max_age_days:
                return False
        return True

    # ---- signal emission ----

    def signals_from_transactions(
        self, transactions: list[CongressTransaction],
        today: date | None = None,
    ) -> list[Signal]:
        today = today or date.today()
        qualifying = [t for t in transactions if self._passes_filters(t, today)]

        # Group by (ticker, tx_type), then apply the cluster gate ONCE — both
        # the price-batching pre-pass and the emission loop below iterate the
        # same filtered dict, so the two can never disagree about which
        # groups qualify (no prices fetched for dropped groups, no emitted
        # group missing its prices).
        grouped: dict[tuple[str, str], list[CongressTransaction]] = defaultdict(list)
        for t in qualifying:
            grouped[(t.ticker, t.tx_type)].append(t)
        clustered = {
            key: txs for key, txs in grouped.items()
            if len({x.politician for x in txs}) >= self.cluster_min
        }

        # PRICE BATCHING: one daily_closes + one last_price call per distinct
        # ticker, covering that ticker's oldest transaction date -> today.
        price_ctx: dict[str, tuple[dict[str, float], Optional[float]]] = {}
        if self.prices is not None:
            need: dict[str, date] = {}
            for (ticker, tx_type), txs in clustered.items():
                if tx_type != "P" or not ticker:
                    continue
                dates = [d for d in (_tx_date(x) for x in txs) if d is not None]
                if not dates:
                    continue
                oldest = min(dates)
                if ticker not in need or oldest < need[ticker]:
                    need[ticker] = oldest
            for ticker, oldest in need.items():
                closes = self.prices.daily_closes(
                    ticker, oldest - timedelta(days=10), today)
                last = self.prices.last_price(ticker)
                if last is None and closes:
                    # Fall back to the latest close we already have.
                    last = closes[max(closes)]
                price_ctx[ticker] = (closes, last)

        signals: list[Signal] = []
        for (ticker, tx_type), txs in sorted(clustered.items()):
            who = {x.politician for x in txs}
            total_lo = sum(x.amount_min or 0 for x in txs)
            names = ", ".join(sorted(who)[:3])
            label = ticker or txs[0].asset_description[:30]
            base = (f"{len(who)} member(s) of Congress bought ${total_lo:,.0f}+ "
                    f"of {label} (e.g. {names})")

            if tx_type == "S":
                signals.append(Signal(
                    label, Action.INFO,
                    f"{len(who)} member(s) of Congress sold {label} "
                    f"(${total_lo:,.0f}+; e.g. {names}) — sells are weak "
                    f"signals (taxes/diversification)."))
                continue

            # ---- purchases: the proximity gate ----
            closes, last = price_ctx.get(ticker, ({}, None))
            best = None  # (abs_dev, dev, tx, base_day, base_close)
            for x in txs:
                xd = _tx_date(x)
                if xd is None:
                    continue
                ref = close_on_or_before(closes, xd)
                if ref is None or last is None or ref[1] <= 0:
                    continue
                dev = (last - ref[1]) / ref[1]
                key = (abs(dev), x.transaction_date)
                if best is None or key < (best[0], best[2].transaction_date):
                    best = (abs(dev), dev, x, ref[0], ref[1])

            if best is None:
                signals.append(Signal(
                    label, Action.INFO,
                    f"{base} — price check unavailable, cannot judge whether "
                    f"it has already run."))
                continue

            _, dev, x, base_day, base_close = best
            detail = (f"{x.politician} bought {x.amount_raw or '?'} on "
                      f"{x.transaction_date} (close ~${base_close:,.2f}); "
                      f"now ${last:,.2f} ({dev:+.1%})")
            if abs(dev) <= self.proximity_threshold:
                signals.append(Signal(
                    ticker, Action.BUY,
                    f"{base}. {detail} — hasn't run yet."))
            elif dev > 0:
                signals.append(Signal(
                    ticker, Action.INFO,
                    f"{base}. {detail} — already ran; you'd be chasing."))
            else:
                signals.append(Signal(
                    ticker, Action.INFO,
                    f"{base}. {detail} — down since the buy; surfaced for "
                    f"review, not a BUY."))
        return signals
