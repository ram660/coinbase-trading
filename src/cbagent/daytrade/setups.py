"""Intraday setups on 5-minute candles, for 24/7 markets. Read-only, public data.

Crypto has no opening bell, so the "opening range" is the first `range_minutes` of the UTC day.

  range_break   first 5-minute close above the UTC-day opening range (long) or below it (short)
  break_24h     first close above the prior 24 hours' high (long) or below its low (short)
  volume_spike  a candle with at least 3x the 24-hour median volume that moved at least 1%
  rsi_bounce    RSI(14) was under 30 within the last hour and the latest close is back above 30 and the prior high
  rsi_fade      the mirror image: RSI over 70, back under 70 and below the prior low (short)

Each check looks at the latest closed candle (index -1). A setup is "triggered" when it fired on one of the last
`fresh` candles, "waiting" when price is still inside the levels, and "expired" when it fired earlier today.
The scanner describes what is happening; it doesn't pick trades.
"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..indicators import rsi
from ..models import Candle
from .common import BAR, bars, utc

SETUPS = ("range_break", "break_24h", "volume_spike", "rsi_bounce", "rsi_fade")


@dataclass
class Hit:
    product_id: str
    setup: str
    status: str        # triggered / waiting / expired / none
    side: str          # long / short / ""
    price: float       # latest close
    level: float       # the level that was crossed (or would be)
    trigger_ts: int    # candle that fired, 0 if none
    detail: str
    volume_x: float = 0.0  # latest candle's volume vs the 24h median


def _day_start(ts: int) -> int:
    return ts - ts % 86400


def opening_range(candles: list[Candle], range_minutes: int = 60) -> tuple[float, float, int] | None:
    """(low, high, index of the first candle after the range) for the UTC day of the latest candle."""
    day = _day_start(candles[-1].start)
    end = day + range_minutes * 60
    inside = [c for c in candles if day <= c.start < end]
    if len(inside) < range_minutes // 5 or candles[-1].start < end:
        return None
    after = next(i for i, c in enumerate(candles) if c.start >= end)
    return min(c.low for c in inside), max(c.high for c in inside), after


def _first_cross(candles: list[Candle], start: int, above: float, below: float) -> tuple[int, str] | None:
    for i in range(start, len(candles)):
        if candles[i].close > above:
            return i, "long"
        if candles[i].close < below:
            return i, "short"
    return None


def _status(i: int, n: int, fresh: int) -> str:
    return "triggered" if i >= n - fresh else "expired"


def check(product_id: str, candles: list[Candle], setup: str, fresh: int = 2, range_minutes: int = 60) -> Hit:
    last = candles[-1]
    vols = [c.volume for c in candles[-289:-1]]
    med = statistics.median(vols) if vols else 0
    vx = last.volume / med if med else 0.0
    none = Hit(product_id, setup, "none", "", last.close, 0.0, 0, "", vx)

    if setup == "range_break":
        rng = opening_range(candles, range_minutes)
        if not rng:
            return Hit(product_id, setup, "waiting", "", last.close, 0.0, 0, f"the first {range_minutes} min of the UTC day are still forming", vx)
        lo, hi, after = rng
        cross = _first_cross(candles, after, hi, lo)
        if not cross:
            return Hit(product_id, setup, "waiting", "", last.close, hi, 0,
                       f"inside today's opening range {lo:g} to {hi:g}", vx)
        i, side = cross
        level = hi if side == "long" else lo
        mid = (lo + hi) / 2
        return Hit(product_id, setup, _status(i, len(candles), fresh), side, last.close, level, candles[i].start,
                   f"range {lo:g} to {hi:g} (mid {mid:g}); {utc(candles[i].start)} closed {candles[i].close:g}", vx)

    if setup == "break_24h":
        if len(candles) < 289 + fresh:
            return none
        for i in range(len(candles) - fresh, len(candles)):
            window = candles[i - 288:i]
            hi, lo = max(c.high for c in window), min(c.low for c in window)
            c = candles[i]
            if c.close > hi or c.close < lo:
                side = "long" if c.close > hi else "short"
                level = hi if side == "long" else lo
                return Hit(product_id, setup, "triggered", side, last.close, level, c.start,
                           f"24h range {lo:g} to {hi:g}; {utc(c.start)} closed {c.close:g}", vx)
        window = candles[-289:-1]
        return Hit(product_id, setup, "waiting", "", last.close, max(c.high for c in window), 0,
                   f"inside the 24h range {min(c.low for c in window):g} to {max(c.high for c in window):g}", vx)

    if setup == "volume_spike":
        for i in range(len(candles) - fresh, len(candles)):
            c, p = candles[i], candles[i - 1]
            m = statistics.median([x.volume for x in candles[max(0, i - 288):i]] or [0])
            move = (c.close / p.close - 1) * 100 if p.close else 0
            if m and c.volume >= 3 * m and abs(move) >= 1:
                return Hit(product_id, setup, "triggered", "long" if move > 0 else "short", last.close, p.close, c.start,
                           f"{c.volume / m:.1f}x the 24h median volume, {move:+.2f}% in 5 min", vx)
        return none

    if setup in ("rsi_bounce", "rsi_fade"):
        closes = [c.close for c in candles]
        if len(closes) < 40:
            return none
        for i in range(len(candles) - fresh, len(candles)):
            now, before = rsi(closes[:i + 1]), [rsi(closes[:j + 1]) for j in range(i - 12, i)]
            c, p = candles[i], candles[i - 1]
            if setup == "rsi_bounce" and min(before) < 30 <= now and c.close > p.high:
                return Hit(product_id, setup, "triggered", "long", last.close, p.high, c.start,
                           f"RSI {min(before):.0f} -> {now:.0f}, closed above the prior high {p.high:g}", vx)
            if setup == "rsi_fade" and max(before) > 70 >= now and c.close < p.low:
                return Hit(product_id, setup, "triggered", "short", last.close, p.low, c.start,
                           f"RSI {max(before):.0f} -> {now:.0f}, closed below the prior low {p.low:g}", vx)
        return none

    raise ValueError(f"unknown setup {setup}; choose from {', '.join(SETUPS)}")


def scan(products: list[str], setups: tuple[str, ...] = SETUPS, workers: int = 6, fresh: int = 2,
         range_minutes: int = 60) -> list[Hit]:
    """Latest day and a bit of 5-minute candles for each product; every setup that is triggered now."""
    def one(pid: str) -> list[Hit]:
        for attempt in range(3):
            try:
                candles = bars(pid, 1.25)
                break
            except Exception:
                time.sleep(1 + 2 * attempt)
        else:
            return []
        if len(candles) < 60:
            return []
        return [h for s in setups if (h := check(pid, candles, s, fresh, range_minutes)).status == "triggered"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [h for hits in pool.map(one, products) for h in hits]


def report(hits: list[Hit], scanned: int) -> str:
    lines = [f"{scanned} coins scanned on 5-minute candles; {len(hits)} setups triggered on the last closed candles.",
             "Descriptive only: a triggered setup is a pattern, not a trade.", ""]
    for setup in SETUPS:
        mine = sorted((h for h in hits if h.setup == setup), key=lambda h: -h.volume_x)
        if not mine:
            continue
        lines.append(f"{setup} ({len(mine)})")
        for h in mine:
            lines.append(f"  {h.product_id:<14} {h.side:<5} {h.price:<12g} vol {h.volume_x:4.1f}x  {h.detail}")
        lines.append("")
    return "\n".join(lines).rstrip()


def describe(h: Hit) -> str:
    side = f" {h.side.upper()}" if h.side else ""
    return f"{h.product_id} {h.setup}: {h.status.upper()}{side} at {h.price:g}. {h.detail}".rstrip()
