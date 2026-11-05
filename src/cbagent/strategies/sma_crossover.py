"""Example strategy: buy when the fast moving average crosses above the slow one, sell when it crosses below."""
from __future__ import annotations

from decimal import Decimal

from ..indicators import sma
from ..models import Candle, Signal
from .base import Strategy


class SmaCrossover(Strategy):
    name = "sma_crossover"

    def __init__(self, fast: int = 20, slow: int = 50, **kw):
        super().__init__(**kw)
        if fast >= slow:
            raise ValueError("fast must be shorter than slow")
        self.fast, self.slow = fast, slow

    @property
    def warmup(self) -> int:
        return self.slow + 1

    def on_candles(self, product_id: str, candles: list[Candle], position_base: Decimal) -> Signal:
        if len(candles) < self.warmup:
            return self.hold("warming up")
        closes = [c.close for c in candles]
        fast_now, slow_now = sma(closes, self.fast), sma(closes, self.slow)
        fast_prev, slow_prev = sma(closes[:-1], self.fast), sma(closes[:-1], self.slow)

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        if crossed_up and position_base == 0:
            return Signal("buy", f"SMA{self.fast} crossed above SMA{self.slow}", quote_size=self.order_quote)
        if crossed_down and position_base > 0:
            return Signal("sell", f"SMA{self.fast} crossed below SMA{self.slow}")
        return self.hold("no crossover")
