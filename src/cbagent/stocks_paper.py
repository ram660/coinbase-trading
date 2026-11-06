"""Paper-trading book for stocks and ETFs that Coinbase lists. Simulated money only; never calls a Coinbase account.

The book uses the same trend rule as Ram's crypto trading: a stock qualifies while its live price is above both its
50- and 200-day averages of closed daily closes. It holds up to `top_n` qualifiers in equal parts. A holding keeps
its place while it still qualifies; empty places go to the qualifiers with the highest 20-day average dollar volume.
While SPY is below its own 200-day average the book holds cash. It is long or cash only: Coinbase's stock pairs
can't be shorted.

Coinbase's public market API doesn't serve its stock pairs, so prices come from Yahoo Finance's public chart
endpoint (no key). The book only trades during the US regular session; outside it, runs just mark it to market.

State lives in data/rotation_stocks.json and trades in data/journal_rotation.jsonl next to the crypto books, so
`cbagent due`, `cbagent rotate` and the dashboard pick it up. It also tracks SPY bought and held from the book's
first run, because a stock book is only worth running if it beats simply holding the index.
"""
from __future__ import annotations

import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .rotation_paper import PaperRotation

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d"

# Liquid stocks and broad ETFs that Coinbase lists as <SYMBOL>-USDC (checked with the Coinbase connector,
# 2026-11-06). Leveraged and inverse ETFs, bond funds and thinly traded names are left out on purpose.
UNIVERSE = (
    "SPY", "QQQ", "NVDA", "TSLA", "GOOG", "AVGO", "TSM", "ASML", "AMAT", "MU", "CSCO", "IBM", "CRM", "ADBE",
    "PANW", "MA", "WMT", "COST", "HD", "PG", "KO", "JNJ", "MRK", "XOM", "CVX", "DIS", "BA",
)
BENCHMARK = "SPY"


@dataclass(frozen=True)
class StockBook:
    name: str = "stocks"
    universe: tuple[str, ...] = UNIVERSE
    top_n: int = 5
    fast: int = 50                 # daily bars
    slow: int = 200
    volume_bars: int = 20          # average dollar volume over this many days, to rank qualifiers
    spy_filter: bool = True        # hold nothing while SPY is below its 200-day average (cut drawdowns in both halves
                                   # of the 2017-26 replay, like the BTC filter on the crypto daily book)
    sticky: bool = True            # keep a holding while it still qualifies; only fill empty slots by volume
    drift_pct: float = 10.0        # top a holding back up to equal weight only when it drifts this far (share of equity)
    rebalance_hours: float = 0.25  # checked every 15 minutes while the market is open
    starting_cash: float = 1000.0
    fee_pct: float = 0.1           # assumption: Coinbase's stock fee schedule isn't public yet; check and adjust
    slippage_pct: float = 0.05
    min_trade_pct: float = 2.0     # skip adjustments smaller than this share of equity, to save fees
    granularity: str = "ONE_DAY"
    description: str = ("Coinbase-listed stocks and ETFs: up to 5 names above both their 50- and 200-day averages, "
                        "kept while they qualify, new ones picked by dollar volume, equal parts; cash while SPY is "
                        "below its 200-day average; US market hours only")


BOOK = StockBook()


@dataclass(frozen=True)
class Daily:
    """One symbol's daily history, oldest first, plus Yahoo's live quote and session times."""
    symbol: str
    times: list[int]
    closes: list[float]        # split-adjusted closes, used for the averages
    adj: list[float]           # also dividend-adjusted, used for backtest returns
    volumes: list[float]
    price: float | None = None
    session: tuple[int, int] | None = None  # (start, end) of the current or most recent regular session


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (cbagent paper book)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_daily(symbol: str, range_: str = "2y") -> Daily:
    data = _get(CHART_URL.format(symbol=urllib.parse.quote(symbol), range=range_))["chart"]["result"][0]
    quote = data["indicators"]["quote"][0]
    adj = (data["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote["close"]
    rows = [(t, c, a, v or 0.0) for t, c, a, v in zip(data.get("timestamp", []), quote["close"], adj, quote["volume"])
            if c is not None and a is not None]
    meta = data.get("meta", {})
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    return Daily(symbol, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows],
                 price=meta.get("regularMarketPrice"),
                 session=(int(reg["start"]), int(reg["end"])) if "start" in reg and "end" in reg else None)


def _retry(fn: Callable, *args, tries: int = 4):
    for attempt in range(tries):
        try:
            return fn(*args)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


def market_open(d: Daily, now: float) -> bool:
    return bool(d.session) and d.session[0] <= now < d.session[1]


def closed_bars(d: Daily, now: float) -> int:
    """How many of d's bars are closed days: today's bar is dropped while its session is still running."""
    if d.times and d.session and d.session[0] <= d.times[-1] < d.session[1] + 1 and now < d.session[1]:
        return len(d.times) - 1
    return len(d.times)


def qualifies(price: float, closes: list[float], book: StockBook = BOOK) -> bool:
    """The trend rule: price above both the fast and the slow average of closed daily closes."""
    if len(closes) < book.slow:
        return False
    return price > statistics.fmean(closes[-book.fast:]) and price > statistics.fmean(closes[-book.slow:])


def dollar_volume(closes: list[float], volumes: list[float], bars: int) -> float:
    pairs = list(zip(closes[-bars:], volumes[-bars:]))
    return statistics.fmean(c * v for c, v in pairs) if pairs else 0.0


def choose(rows: dict[str, tuple[float, list[float], list[float]]], book: StockBook = BOOK,
           benchmark: tuple[float, list[float]] | None = None,
           held: set[str] | frozenset[str] = frozenset()) -> tuple[dict[str, float], list[str]]:
    """Target weights from {symbol: (price, closed closes, closed volumes)}; also returns the ranked qualifiers.
    With `sticky`, names in `held` that still qualify keep their place ahead of new ones."""
    if book.spy_filter and benchmark:
        price, closes = benchmark
        if len(closes) < book.slow or price <= statistics.fmean(closes[-book.slow:]):
            return {}, []
    ranked = sorted((s for s, (px, c, _) in rows.items() if qualifies(px, c, book)),
                    key=lambda s: -dollar_volume(rows[s][1], rows[s][2], book.volume_bars))
    if book.sticky:
        ranked = [x for x in ranked if x in held] + [x for x in ranked if x not in held]
    picks = ranked[: book.top_n]
    return {s: 1 / book.top_n for s in picks}, ranked


class PaperStocks(PaperRotation):
    def __init__(self, data_dir: Path, book: StockBook = BOOK, fetch: Callable[[str], Daily] | None = None,
                 clock: Callable[[], float] | None = None):
        super().__init__(book, Path(data_dir))
        self.fetch = fetch or (lambda s: _retry(fetch_daily, s))
        self.clock = clock or time.time

    def _load(self, symbols) -> dict[str, Daily]:
        with ThreadPoolExecutor(max_workers=3) as pool:
            got = list(pool.map(lambda s: self._safe(s), symbols))
        return {d.symbol: d for d in got if d and d.closes}

    def _safe(self, symbol: str) -> Daily | None:
        try:
            return self.fetch(symbol)
        except Exception:
            return None

    def _mark(self, d: Daily) -> float:
        return float(d.price) if d.price else d.closes[-1]

    def _benchmark(self, prices: dict[str, float]) -> None:
        px = prices.get(BENCHMARK)
        if px and "benchmark" not in self.state:
            self.state["benchmark"] = {"symbol": BENCHMARK, "start_price": px, "start": int(self.clock())}
        if px and "benchmark" in self.state:
            self.state["benchmark"]["price"] = px

    def _pick(self):
        now = self.clock()
        data = self._load(sorted(set(self.book.universe) | set(self.state["holdings"]) | {BENCHMARK}))
        prices = {s: self._mark(d) for s, d in data.items()}
        self._benchmark(prices)
        missing = [s for s in self.state["holdings"] if s not in prices]
        if missing:
            return prices, None, f"no price for {', '.join(missing)}: not trading this run"
        spy = data.get(BENCHMARK)
        if not spy or not market_open(spy, now):
            return prices, None, "US market closed: marked only"
        rows = {}
        for s, d in data.items():
            if s in self.book.universe:
                n = closed_bars(d, now)
                rows[s] = (prices[s], d.closes[:n], d.volumes[:n])
        n = closed_bars(spy, now)
        target, ranked = choose(rows, self.book, (prices[BENCHMARK], spy.closes[:n]), set(self.state["holdings"]))
        note = "picks: " + (", ".join(target) or "none qualified, holding cash")
        if len(ranked) > len(target):
            note += f" ({len(ranked)} qualified)"
        return prices, target, note

    def _prices(self) -> dict[str, float]:
        data = self._load(sorted(set(self.state["holdings"]) | {BENCHMARK}))
        prices = {s: self._mark(d) for s, d in data.items()}
        self._benchmark(prices)
        return prices

    def run(self, now: float | None = None, force: bool = False) -> str:
        return super().run(now=now or self.clock(), force=force)

    def status(self, prices: dict[str, float] | None = None) -> str:
        line = super().status(prices)
        b = self.state.get("benchmark")
        if b and b.get("price"):
            line += f"; SPY held since start {(b['price'] / b['start_price'] - 1) * 100:+.2f}%"
        return line


# ---- backtest

def load_history(book: StockBook = BOOK, years: int = 10) -> dict[str, Daily]:
    symbols = sorted(set(book.universe) | {BENCHMARK})
    with ThreadPoolExecutor(max_workers=3) as pool:
        got = list(pool.map(lambda s: _retry(fetch_daily, s, f"{years}y"), symbols))
    return {d.symbol: d for d in got}


def backtest(history: dict[str, Daily], book: StockBook = BOOK) -> dict:
    """Replay the book on daily closes: decide on day t's close, trade at day t+1's close (no lookahead).
    Returns CAGR, max drawdown, Sharpe, trades and fees, next to SPY bought and held over the same days."""
    spy = history[BENCHMARK]
    days = spy.times
    index = {s: {t: i for i, t in enumerate(d.times)} for s, d in history.items()}
    fee = book.fee_pct / 100 + book.slippage_pct / 100
    cash, held = book.starting_cash, {}   # symbol -> dollar value
    curve, trades, fees, target = [], 0, 0.0, {}
    start = book.slow
    for k in range(start, len(days)):
        t = days[k]
        # mark: grow each holding by its adjusted return since yesterday
        for s in list(held):
            i = index[s].get(t)
            if i and i > 0:
                held[s] *= history[s].adj[i] / history[s].adj[i - 1]
        equity = cash + sum(held.values())
        # trade yesterday's decision at today's close
        gaps = {s: (target.get(s, 0.0) * equity - held.get(s, 0.0)) if s in target else -held[s]
                for s in set(held) | set(target)}
        for s, gap in sorted(gaps.items(), key=lambda kv: kv[1]):  # sells first, so cash is there for buys
            if s in target and abs(gap) < equity * max(book.min_trade_pct, book.drift_pct if s in held else 0) / 100:
                continue
            if gap > 0:
                gap = min(gap, max(cash, 0.0) / (1 + fee))
                if gap <= 0:
                    continue
            cost = abs(gap) * fee
            fees += cost
            trades += 1
            held[s] = held.get(s, 0.0) + gap
            cash -= gap + cost
            if held[s] < 0.01:
                held.pop(s)
        equity = cash + sum(held.values())
        curve.append((t, equity))
        # decide on today's close
        rows = {}
        for s in book.universe:
            i = index.get(s, {}).get(t)
            if i is not None and i >= book.slow:
                d = history[s]
                rows[s] = (d.closes[i], d.closes[: i + 1], d.volumes[: i + 1])
        target, _ = choose(rows, book, (spy.closes[k], spy.closes[: k + 1]), set(held))
    spy_curve = [(days[k], book.starting_cash * spy.adj[k] / spy.adj[start]) for k in range(start, len(days))]
    return {"book": _stats(curve), "spy": _stats(spy_curve), "trades": trades, "fees": fees,
            "from": days[start], "to": days[-1]}


def _stats(curve: list[tuple[int, float]]) -> dict:
    values = [v for _, v in curve]
    years = (curve[-1][0] - curve[0][0]) / (365.25 * 86400) or 1
    rets = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    return {"end": values[-1], "cagr_pct": ((values[-1] / values[0]) ** (1 / years) - 1) * 100,
            "max_dd_pct": mdd * 100, "sharpe": statistics.fmean(rets) / sd * math.sqrt(252) if sd else 0.0}


def backtest_report(history: dict[str, Daily], variants: dict[str, StockBook] | None = None) -> str:
    from dataclasses import replace
    variants = variants or {
        "top 5 + SPY filter (the book)": BOOK,
        "top 5, no SPY filter": replace(BOOK, spy_filter=False),
        "top 5, re-ranked daily": replace(BOOK, sticky=False, drift_pct=0),
        "top 3": replace(BOOK, top_n=3),
        "top 10": replace(BOOK, top_n=10),
        "without NVDA, TSLA, AVGO": replace(BOOK, universe=tuple(s for s in BOOK.universe
                                                                   if s not in ("NVDA", "TSLA", "AVGO"))),
    }
    out, last = [], None
    for label, book in variants.items():
        r = last = backtest(history, book)
        b = r["book"]
        out.append(f"| {label} | {b['cagr_pct']:.1f}% | {b['max_dd_pct']:.1f}% | {b['sharpe']:.2f} | "
                   f"{r['trades']} | ${r['fees']:.0f} | ${b['end']:,.0f} |")
    s = last["spy"]
    frm = datetime.fromtimestamp(last["from"], timezone.utc).date()
    to = datetime.fromtimestamp(last["to"], timezone.utc).date()
    head = [f"Stock book backtest, {frm} to {to}, $1,000 start, fees+slippage {BOOK.fee_pct + BOOK.slippage_pct:.2f}% a trade",
            "", "| variant | CAGR | max drawdown | Sharpe | trades | fees | end value |", "|---|---|---|---|---|---|---|",
            f"| SPY bought and held | {s['cagr_pct']:.1f}% | {s['max_dd_pct']:.1f}% | {s['sharpe']:.2f} | 1 | $0 | ${s['end']:,.0f} |"]
    tail = ["", "Caveat: the universe is today's list of large, liquid names, so the past looks better than it was "
                "(survivorship bias). Treat a small edge over SPY as no edge."]
    return "\n".join(head + out + tail)
