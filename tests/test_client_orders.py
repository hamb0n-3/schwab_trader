import unittest

from client import (
    build_bracket_buy,
    build_equity_sell_stop,
    build_equity_sell_limit_gtc,
)


class TestBracketTopology(unittest.TestCase):
    def setUp(self):
        self.spec = build_bracket_buy("AAPL", 10, 100.0, 105.0, 97.0).build()

    def test_entry_is_day_buy_limit(self):
        self.assertEqual(self.spec["orderType"], "LIMIT")
        self.assertEqual(self.spec["duration"], "DAY")   # entry expires same day
        self.assertEqual(self.spec["price"], "100.00")
        self.assertEqual(self.spec["orderStrategyType"], "TRIGGER")
        leg = self.spec["orderLegCollection"][0]
        self.assertEqual(leg["instruction"], "BUY")
        self.assertEqual(leg["instrument"]["symbol"], "AAPL")
        self.assertEqual(leg["quantity"], 10)

    def test_exits_are_an_oco_pair(self):
        children = self.spec["childOrderStrategies"]
        self.assertEqual(len(children), 1)
        oco = children[0]
        self.assertEqual(oco["orderStrategyType"], "OCO")
        self.assertEqual(len(oco["childOrderStrategies"]), 2)

    def test_both_exits_are_gtc_sells_with_right_types(self):
        legs = self.spec["childOrderStrategies"][0]["childOrderStrategies"]
        by_type = {leg["orderType"]: leg for leg in legs}
        self.assertIn("LIMIT", by_type)   # take-profit
        self.assertIn("STOP", by_type)    # stop-loss
        tp, sl = by_type["LIMIT"], by_type["STOP"]
        self.assertEqual(tp["price"], "105.00")
        self.assertEqual(sl["stopPrice"], "97.00")
        for leg in (tp, sl):
            # GTC on BOTH legs — otherwise the DAY-default take-profit would
            # expire at the close leaving the stop alone (or vice versa).
            self.assertEqual(leg["duration"], "GOOD_TILL_CANCEL")
            self.assertEqual(leg["orderLegCollection"][0]["instruction"], "SELL")
            self.assertEqual(leg["orderLegCollection"][0]["quantity"], 10)

    def test_sell_stop_is_gtc(self):
        self.assertEqual(
            build_equity_sell_stop("AAPL", 5, 90.0).build()["duration"],
            "GOOD_TILL_CANCEL")

    def test_sell_limit_gtc_override(self):
        # equity_sell_limit defaults to DAY; the helper must force GTC.
        self.assertEqual(
            build_equity_sell_limit_gtc("AAPL", 5, 110.0).build()["duration"],
            "GOOD_TILL_CANCEL")


if __name__ == "__main__":
    unittest.main()
