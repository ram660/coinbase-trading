"""Shared bits for the intraday tools: where files live, fetching 5-minute candles, price rounding."""
from __future__ import annotations

import json
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

from ..market_data import BASE_URL, _get, fetch_candles
from ..models import Candle

BAR = 300                # 5-minute candles
BARS_PER_DAY = 86400 // BAR


def folder(data_dir: Path) -> Path:
    out = data_dir / "daytrade"
    out.mkdir(parents=True, exist_ok=True)
    return out


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def bars(product_id: str, days: float, end: int | None = None) -> list[Candle]:
    """Closed 5-minute candles for the last `days` days, oldest first."""
    return fetch_candles(product_id, "FIVE_MINUTE", int(days * BARS_PER_DAY), end=end)


def product_info(product_id: str) -> dict:
    return _get(f"{BASE_URL}/{product_id}")


def live_price(product_id: str) -> float:
    return float(product_info(product_id)["price"])


def round_to(value: float | Decimal, increment: str | None, down: bool = False) -> Decimal:
    """Round to the product's increment (e.g. '0.01'); without one, keep 6 significant digits."""
    v = Decimal(str(value))
    if increment and Decimal(increment) > 0:
        inc = Decimal(increment)
        steps = (v / inc).quantize(Decimal("1"), rounding=ROUND_DOWN if down else ROUND_HALF_UP)
        return (steps * inc).quantize(inc.normalize()) if inc < 1 else steps * inc
    if v == 0:
        return v
    return Decimal(f"{float(v):.6g}")


def utc(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
