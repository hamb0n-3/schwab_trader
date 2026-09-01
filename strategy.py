from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Action(Enum):
    BUY = "BUY"
    EXIT = "EXIT"
    HOLD = "HOLD"
    # Surfaced-but-not-a-trade-directive (e.g. an insider option exercise or
    # grant we want to report but not act on). The bot's routing only ever acts
    # on BUY/EXIT, so an INFO signal is logged/recorded and otherwise ignored.
    INFO = "INFO"


@dataclass
class Signal:
    symbol: str
    action: Action
    reason: str = ""


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


class Strategy(Protocol):
    def evaluate(self, symbol: str, closes: list[float], holding: bool) -> Signal:
        ...


@dataclass
class SmaCrossStrategy:
    fast: int
    slow: int

    def evaluate(self, symbol: str, closes: list[float], holding: bool) -> Signal:
        if len(closes) < self.slow + 1:
            return Signal(symbol, Action.HOLD, "Not enough history.")

        fast_now = sma(closes, self.fast)
        slow_now = sma(closes, self.slow)
        fast_prev = sma(closes[:-1], self.fast)
        slow_prev = sma(closes[:-1], self.slow)

        if None in (fast_now, slow_now, fast_prev, slow_prev):
            return Signal(symbol, Action.HOLD, "Indicators unavailable.")

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up and not holding:
            return Signal(
                symbol, Action.BUY,
                f"SMA{self.fast} crossed above SMA{self.slow} "
                f"({fast_now:.2f} > {slow_now:.2f}).",
            )
        if crossed_down and holding:
            return Signal(
                symbol, Action.EXIT,
                f"SMA{self.fast} crossed below SMA{self.slow} "
                f"({fast_now:.2f} < {slow_now:.2f}).",
            )
        return Signal(symbol, Action.HOLD, "No crossover.")
