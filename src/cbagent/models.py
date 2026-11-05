"""Plain data types shared by strategies, risk, brokers and the engine."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Candle:
    start: int  # unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    action: Literal["buy", "sell", "hold"]
    reason: str
    # For buys: how much quote currency to spend. For sells: fraction of the position (1 = all).
    quote_size: Decimal | None = None
    sell_fraction: Decimal = Decimal("1")


@dataclass(frozen=True)
class OrderRequest:
    product_id: str
    side: Side
    quote_size: Decimal  # value of the order in quote currency
    price: Decimal       # reference price used for risk checks
    reason: str


@dataclass(frozen=True)
class Fill:
    product_id: str
    side: Side
    base_size: Decimal
    price: Decimal
    fee: Decimal
    ts: int
    order_id: str
    reason: str


@dataclass
class Position:
    base: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")  # quote spent on the current holding, fees included
