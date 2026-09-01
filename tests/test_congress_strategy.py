import unittest
from datetime import date

from signals.congress_parsers import CongressTransaction
from signals.congress_strategy import (
    CongressPtrStrategy,
    close_on_or_before,
)
from strategy import Action

TODAY = date(2026, 6, 11)


def tx(politician="Nancy Pelosi", ticker="NVDA", tx_type="P",
       tx_date="2026-05-12", amount_min=15001.0, amount_max=50000.0,
       asset_type="Stock", chamber="house", seq=0):
    return CongressTransaction(
        chamber=chamber, politician=politician, owner="",
        ticker=ticker, asset_description=f"{ticker} common",
        asset_type=asset_type, tx_type=tx_type, raw_type=tx_type,
        transaction_date=tx_date, notification_date="",
        filing_date="2026-06-10", amount_min=amount_min,
        amount_max=amount_max, amount_raw="$15,001 - $50,000",
        doc_id="d1", link="", tx_seq=seq,
    )


class FakeProvider:
    def __init__(self, closes, last):
        self._closes = closes      # symbol -> {iso: close}
        self._last = last          # symbol -> price
        self.daily_calls = []
        self.last_calls = []

    def daily_closes(self, symbol, start, end):
        self.daily_calls.append(symbol)
        return self._closes.get(symbol, {})

    def last_price(self, symbol):
        self.last_calls.append(symbol)
        return self._last.get(symbol)


class TestCloseOnOrBefore(unittest.TestCase):
    def test_exact_and_prior(self):
        closes = {"2026-05-11": 100.0, "2026-05-12": 101.0}
        self.assertEqual(close_on_or_before(closes, date(2026, 5, 12)),
                         ("2026-05-12", 101.0))
        # weekend: fall back to the nearest prior close
        self.assertEqual(close_on_or_before(closes, date(2026, 5, 14)),
                         ("2026-05-12", 101.0))

    def test_too_far(self):
        self.assertIsNone(close_on_or_before({"2026-01-01": 5.0},
                                             date(2026, 5, 12)))


class TestProximityGate(unittest.TestCase):
    def strat(self, provider, **kw):
        return CongressPtrStrategy(prices=provider, **kw)

    def test_hasnt_run_yet_is_buy(self):
        p = FakeProvider({"NVDA": {"2026-05-12": 100.0}}, {"NVDA": 103.0})
        sigs = self.strat(p).signals_from_transactions([tx()], today=TODAY)
        self.assertEqual(len(sigs), 1)
        self.assertIs(sigs[0].action, Action.BUY)
        self.assertIn("hasn't run yet", sigs[0].reason)
        self.assertIn("Nancy Pelosi", sigs[0].reason)

    def test_already_ran_is_info(self):
        p = FakeProvider({"NVDA": {"2026-05-12": 100.0}}, {"NVDA": 125.0})
        sigs = self.strat(p).signals_from_transactions([tx()], today=TODAY)
        self.assertIs(sigs[0].action, Action.INFO)
        self.assertIn("already ran", sigs[0].reason)

    def test_fell_is_info(self):
        p = FakeProvider({"NVDA": {"2026-05-12": 100.0}}, {"NVDA": 80.0})
        sigs = self.strat(p).signals_from_transactions([tx()], today=TODAY)
        self.assertIs(sigs[0].action, Action.INFO)
        self.assertIn("down since the buy", sigs[0].reason)

    def test_weekend_purchase_uses_prior_close(self):
        # Tx on Saturday 2026-05-16; closes only exist for Friday the 15th.
        p = FakeProvider({"NVDA": {"2026-05-15": 100.0}}, {"NVDA": 101.0})
        sigs = self.strat(p).signals_from_transactions(
            [tx(tx_date="2026-05-16")], today=TODAY)
        self.assertIs(sigs[0].action, Action.BUY)

    def test_no_provider_is_info_only(self):
        sigs = CongressPtrStrategy(prices=None).signals_from_transactions(
            [tx()], today=TODAY)
        self.assertIs(sigs[0].action, Action.INFO)
        self.assertIn("price check unavailable", sigs[0].reason)

    def test_missing_prices_is_info(self):
        p = FakeProvider({}, {})
        sigs = self.strat(p).signals_from_transactions([tx()], today=TODAY)
        self.assertIs(sigs[0].action, Action.INFO)

    def test_one_history_call_per_ticker(self):
        p = FakeProvider(
            {"NVDA": {"2026-05-12": 100.0}, "MSFT": {"2026-05-01": 400.0}},
            {"NVDA": 101.0, "MSFT": 401.0})
        txs = [tx(seq=0), tx(politician="Ro Khanna", tx_date="2026-05-20", seq=1),
               tx(ticker="MSFT", tx_date="2026-05-01", seq=2)]
        self.strat(p).signals_from_transactions(txs, today=TODAY)
        self.assertEqual(sorted(p.daily_calls), ["MSFT", "NVDA"])
        self.assertEqual(sorted(p.last_calls), ["MSFT", "NVDA"])

    def test_custom_threshold(self):
        p = FakeProvider({"NVDA": {"2026-05-12": 100.0}}, {"NVDA": 108.0})
        tight = self.strat(p, proximity_threshold=0.05)
        self.assertIs(tight.signals_from_transactions([tx()], today=TODAY)[0]
                      .action, Action.INFO)
        p2 = FakeProvider({"NVDA": {"2026-05-12": 100.0}}, {"NVDA": 108.0})
        loose = self.strat(p2, proximity_threshold=0.10)
        self.assertIs(loose.signals_from_transactions([tx()], today=TODAY)[0]
                      .action, Action.BUY)


class TestFilters(unittest.TestCase):
    def test_min_amount_lower_bound(self):
        s = CongressPtrStrategy(min_amount=15_000)
        small = tx(amount_min=1001.0, amount_max=15000.0)
        self.assertEqual(s.signals_from_transactions([small], today=TODAY), [])
        unknown = tx(amount_min=None, amount_max=None)
        self.assertEqual(s.signals_from_transactions([unknown], today=TODAY), [])

    def test_politician_filter(self):
        s = CongressPtrStrategy(politicians={"pelosi"})
        self.assertEqual(len(s.signals_from_transactions([tx()], today=TODAY)), 1)
        other = tx(politician="Tommy Tuberville")
        self.assertEqual(s.signals_from_transactions([other], today=TODAY), [])

    def test_ticker_filter(self):
        s = CongressPtrStrategy(tickers={"MSFT"})
        self.assertEqual(s.signals_from_transactions([tx()], today=TODAY), [])

    def test_age_gate(self):
        s = CongressPtrStrategy(max_age_days=10)
        old = tx(tx_date="2026-05-12")  # 30 days before TODAY
        self.assertEqual(s.signals_from_transactions([old], today=TODAY), [])
        s2 = CongressPtrStrategy(max_age_days=60)
        undated = tx(tx_date="")
        self.assertEqual(s2.signals_from_transactions([undated], today=TODAY), [])

    def test_age_gate_disabled_with_bad_date_no_crash(self):
        s = CongressPtrStrategy(max_age_days=0)
        undated = tx(tx_date="")
        sigs = s.signals_from_transactions([undated], today=TODAY)
        self.assertEqual(len(sigs), 1)          # surfaced as INFO, no crash
        self.assertIs(sigs[0].action, Action.INFO)

    def test_stock_only(self):
        s = CongressPtrStrategy()
        bond = tx(asset_type="Municipal Security")
        self.assertEqual(s.signals_from_transactions([bond], today=TODAY), [])
        s2 = CongressPtrStrategy(stock_only=False)
        self.assertEqual(len(s2.signals_from_transactions([bond], today=TODAY)), 1)

    def test_sells_off_by_default_info_when_on(self):
        sale = tx(tx_type="S")
        self.assertEqual(CongressPtrStrategy().signals_from_transactions(
            [sale], today=TODAY), [])
        sigs = CongressPtrStrategy(include_sells=True).signals_from_transactions(
            [sale], today=TODAY)
        self.assertIs(sigs[0].action, Action.INFO)
        self.assertIn("sold", sigs[0].reason)

    def test_cluster_min(self):
        s = CongressPtrStrategy(cluster_min=2)
        self.assertEqual(s.signals_from_transactions([tx()], today=TODAY), [])
        two = [tx(), tx(politician="Ro Khanna", seq=1)]
        self.assertEqual(len(s.signals_from_transactions(two, today=TODAY)), 1)


if __name__ == "__main__":
    unittest.main()
