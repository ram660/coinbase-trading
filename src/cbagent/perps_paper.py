"""Paper-trading book for Coinbase perpetual futures. Simulated money only; never calls a Coinbase account.

The book follows the trend on BTC and ETH perps: long when the daily close is above both its 50- and 200-day
averages, short when it is below both, flat otherwise. Positions are sized by volatility and capped at 2x leverage
in total, in whole contracts of Coinbase's nano perps (0.01 BTC and 0.1 ETH).

Each hourly run:
  1. charges funding for the hours since the last run. Coinbase's public API doesn't publish the funding rate of these
     perps, so it is estimated from the perp's premium over spot (premium / 24 per hour, capped), which is how
     perp funding is set. Longs pay when the perp trades above spot, shorts when below.
  2. checks liquidation against the worst hourly price since the last run (low for longs, high for shorts). If equity
     there falls below the maintenance margin, every position is closed at that price with a penalty.
  3. stops the book (flat, for good) if equity falls max_drawdown_pct from its peak.
  4. rebalances to the trend targets once a day, unless data/KILL_SWITCH exists.

Fees are 0.02% of notional plus $0.11 per contract (Coinbase's introductory perps fee in Canada, Sept 2026). The book
also tracks what fees would have been at stress_fee_pct, to see whether the result survives the end of that promo.

State lives in data/perps_<book>.json and trades go to data/journal_rotation.jsonl next to the spot books, so the
dashboard and `cbagent due` pick them up. `backtest()` replays the same rules over years of daily spot closes.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .market_data import fetch_candles
from .models import Candle

HOUR = 3600


@dataclass(frozen=True)
class Market:
    asset: str
    perp: str            # Coinbase Financial Markets nano perp, USD margined
    spot: str            # spot pair for the signal and the funding estimate
    contract_size: float  # base units per contract


MARKETS = {
    "BTC": Market("BTC", "BIP-20DEC30-CDE", "BTC-USD", 0.01),
    "ETH": Market("ETH", "ETP-20DEC30-CDE", "ETH-USD", 0.1),
}


@dataclass(frozen=True)
class PerpBook:
    name: str = "perps"
    assets: tuple[str, ...] = ("BTC", "ETH")
    fast: int = 50                 # daily bars
    slow: int = 200
    vol_bars: int = 30
    target_vol_pct: float = 50.0   # annualised volatility each position is sized to
    max_leverage: float = 2.0      # total notional / equity
    rebalance_hours: int = 24
    starting_cash: float = 1000.0
    fee_pct: float = 0.02
    fee_per_contract: float = 0.11
    stress_fee_pct: float = 0.05
    slippage_pct: float = 0.02
    maintenance_pct: float = 5.0   # of notional
    liquidation_penalty_pct: float = 1.0
    max_drawdown_pct: float = 40.0  # 30% would have stopped it for good in the 2021-26 and 2023-26 replays
    max_funding_hourly_pct: float = 0.1
    max_gap_hours: int = 48        # charge at most this many hours of funding for one gap between runs
    min_trade_pct: float = 5.0     # skip adjustments smaller than this share of equity, to save fees
    description: str = ("BTC and ETH perps: long above the 50- and 200-day averages, short below both, flat otherwise; "
                        "sized by volatility, at most 2x leverage. Backtested on daily data only")


BOOK = PerpBook()


def signal(closes: list[float], book: PerpBook = BOOK) -> int:
    """+1 long, -1 short, 0 flat, from daily closes (oldest first)."""
    if len(closes) < book.slow:
        return 0
    last, fast, slow = closes[-1], statistics.fmean(closes[-book.fast:]), statistics.fmean(closes[-book.slow:])
    if last > fast and last > slow:
        return 1
    if last < fast and last < slow:
        return -1
    return 0


def annual_vol(closes: list[float], bars: int) -> float:
    rets = [b / a - 1 for a, b in zip(closes[-bars - 1:-1], closes[-bars:])]
    return statistics.pstdev(rets) * math.sqrt(365) if len(rets) >= 2 else 0.0


def target_weights(daily: dict[str, list[float]], book: PerpBook = BOOK) -> dict[str, float]:
    """Signed notional as a fraction of equity per asset; total absolute weight never exceeds max_leverage."""
    n = len(book.assets)
    weights = {}
    for a in book.assets:
        closes = daily.get(a, [])
        s = signal(closes, book)
        vol = annual_vol(closes, book.vol_bars)
        if s == 0 or vol <= 0:
            weights[a] = 0.0
            continue
        weights[a] = s * min(book.target_vol_pct / 100 / vol, book.max_leverage) / n
    total = sum(abs(w) for w in weights.values())
    if total > book.max_leverage:
        weights = {a: w * book.max_leverage / total for a, w in weights.items()}
    return weights


def funding_rate_hourly(premium: float, book: PerpBook = BOOK) -> float:
    """Estimated hourly funding rate from the perp's premium over spot; positive means longs pay shorts."""
    cap = book.max_funding_hourly_pct / 100
    return max(-cap, min(cap, premium / 24))


Fetch = Callable[[str, str, int], list[Candle]]


class PaperPerps:
    def __init__(self, data_dir: Path, book: PerpBook = BOOK, fetch: Fetch | None = None):
        self.book = book
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / f"perps_{book.name}.json"
        self.journal = self.data_dir / "journal_rotation.jsonl"
        self.fetch = fetch or (lambda pid, gran, n: _retry(fetch_candles, pid, gran, n))
        self.state = {"cash": book.starting_cash, "positions": {}, "fees": 0.0, "fees_stress": 0.0, "funding": 0.0,
                      "realized": 0.0, "liquidations": 0, "peak": book.starting_cash, "stopped": False,
                      "last_rebalance": 0, "last_run": 0, "equity": []}
        if self.path.exists():
            self.state.update(json.loads(self.path.read_text(encoding="utf-8")))

    # ---- accounting
    def equity(self, prices: dict[str, float]) -> float:
        return self.state["cash"] + sum(self._upnl(a, prices.get(a, p["entry"])) for a, p in self.state["positions"].items())

    def _upnl(self, asset: str, price: float) -> float:
        p = self.state["positions"][asset]
        return p["contracts"] * MARKETS[asset].contract_size * (price - p["entry"])

    def notional(self, prices: dict[str, float]) -> float:
        return sum(abs(p["contracts"]) * MARKETS[a].contract_size * prices.get(a, p["entry"])
                   for a, p in self.state["positions"].items())

    def _log(self, **data) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "book": self.book.name, **data}) + "\n")

    def log_error(self, error: Exception) -> None:
        self._log(event="error", error=f"{type(error).__name__}: {error}"[:300])

    def _trade(self, asset: str, delta: int, price: float, penalty_pct: float = 0.0, reason: str = "") -> None:
        """Buy (delta > 0) or sell (delta < 0) contracts at market, with slippage and fees."""
        if delta == 0:
            return
        m, b = MARKETS[asset], self.book
        fill = price * (1 + b.slippage_pct / 100 * (1 if delta > 0 else -1))
        pos = self.state["positions"].get(asset, {"contracts": 0, "entry": fill})
        held, entry = pos["contracts"], pos["entry"]
        realized = 0.0
        if held and (held > 0) != (delta > 0):  # reducing, closing or flipping
            closed = min(abs(delta), abs(held)) * (1 if held > 0 else -1)
            realized = closed * m.contract_size * (fill - entry)
        new = held + delta
        if new == 0:
            self.state["positions"].pop(asset, None)
        else:
            if held == 0 or (held > 0) != (new > 0):
                entry = fill  # fresh position or flipped: the remainder opens at the fill
            elif abs(new) > abs(held):
                entry = (held * entry + delta * fill) / new
            self.state["positions"][asset] = {"contracts": new, "entry": entry}
        traded = abs(delta) * m.contract_size * fill
        fee = traded * (b.fee_pct + penalty_pct) / 100 + abs(delta) * b.fee_per_contract
        stress = traded * (b.stress_fee_pct + penalty_pct) / 100 + abs(delta) * b.fee_per_contract
        self.state["cash"] += realized - fee
        self.state["realized"] += realized
        self.state["fees"] += fee
        self.state["fees_stress"] += stress
        self._log(event="fill", product=f"{asset}-PERP", side="BUY" if delta > 0 else "SELL",
                  qty=abs(delta) * m.contract_size, contracts=delta, price=fill, fee=fee, realized=realized,
                  reason=reason)

    def _close_all(self, prices: dict[str, float], penalty_pct: float = 0.0, reason: str = "") -> None:
        for a in list(self.state["positions"]):
            self._trade(a, -self.state["positions"][a]["contracts"], prices[a], penalty_pct, reason)

    # ---- market data
    def _hourly(self, hours: int) -> tuple[dict[str, float], dict[str, float], dict[str, list[Candle]]]:
        """Latest perp prices, perp premium over spot, and the perp's hourly bars covering `hours`."""
        prices, premiums, bars = {}, {}, {}
        for a in self.book.assets:
            m = MARKETS[a]
            perp = self.fetch(m.perp, "ONE_HOUR", hours + 1)
            spot = self.fetch(m.spot, "ONE_HOUR", 2)
            if not perp:
                continue
            prices[a], bars[a] = perp[-1].close, perp
            premiums[a] = perp[-1].close / spot[-1].close - 1 if spot else 0.0
        return prices, premiums, bars

    def _daily(self) -> dict[str, list[float]]:
        n = max(self.book.slow, self.book.vol_bars) + 5
        return {a: [c.close for c in self.fetch(MARKETS[a].spot, "ONE_DAY", n)] for a in self.book.assets}

    # ---- one hourly run
    def run(self, now: float | None = None, force: bool = False) -> str:
        now = now or time.time()
        b, st = self.book, self.state
        last = st["last_run"] or now
        gap_h = min(b.max_gap_hours, max(0.0, (now - last) / HOUR))
        prices, premiums, bars = self._hourly(max(2, math.ceil(gap_h)))
        missing = [a for a in st["positions"] if a not in prices]
        if missing:
            raise RuntimeError(f"no perp price for {', '.join(missing)}")
        lines = []

        # 1. funding for the hours since the last run
        for a, p in st["positions"].items():
            pay = p["contracts"] * MARKETS[a].contract_size * prices[a] * funding_rate_hourly(premiums[a], b) * gap_h
            st["cash"] -= pay
            st["funding"] += pay
        st["premiums"] = {a: round(v, 6) for a, v in premiums.items()}

        # 2. liquidation at the worst hourly price since the last run
        if st["positions"]:
            since = last - HOUR
            worst = {}
            for a, p in st["positions"].items():
                window = [c for c in bars[a] if c.start >= since] or bars[a][-1:]
                worst[a] = min(c.low for c in window) if p["contracts"] > 0 else max(c.high for c in window)
            if self.equity(worst) < self.notional(worst) * b.maintenance_pct / 100:
                self._close_all(worst, b.liquidation_penalty_pct, "liquidation")
                st["liquidations"] += 1
                self._log(event="liquidation", equity=self.equity(prices))
                lines.append("LIQUIDATED at the worst price since the last run")

        # 3. drawdown stop
        equity = self.equity(prices)
        st["peak"] = max(st["peak"], equity)
        if not st["stopped"] and equity <= st["peak"] * (1 - b.max_drawdown_pct / 100):
            self._close_all(prices, reason="drawdown stop")
            st["stopped"] = True
            self._log(event="rebalance", note=f"stopped: {b.max_drawdown_pct:.0f}% below peak, flat for good",
                      equity=self.equity(prices))
            lines.append("stopped: drawdown limit hit, book is flat for good")

        # 4. daily rebalance
        due = force or now - st["last_rebalance"] >= b.rebalance_hours * HOUR - 300
        killed = (self.data_dir / "KILL_SWITCH").exists()
        if due and not st["stopped"] and not killed:
            note = self._rebalance(self._daily(), prices)
            st["last_rebalance"] = now
            self._log(event="rebalance", note=note, equity=self.equity(prices))
            lines.append(f"rebalanced: {note}")
        elif due and killed:
            lines.append("kill switch on: not rebalancing")

        equity = self.equity(prices)
        st["peak"] = max(st["peak"], equity)
        st["equity"].append([int(now), round(equity, 2)])
        st["prices"] = prices
        st["last_run"] = int(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        return "\n".join([self.status(prices)] + lines)

    def _rebalance(self, daily: dict[str, list[float]], prices: dict[str, float]) -> str:
        b = self.book
        weights = target_weights(daily, b)
        equity = self.equity(prices)
        parts = []
        # shrinking trades first, so total leverage never overshoots in between
        plans = []
        for a in b.assets:
            if a not in prices:
                continue
            unit = MARKETS[a].contract_size * prices[a]
            want = int(weights.get(a, 0.0) * equity / unit)  # toward zero: never above the cap
            have = self.state["positions"].get(a, {}).get("contracts", 0)
            small = abs(want - have) * unit < equity * b.min_trade_pct / 100 and (want != 0 or have == 0)
            plans.append((abs(want) < abs(have), a, 0 if small else want - have))
            side = "long" if want > 0 else "short" if want < 0 else "flat"
            parts.append(f"{a} {side} {abs(want * unit) / equity:.2f}x" if want else f"{a} flat")
        for _, a, delta in sorted(plans, key=lambda p: not p[0]):
            self._trade(a, delta, prices[a], reason="rebalance")
        return ", ".join(parts)

    def status(self, prices: dict[str, float] | None = None) -> str:
        st = self.state
        prices = prices if prices is not None else st.get("prices", {})
        equity = self.equity(prices)
        start = self.book.starting_cash
        held = ", ".join(
            f"{a} {'long' if p['contracts'] > 0 else 'short'} {abs(p['contracts'])} ct"
            for a, p in st["positions"].items()) or "flat"
        lev = self.notional(prices) / equity if equity > 0 else 0.0
        stress = equity - (st["fees_stress"] - st["fees"])
        return (f"[{self.book.name}] equity {equity:.2f} ({(equity / start - 1) * 100:+.2f}% since start), "
                f"leverage {lev:.2f}x, fees {st['fees']:.2f}, funding paid {st['funding']:.2f}, "
                f"at {self.book.stress_fee_pct}% fees {stress:.2f}, positions: {held}"
                + (", STOPPED" if st["stopped"] else ""))


def _retry(fn, *args):
    for attempt in range(4):
        try:
            return fn(*args)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    return []


# ---- backtest over daily spot history
def backtest(daily: dict[str, list[float]], book: PerpBook = BOOK, funding_apr_pct: float = 10.0,
             fee_pct: float | None = None) -> dict:
    """Replay the book's rules over aligned daily closes (oldest first). Perps track spot here, longs pay
    funding_apr_pct a year and shorts receive it, and liquidation is checked on daily closes only, so intraday
    wicks are missed. Contracts are fractional. Returns summary numbers."""
    fee = book.fee_pct if fee_pct is None else fee_pct
    n = min(len(c) for c in daily.values())
    daily = {a: c[-n:] for a, c in daily.items()}
    start = max(book.slow, book.vol_bars) + 1
    equity, peak, max_dd = book.starting_cash, book.starting_cash, 0.0
    qty = {a: 0.0 for a in book.assets}
    fees = funding = 0.0
    trades = liquidations = 0
    stopped_day = None
    curve = [equity]
    for i in range(start, n):
        px = {a: daily[a][i] for a in book.assets}
        prev = {a: daily[a][i - 1] for a in book.assets}
        equity += sum(qty[a] * (px[a] - prev[a]) for a in book.assets)
        day_funding = sum(qty[a] * px[a] for a in book.assets) * funding_apr_pct / 100 / 365
        equity -= day_funding
        funding += day_funding
        notional = sum(abs(qty[a]) * px[a] for a in book.assets)
        if notional and equity < notional * book.maintenance_pct / 100:
            equity -= notional * (fee + book.liquidation_penalty_pct) / 100
            qty = {a: 0.0 for a in book.assets}
            liquidations += 1
        if equity <= 0:
            equity = 0.0
            curve.append(0.0)
            break
        if stopped_day is None and equity <= peak * (1 - book.max_drawdown_pct / 100):
            stopped_day = i - start + 1  # the paper book goes flat for good here
            notional = sum(abs(qty[a]) * px[a] for a in book.assets)
            equity -= notional * (fee + book.slippage_pct) / 100
            qty = {a: 0.0 for a in book.assets}
        if stopped_day is not None:
            curve.append(equity)
            continue
        w = target_weights({a: daily[a][: i + 1] for a in book.assets}, book)
        for a in book.assets:
            want = w[a] * equity / px[a]
            delta = want - qty[a]
            if abs(delta) * px[a] < equity * book.min_trade_pct / 100 and (want != 0 or qty[a] == 0):
                continue
            cost = abs(delta) * px[a] * (fee + book.slippage_pct) / 100 + \
                abs(delta) / MARKETS[a].contract_size * book.fee_per_contract
            equity -= cost
            fees += cost
            qty[a] = want
            trades += 1
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        curve.append(equity)
    days = max(1, len(curve) - 1)
    btc = daily["BTC"][start - 1:start + days] if "BTC" in daily else []
    btc_ret = btc[-1] / btc[0] - 1 if len(btc) > 1 else 0.0
    btc_dd, btc_peak = 0.0, 0.0
    for c in btc:
        btc_peak = max(btc_peak, c)
        btc_dd = max(btc_dd, 1 - c / btc_peak)
    total = equity / book.starting_cash - 1
    return {"days": days, "total_pct": total * 100,
            "cagr_pct": ((1 + total) ** (365 / days) - 1) * 100 if total > -1 else -100.0,
            "max_dd_pct": max_dd * 100, "trades": trades, "fees": fees, "funding": funding,
            "liquidations": liquidations, "stopped_day": stopped_day, "btc_hold_pct": btc_ret * 100, "btc_hold_dd_pct": btc_dd * 100}


def load_daily(days: int, book: PerpBook = BOOK) -> dict[str, list[float]]:
    out = {}
    for a in book.assets:
        bars = _retry(fetch_candles, MARKETS[a].spot, "ONE_DAY", days)
        out[a] = [c.close for c in bars]
    return out


def backtest_report(daily: dict[str, list[float]], book: PerpBook = BOOK, funding_apr_pct: float = 10.0) -> str:
    rows = []
    for label, fee, apr in [(f"{book.fee_pct}% fee, {funding_apr_pct:g}%/yr funding", book.fee_pct, funding_apr_pct),
                            (f"{book.stress_fee_pct}% fee, {funding_apr_pct:g}%/yr funding", book.stress_fee_pct, funding_apr_pct),
                            (f"{book.fee_pct}% fee, {2 * funding_apr_pct:g}%/yr funding", book.fee_pct, 2 * funding_apr_pct)]:
        r = backtest(daily, book, apr, fee)
        rows.append(f"{label:<32} {r['total_pct']:>+9.1f}% {r['cagr_pct']:>+7.1f}%/yr  max dd {r['max_dd_pct']:5.1f}%  "
                    f"trades {r['trades']:4d}  fees ${r['fees']:8.2f}  funding ${r['funding']:8.2f}  "
                    f"liquidations {r['liquidations']}"
                    + (f"  STOPPED on day {r['stopped_day']}" if r["stopped_day"] else ""))
    r = backtest(daily, book, funding_apr_pct)
    rows.append(f"{'Buy and hold BTC (spot)':<32} {r['btc_hold_pct']:>+9.1f}%            max dd {r['btc_hold_dd_pct']:5.1f}%")
    return (f"Perps trend book over {r['days']} days of daily closes, $1,000 start. Simulated; perps assumed to track "
            f"spot, liquidation checked on daily closes only.\n" + "\n".join(rows))
