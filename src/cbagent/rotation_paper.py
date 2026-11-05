"""Paper-trading momentum rotation books. Simulated money only; never calls a Coinbase account.

Each run marks the book to market and, when a rebalance is due, holds the top N liquid coins by risk-adjusted
momentum in equal parts (cash otherwise). State lives in data/rotation_<book>.json, trades in
data/journal_rotation.jsonl, so a scheduled task can call `cbagent rotate` every hour.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .market_data import fetch_candles
from .scanner import list_products


@dataclass(frozen=True)
class Book:
    name: str
    granularity: str
    top_n: int
    lookback: int          # bars of momentum
    vol_bars: int          # bars of volatility for the risk adjustment
    trend_bars: int        # a coin must close above its average over this many bars
    btc_filter_bars: int   # 0 = no filter; else only hold coins while BTC is above this many-bar average
    rebalance_hours: int
    description: str
    starting_cash: float = 1000.0
    fee_pct: float = 0.5
    slippage_pct: float = 0.05
    min_trade_pct: float = 2.0  # skip adjustments smaller than this share of equity, to save fees


BOOKS = {
    "daily": Book("daily", "ONE_DAY", top_n=3, lookback=30, vol_bars=30, trend_bars=50, btc_filter_bars=200,
                  rebalance_hours=24, description="Tested: top 3 by 30-day momentum, daily, only while BTC > 200-day average"),
    "fast": Book("fast", "ONE_HOUR", top_n=3, lookback=72, vol_bars=48, trend_bars=50, btc_filter_bars=200,
                 rebalance_hours=4, description="Aggressive, untested: top 3 by 3-day momentum on hourly data, every 4 hours"),
}


def score(closes: list[float], book: Book) -> float | None:
    need = max(book.lookback, book.trend_bars, book.vol_bars) + 1
    if len(closes) < need:
        return None
    rets = [b / a - 1 for a, b in zip(closes[-book.vol_bars - 1:-1], closes[-book.vol_bars:])]
    vol = statistics.pstdev(rets)
    if vol <= 0 or closes[-1] < statistics.fmean(closes[-book.trend_bars:]):
        return None
    momentum = closes[-1] / closes[-1 - book.lookback] - 1
    return momentum / (vol * math.sqrt(book.lookback)) if momentum > 0 else None


def _closes(pid: str, book: Book) -> list[float]:
    bars = max(book.lookback, book.trend_bars, book.vol_bars, book.btc_filter_bars) + 5
    for attempt in range(4):
        try:
            return [c.close for c in fetch_candles(pid, book.granularity, bars)]
        except Exception:
            time.sleep(2 ** attempt)
    return []


def pick(book: Book, quote: str = "USDC") -> tuple[dict[str, float], dict[str, float], str]:
    """Return (latest prices, target weights, note) for the book right now."""
    ids = sorted({p["product_id"] for p in list_products(quote, 1_000_000)} | {f"BTC-{quote}"})
    with ThreadPoolExecutor(max_workers=3) as pool:
        series = dict(zip(ids, pool.map(lambda pid: _closes(pid, book), ids)))
    prices = {p: s[-1] for p, s in series.items() if s}
    btc = series.get(f"BTC-{quote}", [])
    if book.btc_filter_bars:
        if len(btc) < book.btc_filter_bars:
            return prices, {}, "no BTC history: holding cash"
        if btc[-1] <= statistics.fmean(btc[-book.btc_filter_bars:]):
            return prices, {}, f"BTC below its {book.btc_filter_bars}-bar average: holding cash"
    scored = sorted(((s, p) for p, closes in series.items() if (s := score(closes, book)) is not None), reverse=True)
    picks = [p for _, p in scored[: book.top_n]]
    note = "picks: " + (", ".join(f"{p} ({s:.2f})" for s, p in scored[: book.top_n]) or "none qualified, holding cash")
    return prices, {p: 1 / book.top_n for p in picks}, note


class PaperRotation:
    def __init__(self, book: Book, data_dir: Path):
        self.book = book
        self.path = data_dir / f"rotation_{book.name}.json"
        self.journal = data_dir / "journal_rotation.jsonl"
        self.state = {"cash": book.starting_cash, "holdings": {}, "last_rebalance": 0, "equity": [], "fees": 0.0}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))

    def equity(self, prices: dict[str, float]) -> float:
        return self.state["cash"] + sum(q * prices.get(p, 0.0) for p, q in self.state["holdings"].items())

    def _log(self, **data) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "book": self.book.name, **data}) + "\n")

    def log_error(self, error: Exception) -> None:
        self._log(event="error", error=f"{type(error).__name__}: {error}"[:300])

    def _trade(self, pid: str, value: float, price: float) -> None:
        """Buy (value > 0) or sell (value < 0) about `value` of quote at market, with fee and slippage."""
        fee_rate, slip = self.book.fee_pct / 100, self.book.slippage_pct / 100
        held = self.state["holdings"].get(pid, 0.0)
        if value > 0:
            fill = price * (1 + slip)
            qty = value * (1 - fee_rate) / fill
            self.state["cash"] -= value
            self.state["holdings"][pid] = held + qty
            fee = value * fee_rate
        else:
            fill = price * (1 - slip)
            qty = min(held, -value / price)
            proceeds = qty * fill
            fee = proceeds * fee_rate
            self.state["cash"] += proceeds - fee
            self.state["holdings"][pid] = held - qty
            if self.state["holdings"][pid] * price < 0.01:
                del self.state["holdings"][pid]
        self.state["fees"] += fee
        self._log(event="fill", product=pid, side="BUY" if value > 0 else "SELL", qty=qty, price=fill, fee=fee)

    def run(self, now: float | None = None, force: bool = False) -> str:
        now = now or time.time()
        due = force or now - self.state["last_rebalance"] >= self.book.rebalance_hours * 3600 - 300
        prices, target, note = pick(self.book) if due else (self._prices(), None, "not due")
        equity = self.equity(prices)
        lines = []
        if target is not None:
            for p in list(self.state["holdings"]):  # sells first so cash is there for buys
                gap = target.get(p, 0) * equity - self.state["holdings"][p] * prices.get(p, 0)
                if p not in target or gap < -equity * self.book.min_trade_pct / 100:
                    self._trade(p, gap if p in target else -self.state["holdings"][p] * prices[p] * 1.01, prices[p])
            for p, w in target.items():
                gap = w * equity - self.state["holdings"].get(p, 0) * prices[p]
                if gap > equity * self.book.min_trade_pct / 100:
                    self._trade(p, min(gap, self.state["cash"]), prices[p])
            self.state["last_rebalance"] = now
            self._log(event="rebalance", note=note, equity=self.equity(prices))
            lines.append(f"rebalanced: {note}")
        equity = self.equity(prices)
        self.state["equity"].append([int(now), round(equity, 2)])
        self.state["prices"] = {p: prices[p] for p in self.state["holdings"] if p in prices}  # marks, for the dashboard
        self.state["last_run"] = int(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        return "\n".join([self.status(prices)] + lines)

    def _prices(self) -> dict[str, float]:
        out = {}
        for p in self.state["holdings"]:
            closes = fetch_candles(p, self.book.granularity, 2)
            if closes:
                out[p] = closes[-1].close
        return out

    def status(self, prices: dict[str, float] | None = None) -> str:
        prices = prices if prices is not None else self._prices()
        equity = self.equity(prices)
        start = self.book.starting_cash
        held = ", ".join(f"{p.split('-')[0]} {q * prices.get(p, 0):.0f}" for p, q in self.state["holdings"].items()) or "all cash"
        return (f"[{self.book.name}] equity {equity:.2f} ({(equity / start - 1) * 100:+.2f}% since start), "
                f"fees {self.state['fees']:.2f}, holding: {held}")
