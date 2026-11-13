"""Loads config/default.toml, overlays config/local.toml, and reads config/.env without printing it."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
LIVE_CONFIRM_PHRASE = "I accept real money risk"


@dataclass(frozen=True)
class RiskConfig:
    max_order_quote: Decimal
    max_position_quote: Decimal
    max_total_exposure_quote: Decimal
    max_daily_loss_quote: Decimal
    max_drawdown_pct: Decimal
    max_orders_per_day: int
    kill_switch_file: str
    on_drawdown: str = "flatten"  # "flatten" sells everything when the drawdown limit trips; "freeze" holds
    cooloff_days: int = 0         # restart this many days after an automatic trip; 0 stays off
    reentry_size_pct: Decimal = Decimal("50")


@dataclass(frozen=True)
class PaperConfig:
    starting_cash: Decimal
    fee_pct: Decimal
    slippage_pct: Decimal


@dataclass(frozen=True)
class Config:
    mode: str
    products: list[str]
    granularity: str
    poll_seconds: int
    data_dir: Path
    strategy: dict
    exits: dict
    risk: RiskConfig
    paper: PaperConfig
    env: dict[str, str] = field(repr=False, default_factory=dict)
    live: dict = field(default_factory=dict)
    portfolio: dict = field(default_factory=dict)
    options_alerts: dict = field(default_factory=dict)

    @property
    def kill_switch_path(self) -> Path:
        return self.data_dir / self.risk.kill_switch_file


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').replace("\\n", "\n")
    for key in ("COINBASE_API_KEY", "COINBASE_API_SECRET", "CBAGENT_LIVE_CONFIRM", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _dec(section: dict, key: str) -> Decimal:
    return Decimal(str(section[key]))


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    raw = tomllib.loads((config_dir / "default.toml").read_text(encoding="utf-8"))
    local = config_dir / "local.toml"
    if local.exists():
        raw = _merge(raw, tomllib.loads(local.read_text(encoding="utf-8")))

    mode = raw["mode"]
    if mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")

    r, p = raw["risk"], raw["paper"]
    on_drawdown = r.get("on_drawdown", "flatten")
    if on_drawdown not in ("flatten", "freeze"):
        raise ValueError(f"risk.on_drawdown must be 'flatten' or 'freeze', got {on_drawdown!r}")
    data_dir = Path(raw.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return Config(
        mode=mode,
        products=list(raw["products"]),
        granularity=raw["granularity"],
        poll_seconds=int(raw["poll_seconds"]),
        data_dir=data_dir,
        strategy=dict(raw["strategy"]),
        exits=dict(raw.get("exits", {})),
        risk=RiskConfig(
            max_order_quote=_dec(r, "max_order_quote"),
            max_position_quote=_dec(r, "max_position_quote"),
            max_total_exposure_quote=_dec(r, "max_total_exposure_quote"),
            max_daily_loss_quote=_dec(r, "max_daily_loss_quote"),
            max_drawdown_pct=_dec(r, "max_drawdown_pct"),
            max_orders_per_day=int(r["max_orders_per_day"]),
            kill_switch_file=r["kill_switch_file"],
            on_drawdown=on_drawdown,
            cooloff_days=int(r.get("cooloff_days", 0)),
            reentry_size_pct=Decimal(str(r.get("reentry_size_pct", 50))),
        ),
        paper=PaperConfig(
            starting_cash=_dec(p, "starting_cash"),
            fee_pct=_dec(p, "fee_pct"),
            slippage_pct=_dec(p, "slippage_pct"),
        ),
        env=_read_env(config_dir / ".env"),
        live=dict(raw.get("live", {})),
        portfolio=dict(raw.get("portfolio", {})),
        options_alerts=dict(raw.get("options_alerts", {})),
    )
