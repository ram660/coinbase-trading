"""ATR trailing stop, applied by the engine to every strategy.

While a position is open, track the highest close since entry. If the close falls more than
`mult` x ATR below that high, sell everything, whatever the strategy says.
"""
from __future__ import annotations

from .indicators import atr
from .models import Candle


class AtrTrailingStop:
    def __init__(self, period: int = 14, mult: float = 3.0):
        self.period, self.mult = period, mult
        self.highs: dict[str, float] = {}

    def check(self, product_id: str, candles: list[Candle]) -> str | None:
        """Call only while holding. Returns a reason if the stop is hit."""
        if len(candles) <= self.period:
            return None
        close = candles[-1].close
        high = max(self.highs.get(product_id, close), close)  # after a restart, trail from now
        self.highs[product_id] = high
        stop = high - self.mult * atr(candles, self.period)
        if close <= stop:
            return f"ATR stop: close {close:.2f} under {stop:.2f} ({self.mult}x ATR below high {high:.2f})"
        return None

    def on_entry(self, product_id: str, price: float) -> None:
        self.highs[product_id] = price

    def on_exit(self, product_id: str) -> None:
        self.highs.pop(product_id, None)
