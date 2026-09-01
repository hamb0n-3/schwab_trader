import unittest

from client import SchwabClient


def leg(symbol, instruction="SELL", qty=10):
    return {"instruction": instruction, "quantity": qty,
            "instrument": {"symbol": symbol}}


def filled_bracket(symbol="AAPL", tp_status="WORKING",
                   stop_status="AWAITING_STOP_CONDITION"):
    return {
        "orderId": 100, "status": "FILLED",
        "orderLegCollection": [leg(symbol, "BUY")],
        "childOrderStrategies": [
            {"orderId": 101, "status": tp_status,
             "orderLegCollection": [leg(symbol)]},
            {"orderId": 102, "status": stop_status,
             "orderLegCollection": [leg(symbol)]},
        ],
    }


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeRaw:

    def __init__(self, pages=None):
        # pages[i] -> payload list (or an Exception to raise) for the i-th
        # get_orders_for_account call; the last entry repeats.
        self.pages = pages if pages is not None else [[]]
        self.calls = 0
        self.cancelled: list[int] = []
        self.order_status: dict[int, str] = {}
        self.cancel_status_code = 200

    def get_orders_for_account(self, h, from_entered_datetime,
                               to_entered_datetime):
        payload = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(payload)

    def cancel_order(self, oid, h):
        self.cancelled.append(oid)
        return FakeResponse({}, status_code=self.cancel_status_code)

    def get_order(self, oid, h):
        return FakeResponse(
            {"orderId": oid, "status": self.order_status.get(oid, "CANCELED")})


def client(pages=None):
    c = SchwabClient(FakeRaw(pages=pages), "h")
    c._CONFIRM_WAIT_SECONDS = 0     # no real sleeping in tests
    return c


class TestOpenOrdersLiveness(unittest.TestCase):
    def test_filled_bracket_with_live_children_is_visible(self):
        # Judging by the top-level FILLED status alone would hide the resting
        # stop/TP of every protected position from the stacking guard AND the
        # exit cancel pass.
        c = client([[filled_bracket()], []])
        out = c.open_orders()
        self.assertEqual(len(out), 1)
        self.assertEqual(len(c.working_orders_for_symbol("AAPL", out)), 1)

    def test_fully_terminal_bracket_is_not(self):
        o = filled_bracket(tp_status="FILLED", stop_status="CANCELED")
        c = client([[o], []])
        self.assertEqual(c.open_orders(), [])

    def test_awaiting_stop_condition_is_live(self):
        o = {"orderId": 7, "status": "AWAITING_STOP_CONDITION",
             "orderLegCollection": [leg("MSFT")]}
        c = client([[o], []])
        self.assertEqual(len(c.open_orders()), 1)

    def test_pending_cancel_is_still_live(self):
        # A cancel REQUEST does not stop the order from filling until the
        # broker confirms it; the guard must keep treating it as live.
        o = {"orderId": 8, "status": "PENDING_CANCEL",
             "orderLegCollection": [leg("MSFT")]}
        c = client([[o], []])
        self.assertEqual(len(c.open_orders()), 1)

    def test_pages_all_windows_and_dedups_boundary_orders(self):
        o = filled_bracket()
        c = client([[o], [o], [o], [o]])    # same orderId in every window
        out = c.open_orders()
        self.assertEqual(len(out), 1)       # deduped
        self.assertEqual(c._c.calls, SchwabClient._ORDER_PAGES)

    def test_newest_window_failure_raises(self):
        # Callers fail safe on unknown order state — the newest window IS the
        # order state, so it must not be silently degraded.
        c = client([RuntimeError("down")])
        with self.assertRaises(Exception):
            c.open_orders()

    def test_older_window_failure_degrades_gracefully(self):
        c = client([[filled_bracket()], RuntimeError("400"), [], []])
        out = c.open_orders()
        self.assertEqual(len(out), 1)       # newest window still served
        self.assertEqual(c._c.calls, 2)     # stopped paging at the failure


class TestCancelConfirmation(unittest.TestCase):
    def test_live_parent_cancelled_once_children_left_to_cascade(self):
        raw_order = {
            "orderId": 50, "status": "WORKING",
            "orderLegCollection": [leg("AAPL", "BUY")],
            "childOrderStrategies": [
                {"orderId": 51, "status": "AWAITING_PARENT_ORDER",
                 "orderLegCollection": [leg("AAPL")]}],
        }
        c = client()
        c._c.order_status = {50: "CANCELED"}
        self.assertEqual(c._cancel_live_tree(raw_order), 0)
        # cancelling the live parent cascades; racing the children would 4xx
        self.assertEqual(c._c.cancelled, [50])

    def test_filled_parent_cancels_live_children_not_itself(self):
        c = client()
        c._c.order_status = {101: "CANCELED", 102: "CANCELED"}
        self.assertEqual(c._cancel_live_tree(filled_bracket()), 0)
        self.assertEqual(sorted(c._c.cancelled), [101, 102])
        self.assertNotIn(100, c._c.cancelled)   # never cancel a FILLED parent

    def test_oco_sibling_race_is_not_a_failure(self):
        # Cancelling one OCO leg auto-cancels its sibling, so the sibling's
        # own cancel request may 4xx — the status poll showing CANCELED is
        # what counts.
        c = client()
        c._c.cancel_status_code = 400
        c._c.order_status = {101: "CANCELED", 102: "CANCELED"}
        self.assertEqual(c._cancel_live_tree(filled_bracket()), 0)

    def test_unconfirmed_cancel_counts_failed(self):
        # 2xx on the cancel request but the order never leaves WORKING: that
        # leg can still execute, so the exit path must count it and abort.
        c = client()
        c._c.order_status = {101: "WORKING", 102: "CANCELED"}
        self.assertEqual(c._cancel_live_tree(filled_bracket()), 1)

    def test_fill_during_cancel_counts_as_gone(self):
        # FILLED is terminal: the leg can't fire after the flatten; the
        # position re-read (executor) picks up the changed quantity.
        c = client()
        c._c.order_status = {101: "FILLED", 102: "CANCELED"}
        self.assertEqual(c._cancel_live_tree(filled_bracket()), 0)


class FakeQuoteRaw:
    def __init__(self, quote):
        self._q = quote

    def get_quote(self, symbol):
        return FakeResponse({symbol.upper(): {"quote": self._q}})


class TestFreshPrice(unittest.TestCase):
    def test_close_only_quote_rejected_when_fresh_required(self):
        # closePrice is the PRIOR session: pricing a bracket off it on a gap
        # day defeats the deviation guard (same stale baseline on both sides).
        c = SchwabClient(FakeQuoteRaw({"closePrice": 101.5}), "h")
        self.assertIsNone(c.get_last_price("AAPL", require_fresh=True))
        self.assertEqual(c.get_last_price("AAPL"), 101.5)   # display path

    def test_current_session_prices_accepted(self):
        c = SchwabClient(FakeQuoteRaw({"lastPrice": 99.0}), "h")
        self.assertEqual(c.get_last_price("AAPL", require_fresh=True), 99.0)
        c2 = SchwabClient(FakeQuoteRaw({"mark": 98.5}), "h")
        self.assertEqual(c2.get_last_price("AAPL", require_fresh=True), 98.5)


if __name__ == "__main__":
    unittest.main()
