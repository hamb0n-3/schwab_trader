import unittest

from signals.form4_parser import InsiderTransaction
from signals.insider_strategy import InsiderBuyStrategy
from strategy import Action


def tx(ticker="ACME", owner="Jane Doe", cik="0001", shares=1000.0,
       price=75.0, code="P", date="2026-07-01", derivative=False):
    return InsiderTransaction(
        issuer_name="Acme Corp", ticker=ticker, issuer_cik="0009",
        owner_name=owner, owner_cik=cik, is_director=False, is_officer=True,
        officer_title="CEO", security_title="Common Stock",
        transaction_code=code, transaction_date=date, shares=shares,
        price_per_share=price, acquired_disposed="A" if code == "P" else "D",
        is_derivative=derivative,
    )


def strat(**kw):
    kw.setdefault("min_dollar_value", 100_000.0)
    kw.setdefault("max_age_days", 0)    # keep fixtures time-independent
    return InsiderBuyStrategy(**kw)


class TestSizeGateAggregation(unittest.TestCase):
    def test_lot_split_purchase_passes_gate_on_total(self):
        # $450k bought in six $75k executions: every ROW is below the $100k
        # gate; the PURCHASE is far above it. One BUY, full total.
        rows = [tx(shares=1000, price=75.0) for _ in range(6)]
        sigs = strat().signals_from_transactions(rows)
        self.assertEqual(len(sigs), 1)
        self.assertIs(sigs[0].action, Action.BUY)
        self.assertIn("450,000", sigs[0].reason)

    def test_small_insiders_do_not_ride_a_big_one(self):
        rows = [tx(shares=2000, price=75.0),                          # $150k
                tx(owner="Bob Roe", cik="0002", shares=10, price=75.0)]  # $750
        sigs = strat().signals_from_transactions(rows)
        self.assertEqual(len(sigs), 1)
        self.assertIn("1 insider(s)", sigs[0].reason)
        self.assertIn("150,000", sigs[0].reason)      # $750 not in the total
        self.assertNotIn("Bob Roe", sigs[0].reason)

    def test_cluster_counts_only_qualifying_insiders(self):
        rows = [tx(shares=2000, price=75.0),
                tx(owner="Bob Roe", cik="0002", shares=10, price=75.0)]
        self.assertEqual(strat(cluster_min=2).signals_from_transactions(rows),
                         [])

    def test_two_qualifying_insiders_form_a_cluster(self):
        rows = [tx(shares=1000, price=75.0), tx(shares=1000, price=75.0),
                tx(owner="Bob Roe", cik="0002", shares=2000, price=75.0)]
        sigs = strat(cluster_min=2).signals_from_transactions(rows)
        self.assertEqual(len(sigs), 1)
        self.assertIn("2 insider(s)", sigs[0].reason)

    def test_insider_grouping_gates_per_ticker_total(self):
        rows = [tx(shares=1000, price=75.0), tx(shares=1000, price=75.0),
                tx(ticker="OTHR", shares=10, price=75.0)]
        sigs = strat(group_by="insider").signals_from_transactions(rows)
        self.assertEqual(len(sigs), 1)
        self.assertIn("150,000", sigs[0].reason)      # ACME lots summed
        self.assertNotIn("OTHR", sigs[0].reason)      # $750 ticker gated out


class TestTickerValidation(unittest.TestCase):
    def test_untradeable_ticker_demoted_to_info(self):
        # EDGAR issuers without a listed security file "NONE"/"N/A"; a BUY
        # keyed to that string is not an actionable order symbol.
        rows = [tx(ticker="NONE", shares=3000, price=75.0)]
        sigs = strat().signals_from_transactions(rows)
        self.assertEqual(len(sigs), 1)
        self.assertIs(sigs[0].action, Action.INFO)
        self.assertIn("no tradeable ticker", sigs[0].reason)

    def test_real_ticker_stays_a_buy(self):
        rows = [tx(shares=3000, price=75.0)]
        sigs = strat().signals_from_transactions(rows)
        self.assertIs(sigs[0].action, Action.BUY)


if __name__ == "__main__":
    unittest.main()
