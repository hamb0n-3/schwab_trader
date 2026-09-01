from __future__ import annotations

import argparse
import csv
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from strategy import Action, SmaCrossStrategy, Strategy

logger = logging.getLogger("schwab_bot.backtest")


# --------------------------- data structures ---------------------------

@dataclass
class Bar:
    date: str
    close: float
    high: float
    low: float
    open: float


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: int
    reason: str  # why we exited: TAKE_PROFIT / STOP_LOSS / SIGNAL / EOD
    commissions: float = 0.0  # round-trip commission charged on this trade

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.shares - self.commissions

    @property
    def return_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price


@dataclass
class BacktestResult:
    symbol: str
    starting_cash: float
    ending_equity: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        if self.starting_cash == 0:
            return 0.0
        return (self.ending_equity - self.starting_cash) / self.starting_cash

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades)

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.trades if t.pnl < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        if gross_loss == 0:
            return float("inf") if gross_win > 0 else 0.0
        return gross_win / gross_loss

    @property
    def max_drawdown_pct(self) -> float:
        peak = -float("inf")
        max_dd = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                dd = (peak - eq) / peak
                max_dd = max(max_dd, dd)
        return max_dd

    def summary(self) -> str:
        lines = [
            f"Backtest result — {self.symbol}",
            f"  Trades:          {self.num_trades}",
            f"  Win rate:        {self.win_rate:.1%}",
            f"  Total return:    {self.total_return_pct:+.1%}",
            f"  Ending equity:   ${self.ending_equity:,.2f} "
            f"(from ${self.starting_cash:,.2f})",
            f"  Avg win / loss:  ${self.avg_win:,.2f} / ${self.avg_loss:,.2f}",
            f"  Profit factor:   {self.profit_factor:.2f}",
            f"  Max drawdown:    {self.max_drawdown_pct:.1%}",
        ]
        return "\n".join(lines)


# --------------------------- the engine ---------------------------

@dataclass
class Backtester:
    strategy: Strategy
    starting_cash: float = 10_000.0
    position_size_usd: float = 1_000.0
    take_profit_pct: float = 0.05
    stop_loss_pct: float = 0.03
    commission_per_trade: float = 0.0     # Schwab equities are $0; set if you wish
    slippage_pct: float = 0.0005          # 5 bps per fill, applied adversely
    warmup_bars: int = 1                  # strategy decides its own min history

    def run(self, symbol: str, bars: list[Bar]) -> BacktestResult:
        cash = self.starting_cash
        shares = 0
        entry_price = 0.0
        entry_date = ""
        tp_price = 0.0
        sl_price = 0.0
        # Signal generated at the PRIOR bar's close, to execute at THIS bar's
        # open. You cannot observe a completed daily close and still trade at
        # that close — filling signals on the close that produced them is
        # lookahead, and it also diverges from the live bot, whose next action
        # after an end-of-day cross lands in the next session.
        pending = None

        result = BacktestResult(symbol, self.starting_cash, self.starting_cash)
        closes: list[float] = []
        round_trip = 2 * self.commission_per_trade

        def close_position(date: str, fill: float, reason: str) -> None:
            nonlocal cash, shares
            cash += fill * shares - self.commission_per_trade
            result.trades.append(Trade(
                symbol, entry_date, entry_price, date, fill, shares, reason,
                commissions=round_trip))
            shares = 0

        for i, bar in enumerate(bars):
            closes.append(bar.close)

            # ---- 1. Execute the prior close's signal at THIS bar's open ----
            if pending is Action.EXIT and shares > 0:
                close_position(bar.date, self._sell_fill(bar.open), "SIGNAL")
            elif pending is Action.BUY and shares == 0:
                fill = self._buy_fill(bar.open)
                qty = int(math.floor(self.position_size_usd / fill)) if fill > 0 else 0
                cost = qty * fill + self.commission_per_trade
                if qty > 0 and cost <= cash:
                    cash -= cost
                    shares = qty
                    entry_price = fill
                    entry_date = bar.date
                    tp_price = round(entry_price * (1 + self.take_profit_pct), 2)
                    sl_price = round(entry_price * (1 - self.stop_loss_pct), 2)
            pending = None

            # ---- 2. Bracket exits against THIS bar, gap-aware ----
            # A same-bar entry fills at the open, so the gap branches below
            # can't trigger on it (its stop sits below and its target above
            # that same open); only the intrabar branches can — which is what
            # a live bracket armed at the open would do.
            if shares > 0:
                exit_price = None
                reason = ""
                if bar.open <= sl_price:
                    # Gapped through the stop: a stop-market triggers at the
                    # open and fills there — booking sl_price would flatten
                    # every overnight gap-down to the configured -SL%, hiding
                    # exactly the tail risk a backtest must show.
                    exit_price, reason = bar.open, "STOP_LOSS"
                elif bar.open >= tp_price:
                    # Gapped above the target: the resting limit fills at the
                    # (better) opening print, not at the limit price.
                    exit_price, reason = bar.open, "TAKE_PROFIT"
                elif bar.low <= sl_price:
                    # Both touched intrabar -> stop first (pessimistic).
                    exit_price, reason = sl_price, "STOP_LOSS"
                elif bar.high >= tp_price:
                    exit_price, reason = tp_price, "TAKE_PROFIT"
                if exit_price is not None:
                    close_position(bar.date, self._sell_fill(exit_price), reason)

            # ---- 3. Evaluate the strategy on closes through THIS bar; the
            # resulting signal executes at the NEXT bar's open ----
            if i + 1 >= self.warmup_bars:
                signal = self.strategy.evaluate(symbol, closes, holding=shares > 0)
                if signal.action in (Action.BUY, Action.EXIT):
                    pending = signal.action

            # ---- 4. Mark-to-market equity for the curve ----
            equity = cash + shares * bar.close
            result.equity_curve.append((bar.date, equity))

        # ---- close any open position at the last bar (EOD) ----
        if shares > 0:
            last = bars[-1]
            close_position(last.date, self._sell_fill(last.close), "EOD")

        result.ending_equity = cash
        if result.equity_curve:
            result.equity_curve[-1] = (result.equity_curve[-1][0], cash)
        return result

    # slippage is applied adversely: you buy a touch higher, sell a touch lower
    def _buy_fill(self, price: float) -> float:
        return round(price * (1 + self.slippage_pct), 4)

    def _sell_fill(self, price: float) -> float:
        return round(price * (1 - self.slippage_pct), 4)


# --------------------------- data loading ---------------------------

_CSV_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")


def _parse_csv_date(raw: str) -> datetime:
    for fmt in _CSV_DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(
        f"Unrecognized date {raw!r} in CSV (expected one of: "
        f"{', '.join(_CSV_DATE_FORMATS)}). The engine must be able to order "
        f"bars chronologically — SMAs computed on out-of-order bars are "
        f"silently, confidently wrong."
    )


def load_csv(path: str) -> list[Bar]:
    bars: list[Bar] = []
    keyed: list[tuple[datetime, Bar]] = []
    # utf-8-sig: Excel/Windows CSVs carry a BOM that would otherwise glue
    # itself to the first header ('﻿date'), hiding the date column.
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.lower(): c for c in (reader.fieldnames or [])}
        if "close" not in cols:
            raise ValueError("CSV must have a 'close' column.")
        for row in reader:
            close = float(row[cols["close"]])
            high = float(row[cols["high"]]) if "high" in cols else close
            low = float(row[cols["low"]]) if "low" in cols else close
            op = float(row[cols["open"]]) if "open" in cols else close
            date = row[cols["date"]] if "date" in cols else str(len(bars))
            bar = Bar(date=date, close=close, high=high, low=low, open=op)
            bars.append(bar)
            if "date" in cols:
                keyed.append((_parse_csv_date(date), bar))
    if keyed:
        keyed.sort(key=lambda kb: kb[0])
        seen_dates = {k for k, _ in keyed}
        if len(seen_dates) != len(keyed):
            logger.warning("CSV contains duplicate dates — check the export.")
        bars = [b for _, b in keyed]
    return bars


def load_from_schwab(symbol: str, years: int) -> list[Bar]:
    from config import CONFIG
    from auth import get_client, resolve_account_hash
    from schwab.client import Client

    raw = get_client(CONFIG.api_key, CONFIG.app_secret,
                     CONFIG.callback_url, CONFIG.token_path)
    # We only need market data; account hash isn't required for price history,
    # but resolving it confirms the token works.
    resolve_account_hash(raw, CONFIG.account_index)

    resp = raw.get_price_history_every_day(symbol)
    resp.raise_for_status()
    candles = resp.json().get("candles", [])
    bars: list[Bar] = []
    # Use UTC consistently: candle timestamps are epoch-ms UTC, so a naive local
    # now() would shift the cutoff by the host's UTC offset, and 365-day "years"
    # ignore leap days. 365.25 + UTC keeps the window accurate and the date
    # labels stable regardless of the machine's timezone.
    cutoff = datetime.now(timezone.utc).timestamp() - round(years * 365.25) * 24 * 3600
    for c in candles:
        # Schwab candle timestamps are epoch milliseconds.
        ts = c.get("datetime", 0) / 1000.0
        if ts < cutoff:
            continue
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        bars.append(Bar(
            date=date,
            close=float(c["close"]),
            high=float(c.get("high", c["close"])),
            low=float(c.get("low", c["close"])),
            open=float(c.get("open", c["close"])),
        ))
    # Drop today's candle if present: run intraday, Schwab includes the
    # still-forming bar, which would feed a partial day's "close" into the
    # final signal and the EOD mark.
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if bars and bars[-1].date == today_utc:
        logger.info("Excluding today's still-forming candle (%s).", bars[-1].date)
        bars = bars[:-1]
    return bars


# --------------------------- CLI ---------------------------

def main():
    from logging_setup import setup_logging
    setup_logging("INFO", None)

    p = argparse.ArgumentParser(description="Backtest a strategy on daily bars.")
    p.add_argument("--symbol", default="AAPL")
    p.add_argument("--csv", help="CSV file with daily bars (date,close[,open,high,low])")
    p.add_argument("--years", type=int, default=5, help="Years of history (Schwab source)")
    p.add_argument("--fast", type=int, default=20, help="Fast SMA window")
    p.add_argument("--slow", type=int, default=50, help="Slow SMA window")
    p.add_argument("--cash", type=float, default=10_000.0)
    p.add_argument("--size", type=float, default=1_000.0, help="$ per position")
    p.add_argument("--tp", type=float, default=0.05, help="Take-profit fraction")
    p.add_argument("--sl", type=float, default=0.03, help="Stop-loss fraction")
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--slippage", type=float, default=0.0005, help="Per-fill, e.g. 0.0005 = 5bps")
    p.add_argument("--db", help="SQLite path to save results (default: SCHWAB_DB_PATH).")
    p.add_argument("--no-db", action="store_true", help="Do not persist results.")
    args = p.parse_args()

    if args.csv:
        logger.info("Loading bars from CSV: %s", args.csv)
        bars = load_csv(args.csv)
    else:
        logger.info("Loading %d years of daily bars for %s from Schwab...",
                    args.years, args.symbol)
        bars = load_from_schwab(args.symbol, args.years)

    if len(bars) < args.slow + 2:
        logger.error("Not enough bars (%d) for SMA(%d). Need more history.",
                     len(bars), args.slow)
        return

    logger.info("Loaded %d bars (%s to %s).",
                len(bars), bars[0].date, bars[-1].date)

    strategy = SmaCrossStrategy(fast=args.fast, slow=args.slow)
    bt = Backtester(
        strategy=strategy,
        starting_cash=args.cash,
        position_size_usd=args.size,
        take_profit_pct=args.tp,
        stop_loss_pct=args.sl,
        commission_per_trade=args.commission,
        slippage_pct=args.slippage,
    )
    result = bt.run(args.symbol, bars)
    print("\n" + result.summary() + "\n")

    # Persist the run (header + per-trade rows) unless disabled.
    if args.no_db and args.db:
        logger.warning("--db is ignored because --no-db was given.")
    if not args.no_db:
        from config import CONFIG
        from db import Database
        db_path = args.db or CONFIG.db_path
        if db_path:
            with Database(db_path) as db:   # closes even if record_backtest raises
                bt_id = db.record_backtest(
                    result, fast=args.fast, slow=args.slow,
                    take_profit_pct=args.tp, stop_loss_pct=args.sl,
                    bars_from=bars[0].date, bars_to=bars[-1].date,
                )
            logger.info("Saved backtest #%d (%d trades) to %s.",
                        bt_id, result.num_trades, db_path)

    # Buy-and-hold benchmark for honest comparison.
    bh_shares = int(args.cash // bars[0].close)
    bh_end = args.cash - bh_shares * bars[0].close + bh_shares * bars[-1].close
    bh_return = (bh_end - args.cash) / args.cash
    print(f"Buy-and-hold benchmark: {bh_return:+.1%} "
          f"(${bh_end:,.2f} from ${args.cash:,.2f})")
    verdict = "BEAT" if result.total_return_pct > bh_return else "LAGGED"
    print(f"Strategy {verdict} buy-and-hold by "
          f"{abs(result.total_return_pct - bh_return):.1%}.\n")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# HONEST LIMITATIONS — read before trusting any number this prints:
#
# 1. Daily bars only. Intraday path within a bar is unknown; the engine
#    assumes the stop fills before the target when both are touched in one
#    bar, fills signals at the next bar's open, and fills gapped-through
#    stops/targets at the open. Real fills could still be better or worse —
#    and the live bot polls intraday, so it can act on partial-day crosses
#    that never survive to the close; expect live to trade MORE often than
#    this simulates.
# 2. No overfitting protection. Tuning --fast/--slow/--tp/--sl until the number
#    looks great is curve-fitting. Always reserve out-of-sample data and expect
#    live results to be worse than any backtest.
# 3. Costs are modeled but approximate. Real slippage varies with liquidity,
#    order size, and volatility; thin names slip far more than 5 bps.
# 4. Survivorship bias. If you only test symbols that exist today, you've
#    excluded the ones that went to zero — inflating results.
# 5. A good backtest is necessary but NOT sufficient. It can only disprove a
#    bad strategy; it can't prove a good one. Paper-trade before going live.
# ---------------------------------------------------------------------------
