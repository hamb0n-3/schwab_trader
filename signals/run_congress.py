from __future__ import annotations

import argparse
import os

from config import CONFIG
from db import Database
from logging_setup import setup_logging
from notify import Notifier
from signals.congress_client import CongressClient, DEFAULT_USER_AGENT
from signals.congress_feed import CongressFeed
from signals.congress_strategy import CongressPtrStrategy, SchwabPriceProvider


def _build_price_provider(log):
    if not CONFIG.api_key or not CONFIG.app_secret:
        log.warning("Schwab credentials not configured — price-proximity "
                    "checks disabled; all signals will be INFO.")
        return None
    try:
        from auth import get_client, resolve_account_hash
        from client import SchwabClient
        raw = get_client(CONFIG.api_key, CONFIG.app_secret,
                         CONFIG.callback_url, CONFIG.token_path)
        acct = resolve_account_hash(raw, CONFIG.account_index)
        return SchwabPriceProvider(SchwabClient(raw, acct))
    except Exception as e:
        log.warning("Schwab price data unavailable (%s); emitting INFO-only "
                    "signals.", e)
        return None


def main():
    log = setup_logging("INFO", None)

    p = argparse.ArgumentParser(
        description="Congressional STOCK Act (PTR) signal feed.")
    p.add_argument("--chamber", choices=("house", "senate", "both"),
                   default="both")
    p.add_argument("--days-back", type=int, default=30,
                   help="How far back to look at FILING dates.")
    p.add_argument("--max-filings", type=int, default=300,
                   help="Cap NEW filings fetched per run across both chambers "
                        "(rate-limit safety; the rest resume next run). "
                        "0 = unlimited.")
    p.add_argument("--politicians",
                   help="Comma-separated name filter (case-insensitive "
                        "substring), e.g. 'Pelosi,Tuberville'.")
    p.add_argument("--tickers", help="Comma-separated ticker watchlist.")
    p.add_argument("--min-amount", type=float, default=15_000.0,
                   help="Min transaction size — the range's LOWER bound.")
    p.add_argument("--max-age-days", type=int, default=60,
                   help="Max TRANSACTION age (0 = no limit). Trades are "
                        "already 30-45d old when disclosed.")
    p.add_argument("--proximity", type=float, default=0.05,
                   help="BUY when |today/purchase-date close - 1| <= this "
                        "(default 0.05 = 5%%).")
    p.add_argument("--cluster", type=int, default=1,
                   help="Min distinct politicians on the same ticker+action.")
    p.add_argument("--include-sells", action="store_true",
                   help="Surface sales too (always as INFO).")
    p.add_argument("--all-assets", action="store_true",
                   help="Include non-stock assets (options, bonds, funds).")
    p.add_argument("--no-db", action="store_true",
                   help="Disable SQLite persistence (JSON --cache only).")
    p.add_argument("--no-prices", action="store_true",
                   help="Skip Schwab price checks; INFO-only signals.")
    p.add_argument("--cache", default="./congress_seen.json")
    p.add_argument("--rate", type=float, default=2.0,
                   help="Max requests/sec against the official sites.")
    p.add_argument("--user-agent",
                   default=os.environ.get("CONGRESS_USER_AGENT",
                                          DEFAULT_USER_AGENT))
    args = p.parse_args()

    db = None if args.no_db or not CONFIG.db_path else Database(CONFIG.db_path)
    if db is not None:
        # Migrate a legacy JSON seen-cache once, so switching --no-db -> DB
        # doesn't re-emit every previously-seen filing as "new".
        db.import_seen_json(args.cache)

    try:
        with CongressClient(args.user_agent,
                            max_per_second=min(args.rate, 5.0)) as client:
            feed = CongressFeed(client, cache_file=args.cache, db=db)
            log.info("Polling %s PTR filings from the last %d day(s)...",
                     args.chamber, args.days_back)
            cap = None if args.max_filings == 0 else args.max_filings
            transactions = feed.poll(chamber=args.chamber,
                                     days_back=args.days_back,
                                     max_filings=cap)
    finally:
        if db is not None:
            db.close()

    log.info("Fetched %d new congressional transaction(s).", len(transactions))

    provider = None if args.no_prices else _build_price_provider(log)
    strat = CongressPtrStrategy(
        include_sells=args.include_sells,
        min_amount=args.min_amount,
        max_age_days=args.max_age_days,
        politicians={s.strip().lower() for s in
                     (args.politicians or "").split(",") if s.strip()},
        tickers={s.strip().upper() for s in
                 (args.tickers or "").split(",") if s.strip()},
        stock_only=not args.all_assets,
        cluster_min=args.cluster,
        proximity_threshold=args.proximity,
        prices=provider,
    )
    signals = strat.signals_from_transactions(transactions)

    if not signals:
        log.info("No qualifying congressional signals.")
        return

    by_action: dict[str, list] = {}
    for s in signals:
        by_action.setdefault(s.action.value, []).append(s)
    for action in ("BUY", "INFO"):
        rows = by_action.get(action)
        if not rows:
            continue
        print(f"\n=== Congressional {action} signals ===")
        for s in rows:
            print(f"  {s.symbol}: {s.reason}")
    # ONE combined push (not one per signal) to the informational channel.
    Notifier(CONFIG).info("\n".join(
        [f"Congressional signals ({len(signals)}):"]
        + [f"[{a}] {s.symbol}: {s.reason}"
           for a in ("BUY", "INFO") for s in by_action.get(a, [])]
    ))
    print("\nThese are INFORMATIONAL. STOCK Act disclosures lag trades by "
          "30-45 days; the BUY gate only means the price hasn't moved since "
          "the disclosed purchase, not that it will. Backtest before "
          "trusting, and route any real orders through the bot's dry-run + "
          "risk layer.\n")


if __name__ == "__main__":
    main()
