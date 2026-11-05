"""The trading loop: strategy proposes, risk manager decides, broker executes, journal records."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .brokers.base import Broker
from .exits import AtrTrailingStop
from .models import Candle, Fill, OrderRequest, Position, Signal
from .risk import Account, RiskManager
from .strategies.base import Strategy


def _jsonable(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(type(obj))


class Engine:
    def __init__(
        self,
        strategy: Strategy,
        risk: RiskManager,
        broker: Broker,
        journal_path: Path | None = None,
        log: Callable[[str], None] = print,
        stop: AtrTrailingStop | None = None,
    ):
        self.strategy, self.risk, self.broker = strategy, risk, broker
        self.stop = stop if strategy.use_stop else None
        self.last_candle: dict[str, int] = {}  # act once per closed candle, however often the loop polls
        self.journal_path = journal_path
        self.log = log

    def account(self, prices: dict[str, Decimal]) -> Account:
        return Account(self.broker.cash(), dict(self.broker.positions()), prices)

    def journal(self, event: str, **data) -> None:
        if not self.journal_path:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}, default=_jsonable)
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def tick(self, candles_by_product: dict[str, list[Candle]], now_ts: int) -> list[Fill]:
        prices = {p: Decimal(str(c[-1].close)) for p, c in candles_by_product.items() if c}
        for p in self.broker.positions():
            prices.setdefault(p, Decimal("0"))
        self.risk.update(self.account(prices).equity(), datetime.fromtimestamp(now_ts, timezone.utc))
        if self.risk.state.flatten_pending:
            return self.flatten(prices, now_ts)
        if self.risk.kill_switch_active():
            self.log("kill switch is on: skipping this tick")
            return []

        fills: list[Fill] = []
        for product, candles in candles_by_product.items():
            if not candles or self.last_candle.get(product) == candles[-1].start:
                continue
            self.last_candle[product] = candles[-1].start
            pos = self.broker.positions().get(product, Position())
            stop_reason = self.stop.check(product, candles) if self.stop and pos.base > 0 else None
            signal = Signal("sell", stop_reason) if stop_reason else self.strategy.on_candles(product, candles, pos.base)
            if signal.action == "hold":
                continue
            account = self.account(prices)
            if signal.action == "buy":
                order = OrderRequest(product, "BUY", signal.quote_size or self.strategy.order_quote, prices[product], signal.reason)
            else:
                order = OrderRequest(product, "SELL", account.exposure(product) * signal.sell_fraction, prices[product], signal.reason)
            decision = self.risk.check(order, account)
            self.journal("decision", order=asdict(order), approved=decision.approved, why=decision.reason, size=decision.quote_size)
            if not decision.approved:
                self.log(f"{product} {order.side} rejected: {decision.reason}")
                continue
            fill = self.broker.market_order(product, order.side, decision.quote_size, order.price, now_ts, order.reason)
            self.risk.record_order()
            if self.stop:
                if fill.side == "BUY":
                    self.stop.on_entry(product, float(fill.price))
                elif self.broker.positions().get(product, Position()).base <= 0:
                    self.stop.on_exit(product)
            self.journal("fill", fill=asdict(fill))
            self.log(f"{product} {fill.side} {fill.base_size:.8f} @ {fill.price:.2f} ({fill.reason})")
            fills.append(fill)
        return fills

    def flatten(self, prices: dict[str, Decimal], now_ts: int) -> list[Fill]:
        """Sell every holding at market after a drawdown trip. Bypasses the risk check on purpose."""
        fills: list[Fill] = []
        reason = f"kill switch flatten: {self.risk.state.tripped_reason}"
        for product, pos in list(self.broker.positions().items()):
            price = prices.get(product, Decimal("0"))
            if pos.base <= 0 or price <= 0:
                continue
            try:
                fill = self.broker.market_order(product, "SELL", pos.base * price, price, now_ts, reason)
            except Exception as e:  # keep going so one failure doesn't leave the rest held
                self.log(f"{product} flatten failed: {e}")
                self.journal("error", message=f"flatten {product}: {e}")
                continue
            if self.stop:
                self.stop.on_exit(product)
            self.journal("fill", fill=asdict(fill))
            self.log(f"{product} SELL {fill.base_size:.8f} @ {fill.price:.2f} ({reason})")
            fills.append(fill)
        if all(p.base <= 0 or prices.get(n, Decimal("0")) <= 0 for n, p in self.broker.positions().items()):
            self.risk.flatten_done()
        return fills


@dataclass
class BacktestResult:
    start_equity: Decimal
    end_equity: Decimal
    max_drawdown_pct: Decimal
    fees: Decimal
    fills: list[Fill]
    buy_and_hold_pct: dict[str, Decimal]
    kill_switch: str | None
    equity_curve: list[tuple[int, Decimal]] = field(repr=False, default_factory=list)

    @property
    def return_pct(self) -> Decimal:
        return (self.end_equity / self.start_equity - 1) * 100

    def summary(self) -> str:
        bh = ", ".join(f"{p} {v:+.2f}%" for p, v in self.buy_and_hold_pct.items())
        return "\n".join([
            f"Start equity   {self.start_equity:.2f}",
            f"End equity     {self.end_equity:.2f}  ({self.return_pct:+.2f}%)",
            f"Max drawdown   {self.max_drawdown_pct:.2f}%",
            f"Orders         {len(self.fills)}  (fees {self.fees:.2f})",
            f"Buy and hold   {bh}",
            f"Kill switch    {self.kill_switch or 'not tripped'}",
        ])


def run_backtest(engine: Engine, history: dict[str, list[Candle]], start_ts: int = 0) -> BacktestResult:
    """Replay history bar by bar. The engine's broker must be a fresh PaperBroker.

    Candles before start_ts only warm up the indicators; trading and buy-and-hold are measured from start_ts.
    """
    by_ts = {p: {c.start: c for c in cs} for p, cs in history.items()}
    timeline = sorted(set.intersection(*(set(m) for m in by_ts.values())))
    warmup = engine.strategy.warmup
    keep = max(warmup * 2, 100)  # strategies only look back so far; copying less keeps long replays fast
    window: dict[str, list[Candle]] = {p: [] for p in history}

    curve: list[tuple[int, Decimal]] = []
    start_equity = engine.broker.cash()
    peak, max_dd = start_equity, Decimal("0")
    for ts in timeline:
        for p in history:
            window[p].append(by_ts[p][ts])
        if len(window[next(iter(window))]) < warmup or ts < start_ts:
            continue
        engine.tick({p: w[-keep:] for p, w in window.items()}, ts)
        prices = {p: Decimal(str(w[-1].close)) for p, w in window.items()}
        equity = engine.account(prices).equity()
        curve.append((ts, equity))
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    fills = getattr(engine.broker, "fills", [])
    traded = [ts for ts, _ in curve] or timeline
    first = {p: Decimal(str(by_ts[p][traded[0]].close)) for p in history} if traded else {}
    last = {p: Decimal(str(by_ts[p][timeline[-1]].close)) for p in history} if timeline else {}
    return BacktestResult(
        start_equity=start_equity,
        end_equity=curve[-1][1] if curve else start_equity,
        max_drawdown_pct=max_dd,
        fees=sum((f.fee for f in fills), Decimal("0")),
        fills=list(fills),
        buy_and_hold_pct={p: (last[p] / first[p] - 1) * 100 for p in first},
        kill_switch=engine.risk.state.tripped_reason,
        equity_curve=curve,
    )
