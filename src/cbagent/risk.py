"""Risk manager. Every order passes through check() before any broker sees it.

Rules, in order:
1. Kill switch: if the kill switch file exists (or was tripped in memory), the strategy can't trade.
   A manual kill (`cbagent kill`) freezes holdings so a person decides what to do with them.
2. Drawdown: if equity falls max_drawdown_pct below its peak, the kill switch trips automatically. With
   on_drawdown = "flatten" (the default) the engine first sells every holding to cash; "freeze" keeps them.
   With cooloff_days > 0, an automatic trip restarts by itself after that many days, at reentry_size_pct
   order sizes for another cooloff_days. A manual kill never restarts by itself.
3. Daily loss: once today's (UTC) loss reaches max_daily_loss_quote, no new buys until tomorrow. Sells still go.
4. Order count: at most max_orders_per_day orders per UTC day.
5. Sizing: buys are clamped to max_order_quote, the per-product cap, the total exposure cap and available cash.
   A buy that would end up under min_order_quote is rejected.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .config import RiskConfig
from .models import OrderRequest, Position

MIN_ORDER_QUOTE = Decimal("1")


@dataclass
class Account:
    cash: Decimal
    positions: dict[str, Position]
    prices: dict[str, Decimal]

    def exposure(self, product_id: str) -> Decimal:
        pos = self.positions.get(product_id)
        return pos.base * self.prices[product_id] if pos and pos.base else Decimal("0")

    def total_exposure(self) -> Decimal:
        return sum((self.exposure(p) for p in self.positions), Decimal("0"))

    def equity(self) -> Decimal:
        return self.cash + self.total_exposure()


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str
    quote_size: Decimal = Decimal("0")


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: Decimal = Decimal("0")
    peak_equity: Decimal = Decimal("0")
    orders_today: int = 0
    tripped_reason: str | None = None
    flatten_pending: bool = False
    auto_tripped: bool = False        # tripped by the drawdown rule, so the cool-off may restart it
    tripped_at: str | None = None     # ISO time of the trip
    reduced_until: str | None = None  # after a cool-off restart, buys are scaled down until this time
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        for k in ("day_start_equity", "peak_equity"):
            d[k] = str(d[k])
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "RiskState":
        d = json.loads(text)
        for k in ("day_start_equity", "peak_equity"):
            d[k] = Decimal(d[k])
        return cls(**d)


class RiskManager:
    def __init__(self, cfg: RiskConfig, kill_switch_path: Path | None = None, state_path: Path | None = None):
        self.cfg = cfg
        self.kill_switch_path = kill_switch_path
        self.state_path = state_path
        self.state = RiskState()
        self.now = datetime.now(timezone.utc)
        if state_path and state_path.exists():
            self.state = RiskState.from_json(state_path.read_text(encoding="utf-8"))

    # --- kill switch ---------------------------------------------------------------------------

    def kill_switch_active(self) -> bool:
        if self.state.tripped_reason:
            return True
        return bool(self.kill_switch_path and self.kill_switch_path.exists())

    def trip_kill_switch(self, reason: str, auto: bool = False) -> None:
        self.state.tripped_reason = reason
        self.state.auto_tripped = auto
        self.state.tripped_at = self.now.isoformat()
        if self.kill_switch_path:
            self.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
            self.kill_switch_path.write_text(f"{datetime.now(timezone.utc).isoformat()} {reason}\n", encoding="utf-8")
        self._save()

    def flatten_done(self) -> None:
        self.state.flatten_pending = False
        self._save()

    def reset_kill_switch(self) -> None:
        self.state.tripped_reason = None
        self.state.flatten_pending = False
        self.state.auto_tripped = False
        self.state.tripped_at = None
        if self.kill_switch_path and self.kill_switch_path.exists():
            self.kill_switch_path.unlink()
        self._save()

    # --- bookkeeping ---------------------------------------------------------------------------

    def update(self, equity: Decimal, now: datetime) -> None:
        """Call once per tick with current equity, before checking orders."""
        self.now = now
        self._maybe_restart(equity, now)
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        if day != self.state.day:
            self.state.day = day
            self.state.day_start_equity = equity
            self.state.orders_today = 0
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity
        if self.state.peak_equity > 0 and not self.state.tripped_reason:
            drawdown_pct = (self.state.peak_equity - equity) / self.state.peak_equity * 100
            if drawdown_pct >= self.cfg.max_drawdown_pct:
                self.state.flatten_pending = self.cfg.on_drawdown == "flatten"
                self.trip_kill_switch(
                    f"drawdown {drawdown_pct:.2f}% from peak {self.state.peak_equity:.2f} hit the {self.cfg.max_drawdown_pct}% limit",
                    auto=True,
                )
        self._save()

    def _maybe_restart(self, equity: Decimal, now: datetime) -> None:
        """After an automatic trip, restart once cooloff_days have passed. Manual kills never restart."""
        s = self.state
        if not (s.tripped_reason and s.auto_tripped and s.tripped_at and self.cfg.cooloff_days > 0) or s.flatten_pending:
            return
        cooloff = timedelta(days=self.cfg.cooloff_days)
        if now < datetime.fromisoformat(s.tripped_at) + cooloff:
            return
        self.reset_kill_switch()
        s.peak_equity = equity  # measure the next drawdown from here, or it would trip again at once
        s.reduced_until = (now + cooloff).isoformat()
        s.notes.append(f"{now.isoformat()} restarted after cool-off at equity {equity:.2f}")

    def size_factor(self) -> Decimal:
        s = self.state
        if s.reduced_until and self.now < datetime.fromisoformat(s.reduced_until):
            return self.cfg.reentry_size_pct / 100
        return Decimal("1")

    def daily_loss(self, equity: Decimal) -> Decimal:
        return max(Decimal("0"), self.state.day_start_equity - equity)

    def record_order(self) -> None:
        self.state.orders_today += 1
        self._save()

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(self.state.to_json(), encoding="utf-8")

    # --- the check -----------------------------------------------------------------------------

    def check(self, order: OrderRequest, account: Account) -> Decision:
        if self.kill_switch_active():
            return Decision(False, "kill switch is on")
        if self.state.orders_today >= self.cfg.max_orders_per_day:
            return Decision(False, f"already placed {self.state.orders_today} orders today")

        if order.side == "SELL":
            held = account.exposure(order.product_id)
            if held <= 0:
                return Decision(False, "nothing to sell")
            return Decision(True, "ok", min(order.quote_size, held))

        equity = account.equity()
        loss = self.daily_loss(equity)
        if loss >= self.cfg.max_daily_loss_quote:
            return Decision(False, f"daily loss {loss:.2f} reached the {self.cfg.max_daily_loss_quote} limit")

        room = min(
            order.quote_size * self.size_factor(),
            self.cfg.max_order_quote,
            self.cfg.max_position_quote - account.exposure(order.product_id),
            self.cfg.max_total_exposure_quote - account.total_exposure(),
            account.cash,
        )
        if room < MIN_ORDER_QUOTE:
            return Decision(False, "no room under position, exposure or cash limits")
        reason = "ok" if room == order.quote_size else f"clamped from {order.quote_size} to {room:.2f}"
        return Decision(True, reason, room)
