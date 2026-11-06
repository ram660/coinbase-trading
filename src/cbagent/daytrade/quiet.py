"""Quiet mode for scheduled runs: speak on the first run of the UTC day, then only about things not yet reported today.

State is one small file, data/daytrade/seen_today.json: the UTC date and the keys already reported that day.
A new UTC day starts it fresh.
"""
from __future__ import annotations

import time
from pathlib import Path

from .common import folder, read_json, write_json


def _path(data_dir: Path) -> Path:
    return folder(data_dir) / "seen_today.json"


def gate(data_dir: Path, keys: list[str], now: int | None = None) -> tuple[list[str], bool]:
    """Return (keys not reported yet today, whether this is the first run of the UTC day) and remember them."""
    today = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    state = read_json(_path(data_dir), {})
    first = state.get("date") != today
    seen = set() if first else set(state.get("keys", []))
    new = [k for k in dict.fromkeys(keys) if k not in seen]
    write_json(_path(data_dir), {"date": today, "keys": sorted(seen | set(new))})
    return new, first
