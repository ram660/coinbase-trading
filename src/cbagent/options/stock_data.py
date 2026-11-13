"""Daily candles and the live price for US stocks and ETFs from Yahoo Finance's public chart endpoint. No key needed."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from ..models import Candle

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d"


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (cbagent)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def parse_chart(payload: dict) -> tuple[list[Candle], float, bool]:
    """Return (closed daily candles oldest first, live price, whether the regular session is open now)."""
    res = payload["chart"]["result"][0]
    meta, q = res["meta"], res["indicators"]["quote"][0]
    candles = [
        Candle(int(t), float(o), float(h), float(l), float(c), float(v or 0))
        for t, o, h, l, c, v in zip(res.get("timestamp", []), q["open"], q["high"], q["low"], q["close"], q["volume"])
        if None not in (o, h, l, c)
    ]
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    market_time = int(meta.get("regularMarketTime") or 0)
    is_open = bool(period) and period.get("start", 0) <= market_time < period.get("end", 0)
    # While the session is open Yahoo's last daily bar is today's, still forming. Drop it so the averages use closed
    # days only, like the crypto rule. After the close that bar is final and stays.
    if candles and is_open and candles[-1].start >= period.get("start", 0) - 6 * 3600:
        candles = candles[:-1]
    return candles, float(meta["regularMarketPrice"]), is_open


def fetch(symbol: str, days_range: str = "2y") -> tuple[list[Candle], float, bool]:
    return parse_chart(_get(URL.format(symbol=urllib.parse.quote(symbol), range=days_range)))
