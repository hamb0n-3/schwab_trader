"""
run.py — Friendly command-line entry point.

Usage:
    python run.py check       # validate config, authenticate, show account
    python run.py quote AAPL  # print a live quote (tests data entitlement)
    python run.py preview     # build a sample bracket and preview it (no order)
    python run.py run         # start the trading loop
    python run.py panic       # create the kill-switch file (halts/flattens a running bot)
    python run.py resume      # remove the kill-switch file
    python run.py history [N]  # recent orders, equity curve, insider & congress buys
"""

from __future__ import annotations

import os
import sys

from config import CONFIG, LIVE_CONFIRM_PHRASE
from logging_setup import setup_logging


def _client():
    from auth import get_client, resolve_account_hash
    from client import SchwabClient
    raw = get_client(CONFIG.api_key, CONFIG.app_secret, CONFIG.callback_url, CONFIG.token_path)
    acct = resolve_account_hash(raw, CONFIG.account_index)
    return SchwabClient(raw, acct)


def cmd_check():
    log = setup_logging(CONFIG.log_level, CONFIG.log_file)
    for w in CONFIG.warnings():
        log.warning("CONFIG: %s", w)
    problems = CONFIG.validate()
    for p in problems:
        log.error("CONFIG: %s", p)
    if not CONFIG.api_key or not CONFIG.app_secret:
        log.error("Missing credentials — cannot continue.")
        return
    sc = _client()
    snap = sc.snapshot()
    log.info("Account equity: $%.2f | cash: $%.2f | buying power: $%.2f",
             snap.equity, snap.cash, snap.buying_power)
    log.info("Open positions: %d", len(snap.positions))
    for p in snap.positions:
        log.info("  %s: %g shares @ $%.2f (mv $%.2f)",
                 p.symbol, p.quantity, p.average_price, p.market_value)
    mode = "LIVE (real money)" if CONFIG.live_enabled else "DRY-RUN"
    log.info("Trading mode: %s", mode)


def cmd_quote(symbol: str):
    log = setup_logging(CONFIG.log_level, CONFIG.log_file)
    sc = _client()
    price = sc.get_last_price(symbol)
    log.info("%s last price: %s", symbol.upper(), price)


def cmd_preview():
    log = setup_logging(CONFIG.log_level, CONFIG.log_file)
    from client import build_bracket_buy
    sc = _client()
    symbol = CONFIG.symbols[0]
    price = sc.get_last_price(symbol) or 100.0
    qty = max(1, int(CONFIG.position_size_usd // price))
    entry = round(price * 1.001, 2)
    tp = round(entry * (1 + CONFIG.take_profit_pct), 2)
    sl = round(entry * (1 - CONFIG.stop_loss_pct), 2)
    order = build_bracket_buy(symbol, qty, entry, tp, sl)
    log.info("Previewing bracket: %s BUY %d @ %.2f TP %.2f SL %.2f",
             symbol, qty, entry, tp, sl)
    result = sc.preview(order)
    log.info("preview_order ok=%s status=%s", result.get("preview_ok"), result.get("status"))
    if not result.get("preview_ok"):
        log.warning("body: %s", str(result.get("body"))[:800])


def cmd_run():
    from bot import main
    if not CONFIG.dry_run and not CONFIG.live_enabled:
        print("\n*** DRY_RUN is off but LIVE_CONFIRM is not set correctly. ***")
        print(f"*** To trade real money, set LIVE_CONFIRM={LIVE_CONFIRM_PHRASE} ***")
        print("*** The bot will run but stay in safe (no-order) mode.       ***\n")
    main()


def cmd_panic():
    path = CONFIG.kill_switch_file
    with open(path, "w") as f:
        f.write("halt\n")
    print(f"Kill switch ENGAGED: {path}\n"
          f"A running bot will flatten all positions and stop on its next cycle.")


def cmd_resume():
    path = CONFIG.kill_switch_file
    if os.path.exists(path):
        os.remove(path)
        print(f"Kill switch cleared: {path}")
    else:
        print("No kill switch file present.")


def cmd_history(limit: int = 20):
    if not CONFIG.db_path:
        print("No database configured (SCHWAB_DB_PATH is empty).")
        return
    if not os.path.exists(CONFIG.db_path):
        print(f"No database file yet at {CONFIG.db_path}. "
              f"Run the bot or a backtest first.")
        return

    from db import Database
    print(f"\n=== schwab_trader history ({CONFIG.db_path}) ===\n")
    with Database(CONFIG.db_path) as db:   # closes even if a read raises
        _render_history(db, limit)


def _render_history(db, limit: int) -> None:

    counts = db.order_status_counts()
    if counts:
        print("Orders by status: "
              + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        print("Orders by status: (none)")

    orders = db.recent_orders(limit)
    print(f"\n-- Last {len(orders)} order decision(s) --")
    for o in orders:
        bits = [o["ts"][:19], o["mode"], o["status"], o["side"],
                o["order_kind"], o["symbol"], f"qty={o['quantity']:g}"]
        if o["limit_price"] is not None:
            bits.append(f"@{o['limit_price']:.2f}")
        if o["broker_order_id"]:
            bits.append(f"id={o['broker_order_id']}")
        if o["reason"]:
            bits.append(f"({o['reason']})")
        print("  " + " ".join(bits))
    if not orders:
        print("  (none)")

    curve = db.equity_curve()
    print("\n-- Equity curve --")
    if curve:
        eqs = [r["equity"] for r in curve]
        print(f"  snapshots: {len(curve)}  "
              f"first ${eqs[0]:,.2f} ({curve[0]['ts'][:19]})  "
              f"last ${eqs[-1]:,.2f} ({curve[-1]['ts'][:19]})")
        print(f"  min ${min(eqs):,.2f}  max ${max(eqs):,.2f}")
    else:
        print("  (no snapshots yet)")

    buys = db.recent_insider_buys(limit)
    if buys:
        print(f"\n-- Last {len(buys)} insider open-market buy(s) --")
        for b in buys:
            print(f"  {b['transaction_date']} {b['ticker']} {b['owner_name']} "
                  f"{b['shares']:g} @ ${b['price_per_share']:.2f} "
                  f"(${b['dollar_value']:,.0f})")

    cbuys = db.recent_congress_buys(limit)
    if cbuys:
        print(f"\n-- Last {len(cbuys)} congressional purchase(s) --")
        for c in cbuys:
            lo = f"${c['amount_min']:,.0f}" if c['amount_min'] is not None else "?"
            hi = (f"${c['amount_max']:,.0f}" if c['amount_max'] is not None
                  else "open")
            print(f"  {c['transaction_date']} {c['ticker'] or '?'} "
                  f"{c['politician']} ({c['chamber']}) {lo}-{hi}")

    bts = db.recent_backtests(limit)
    if bts:
        print(f"\n-- Last {len(bts)} backtest(s) --")
        for b in bts:
            print(f"  #{b['id']} {b['symbol']} SMA({b['fast']}/{b['slow']}) "
                  f"{b['bars_from']}..{b['bars_to']}  ret {b['total_return_pct']:+.1%}  "
                  f"trades {b['num_trades']}  win {b['win_rate']:.0%}")

    print()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1].lower()
    if cmd == "check":
        cmd_check()
    elif cmd == "quote":
        if len(sys.argv) < 3:
            print("Usage: python run.py quote SYMBOL")
            return
        cmd_quote(sys.argv[2])
    elif cmd == "preview":
        cmd_preview()
    elif cmd == "run":
        cmd_run()
    elif cmd == "panic":
        cmd_panic()
    elif cmd == "resume":
        cmd_resume()
    elif cmd == "history":
        limit = (int(sys.argv[2])
                 if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20)
        cmd_history(limit)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
