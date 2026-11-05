"""Signal lab: test cross-coin ideas from the scanner over rolling windows.

First idea, momentum rotation: every `rebalance_days`, rank the liquid coins by risk-adjusted momentum
(return over `lookback` days divided by 30-day volatility), hold the top `top_n` that are also above their 50-day
average, in equal parts, and sit in cash otherwise. Optionally only invest while BTC is above its 200-day average.
Turnover pays `fee_pct` each way.

Caveat: the universe is today's liquid Coinbase coins, so coins that died or were delisted are missing
(survivorship bias). Results flatter the strategy and the equal-weight basket alike.
"""
from __future__ import annotations

import bisect
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .market_data import fetch_candles
from .scanner import list_products

DAY = 86400


@dataclass(frozen=True)
class Rotation:
    top_n: int = 5
    lookback: int = 90
    rebalance_days: int = 7
    btc_filter: bool = True
    fee_pct: float = 0.5

    @property
    def label(self) -> str:
        return f"top {self.top_n}, {self.lookback}d momentum{', BTC filter' if self.btc_filter else ''}"


def load_universe(quote: str, days: int, min_volume: float = 1_000_000) -> dict[str, dict[int, float]]:
    """Daily closes keyed by timestamp for every liquid non-stable pair, plus BTC."""
    ids = sorted({p["product_id"] for p in list_products(quote, min_volume)} | {f"BTC-{quote}"})

    def one(pid):
        for attempt in range(4):
            try:
                return pid, {c.start: c.close for c in fetch_candles(pid, "ONE_DAY", days)}
            except Exception:
                time.sleep(2 ** attempt)  # public API rate limit
        return pid, {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        return {pid: closes for pid, closes in pool.map(one, ids) if len(closes) > 60}


def _score(series: list[float], lookback: int) -> float | None:
    if len(series) < max(lookback, 50) + 1:
        return None
    rets = [b / a - 1 for a, b in zip(series[-31:], series[-30:])]
    vol = statistics.pstdev(rets) * math.sqrt(365)
    if vol <= 0 or series[-1] < statistics.fmean(series[-50:]):
        return None
    momentum = series[-1] / series[-1 - lookback] - 1
    return momentum / vol if momentum > 0 else None


def simulate(closes: dict[str, dict[int, float]], cfg: Rotation, start: int, end: int, btc_id: str) -> tuple[float, float]:
    """Return (% return, % max drawdown) for the rotation between start and end."""
    days = sorted(t for t in closes[btc_id] if start <= t < end)
    index = {p: (sorted(s), [s[x] for x in sorted(s)]) for p, s in closes.items()}
    need = max(cfg.lookback, 200) + 31

    def upto(p: str, t: int) -> list[float]:
        ts, vals = index[p]
        i = bisect.bisect_right(ts, t)
        return vals[max(0, i - need):i]

    value, peak, worst = 1.0, 1.0, 0.0
    weights: dict[str, float] = {}
    for i, t in enumerate(days):
        if i:
            prev = days[i - 1]
            growth = sum(w * closes[p][t] / closes[p][prev] for p, w in weights.items() if t in closes[p] and prev in closes[p])
            growth += sum(w for p, w in weights.items() if not (t in closes[p] and prev in closes[p]))  # missing day: flat
            cash = 1 - sum(weights.values())
            value *= growth + cash
            # let weights drift with prices
            if value > 0 and weights:
                total = growth + cash
                weights = {p: w * (closes[p][t] / closes[p][prev] if t in closes[p] and prev in closes[p] else 1) / total
                           for p, w in weights.items()}
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
        if i % cfg.rebalance_days:
            continue
        target: dict[str, float] = {}
        btc = upto(btc_id, t)
        risk_on = not cfg.btc_filter or (len(btc) >= 200 and btc[-1] > statistics.fmean(btc[-200:]))
        if risk_on:
            scored = sorted(((s, p) for p in closes if (s := _score(upto(p, t), cfg.lookback)) is not None), reverse=True)
            picks = [p for _, p in scored[: cfg.top_n]]
            target = {p: 1 / cfg.top_n for p in picks}  # fewer picks than top_n leaves the rest in cash
        turnover = sum(abs(target.get(p, 0) - weights.get(p, 0)) for p in set(target) | set(weights))
        value *= 1 - turnover * cfg.fee_pct / 100
        weights = target
    return (value - 1) * 100, worst * 100


def hold_basket(closes: dict[str, dict[int, float]], ids: list[str], start: int, end: int) -> float:
    """Equal-weight buy and hold of `ids` (those trading at `start`), % return."""
    rets = []
    for p in ids:
        ts = sorted(t for t in closes[p] if start <= t < end)
        if ts and ts[0] - start < 3 * DAY:
            rets.append(closes[p][ts[-1]] / closes[p][ts[0]] - 1)
    return statistics.fmean(rets) * 100 if rets else float("nan")


def rolling(closes, configs: list[Rotation], btc_id: str, window_days: int = 365, step_days: int = 90, warmup_days: int = 210):
    if btc_id not in closes:
        raise RuntimeError(f"no price history for {btc_id}; try again in a minute (rate limit)")
    all_days = sorted(closes[btc_id])
    starts = list(range(all_days[0] + warmup_days * DAY, all_days[-1] - window_days * DAY + 1, step_days * DAY))
    alts = [p for p in closes if p != btc_id]
    lines = [f"{len(starts)} windows of {window_days} days, {len(closes)} coins in the universe", ""]
    bench = {"BTC hold": [hold_basket(closes, [btc_id], s, s + window_days * DAY) for s in starts],
             "equal-weight all coins": [hold_basket(closes, alts, s, s + window_days * DAY) for s in starts]}
    for name, rets in bench.items():
        lines.append(f"{name:<40} median {statistics.median(rets):+7.1f}%  worst {min(rets):+7.1f}%  "
                     f"positive {sum(r > 0 for r in rets)}/{len(rets)}")
    for cfg in configs:
        results = [simulate(closes, cfg, s, s + window_days * DAY, btc_id) for s in starts]
        rets = [r for r, _ in results]
        beat = sum(r > b for r, b in zip(rets, bench["BTC hold"]))
        lines.append(f"{cfg.label:<40} median {statistics.median(rets):+7.1f}%  worst {min(rets):+7.1f}%  "
                     f"positive {sum(r > 0 for r in rets)}/{len(rets)}  beat BTC {beat}/{len(rets)}  "
                     f"median dd {statistics.median(d for _, d in results):6.1f}%")
    return "\n".join(lines)
