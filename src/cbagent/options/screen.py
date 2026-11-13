"""Options-idea screen: a stated, mechanical trend rule on stocks, ETFs and crypto, for options Ram places by hand.

Rule (the same trend rule as CLAUDE.md rule 4, applied to options):
  * Averages are of closed daily candles: the 50-day and the 200-day.
  * Bullish (calls) when the live price is above both. Bearish (puts) when it is below both. Flat otherwise.
  * To stop flip-flopping, a flat name only turns bullish or bearish once the price clears the far average by
    `buffer_pct`; it stays bullish (or bearish) until the price is back across either average.
  * Ideas are ranked by 20-day average dollar volume, so the most liquid option chains come first.

Each idea carries the numbers behind it: the averages, RSI, 20-day realized volatility and the move that volatility
implies over 30 days (compare it with the option's implied volatility before paying for it), and the level that
ends the idea. This module only reads public prices and writes text; it never places, previews or sizes an order.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..indicators import rsi, sma
from ..models import Candle

BULL, BEAR, FLAT = "bull", "bear", "flat"
WORD = {BULL: "CALLS", BEAR: "PUTS", FLAT: "flat"}

DEFAULTS = {
    "stocks": ["IBIT", "ETHA", "COIN", "MSTR", "MARA", "HOOD", "SPY", "QQQ", "IWM",
               "NVDA", "TSLA", "AAPL", "AMD", "META", "AMZN", "MSFT", "GOOGL", "PLTR"],
    "crypto": ["BTC-USD", "ETH-USD", "SOL-USD"],
    # Crypto has no options on Wealthsimple; these are the listed stand-ins whose options track it.
    "proxies": {"BTC-USD": ["IBIT", "MSTR", "COIN"], "ETH-USD": ["ETHA", "COIN"], "SOL-USD": ["COIN"]},
    "buffer_pct": 1.0,
    "min_dollar_volume": 50_000_000,
    "digest_hour_utc": 14,   # 6 am Vancouver in winter, before the 9:30 New York open
}


@dataclass
class Reading:
    symbol: str
    kind: str                 # "stock" or "crypto"
    price: float
    sma50: float
    sma200: float
    rsi14: float
    vol20: float              # annualized realized volatility, percent
    dollar_volume: float      # 20-day average, USD
    regime: str = FLAT
    proxies: list[str] = field(default_factory=list)

    @property
    def move30(self) -> float:
        """One standard deviation of price over 30 calendar days, from realized volatility."""
        days = 365 if self.kind == "crypto" else 252
        trading = 30 if self.kind == "crypto" else 21
        return self.price * self.vol20 / 100 * math.sqrt(trading / days)

    @property
    def ends_at(self) -> float:
        """Calls end below the higher average, puts above the lower one."""
        return max(self.sma50, self.sma200) if self.regime == BULL else min(self.sma50, self.sma200)


def realized_vol(closes: list[float], n: int = 20, per_year: int = 252) -> float:
    rets = [math.log(b / a) for a, b in zip(closes[-n - 1:], closes[-n:])]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    return sd * math.sqrt(per_year) * 100


def regime(price: float, s50: float, s200: float, prior: str = FLAT, buffer_pct: float = 1.0) -> str:
    hi, lo = max(s50, s200), min(s50, s200)
    if prior == BULL and price > hi:
        return BULL
    if prior == BEAR and price < lo:
        return BEAR
    if price > hi * (1 + buffer_pct / 100):
        return BULL
    if price < lo * (1 - buffer_pct / 100):
        return BEAR
    return FLAT


def read(symbol: str, kind: str, candles: list[Candle], price: float, prior: str = FLAT,
         buffer_pct: float = 1.0, proxies: list[str] | None = None) -> Reading:
    if len(candles) < 201:
        raise ValueError(f"{symbol}: only {len(candles)} daily candles, need 201")
    closes = [c.close for c in candles]
    r = Reading(
        symbol=symbol, kind=kind, price=price, sma50=sma(closes, 50), sma200=sma(closes, 200), rsi14=rsi(closes, 14),
        vol20=realized_vol(closes, 20, 365 if kind == "crypto" else 252),
        dollar_volume=sum(c.close * c.volume for c in candles[-20:]) / 20,
        proxies=list(proxies or []),
    )
    r.regime = regime(price, r.sma50, r.sma200, prior, buffer_pct)
    return r


def _money(x: float) -> str:
    if x >= 1e9:
        return f"${x / 1e9:.1f}B"
    if x >= 1e6:
        return f"${x / 1e6:.0f}M"
    return f"${x:,.0f}"


def _px(x: float) -> str:
    return f"{x:,.2f}" if x < 1000 else f"{x:,.0f}"


def describe(r: Reading) -> str:
    """One idea, plain text, with the numbers that produced it."""
    d50, d200 = (r.price / r.sma50 - 1) * 100, (r.price / r.sma200 - 1) * 100
    head = f"{r.symbol} {_px(r.price)}: {WORD[r.regime]}"
    lines = [head,
             f"  50d {_px(r.sma50)} ({d50:+.1f}%), 200d {_px(r.sma200)} ({d200:+.1f}%), RSI {r.rsi14:.0f}",
             f"  realized vol {r.vol20:.0f}%, 30-day 1-sigma move about ±{_px(r.move30)}, avg $vol {_money(r.dollar_volume)}"]
    if r.regime in (BULL, BEAR):
        side = "below" if r.regime == BULL else "above"
        lines.append(f"  idea ends if the price goes back {side} {_px(r.ends_at)}")
        if r.regime == BULL and r.rsi14 >= 75 or r.regime == BEAR and r.rsi14 <= 25:
            lines.append("  stretched (RSI): chasing here often buys the top or bottom of the move")
    if r.proxies:
        lines.append(f"  options via {', '.join(r.proxies)}")
    return "\n".join(lines)


STRUCTURE = ("How the rule plays it (long options, so the premium is the most you can lose): 30 to 45 days to "
             "expiry, strike at the money or one strike in the money. If the chain's implied volatility is well "
             "above the realized vol shown, a debit spread with the short strike near the 1-sigma move costs less. "
             "Exit when the idea ends or at half the premium lost, whichever comes first.")


def brief_line(r: Reading) -> str:
    """Two short lines per idea for the digest, which is read on a phone."""
    d50, d200 = (r.price / r.sma50 - 1) * 100, (r.price / r.sma200 - 1) * 100
    edge = "<" if r.regime == BULL else ">"
    flag = "  stretched" if (r.regime == BULL and r.rsi14 >= 75 or r.regime == BEAR and r.rsi14 <= 25) else ""
    via = f"  via {'/'.join(r.proxies)}" if r.proxies else ""
    return (f"{r.symbol} {_px(r.price)}  50d {d50:+.0f}% 200d {d200:+.0f}%  RSI {r.rsi14:.0f}{flag}\n"
            f"  30d move ±{_px(r.move30)} (vol {r.vol20:.0f}%), ends {edge}{_px(r.ends_at)}{via}")


def digest(readings: list[Reading], errors: list[str], min_dollar_volume: float, now: int | None = None) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now or time.time()))
    liquid = lambda r: r.kind == "crypto" or r.dollar_volume >= min_dollar_volume
    calls = sorted([r for r in readings if r.regime == BULL and liquid(r)], key=lambda r: -r.dollar_volume)
    puts = sorted([r for r in readings if r.regime == BEAR and liquid(r)], key=lambda r: -r.dollar_volume)
    flat = [r.symbol for r in readings if r.regime == FLAT]
    out = [f"Options ideas {stamp}", "Rule: live price vs 50- and 200-day averages of closed daily candles. "
           "Above both = calls, below both = puts. Most liquid first. Alerts only, no orders."]
    for title, group in (("CALLS", calls), ("PUTS", puts)):
        out.append(f"\n{title} ({len(group)})")
        out += [brief_line(r) for r in group] or ["  none"]
    out.append(f"\nFlat, no idea: {', '.join(flat) or 'none'}")
    if errors:
        out.append("No data: " + "; ".join(errors))
    out.append("\n" + STRUCTURE)
    return "\n".join(out)


def change_alert(r: Reading, before: str) -> str:
    if r.regime == FLAT:
        what = f"{r.symbol}: {WORD[before]} idea ended, now flat"
    else:
        what = f"{r.symbol}: new {WORD[r.regime]} idea (was {WORD[before]})"
    return f"{what}\n{describe(r)}"


# ---- state and the scheduled run -----------------------------------------------------------------------------

def _state_path(data_dir: Path) -> Path:
    out = data_dir / "options"
    out.mkdir(parents=True, exist_ok=True)
    return out / "state.json"


def load_state(data_dir: Path) -> dict:
    try:
        return json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(data_dir: Path, state: dict) -> None:
    _state_path(data_dir).write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


Fetcher = Callable[[str, str], tuple[list[Candle], float]]


def default_fetch(symbol: str, kind: str) -> tuple[list[Candle], float]:
    if kind == "crypto":
        from ..market_data import fetch_candles, fetch_price

        return fetch_candles(symbol, "ONE_DAY", 260), fetch_price(symbol)
    from .stock_data import fetch

    candles, price, _ = fetch(symbol)
    return candles, price


def scan(settings: dict, priors: dict[str, str] | None = None, fetch: Fetcher = default_fetch
         ) -> tuple[list[Reading], list[str]]:
    s = {**DEFAULTS, **settings}
    priors = priors or {}
    readings, errors = [], []
    names = [(sym, "crypto") for sym in s["crypto"]] + [(sym, "stock") for sym in s["stocks"]]
    for sym, kind in names:
        try:
            candles, price = fetch(sym, kind)
            readings.append(read(sym, kind, candles, price, priors.get(sym, FLAT), float(s["buffer_pct"]),
                                 s["proxies"].get(sym)))
        except Exception as e:  # one bad symbol shouldn't stop the rest
            errors.append(f"{sym} ({str(e)[:80]})")
    return readings, errors


def watch(data_dir: Path, settings: dict, fetch: Fetcher = default_fetch, now: int | None = None,
          force_digest: bool = False) -> list[str]:
    """One scheduled run: return the messages to send (trend changes, then the digest once a day) and save state.

    The first run only records where each name stands, so it doesn't announce every existing trend as new.
    """
    s = {**DEFAULTS, **settings}
    now = now or int(time.time())
    state = load_state(data_dir)
    priors: dict[str, str] = state.get("regimes", {})
    readings, errors = scan(s, priors, fetch)
    messages = []
    if priors:
        for r in readings:
            before = priors.get(r.symbol, FLAT)
            if r.regime != before:
                messages.append(change_alert(r, before))
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    hour = time.gmtime(now).tm_hour
    scheduled = state.get("digest_date") != today and hour >= int(s["digest_hour_utc"])
    if scheduled or force_digest or not priors:  # a forced or first-run digest doesn't replace the morning one
        messages.append(digest(readings, errors, float(s["min_dollar_volume"]), now))
    if scheduled:
        state["digest_date"] = today
    # Keep the prior regime for a symbol whose data failed this run, so a blip doesn't read as a change next time.
    state["regimes"] = {**priors, **{r.symbol: r.regime for r in readings}}
    state["last_run"] = now
    save_state(data_dir, state)
    return messages
