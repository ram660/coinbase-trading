"""Paper broker: simulates market fills with fees and slippage. Never touches Coinbase."""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

from ..config import PaperConfig
from ..models import Fill, Position, Side
from .base import Broker

HUNDRED = Decimal("100")


class PaperBroker(Broker):
    def __init__(self, cfg: PaperConfig, state_path: Path | None = None):
        self.cfg = cfg
        self.state_path = state_path
        self._cash = cfg.starting_cash
        self._positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        if state_path and state_path.exists():
            self._load()

    def cash(self) -> Decimal:
        return self._cash

    def positions(self) -> dict[str, Position]:
        return self._positions

    def market_order(self, product_id: str, side: Side, quote_size: Decimal, price: Decimal, ts: int, reason: str) -> Fill:
        pos = self._positions.setdefault(product_id, Position())
        slip = self.cfg.slippage_pct / HUNDRED
        fee_rate = self.cfg.fee_pct / HUNDRED
        if side == "BUY":
            fill_price = price * (1 + slip)
            fee = quote_size * fee_rate
            base = (quote_size - fee) / fill_price
            if quote_size > self._cash:
                raise ValueError("paper broker: not enough cash")
            self._cash -= quote_size
            pos.base += base
            pos.cost += quote_size
        else:
            fill_price = price * (1 - slip)
            base = min(pos.base, quote_size / price)
            if base <= 0:
                raise ValueError("paper broker: nothing to sell")
            proceeds = base * fill_price
            fee = proceeds * fee_rate
            share = base / pos.base
            self._cash += proceeds - fee
            pos.cost -= pos.cost * share
            pos.base -= base
            if pos.base <= Decimal("1e-12"):
                pos.base, pos.cost = Decimal("0"), Decimal("0")
        fill = Fill(product_id, side, base, fill_price, fee, ts, f"paper-{uuid.uuid4().hex[:12]}", reason)
        self.fills.append(fill)
        self._save()
        return fill

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cash": str(self._cash),
            "positions": {p: {"base": str(v.base), "cost": str(v.cost)} for p, v in self._positions.items()},
        }
        self.state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load(self) -> None:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._cash = Decimal(data["cash"])
        self._positions = {
            p: Position(Decimal(v["base"]), Decimal(v["cost"])) for p, v in data["positions"].items()
        }
