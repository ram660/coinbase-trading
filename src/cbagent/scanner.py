"""Market scanner: measures every liquid Coinbase spot pair on daily data. Read-only, public data, no key.

It describes; it doesn't recommend. The two scores are fixed rules that rank coins by trend strength
and recent relative strength, so you can see what's moving and then test ideas with the backtester.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .indicators import rsi, sma
from .market_data import BASE_URL, fetch_candles
from .models import Candle

HISTORY_DAYS = 220
STABLECOINS = {"PAXG", "XAUT", "USDT", "USDC", "EURC", "DAI", "PYUSD", "USDS", "GUSD", "PAX", "USDP", "USD1", "RLUSD", "FDUSD", "TUSD"}


@dataclass
class CoinStats:
    product_id: str
    name: str
    price: float
    volume_24h_quote: float
    days: int
    ret_7d: float
    ret_30d: float
    ret_90d: float
    vs_btc_30d: float
    vol_30d_annual: float
    rsi_14: float
    above_sma50: bool
    above_sma200: bool | None
    sma50_over_200: bool | None
    off_high_pct: float
    btc_correlation_90d: float | None
    trend_score: float  # long-horizon rule: trend state plus risk-adjusted 90-day momentum
    swing_score: float  # shorter-horizon rule: 30-day strength vs BTC plus trend and RSI zone


def list_products(quote: str, min_volume: float) -> list[dict]:
    req = urllib.request.Request(f"{BASE_URL}?product_type=SPOT", headers={"User-Agent": "cbagent/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        products = json.load(resp)["products"]
    return [
        p for p in products
        if p["quote_currency_id"] == quote and p["base_currency_id"] not in STABLECOINS and p["status"] == "online" and not p["trading_disabled"]
        and not p.get("view_only") and not p.get("cancel_only")
        and float(p.get("approximate_quote_24h_volume") or 0) >= min_volume
    ]


def _ret(closes: list[float], n: int) -> float:
    return (closes[-1] / closes[-1 - n] - 1) * 100 if len(closes) > n else float("nan")


def _daily_returns(closes: list[float]) -> list[float]:
    return [b / a - 1 for a, b in zip(closes, closes[1:]) if a > 0]


def _corr(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 30:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return cov / den if den else None


def measure(product: dict, candles: list[Candle], btc: list[Candle]) -> CoinStats | None:
    if len(candles) < 60:
        return None
    closes = [c.close for c in candles]
    btc_by_ts = {c.start: c.close for c in btc}
    paired = [(c.close, btc_by_ts[c.start]) for c in candles if c.start in btc_by_ts]
    rets = _daily_returns(closes[-31:])
    vol = statistics.pstdev(rets) * math.sqrt(365) * 100 if len(rets) > 5 else float("nan")
    ret30, ret90 = _ret(closes, 30), _ret(closes, 90)
    btc30 = _ret([b for _, b in paired], 30) if len(paired) > 30 else float("nan")
    has200 = len(closes) >= 200
    s50 = sma(closes, 50)
    s200 = sma(closes, 200) if has200 else None
    coin_r = _daily_returns([c for c, _ in paired][-91:])
    btc_r = _daily_returns([b for _, b in paired][-91:])
    r14 = rsi(closes, 14)

    above50 = closes[-1] > s50
    above200 = closes[-1] > s200 if s200 else None
    cross = s50 > s200 if s200 else None
    risk_adj_90 = ret90 / vol if vol and not math.isnan(ret90) and vol > 0 else 0.0
    trend_score = (1.0 if above50 else -1.0) + (1.0 if above200 else -1.0 if above200 is False else 0.0) \
        + (1.0 if cross else -1.0 if cross is False else 0.0) + max(-2.0, min(2.0, risk_adj_90 * 2))
    vs_btc = ret30 - btc30 if not math.isnan(btc30) else 0.0
    swing_score = max(-2.0, min(2.0, vs_btc / 15)) + (1.0 if above50 else -1.0) + (0.5 if 50 <= r14 <= 70 else -0.5 if r14 > 80 or r14 < 30 else 0.0)

    return CoinStats(
        product_id=product["product_id"],
        name=product.get("base_name", ""),
        price=closes[-1],
        volume_24h_quote=float(product.get("approximate_quote_24h_volume") or 0),
        days=len(closes),
        ret_7d=_ret(closes, 7),
        ret_30d=ret30,
        ret_90d=ret90,
        vs_btc_30d=vs_btc,
        vol_30d_annual=vol,
        rsi_14=r14,
        above_sma50=above50,
        above_sma200=above200,
        sma50_over_200=cross,
        off_high_pct=(closes[-1] / max(closes) - 1) * 100,
        btc_correlation_90d=_corr(coin_r, btc_r),
        trend_score=round(trend_score, 2),
        swing_score=round(swing_score, 2),
    )


def scan(quote: str = "USDC", min_volume: float = 1_000_000, workers: int = 6) -> list[CoinStats]:
    products = list_products(quote, min_volume)
    btc = fetch_candles(f"BTC-{quote}", "ONE_DAY", HISTORY_DAYS)

    def one(p: dict) -> CoinStats | None:
        for attempt in range(3):
            try:
                return measure(p, fetch_candles(p["product_id"], "ONE_DAY", HISTORY_DAYS), btc)
            except Exception:
                time.sleep(1 + attempt * 2)
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [r for r in pool.map(one, products) if r]
    return results


def save_csv(rows: list[CoinStats], data_dir: Path) -> Path:
    out = data_dir / "scans" / f"scan_{datetime.now(timezone.utc):%Y%m%d_%H%M}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0])))
        writer.writeheader()
        writer.writerows(asdict(r) for r in rows)
    return out


def _fmt(r: CoinStats, held: bool) -> str:
    trend = "up" if r.sma50_over_200 else "down" if r.sma50_over_200 is False else "n/a"
    corr = f"{r.btc_correlation_90d:.2f}" if r.btc_correlation_90d is not None else " n/a"
    return (f"{'*' if held else ' '}{r.product_id:<12} {r.ret_7d:+7.1f}% {r.ret_30d:+7.1f}% {r.ret_90d:+7.1f}% "
            f"{r.vs_btc_30d:+7.1f}% {r.vol_30d_annual:6.0f}% {r.rsi_14:5.0f} {trend:>5} {r.off_high_pct:+7.1f}% {corr:>5} "
            f"{r.volume_24h_quote / 1e6:8.1f}M  T{r.trend_score:+.1f} S{r.swing_score:+.1f}")


HEADER = (" product        7d      30d      90d   vs BTC  vol/yr  RSI 50/200  off high  corr   24h vol  scores")


def report(rows: list[CoinStats], held: set[str], top: int) -> str:
    lines = [f"{len(rows)} coins measured. * = you hold it. T = trend score, S = swing score (rules, not advice).", ""]
    for title, key in (("Strongest long-term trend", "trend_score"), ("Strongest 30-day swing vs BTC", "swing_score")):
        lines += [title, HEADER]
        lines += [_fmt(r, r.product_id.split("-")[0] in held) for r in sorted(rows, key=lambda r: getattr(r, key), reverse=True)[:top]]
        lines.append("")
    mine = [r for r in rows if r.product_id.split("-")[0] in held]
    if mine:
        lines += ["Your holdings", HEADER] + [_fmt(r, True) for r in sorted(mine, key=lambda r: r.product_id)]
    missing = held - {r.product_id.split("-")[0] for r in rows}
    if missing:
        lines.append(f"Not in this scan (no liquid pair or too little volume): {', '.join(sorted(missing))}")
    return "\n".join(lines)
