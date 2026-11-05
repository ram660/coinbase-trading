"""Robustness check: run each candidate over many rolling windows instead of one lucky stretch.

For every window it replays the strategy with a fresh $ starting_cash, sized to use the whole allocation, and
compares it with buying and holding the same products in equal parts. A candidate is only worth paper trading if
it holds up across most windows, not just on average.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from decimal import Decimal

from .brokers.paper import PaperBroker
from .config import Config
from .engine import Engine, run_backtest
from .exits import AtrTrailingStop
from .models import Candle
from .risk import RiskManager
from .strategies import build_strategy

DAY = 86400
PREFIX_DAYS = 210  # indicator warm-up before each window; covers a 200-day average

CANDIDATES: list[tuple[str, dict]] = [
    *[(f"trend {p}d band {b}%", {"name": "trend", "period": p, "band_pct": b}) for p in (50, 100, 150, 200) for b in (0, 2, 5)],
    ("sma cross 20/50", {"name": "sma_crossover", "fast": 20, "slow": 50}),
    ("sma cross 50/200", {"name": "sma_crossover", "fast": 50, "slow": 200}),
    ("confluence", {"name": "confluence"}),
    ("weekly buy + dips", {"name": "dca"}),
]


@dataclass
class Row:
    label: str
    returns: list[Decimal]
    vs_hold: list[Decimal]
    drawdowns: list[Decimal]
    trips: int

    def line(self) -> str:
        n = len(self.returns)
        beat = sum(1 for v in self.vs_hold if v > 0)
        positive = sum(1 for r in self.returns if r > 0)
        return (f"{self.label:<22} median {statistics.median(self.returns):+6.1f}%  worst {min(self.returns):+6.1f}%  "
                f"positive {positive}/{n}  beat hold {beat}/{n}  median dd {statistics.median(self.drawdowns):4.1f}%  "
                f"kill trips {self.trips}")


def run(cfg: Config, history: dict[str, list[Candle]], window_days: int, step_days: int, use_stop: bool) -> list[Row]:
    products = list(history)
    cash = cfg.paper.starting_cash
    per_product = (cash / len(products)).quantize(Decimal("0.01"))
    # Size orders to use the whole allocation; keep the drawdown, cool-off and daily-loss rules from config.
    risk = replace(cfg.risk, max_order_quote=per_product, max_position_quote=per_product * 3,
                   max_total_exposure_quote=cash * 3, max_daily_loss_quote=cash, max_orders_per_day=100)
    common = sorted(set.intersection(*({c.start for c in cs} for cs in history.values())))
    first_start = common[0] + PREFIX_DAYS * DAY
    starts = range(first_start, common[-1] - window_days * DAY + 1, step_days * DAY)

    rows: list[Row] = []
    for label, settings in CANDIDATES:
        size = per_product if settings["name"] != "dca" else (cash / len(products) / (window_days // 7 + 10)).quantize(Decimal("0.01"))
        row = Row(label, [], [], [], 0)
        for start in starts:
            end = start + window_days * DAY
            window = {p: [c for c in cs if start - PREFIX_DAYS * DAY <= c.start < end] for p, cs in history.items()}
            strategy = build_strategy({**settings, "order_quote": float(size)})
            stop = AtrTrailingStop(int(cfg.exits.get("atr_period", 14)), float(cfg.exits.get("atr_mult", 3))) if use_stop else None
            engine = Engine(strategy, RiskManager(risk), PaperBroker(replace(cfg.paper, starting_cash=cash)),
                            log=lambda _m: None, stop=stop)
            result = run_backtest(engine, window, start_ts=start)
            hold = sum(result.buy_and_hold_pct.values()) / len(products)
            row.returns.append(result.return_pct)
            row.vs_hold.append(result.return_pct - hold)
            row.drawdowns.append(result.max_drawdown_pct)
            row.trips += 1 if result.kill_switch or any("restarted" in n for n in engine.risk.state.notes) else 0
        rows.append(row)
    return rows


def hold_line(history: dict[str, list[Candle]], window_days: int, step_days: int) -> str:
    common = sorted(set.intersection(*({c.start for c in cs} for cs in history.values())))
    by_ts = {p: {c.start: c.close for c in cs} for p, cs in history.items()}
    out = []
    for start in range(common[0] + PREFIX_DAYS * DAY, common[-1] - window_days * DAY + 1, step_days * DAY):
        end = max(t for t in common if t < start + window_days * DAY)
        s = min(t for t in common if t >= start)
        out.append(sum(by_ts[p][end] / by_ts[p][s] - 1 for p in history) / len(history) * 100)
    pos = sum(1 for r in out if r > 0)
    return f"{'buy and hold':<22} median {statistics.median(out):+6.1f}%  worst {min(out):+6.1f}%  positive {pos}/{len(out)}"
