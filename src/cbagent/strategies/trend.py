"""Slow trend following: hold while the close is above its long moving average, step aside below it.

A band around the average stops it flipping in and out when the price hugs the line. Meant for daily candles,
where it trades only a handful of times a year.
"""
from __future__ import annotations

from decimal import Decimal

from ..indicators import sma
from ..models import Candle, Signal
from .base import Strategy


class TrendFollow(Strategy):
    name = "trend"

    def __init__(self, period: int = 100, band_pct: float = 2.0, **kw):
        super().__init__(**kw)
        self.period, self.band = period, band_pct / 100

    @property
    def warmup(self) -> int:
        return self.period

    def on_candles(self, product_id: str, candles: list[Candle], position_base: Decimal) -> Signal:
        if len(candles) < self.warmup:
            return self.hold("warming up")
        close = candles[-1].close
        avg = sma([c.close for c in candles], self.period)
        if position_base == 0 and close > avg * (1 + self.band):
            return Signal("buy", f"close {close:.2f} above SMA{self.period} {avg:.2f}", quote_size=self.order_quote)
        if position_base > 0 and close < avg * (1 - self.band):
            return Signal("sell", f"close {close:.2f} below SMA{self.period} {avg:.2f}")
        return self.hold(f"close {close:.2f}, SMA{self.period} {avg:.2f}")
