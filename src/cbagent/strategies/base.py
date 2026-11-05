"""Strategy interface. Strategies only propose; they never size past the risk limits or place orders."""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from ..models import Candle, Signal


class Strategy(ABC):
    name: str = "base"
    use_stop: bool = True  # whether the engine's ATR trailing stop applies

    def __init__(self, order_quote: float = 50, **params):
        self.order_quote = Decimal(str(order_quote))
        self.params = params

    @property
    @abstractmethod
    def warmup(self) -> int:
        """How many closed candles on_candles needs before it can give a signal."""

    @abstractmethod
    def on_candles(self, product_id: str, candles: list[Candle], position_base: Decimal) -> Signal:
        """Look at closed candles (oldest first) and the current holding, and return a signal."""

    def hold(self, reason: str) -> Signal:
        return Signal("hold", reason)
