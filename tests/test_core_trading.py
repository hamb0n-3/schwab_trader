import os
import tempfile
import unittest

# Only credentials/db here; mode and risk knobs are set per-test inside
# make_stack so this module's import can't leak live-mode config into other
# test files discovered in the same process.
os.environ.setdefault("SCHWAB_API_KEY", "test-key")
os.environ.setdefault("SCHWAB_APP_SECRET", "test-secret")
os.environ.setdefault("SCHWAB_DB_PATH", "")

from client import AccountSnapshot, Position, SchwabClient  # noqa: E402
from config import Config  # noqa: E402
from executor import Executor, _exit_quantity  # noqa: E402
from risk import RiskManager  # noqa: E402


def snap(cash=50_000.0, equity=50_000.0, positions=()):
    return AccountSnapshot("h", cash, equity, cash, list(positions))


def pos(symbol, qty, price=100.0):
    return Position(symbol, qty, price, qty * price)


class FakeNotifier:
    def __init__(self):
        self.infos: list[str] = []
        self.urgents: list[str] = []

    def info(self, m):
        self.infos.append(m)

    def urgent(self, m):
        self.urgents.append(m)


class FakeClient:

    def __init__(self, last_price=100.0, orders=None):
        self.account_hash = "h"
        self._last_price = last_price
        self._orders = orders or []
        self.placed: list = []
        self.cancel_failures = 0
        self.place_raises = False
        self.open_orders_calls = 0
        self._snapshot = snap()

    def open_orders(self):
        self.open_orders_calls += 1
        return self._orders

    def working_orders_for_symbol(self, symbol, orders=None):
        return SchwabClient.working_orders_for_symbol(self, symbol, orders)

    _symbols_in_order = staticmethod(SchwabClient._symbols_in_order)

    def get_last_price(self, symbol, require_fresh=False):
        return self._last_price

    def preview(self, order):
        return {"preview_ok": True, "status": 200, "body": {}}

    def place(self, order):
        if self.place_raises:
            raise RuntimeError("api down")
        self.placed.append(order)
        return 12345

    def cancel_working_orders_for_symbol(self, symbol):
        return self.cancel_failures

    def cancel_all_working_orders(self):
        return 0

    def snapshot(self):
        return self._snapshot


def buy_order(symbol, qty=10, price=100.0):
    return {
        "orderId": 1, "status": "WORKING", "price": price,
        "orderLegCollection": [
            {"instruction": "BUY", "quantity": qty,
             "instrument": {"symbol": symbol}},
        ],
    }


def sell_order(symbol, qty=10):
    return {
        "orderId": 2, "status": "WORKING",
        "orderLegCollection": [
            {"instruction": "SELL", "quantity": qty,
             "instrument": {"symbol": symbol}},
        ],
    }


_CFG_DEFAULTS = {
    "DRY_RUN": "false",
    "LIVE_CONFIRM": "I_UNDERSTAND_THIS_TRADES_REAL_MONEY",
    "MAX_POSITION_USD": "2500", "MAX_TOTAL_EXPOSURE_USD": "10000",
    "MAX_OPEN_POSITIONS": "5", "MAX_DAILY_LOSS_USD": "500",
    "MAX_PRICE_DEVIATION_PCT": "0.02", "PER_SYMBOL_COOLDOWN_SECONDS": "300",
    "POSITION_SIZE_USD": "1000", "MAX_ENTRIES_PER_SYMBOL_PER_DAY": "10",
    "TAKE_PROFIT_PCT": "0.05", "STOP_LOSS_PCT": "0.03",
}


def make_stack(client=None, **cfg_env):
    # Reset to known defaults first — Config reads the process environment,
    # so one test's overrides must not leak into the next.
    for k, v in _CFG_DEFAULTS.items():
        os.environ[k] = v
    for k, v in cfg_env.items():
        os.environ[k] = str(v)
    config = Config()
    client = client or FakeClient()
    notifier = FakeNotifier()
    risk = RiskManager(config, notifier=notifier)
    risk.config.daily_state_file = ""
    ex = Executor(client, config, risk, notifier=notifier)
    return client, config, risk, ex, notifier


class TestRiskCaps(unittest.TestCase):
    def approve(self, risk, snapshot, symbol="AAPL", qty=10, price=100.0):
        return risk.approve_entry(symbol, qty, price, price, snapshot)

    def test_per_order_dollar_cap(self):
        _, _, risk, _, _ = make_stack(MAX_POSITION_USD="500")
        d = self.approve(risk, snap())          # $1000 notional > $500
        self.assertFalse(d.allowed)
        self.assertIn("MAX_POSITION_USD", d.reason)

    def test_exposure_cap_counts_pending_and_positions(self):
        _, _, risk, _, _ = make_stack(MAX_POSITION_USD="6000",
                                      MAX_TOTAL_EXPOSURE_USD="2500")
        s = snap(positions=[pos("MSFT", 10)])   # $1000 exposure
        risk.begin_cycle()
        risk.reserve_entry("NVDA", 1000.0, is_new=True)
        d = self.approve(risk, s, "AAPL", 10, 100.0)   # 1000+1000+1000 > 2500
        self.assertFalse(d.allowed)
        self.assertIn("exposure", d.reason.lower())

    def test_exposure_cap_counts_inflight_prior_cycle_entries(self):
        _, _, risk, _, _ = make_stack(MAX_POSITION_USD="6000",
                                      MAX_TOTAL_EXPOSURE_USD="2500")
        risk.begin_cycle()
        risk.set_inflight({"TSLA", "AMD"}, 2000.0)  # unfilled prior entries
        d = self.approve(risk, snap(), "AAPL", 10, 100.0)  # 2000+1000 > 2500
        self.assertFalse(d.allowed)

    def test_max_open_positions_counts_inflight_symbols(self):
        _, _, risk, _, _ = make_stack(MAX_OPEN_POSITIONS="2",
                                      MAX_TOTAL_EXPOSURE_USD="100000")
        risk.begin_cycle()
        risk.set_inflight({"TSLA", "AMD"}, 0.0)     # 2 in-flight new symbols
        d = self.approve(risk, snap(), "AAPL")
        self.assertFalse(d.allowed)
        self.assertIn("MAX_OPEN_POSITIONS", d.reason)
        # ...but an in-flight symbol itself is not double-blocked by the cap
        d2 = self.approve(risk, snap(), "TSLA")
        self.assertTrue(d2.allowed)

    def test_price_deviation_guard(self):
        _, _, risk, _, _ = make_stack(MAX_PRICE_DEVIATION_PCT="0.02")
        d = risk.approve_entry("AAPL", 10, 110.0, 100.0, snap())  # 10% off
        self.assertFalse(d.allowed)
        self.assertIn("deviates", d.reason)

    def test_cooldown(self):
        _, _, risk, _, _ = make_stack()
        risk.record_order("AAPL")
        d = self.approve(risk, snap())
        self.assertFalse(d.allowed)
        self.assertIn("Cooldown", d.reason)

    def test_daily_entry_cap(self):
        # The SMA cross stays true all day: after a stop-out the signal
        # re-fires, and this cap is what stops the churn.
        _, _, risk, _, _ = make_stack(MAX_ENTRIES_PER_SYMBOL_PER_DAY="2")
        risk.begin_cycle()
        for _ in range(2):
            self.assertTrue(self.approve(risk, snap()).allowed)
            risk.reserve_entry("AAPL", 1000.0, is_new=True)
            risk.begin_cycle()    # new cycle clears reservations, NOT the cap
        d = self.approve(risk, snap())
        self.assertFalse(d.allowed)
        self.assertIn("entry cap", d.reason.lower())
        # other symbols unaffected
        self.assertTrue(self.approve(risk, snap(), "MSFT").allowed)


class TestDailyLossHalt(unittest.TestCase):
    def test_trip_blocks_entries_and_pages_once(self):
        _, _, risk, _, notifier = make_stack(MAX_DAILY_LOSS_USD="500")
        risk.begin_session(snap(equity=10_000))
        down = snap(equity=9_400)
        self.assertTrue(risk.check_daily_loss(down))
        self.assertTrue(risk.check_daily_loss(down))   # still halted
        self.assertEqual(len(notifier.urgents), 1)     # transition pages once
        d = risk.approve_entry("AAPL", 1, 100.0, 100.0, down)
        self.assertFalse(d.allowed)
        self.assertIn("halted", d.reason.lower())

    def test_within_limit_no_halt(self):
        _, _, risk, _, _ = make_stack(MAX_DAILY_LOSS_USD="500")
        risk.begin_session(snap(equity=10_000))
        self.assertFalse(risk.check_daily_loss(snap(equity=9_700)))


class TestExecutorEntry(unittest.TestCase):
    def test_stacking_guard_skips_and_caches(self):
        client = FakeClient(orders=[buy_order("AAPL")])
        client, _, risk, ex, _ = make_stack(client=client)
        ex.begin_cycle()
        ex.enter("AAPL", snap())                # working order -> skip
        ex.enter("MSFT", snap())                # no working order -> proceeds
        self.assertEqual(client.open_orders_calls, 1)   # one fetch per cycle
        self.assertEqual(len(client.placed), 1)         # only MSFT placed

    def test_unknown_order_state_fails_safe(self):
        client = FakeClient()
        client.open_orders = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        client, _, risk, ex, _ = make_stack(client=client)
        ex.begin_cycle()
        ex.enter("AAPL", snap())
        self.assertEqual(client.placed, [])     # no order on unknown state

    def test_degenerate_bracket_blocked_on_penny_stock(self):
        # At $0.10, ±5%/3% legs round to the entry itself — TP==SL==entry is
        # not a bracket; the entry must be refused, not placed.
        client = FakeClient(last_price=0.10)
        client, _, risk, ex, _ = make_stack(client=client)
        ex.begin_cycle()
        ex.enter("PENNY", snap())
        self.assertEqual(client.placed, [])
        # blocked BEFORE the cooldown stamp (it never became an order attempt)
        self.assertNotIn("PENNY", risk._last_order_time)

    def test_normal_price_bracket_not_blocked(self):
        client = FakeClient(last_price=100.0)
        client, _, risk, ex, _ = make_stack(client=client)
        ex.begin_cycle()
        ex.enter("AAPL", snap())
        self.assertEqual(len(client.placed), 1)

    def test_inflight_registered_from_order_cache(self):
        # A prior-cycle unfilled BUY must consume exposure/slots; resting
        # SELLs (bracket exits on held positions) must not.
        client = FakeClient(orders=[buy_order("TSLA", qty=10, price=200.0),
                                    sell_order("MSFT")])
        client, _, risk, ex, _ = make_stack(client=client)
        ex.begin_cycle()
        ex._working_orders("AAPL")              # triggers fetch + register
        self.assertEqual(risk._inflight_symbols, {"TSLA"})
        self.assertEqual(risk._inflight_notional, 2000.0)

    def test_inflight_uses_remaining_quantity_on_partial_fill(self):
        # 10-share entry, 6 already filled (and thus in snapshot.positions):
        # only the 4 unfilled shares may count, or exposure double-counts.
        o = buy_order("TSLA", qty=10, price=200.0)
        o["remainingQuantity"] = 4
        client = FakeClient(orders=[o])
        client, _, risk, ex, _ = make_stack(client=client)
        ex.begin_cycle()
        ex._working_orders("AAPL")
        self.assertEqual(risk._inflight_notional, 800.0)   # 4 * $200

    def test_inflight_ignores_buy_to_cover(self):
        o = buy_order("TSLA", qty=10, price=200.0)
        o["orderLegCollection"][0]["instruction"] = "BUY_TO_COVER"
        client = FakeClient(orders=[o])
        client, _, risk, ex, _ = make_stack(client=client)
        ex.begin_cycle()
        ex._working_orders("AAPL")
        self.assertEqual(risk._inflight_symbols, set())

    def test_live_place_failure_pages_and_reserves(self):
        # An ambiguous placement (raised after Schwab may have accepted) must
        # page, record an ERROR, and STILL reserve/stamp so we don't re-stack
        # a possible duplicate this run.
        client = FakeClient(last_price=100.0)
        client.place_raises = True
        client, _, risk, ex, notifier = make_stack(client=client)
        ex.begin_cycle()
        ex.enter("AAPL", snap())
        self.assertEqual(len(notifier.urgents), 1)
        self.assertIn("ENTRY UNKNOWN", notifier.urgents[0])
        self.assertIn("AAPL", risk._last_order_time)     # cooldown stamped
        self.assertIn("AAPL", risk._pending_symbols)     # caps reserved
        self.assertEqual(risk._entries_today.get("AAPL"), 1)  # daily cap counts


class TestExecutorDryRun(unittest.TestCase):

    def test_dry_run_entry_previews_stamps_reserves(self):
        client = FakeClient(last_price=100.0)
        client, _, risk, ex, notifier = make_stack(client=client, DRY_RUN="true")
        ex.begin_cycle()
        ex.enter("AAPL", snap())
        self.assertEqual(client.placed, [])              # no real order
        self.assertIn("AAPL", risk._last_order_time)     # cooldown stamped
        self.assertIn("AAPL", risk._pending_symbols)     # caps reserved
        self.assertEqual(risk._entries_today.get("AAPL"), 1)
        self.assertTrue(any("DRY-RUN" in m for m in notifier.infos))

    def test_dry_run_exit_previews_and_stamps(self):
        s = snap(positions=[pos("AAPL", 10, 150.0)])
        client = FakeClient()
        client._snapshot = s
        client, _, risk, ex, notifier = make_stack(client=client, DRY_RUN="true")
        ex.exit_position("AAPL", s)
        self.assertEqual(client.placed, [])
        self.assertIn("AAPL", risk._last_order_time)     # cooldown stamped
        self.assertTrue(any("DRY-RUN" in m and "exit" in m.lower()
                            for m in notifier.infos))


class TestExecutorExit(unittest.TestCase):
    def held_snap(self):
        return snap(positions=[pos("AAPL", 10, 150.0)])

    def test_cancel_failure_aborts_and_pages(self):
        client = FakeClient()
        client.cancel_failures = 1
        client, _, risk, ex, notifier = make_stack(client=client)
        aborted = ex.exit_position("AAPL", self.held_snap())
        self.assertTrue(aborted)
        self.assertEqual(client.placed, [])     # market sell NOT placed
        self.assertEqual(len(notifier.urgents), 1)
        self.assertIn("EXIT ABORTED", notifier.urgents[0])

    def test_place_failure_after_cancel_pages_unprotected(self):
        client = FakeClient()
        client._snapshot = self.held_snap()     # post-cancel refresh
        client.place_raises = True
        client, _, risk, ex, notifier = make_stack(client=client)
        aborted = ex.exit_position("AAPL", self.held_snap())
        self.assertTrue(aborted)                # surfaced as a failed exit
        self.assertEqual(len(notifier.urgents), 1)
        self.assertIn("POSITION UNPROTECTED", notifier.urgents[0])

    def test_snapshot_failure_after_cancel_pages_unprotected(self):
        # Cancels succeeded, then the position re-read failed: the resting
        # protection is ALREADY stripped, so this must surface as a failed
        # exit and page — not return "no failure" and go quiet.
        client = FakeClient()
        client.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        client, _, risk, ex, notifier = make_stack(client=client)
        aborted = ex.exit_position("AAPL", self.held_snap())
        self.assertTrue(aborted)
        self.assertEqual(client.placed, [])     # stale-qty sell NOT placed
        self.assertEqual(len(notifier.urgents), 1)
        self.assertIn("POSITION UNPROTECTED", notifier.urgents[0])

    def test_successful_exit(self):
        client = FakeClient()
        client._snapshot = self.held_snap()
        client, _, risk, ex, notifier = make_stack(client=client)
        aborted = ex.exit_position("AAPL", self.held_snap())
        self.assertFalse(aborted)
        self.assertEqual(len(client.placed), 1)
        self.assertEqual(notifier.urgents, [])

    def test_flatten_aggregates_failures_into_one_page(self):
        client = FakeClient()
        client.cancel_failures = 1
        s = snap(positions=[pos("AAPL", 10), pos("MSFT", 5), pos("NVDA", 2)])
        client._snapshot = s
        client, _, risk, ex, notifier = make_stack(client=client)
        ex.flatten_all(s)
        # one FLATTEN ALL + one aggregated FLATTEN INCOMPLETE — not 4 pages
        self.assertEqual(len(notifier.urgents), 2)
        self.assertIn("FLATTEN ALL", notifier.urgents[0])
        self.assertIn("FLATTEN INCOMPLETE", notifier.urgents[1])
        self.assertIn("AAPL, MSFT, NVDA", notifier.urgents[1])

    def test_flatten_isolates_a_raising_symbol(self):
        # One symbol's exit raises mid-flatten; the symbols AFTER it must
        # still be attempted (covering as many positions as possible is the
        # whole point of a kill-switch flatten), and the raiser is reported.
        class RaisingClient(FakeClient):
            def cancel_working_orders_for_symbol(self, symbol):
                if symbol == "MSFT":
                    raise RuntimeError("broker error on MSFT")
                return 0
        s = snap(positions=[pos("AAPL", 10), pos("MSFT", 5), pos("NVDA", 2)])
        client = RaisingClient()
        client._snapshot = s
        client, _, risk, ex, notifier = make_stack(client=client)
        ex.flatten_all(s)
        # AAPL and NVDA sold despite MSFT raising in the middle
        sold = {o.build()["orderLegCollection"][0]["instrument"]["symbol"]
                for o in client.placed}
        self.assertEqual(sold, {"AAPL", "NVDA"})
        # MSFT surfaced in the aggregated incomplete page
        self.assertTrue(any("FLATTEN INCOMPLETE" in m and "MSFT" in m
                            for m in notifier.urgents))

    def test_exit_quantity_fractional(self):
        self.assertEqual(_exit_quantity(10.0), 10)
        self.assertIsInstance(_exit_quantity(10.0), int)
        self.assertEqual(_exit_quantity(10.5), 10.5)


class TestKillSwitchAndRollover(unittest.TestCase):
    def approve(self, risk, snapshot, symbol="AAPL"):
        return risk.approve_entry(symbol, 10, 100.0, 100.0, snapshot)

    def test_kill_switch_transition_pages_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ksf = os.path.join(tmp, "KILL")
            _, _, risk, _, notifier = make_stack(KILL_SWITCH_FILE=ksf)
            self.assertFalse(risk.kill_switch_active())   # no file yet
            with open(ksf, "w") as f:
                f.write("halt")
            self.assertTrue(risk.kill_switch_active())    # file present -> halt
            self.assertTrue(risk.halted)
            self.assertEqual(len(notifier.urgents), 1)
            # still active next cycle, but the transition page fires only once
            self.assertTrue(risk.kill_switch_active())
            self.assertEqual(len(notifier.urgents), 1)

    def test_day_rollover_clears_halt_and_rebaselines(self):
        _, _, risk, _, _ = make_stack(MAX_DAILY_LOSS_USD="500")
        risk.begin_session(snap(equity=10_000))
        self.assertTrue(risk.check_daily_loss(snap(equity=9_400)))  # halt
        self.assertTrue(risk.halted)
        risk.baseline_date = "2000-01-01"   # simulate crossing into a new ET day
        out = risk.check_daily_loss(snap(equity=9_400))
        self.assertFalse(out)               # re-baselined to 9_400 -> no drawdown
        self.assertFalse(risk.halted)       # prior halt cleared
        self.assertEqual(risk.starting_equity, 9_400)

    def test_entry_cap_resets_on_new_day(self):
        _, _, risk, _, _ = make_stack(MAX_ENTRIES_PER_SYMBOL_PER_DAY="1")
        risk.begin_cycle()
        self.assertTrue(self.approve(risk, snap()).allowed)
        risk.reserve_entry("AAPL", 1000.0, is_new=True)
        risk.begin_cycle()
        self.assertFalse(self.approve(risk, snap()).allowed)   # capped today
        risk._entries_day = "2000-01-01"    # cross into a new ET day
        self.assertTrue(self.approve(risk, snap()).allowed)    # cap reset


class TestFailureEscalation(unittest.TestCase):

    def make_bot(self):
        import bot as botmod

        class Shell:  # duck-typed TradingBot for the helpers under test
            pass

        b = Shell()
        b._consecutive_errors = 0
        b.notifier = FakeNotifier()
        b._note = lambda err: botmod.TradingBot._note_cycle_error(b, err)
        b._ok = lambda: botmod.TradingBot._note_cycle_success(b)
        return b, botmod.FAILURE_ALERT_THRESHOLD

    def test_pages_at_threshold(self):
        b, n = self.make_bot()
        for _ in range(n):
            b._note(RuntimeError("token expired"))
        self.assertEqual(len(b.notifier.urgents), 1)
        self.assertIn("consecutive cycle errors", b.notifier.urgents[0])

    def test_repages_at_each_threshold_multiple(self):
        b, n = self.make_bot()
        for _ in range(2 * n):          # a sustained outage keeps reminding
            b._note(RuntimeError("down"))
        self.assertEqual(len(b.notifier.urgents), 2)   # at n and at 2n
        self.assertIn(str(2 * n), b.notifier.urgents[1])

    def test_below_threshold_is_silent(self):
        b, n = self.make_bot()
        for _ in range(n - 1):
            b._note(RuntimeError("transient"))
        self.assertEqual(b.notifier.urgents, [])

    def test_recovery_after_alerted_streak(self):
        b, n = self.make_bot()
        for _ in range(n):
            b._note(RuntimeError("x"))
        b._ok()                          # a cycle finally succeeds
        self.assertEqual(b._consecutive_errors, 0)
        self.assertTrue(any("recovered" in m for m in b.notifier.infos))
        # a brief blip below threshold then recovery is silent (no false alarm)
        b._note(RuntimeError("blip"))
        before = len(b.notifier.infos)
        b._ok()
        self.assertEqual(len(b.notifier.infos), before)


if __name__ == "__main__":
    unittest.main()
