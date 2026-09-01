from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from signals.form4_parser import InsiderTransaction
from strategy import Action, Signal

logger = logging.getLogger("schwab_bot.edgar.strategy")

# Form 4 transaction code -> the Signal action we emit for it. Codes not listed
# here fall back to INFO (surfaced, but not a trade directive). See form4_parser
# for the full code reference (P buy, S sell, A grant, M exercise, G gift, F tax).
CODE_ACTION = {
    "P": Action.BUY,    # open-market purchase
    "S": Action.EXIT,   # open-market sale
    "M": Action.INFO,   # exercise of a derivative (option)
    "A": Action.INFO,   # grant / award (compensation)
    "G": Action.INFO,   # gift
    "F": Action.INFO,   # shares withheld for taxes
}

# A natural-language verb per action, for the human-readable reason string.
_ACTION_VERB = {Action.BUY: "bought", Action.EXIT: "sold", Action.INFO: "transacted"}

# Placeholder "tickers" EDGAR issuers file when they have no listed security
# (common in firehose mode; private-company code-P purchases pass the code
# filter). A BUY/EXIT keyed to one of these is not an actionable order symbol.
_UNTRADEABLE = {"", "NONE", "N/A", "NA", "NULL"}


def _tradeable(ticker: str) -> bool:
    return (ticker or "").strip().upper() not in _UNTRADEABLE


@dataclass
class InsiderBuyStrategy:
    # Which Form 4 transaction codes to surface. Default {"P"} preserves the
    # original buy-only behavior. Each code maps to an action via CODE_ACTION.
    codes: set[str] = field(default_factory=lambda: {"P"})
    # Surface derivative transactions (e.g. option exercises)? Default False:
    # only non-derivative (actual share) transactions, as before.
    include_derivative: bool = False
    min_dollar_value: float = 100_000.0
    officers_directors_only: bool = True
    max_age_days: int = 14
    cluster_min: int = 1
    # How to aggregate qualifying transactions into signals:
    #   "ticker"  — group by (ticker, action): "N insiders bought $X of TICKER"
    #               (the default; cluster_min counts distinct insiders).
    #   "insider" — group by (owner, action): "PERSON bought $X across {tickers}"
    #               (answers "who is trading across my watchlist"; cluster_min
    #               counts distinct tickers that person hit).
    group_by: str = "ticker"

    def _action_for(self, t: InsiderTransaction) -> Action | None:
        if t.transaction_code not in self.codes:
            return None
        return CODE_ACTION.get(t.transaction_code, Action.INFO)

    def _passes_filters(self, t: InsiderTransaction, today: datetime) -> bool:
        # The transaction code must be one we're configured to surface. The code
        # is the authoritative signal (P=purchase, S=sale, ...); we don't
        # separately gate on acquired/disposed since it's implied by the code.
        #
        # NOTE: min_dollar_value is deliberately NOT applied here. Form 4s
        # routinely split one purchase into several same-day rows (one per
        # execution price), so a per-row gate silently suppresses exactly the
        # large buys this feed exists to catch (six $75k lots of a $450k buy
        # would each fail a $100k gate). The gate is applied to each insider's
        # per-group TOTAL in the aggregation steps below.
        if self._action_for(t) is None:
            return False
        if t.is_derivative and not self.include_derivative:
            return False
        if self.officers_directors_only and not (t.is_officer or t.is_director):
            return False
        # Age check on the TRANSACTION date (when they traded), not filing date.
        # When an age limit is active, a filing whose date is missing or
        # unparseable CANNOT be confirmed recent — reject it rather than letting
        # an arbitrarily old (or undated) trade slip through the staleness gate.
        if self.max_age_days:
            if not t.transaction_date:
                return False
            try:
                tx_date = datetime.strptime(t.transaction_date, "%Y-%m-%d")
            except ValueError:
                return False
            if today - tx_date > timedelta(days=self.max_age_days):
                return False
        return True

    def signals_from_transactions(
        self, transactions: list[InsiderTransaction], now: datetime | None = None
    ) -> list[Signal]:
        now = now or datetime.now()
        qualifying = [
            (self._action_for(t), t)
            for t in transactions
            if self._passes_filters(t, now)
        ]
        if self.group_by == "insider":
            return self._signals_by_insider(qualifying)
        return self._signals_by_ticker(qualifying)

    def _signals_by_ticker(self, qualifying) -> list[Signal]:
        # Group by (ticker, action); cluster_min counts DISTINCT INSIDERS.
        grouped: dict[tuple[str, Action], list[InsiderTransaction]] = defaultdict(list)
        for action, t in qualifying:
            grouped[(t.ticker, action)].append(t)

        signals: list[Signal] = []
        for (ticker, action), txs in grouped.items():
            # Size-gate each INSIDER's total (their lot-split rows summed),
            # not each row: min_dollar_value means "ignore small/symbolic
            # PURCHASES", and a purchase is what a person did that day, not
            # one execution print of it. Keyed by CIK (the stable person
            # identifier); name only when the filing carried no owner CIK.
            by_insider: dict[str, float] = defaultdict(float)
            for t in txs:
                by_insider[t.owner_cik or t.owner_name] += t.dollar_value
            keep = {k for k, total in by_insider.items()
                    if total >= self.min_dollar_value}
            if len(keep) < self.cluster_min:
                continue
            txs = [t for t in txs if (t.owner_cik or t.owner_name) in keep]
            total_value = sum(by_insider[k] for k in keep)
            codes = "/".join(sorted({t.transaction_code for t in txs}))
            names = ", ".join(sorted({t.owner_name for t in txs if t.owner_name})[:3])
            verb = _ACTION_VERB.get(action, "transacted")
            reason = (
                f"{len(keep)} insider(s) {verb} ${total_value:,.0f} "
                f"of {ticker} [code {codes}] (e.g. {names})"
            )
            # An unlisted/placeholder issuer symbol is not tradeable: EDGAR
            # issuers without a listed security file "NONE"/"N/A". Surface the
            # activity, but never as a trade directive keyed to a symbol some
            # downstream automation might submit. (congress_strategy applies
            # the same rule to its BUY path.)
            if action in (Action.BUY, Action.EXIT) and not _tradeable(ticker):
                signals.append(Signal(ticker, Action.INFO,
                                      f"[no tradeable ticker] {reason}"))
                continue
            signals.append(Signal(ticker, action, reason))
        return signals

    def _signals_by_insider(self, qualifying) -> list[Signal]:
        # Group by (owner, action); cluster_min counts DISTINCT TICKERS the
        # person traded. The Signal.symbol is the owner identifier (CIK if known,
        # else name) so downstream can key on the person.
        grouped: dict[tuple[str, Action], list[InsiderTransaction]] = defaultdict(list)
        for action, t in qualifying:
            owner_key = (t.owner_cik or t.owner_name)
            grouped[(owner_key, action)].append(t)

        signals: list[Signal] = []
        for (owner_key, action), txs in grouped.items():
            # Size-gate each TICKER's total for this person (lot-split rows
            # summed) — the per-row version of this gate hides exactly the
            # large multi-execution purchases the feed exists to catch.
            by_ticker: dict[str, float] = defaultdict(float)
            for t in txs:
                by_ticker[t.ticker] += t.dollar_value
            tickers = {tk for tk, total in by_ticker.items()
                       if total >= self.min_dollar_value}
            if len(tickers) < self.cluster_min:
                continue
            txs = [t for t in txs if t.ticker in tickers]
            total_value = sum(by_ticker[tk] for tk in tickers)
            who = txs[0].owner_name or owner_key
            codes = "/".join(sorted({t.transaction_code for t in txs}))
            tick_list = ", ".join(sorted(tickers)[:5])
            verb = _ACTION_VERB.get(action, "transacted")
            reason = (
                f"{who} {verb} ${total_value:,.0f} across {len(tickers)} "
                f"ticker(s): {tick_list} [code {codes}]"
            )
            signals.append(Signal(owner_key, action, reason))
        return signals
