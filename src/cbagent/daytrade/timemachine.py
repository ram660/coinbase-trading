"""Time Machine: how often would this take-profit / stop pair hit the target first, the stop first, or neither?

Two answers, both from the coin's own recent 5-minute candles:

1. Replays. The next `hours` are rebuilt `runs` times. Each 5-minute step is a real candle drawn at random from
   the same time of day (UTC) in the lookback, scaled to the replay's running price, so the replays carry the
   coin's usual intraday rhythm (quiet Asia hours, busy US open) and its real wick sizes.
2. History. The same distances (stop and target as % of entry) are tested from every past 5-minute close in
   the lookback, walking forward through what really happened.

A candle that touches both the stop and the target is counted as a stop (we can't tell which came first), and a
candle that opens past a level fills at its open. Fees are charged on the way in and out. It is a simulation of
the recent past, not a forecast: a listing, a hack or a headline can do what the last few weeks never did.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import asdict, dataclass, field

from ..models import Candle
from .common import BAR, BARS_PER_DAY


@dataclass
class Plan:
    product_id: str
    side: str          # "long" or "short"
    entry: float
    stop: float
    target: float
    start: int = 0     # unix seconds of the entry; sets the time of day the replays start from
    hours: float = 24  # how long the trade may stay open

    def __post_init__(self):
        if self.side not in ("long", "short"):
            raise ValueError("side must be long or short")
        lo, hi = (self.stop, self.target) if self.side == "long" else (self.target, self.stop)
        if not lo < self.entry < hi:
            raise ValueError(f"a {self.side} needs stop and target on opposite sides of entry "
                             f"(entry {self.entry}, stop {self.stop}, target {self.target})")

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_r(self) -> float:
        return abs(self.target - self.entry) / self.risk


@dataclass
class Tally:
    target: int = 0
    stop: int = 0
    open: int = 0
    both_in_one_bar: int = 0                     # counted as stops
    r_values: list[float] = field(default_factory=list, repr=False)
    end_moves: list[float] = field(default_factory=list, repr=False)  # % move to the end for trades still open
    bars_to_exit: list[int] = field(default_factory=list, repr=False)

    @property
    def n(self) -> int:
        return self.target + self.stop + self.open

    def pct(self, k: str) -> float:
        return 100 * getattr(self, k) / self.n if self.n else 0.0

    @property
    def avg_r(self) -> float:
        return statistics.fmean(self.r_values) if self.r_values else 0.0


@dataclass
class Result:
    plan: Plan
    fee_pct: float
    lookback_days: float
    replays: Tally
    history: Tally
    seed: int

    def fee_r(self) -> float:
        """Round-trip fees in R (fractions of the risk per unit)."""
        return 2 * self.fee_pct / 100 * self.plan.entry / self.plan.risk

    def to_dict(self) -> dict:
        def t(x: Tally) -> dict:
            return {"runs": x.n, "target_pct": round(x.pct("target"), 1), "stop_pct": round(x.pct("stop"), 1),
                    "open_pct": round(x.pct("open"), 1), "both_in_one_bar": x.both_in_one_bar,
                    "avg_r_after_fees": round(x.avg_r, 3)}
        return {"plan": asdict(self.plan), "fee_pct": self.fee_pct, "lookback_days": self.lookback_days,
                "seed": self.seed, "replays": t(self.replays), "history": t(self.history)}


def _shapes(candles: list[Candle]) -> dict[int, list[tuple[float, float, float, float]]]:
    """Each candle as (open, high, low, close) relative to the previous close, grouped by 5-minute slot of the UTC day."""
    out: dict[int, list] = {}
    for prev, c in zip(candles, candles[1:]):
        if prev.close <= 0 or c.start - prev.start != BAR:
            continue
        p = prev.close
        out.setdefault((c.start % 86400) // BAR, []).append((c.open / p, c.high / p, c.low / p, c.close / p))
    return out


def _walk(plan: Plan, entry: float, stop: float, target: float, steps, fee_r: float, tally: Tally) -> None:
    """Walk OHLC steps forward from entry and add the outcome to tally."""
    long = plan.side == "long"
    risk = abs(entry - stop)
    price = entry
    n = 0
    for o, h, l, c in steps:
        n += 1
        hit_stop = l <= stop if long else h >= stop
        hit_target = h >= target if long else l <= target
        if hit_stop or hit_target:
            if hit_stop and hit_target:
                tally.both_in_one_bar += 1
            if hit_stop:
                gapped = o <= stop if long else o >= stop
                fill = o if gapped else stop
                tally.stop += 1
            else:
                gapped = o >= target if long else o <= target
                fill = o if gapped else target
                tally.target += 1
            move = (fill - entry) if long else (entry - fill)
            tally.r_values.append(move / risk - fee_r)
            tally.bars_to_exit.append(n)
            return
        price = c
    move = (price - entry) if long else (entry - price)
    tally.open += 1
    tally.r_values.append(move / risk - fee_r)
    tally.end_moves.append((price / entry - 1) * 100)


def replay(plan: Plan, candles: list[Candle], runs: int = 10_000, seed: int = 7, fee_pct: float = 0.5) -> Tally:
    shapes = _shapes(candles)
    if not shapes:
        raise ValueError("not enough consecutive 5-minute candles to replay")
    all_shapes = [s for v in shapes.values() for s in v]
    rng = random.Random(seed)
    horizon = max(1, int(plan.hours * 3600 // BAR))
    start_slot = (plan.start % 86400) // BAR if plan.start else 0
    fee_r = 2 * fee_pct / 100 * plan.entry / plan.risk
    tally = Tally()

    def steps():
        price = plan.entry
        for k in range(1, horizon + 1):
            bucket = shapes.get((start_slot + k) % BARS_PER_DAY) or all_shapes
            o, h, l, c = rng.choice(bucket)
            yield price * o, price * h, price * l, price * c
            price *= c

    for _ in range(runs):
        _walk(plan, plan.entry, plan.stop, plan.target, steps(), fee_r, tally)
    return tally


def history(plan: Plan, candles: list[Candle], fee_pct: float = 0.5, every: int = 1) -> Tally:
    """The plan's % distances, tested from every past close that has `hours` of real candles after it."""
    horizon = max(1, int(plan.hours * 3600 // BAR))
    stop_f, target_f = plan.stop / plan.entry, plan.target / plan.entry
    fee_r = 2 * fee_pct / 100 / abs(1 - stop_f)
    tally = Tally()
    for i in range(0, len(candles) - horizon, every):
        e = candles[i].close
        steps = ((c.open, c.high, c.low, c.close) for c in candles[i + 1:i + 1 + horizon])
        _walk(plan, e, e * stop_f, e * target_f, steps, fee_r, tally)
    return tally


def run(plan: Plan, candles: list[Candle], runs: int = 10_000, seed: int = 7, fee_pct: float = 0.5) -> Result:
    days = (candles[-1].start - candles[0].start) / 86400 if len(candles) > 1 else 0
    return Result(plan, fee_pct, round(days, 1), replay(plan, candles, runs, seed, fee_pct),
                  history(plan, candles, fee_pct), seed)


def _verdict(avg_r: float) -> str:
    if avg_r > 0.15:
        return "the recent past leans in its favour"
    if avg_r < -0.15:
        return "the recent past leans against it"
    return "about a coin flip once fees are paid"


def report(res: Result) -> str:
    p, r, h = res.plan, res.replays, res.history
    pct = lambda x: abs(x / p.entry - 1) * 100
    lines = [
        f"Time Machine: {p.product_id} {p.side.upper()} from {p.entry:g}, stop {p.stop:g} (-{pct(p.stop):.2f}%), "
        f"target {p.target:g} (+{pct(p.target):.2f}%), reward {p.reward_r:.2f}R, out after {p.hours:g}h",
        f"Fees {res.fee_pct:g}% each way cost {res.fee_r():.2f}R per trade. Built from {res.lookback_days:g} days of "
        f"5-minute candles.",
        "",
        f"{r.n:,} replays (seed {res.seed}): target first {r.pct('target'):.1f}%, stop first {r.pct('stop'):.1f}%, "
        f"still open {r.pct('open'):.1f}%",
    ]
    if r.end_moves:
        q = statistics.quantiles(r.end_moves, n=10) if len(r.end_moves) >= 10 else [min(r.end_moves), max(r.end_moves)]
        lines.append(f"  still-open replays ended between {q[0]:+.2f}% and {q[-1]:+.2f}% (10th to 90th percentile)")
    if r.bars_to_exit:
        lines.append(f"  median time to stop or target: {statistics.median(r.bars_to_exit) * BAR / 3600:.1f}h")
    lines.append(f"  average result after fees {r.avg_r:+.2f}R: {_verdict(r.avg_r)}")
    if h.n:
        lines += [
            f"History: the same distances from {h.n:,} past 5-minute closes: target {h.pct('target'):.1f}%, "
            f"stop {h.pct('stop'):.1f}%, neither {h.pct('open'):.1f}%",
            f"  average result after fees {h.avg_r:+.2f}R: {_verdict(h.avg_r)}",
        ]
    both = r.both_in_one_bar + h.both_in_one_bar
    if both:
        lines.append(f"{both} runs touched both levels inside one candle and were counted as stops.")
    lines.append("A replay of recent candles, not a forecast.")
    return "\n".join(lines)
