"""Technical indicators on plain lists of floats, oldest first. Each returns the latest value only."""
from __future__ import annotations

from .models import Candle


def sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


def ema_series(values: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: list[float], n: int) -> float:
    return ema_series(values, n)[-1]


def rsi(values: list[float], n: int = 14) -> float:
    """Wilder's RSI, 0-100."""
    gains, losses = [], []
    for prev, cur in zip(values, values[1:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain, avg_loss = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float]:
    """Return (macd line, signal line)."""
    fast_s, slow_s = ema_series(values, fast), ema_series(values, slow)
    line = [f - s for f, s in zip(fast_s[slow - fast:], slow_s)]
    return line[-1], ema(line, signal)


def bollinger(values: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float]:
    """Return (lower, middle, upper)."""
    window = values[-n:]
    mid = sum(window) / n
    sd = (sum((v - mid) ** 2 for v in window) / n) ** 0.5
    return mid - k * sd, mid, mid + k * sd


def atr(candles: list[Candle], n: int = 14) -> float:
    """Wilder's average true range."""
    trs = [
        max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        for p, c in zip(candles, candles[1:])
    ]
    value = sum(trs[:n]) / n
    for tr in trs[n:]:
        value = (value * (n - 1) + tr) / n
    return value
