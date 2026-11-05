"""Public Coinbase Advanced Trade market data. No API key needed."""
from __future__ import annotations

import json
import time
import urllib.request

from .models import Candle

BASE_URL = "https://api.coinbase.com/api/v3/brokerage/market/products"
GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}
MAX_CANDLES_PER_REQUEST = 300  # the API caps a request at 350


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "cbagent/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_candles(product_id: str, granularity: str, count: int, end: int | None = None) -> list[Candle]:
    """Return up to `count` closed candles, oldest first."""
    step = GRANULARITY_SECONDS[granularity]
    end = end or int(time.time())
    end -= end % step  # drop the candle that is still forming
    out: dict[int, Candle] = {}
    while len(out) < count:
        batch = min(MAX_CANDLES_PER_REQUEST, count - len(out))
        start = end - batch * step
        url = f"{BASE_URL}/{product_id}/candles?start={start}&end={end}&granularity={granularity}"
        rows = _get(url).get("candles", [])
        if not rows:
            break
        for row in rows:
            c = Candle(
                start=int(row["start"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
            if c.start < end:
                out[c.start] = c
        end = start
    return sorted(out.values(), key=lambda c: c.start)[-count:]


def fetch_price(product_id: str) -> float:
    return float(_get(f"{BASE_URL}/{product_id}")["price"])
