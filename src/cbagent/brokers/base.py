"""Broker interface shared by the paper simulator and the live Coinbase broker."""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from ..models import Fill, Position, Side


class Broker(ABC):
    @abstractmethod
    def cash(self) -> Decimal: ...

    @abstractmethod
    def positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def market_order(self, product_id: str, side: Side, quote_size: Decimal, price: Decimal, ts: int, reason: str) -> Fill:
        """Buy `quote_size` worth, or sell `quote_size` worth of the holding, at market."""
