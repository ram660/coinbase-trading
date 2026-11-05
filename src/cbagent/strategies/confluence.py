"""Confluence scoring: five indicators each vote -1, 0 or +1, and trades need several to agree.

Idea taken from Byte-Ventures/claude-trader (no code copied). Votes:
  RSI        oversold +1, overbought -1
  MACD       line above signal +1, below -1
  Bollinger  close under the lower band +1, over the upper band -1
  EMA trend  fast EMA above slow EMA +1, below -1
  Volume     on a volume spike, votes the direction of the last candle
"""
from __future__ import annotations

from decimal import Decimal

from ..indicators import bollinger, ema, macd, rsi
from ..models import Candle, Signal
from .base import Strategy


class Confluence(Strategy):
    name = "confluence"

    def __init__(
        self,
        buy_score: int = 3,
        sell_score: int = -2,
        rsi_period: int = 14,
        rsi_low: float = 30,
        rsi_high: float = 70,
        ema_fast: int = 12,
        ema_slow: int = 50,
        bb_period: int = 20,
        volume_spike: float = 1.5,
        **kw,
    ):
        super().__init__(**kw)
        self.buy_score, self.sell_score = buy_score, sell_score
        self.rsi_period, self.rsi_low, self.rsi_high = rsi_period, rsi_low, rsi_high
        self.ema_fast, self.ema_slow = ema_fast, ema_slow
        self.bb_period, self.volume_spike = bb_period, volume_spike

    @property
    def warmup(self) -> int:
        return max(self.ema_slow, 26 + 9, self.bb_period, self.rsi_period * 3) + 1

    def votes(self, candles: list[Candle]) -> dict[str, int]:
        closes = [c.close for c in candles]
        last = candles[-1]
        r = rsi(closes, self.rsi_period)
        line, sig = macd(closes)
        lower, _mid, upper = bollinger(closes, self.bb_period)
        avg_vol = sum(c.volume for c in candles[-21:-1]) / 20
        spike = avg_vol > 0 and last.volume > avg_vol * self.volume_spike
        return {
            "rsi": 1 if r < self.rsi_low else -1 if r > self.rsi_high else 0,
            "macd": 1 if line > sig else -1,
            "bollinger": 1 if last.close < lower else -1 if last.close > upper else 0,
            "ema": 1 if ema(closes, self.ema_fast) > ema(closes, self.ema_slow) else -1,
            "volume": (1 if last.close > last.open else -1) if spike else 0,
        }

    def on_candles(self, product_id: str, candles: list[Candle], position_base: Decimal) -> Signal:
        if len(candles) < self.warmup:
            return self.hold("warming up")
        votes = self.votes(candles)
        score = sum(votes.values())
        why = f"score {score:+d} ({', '.join(f'{k} {v:+d}' for k, v in votes.items())})"
        if score >= self.buy_score and position_base == 0:
            return Signal("buy", why, quote_size=self.order_quote)
        if score <= self.sell_score and position_base > 0:
            return Signal("sell", why)
        return self.hold(why)
