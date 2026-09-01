import os
import tempfile
import unittest

from backtest import Backtester, Bar, load_csv
from strategy import Action, Signal


class ScriptedStrategy:

    def __init__(self, script):
        self.script = script            # bar index -> Action

    def evaluate(self, symbol, closes, holding):
        action = self.script.get(len(closes) - 1, Action.HOLD)
        return Signal(symbol, action, "scripted")


def bars(*ohlc):
    return [Bar(date=f"D{i}", open=o, high=h, low=l, close=c)
            for i, (o, h, l, c) in enumerate(ohlc)]


def run_bt(script, data, **kw):
    kw.setdefault("slippage_pct", 0.0)
    bt = Backtester(strategy=ScriptedStrategy(script), **kw)
    return bt.run("TEST", data)


class TestNextOpenFills(unittest.TestCase):
    def test_buy_signal_fills_next_open_not_same_close(self):
        data = bars((99, 100, 98, 100),       # BUY signal at this close
                    (104, 104, 104, 104))     # fill must be HERE, at the open
        r = run_bt({0: Action.BUY}, data)
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.trades[0].entry_date, "D1")
        self.assertEqual(r.trades[0].entry_price, 104.0)

    def test_exit_signal_fills_next_open(self):
        data = bars((99, 100, 98, 100),       # BUY signal
                    (100, 101, 99, 100),      # entry at open
                    (100, 101, 99, 100),      # EXIT signal at this close
                    (102, 103, 101, 102))     # exit fills at this open
        r = run_bt({0: Action.BUY, 2: Action.EXIT}, data)
        t = r.trades[0]
        self.assertEqual(t.reason, "SIGNAL")
        self.assertEqual(t.exit_date, "D3")
        self.assertEqual(t.exit_price, 102.0)

    def test_final_bar_signal_never_fills(self):
        # No next open exists; live would place the order next session.
        data = bars((99, 100, 98, 100))
        r = run_bt({0: Action.BUY}, data)
        self.assertEqual(r.trades, [])
        self.assertEqual(r.ending_equity, r.starting_cash)


class TestGapAwareBrackets(unittest.TestCase):

    def entry_then(self, last_bar):
        return bars((99, 100, 98, 100),       # signal bar
                    (100, 101, 99, 100),      # entry at open = 100
                    last_bar)

    def test_gap_through_stop_fills_at_open(self):
        # Overnight collapse to 85: booking -3% (the stop price) here is the
        # exact optimism this engine must not have — real stop-markets fill
        # at/near the open.
        r = run_bt({0: Action.BUY}, self.entry_then((85, 86, 80, 82)))
        t = r.trades[0]
        self.assertEqual(t.reason, "STOP_LOSS")
        self.assertEqual(t.exit_price, 85.0)

    def test_gap_above_target_fills_at_open(self):
        # The resting TP limit fills at the (better) opening print.
        r = run_bt({0: Action.BUY}, self.entry_then((110, 112, 108, 111)))
        t = r.trades[0]
        self.assertEqual(t.reason, "TAKE_PROFIT")
        self.assertEqual(t.exit_price, 110.0)

    def test_intrabar_stop_fills_at_stop_price(self):
        r = run_bt({0: Action.BUY}, self.entry_then((100, 101, 96, 98)))
        t = r.trades[0]
        self.assertEqual(t.reason, "STOP_LOSS")
        self.assertEqual(t.exit_price, 97.0)

    def test_both_touched_intrabar_assumes_stop_first(self):
        r = run_bt({0: Action.BUY}, self.entry_then((100, 106, 96, 100)))
        self.assertEqual(r.trades[0].reason, "STOP_LOSS")

    def test_same_bar_entry_can_stop_out_intrabar(self):
        # Brackets arm at the open fill; a plunge later the same bar stops out.
        data = bars((99, 100, 98, 100),
                    (100, 101, 95, 96))       # low 95 < SL 97
        r = run_bt({0: Action.BUY}, data)
        t = r.trades[0]
        self.assertEqual(t.reason, "STOP_LOSS")
        self.assertEqual(t.exit_price, 97.0)  # intrabar, NOT a gap fill


class TestCommissions(unittest.TestCase):
    def test_trade_pnl_is_net_of_commissions(self):
        # Flat price, $1 commission each way: the trade must report -$2, not
        # $0 — otherwise win_rate/profit_factor disagree with ending equity.
        data = bars((99, 100, 98, 100),
                    (100, 100, 100, 100),
                    (100, 100, 100, 100))
        r = run_bt({0: Action.BUY}, data, commission_per_trade=1.0)
        t = r.trades[0]                        # 10 shares, entry=exit=100
        self.assertEqual(t.pnl, -2.0)
        self.assertEqual(r.ending_equity, r.starting_cash - 2.0)
        self.assertEqual(r.win_rate, 0.0)      # a commission-loser is a loss


class TestLoadCsv(unittest.TestCase):
    def _write(self, tmp, text):
        p = os.path.join(tmp, "d.csv")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_newest_first_csv_is_sorted_ascending(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "date,close\n"
                                 "2026-01-03,3\n2026-01-02,2\n2026-01-01,1\n")
            loaded = load_csv(p)
            self.assertEqual([b.close for b in loaded], [1.0, 2.0, 3.0])

    def test_us_style_dates_sort_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "date,close\n"
                                 "01/10/2026,10\n01/02/2026,2\n")
            loaded = load_csv(p)
            self.assertEqual([b.close for b in loaded], [2.0, 10.0])

    def test_unparseable_date_raises_instead_of_misordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, "date,close\nnot-a-date,3\n")
            with self.assertRaises(ValueError):
                load_csv(p)


if __name__ == "__main__":
    unittest.main()
