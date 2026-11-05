"""Scheduled buying: a fixed amount on one weekday, more after a big drop. Never sells.

Meant for daily candles. It decides from the candle's date, so it needs no memory between runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from ..models import Candle, Signal
from .base import Strategy

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class Dca(Strategy):
    name = "dca"
    use_stop = False  # selling on a stop defeats the point of accumulating

    def __init__(self, weekday: str = "monday", dip_pct: float = 15, dip_lookback: int = 30, dip_multiplier: float = 2, **kw):
        super().__init__(**kw)
        self.weekday = WEEKDAYS.index(weekday.lower())
        self.dip, self.lookback = dip_pct / 100, dip_lookback
        self.dip_multiplier = Decimal(str(dip_multiplier))

    @property
    def warmup(self) -> int:
        return self.lookback

    def on_candles(self, product_id: str, candles: list[Candle], position_base: Decimal) -> Signal:
        last = candles[-1]
        if datetime.fromtimestamp(last.start, timezone.utc).weekday() != self.weekday:
            return self.hold("not a buy day")
        high = max(c.high for c in candles[-self.lookback:])
        if last.close <= high * (1 - self.dip):
            return Signal("buy", f"weekly buy, doubled: {1 - last.close / high:.0%} under the {self.lookback}-day high",
                          quote_size=self.order_quote * self.dip_multiplier)
        return Signal("buy", "weekly buy", quote_size=self.order_quote)
