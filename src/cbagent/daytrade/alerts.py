"""Price alerts kept in data/daytrade/alerts.json. `check` reads live public prices and rings each alert once."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .common import folder, live_price, read_json, utc, write_json


def _path(data_dir: Path) -> Path:
    return folder(data_dir) / "alerts.json"


def load(data_dir: Path) -> list[dict]:
    return read_json(_path(data_dir), [])


def add(data_dir: Path, product_id: str, direction: str, price: float, note: str = "", now: int | None = None) -> dict:
    if direction not in ("above", "below"):
        raise ValueError("direction must be above or below")
    alerts = load(data_dir)
    alert = {"id": max([a["id"] for a in alerts] or [0]) + 1, "product_id": product_id.upper(), "direction": direction,
             "price": price, "note": note, "created": now or int(time.time()), "fired": None}
    alerts.append(alert)
    write_json(_path(data_dir), alerts)
    return alert


def remove(data_dir: Path, ids: list[int] | None = None) -> int:
    """Remove the given ids, or every alert that already rang when ids is None."""
    alerts = load(data_dir)
    keep = [a for a in alerts if a["id"] not in ids] if ids is not None else [a for a in alerts if not a["fired"]]
    write_json(_path(data_dir), keep)
    return len(alerts) - len(keep)


def check(data_dir: Path, price_of: Callable[[str], float] = live_price, now: int | None = None) -> list[str]:
    alerts = load(data_dir)
    prices: dict[str, float] = {}
    rang = []
    for a in alerts:
        if a["fired"]:
            continue
        pid = a["product_id"]
        if pid not in prices:
            try:
                prices[pid] = price_of(pid)
            except Exception as e:  # one bad product shouldn't stop the rest
                rang.append(f"[{a['id']}] {pid}: price unavailable ({e})")
                continue
        p = prices[pid]
        if (p >= a["price"]) if a["direction"] == "above" else (p <= a["price"]):
            a["fired"] = {"ts": now or int(time.time()), "price": p}
            rang.append(f"[{a['id']}] {pid} is {p:g}, {a['direction']} {a['price']:g}. {a['note']}".rstrip())
    write_json(_path(data_dir), alerts)
    return rang


def listing(data_dir: Path) -> str:
    alerts = load(data_dir)
    if not alerts:
        return "No alerts."
    out = []
    for a in alerts:
        state = f"rang {utc(a['fired']['ts'])} at {a['fired']['price']:g}" if a["fired"] else "waiting"
        out.append(f"[{a['id']}] {a['product_id']:<12} {a['direction']} {a['price']:g}  {state}  {a['note']}".rstrip())
    return "\n".join(out)
