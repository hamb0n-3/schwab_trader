from __future__ import annotations

import argparse
import os
import sys

from config import CONFIG
from db import Database
from logging_setup import setup_logging
from notify import Notifier
from signals.edgar_client import EdgarClient
from signals.form4_feed import Form4Feed
from signals.insider_strategy import InsiderBuyStrategy


def main():
    log = setup_logging("INFO", None)

    p = argparse.ArgumentParser(description="EDGAR Form 4 insider feed.")
    # ---- mode: choose exactly one of tickers / insiders / ciks / firehose ----
    p.add_argument("--tickers", help="Comma-separated issuer watchlist "
                   "(track who trades THESE stocks)")
    p.add_argument("--insiders", help="Comma-separated insider NAMES "
                   "(track these PEOPLE across all companies)")
    p.add_argument("--ciks", help="Comma-separated reporting-owner CIKs "
                   "(track these people by exact CIK — names aren't unique)")
    p.add_argument("--firehose", action="store_true",
                   help="Scan the EDGAR daily index for ALL Form 4 filers "
                        "(market-wide discovery; high volume — see --max-filings)")
    p.add_argument("--days-back", type=int, default=1,
                   help="Firehose: how many days of the daily index to scan.")
    p.add_argument("--max-filings", type=int, default=300,
                   help="Firehose: cap filings processed per run (rate-limit "
                        "safety). 0 = unlimited.")
    p.add_argument("--group-by", choices=("ticker", "insider"), default="ticker",
                   help="Aggregate signals by ticker (default) or by insider "
                        "('who is trading across the set').")
    p.add_argument("--user-agent", default=os.environ.get("EDGAR_USER_AGENT", ""),
                   help="Contact UA: 'MyBot/1.0 (you@example.com)'")
    p.add_argument("--min-value", type=float, default=100_000.0)
    p.add_argument("--cluster", type=int, default=1,
                   help="Min distinct insiders for the same ticker+action")
    p.add_argument("--max-age-days", type=int, default=14)
    p.add_argument("--all-roles", action="store_true",
                   help="Include 10%% holders, not just officers/directors")
    p.add_argument("--codes", default="P",
                   help="Comma-separated Form 4 codes to surface "
                        "(P=buy, S=sell, M=exercise, A=grant, G=gift, F=tax). "
                        "Default: P.")
    p.add_argument("--include-derivative", action="store_true",
                   help="Also surface derivative transactions (e.g. option "
                        "exercises). Default: non-derivative (share) only.")
    p.add_argument("--cache", default="./edgar_seen.json")
    p.add_argument("--rate", type=float, default=5.0, help="Max requests/sec (<=10)")
    p.add_argument("--no-db", action="store_true",
                   help="Disable SQLite persistence (use the JSON --cache only).")
    args = p.parse_args()

    if not args.user_agent or "@" not in args.user_agent:
        log.error("A contact User-Agent is required by SEC. "
                  "Use --user-agent 'MyBot/1.0 (you@example.com)' "
                  "or set EDGAR_USER_AGENT.")
        sys.exit(1)

    # Exactly one mode.
    modes = [bool(args.tickers), bool(args.insiders), bool(args.ciks), args.firehose]
    if sum(modes) != 1:
        log.error("Choose exactly ONE of --tickers / --insiders / --ciks / "
                  "--firehose.")
        sys.exit(1)

    db = None if args.no_db or not CONFIG.db_path else Database(CONFIG.db_path)
    if db is not None:
        # Migrate the legacy JSON seen-cache once, so switching from --no-db to
        # the DB doesn't re-emit every previously-seen filing as "new".
        db.import_legacy(CONFIG, edgar_cache_path=args.cache)

    try:
        with EdgarClient(args.user_agent, max_per_second=min(args.rate, 9.0)) as client:
            feed = Form4Feed(client, cache_file=args.cache, db=db)
            if args.tickers:
                tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
                log.info("Polling EDGAR Form 4 for tickers: %s", ", ".join(tickers))
                transactions = feed.poll(tickers)
            elif args.insiders:
                names = [n.strip() for n in args.insiders.split(",") if n.strip()]
                log.info("Polling EDGAR Form 4 for insiders: %s", ", ".join(names))
                transactions = feed.poll_owners(names)
            elif args.ciks:
                ciks = [c.strip() for c in args.ciks.split(",") if c.strip()]
                log.info("Polling EDGAR Form 4 for owner CIKs: %s", ", ".join(ciks))
                transactions = feed.poll_owners(ciks)
            else:  # firehose
                cap = None if args.max_filings == 0 else args.max_filings
                log.info("Firehose: scanning %d day(s) of the EDGAR daily index "
                         "(cap %s).", args.days_back, cap if cap else "none")
                transactions = feed.poll_firehose(days_back=args.days_back,
                                                  max_filings=cap)
    finally:
        if db is not None:
            db.close()

    log.info("Fetched %d new insider transaction(s).", len(transactions))

    codes = {c.strip().upper() for c in args.codes.split(",") if c.strip()}
    strat = InsiderBuyStrategy(
        codes=codes,
        include_derivative=args.include_derivative,
        min_dollar_value=args.min_value,
        officers_directors_only=not args.all_roles,
        max_age_days=args.max_age_days,
        cluster_min=args.cluster,
        group_by=args.group_by,
    )
    signals = strat.signals_from_transactions(transactions)

    if not signals:
        log.info("No qualifying insider signals for codes %s.",
                 ",".join(sorted(codes)))
        return
    # Group the output by action so BUY / EXIT / INFO are visually separated.
    by_action: dict[str, list] = {}
    for s in signals:
        by_action.setdefault(s.action.value, []).append(s)
    for action in ("BUY", "EXIT", "INFO"):
        rows = by_action.get(action)
        if not rows:
            continue
        print(f"\n=== Insider {action} signals ===")
        for s in rows:
            print(f"  {s.symbol}: {s.reason}")
    # ONE combined push (not one per signal) to the informational channel.
    Notifier(CONFIG).info("\n".join(
        [f"Insider signals ({len(signals)}):"]
        + [f"[{a}] {s.symbol}: {s.reason}"
           for a in ("BUY", "EXIT", "INFO") for s in by_action.get(a, [])]
    ))
    print("\nThese are INFORMATIONAL. To act on them, wire the feed into the bot "
          "behind the dry-run + risk layer. Mirroring insider activity is a slow, "
          "weak, widely-arbitraged signal — backtest with the disclosure lag "
          "before trusting it.\n")


if __name__ == "__main__":
    main()
