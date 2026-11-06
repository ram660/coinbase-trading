"""Terminal trend bot: Ram's live spot plan, run by itself without Claude.

Every `every_minutes` it:
1. Reads its open positions from Coinbase: each open SELL bracket (take-profit + stop) is one bot position.
   Coinbase holds those exit orders on its servers, so they work even while this bot is off.
2. Reports any position whose bracket filled since the last check (exit price, P&L after fees).
3. If a slot is free, scans the top USDC spot coins by 24h volume with Ram's rule: live price vs the 50- and 200-day
   averages of closed daily candles; long above both. Highest-volume qualifier not already held wins.
4. Buys it at market with min(USDC - reserve, per_order_usdc) and attaches a bracket +/- bracket_usd from entry.

It only ever places BUY orders with an attached bracket. It never sells, cancels, converts or transfers by itself,
never holds more than max_positions, never two in one coin, never above per_order_usdc (capped at [risk]
max_order_quote). Without --live it is a dry run that places nothing.

Real orders need all three live gates (mode = "live", --live, CBAGENT_LIVE_CONFIRM) and a View+Trade key that
cannot transfer funds. The key's portfolio is the one it trades.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable

from .config import LIVE_CONFIRM_PHRASE, Config
from .market_data import BASE_URL, _get

API = "/api/v3/brokerage"
STABLES = {"USDC", "USDT", "DAI", "PYUSD", "EURC", "USD1", "GUSD", "PAX", "FDUSD", "RLUSD", "USDS"}
DAY = 86400


@dataclass(frozen=True)
class BotSettings:
    per_order_usdc: Decimal = Decimal("30")
    min_order_usdc: Decimal = Decimal("25")
    reserve_usdc: Decimal = Decimal("0.50")
    max_positions: int = 2
    bracket_usd: Decimal = Decimal("3")
    top_n: int = 50
    fast: int = 50
    slow: int = 200
    every_minutes: float = 15
    skip: tuple[str, ...] = ("ELSA-USDC", "CLANKER-USDC", "ALEO-USDC")

    @classmethod
    def from_config(cls, cfg: Config) -> "BotSettings":
        raw = dict(cfg.trend_bot)
        s = cls(
            per_order_usdc=Decimal(str(raw.get("per_order_usdc", cls.per_order_usdc))),
            min_order_usdc=Decimal(str(raw.get("min_order_usdc", cls.min_order_usdc))),
            reserve_usdc=Decimal(str(raw.get("reserve_usdc", cls.reserve_usdc))),
            max_positions=int(raw.get("max_positions", cls.max_positions)),
            bracket_usd=Decimal(str(raw.get("bracket_usd", cls.bracket_usd))),
            top_n=int(raw.get("top_n", cls.top_n)),
            fast=int(raw.get("fast", cls.fast)),
            slow=int(raw.get("slow", cls.slow)),
            every_minutes=float(raw.get("every_minutes", cls.every_minutes)),
            skip=tuple(raw.get("skip", cls.skip)),
        )
        if s.per_order_usdc > cfg.risk.max_order_quote:
            raise ValueError(f"trend_bot.per_order_usdc {s.per_order_usdc} is above risk.max_order_quote "
                             f"{cfg.risk.max_order_quote}")
        if s.every_minutes < 1:
            raise ValueError("trend_bot.every_minutes must be at least 1")
        return s


@dataclass
class Row:
    product_id: str
    volume_usd: float
    live: float
    fast_avg: float
    slow_avg: float

    @property
    def signal(self) -> str:
        if self.live > self.fast_avg and self.live > self.slow_avg:
            return "LONG"
        if self.live < self.fast_avg and self.live < self.slow_avg:
            return "SHORT"
        return "FLAT"


# ---------- public market scan (no key) ----------

def _volume(p: dict) -> float:
    return float(p.get("approximate_quote_24h_volume") or 0) or float(p.get("volume_24h") or 0) * float(p.get("price") or 0)


def scan(settings: BotSettings, get: Callable[[str], dict] = _get, now: int | None = None) -> list[Row]:
    """Top-volume USDC spot coins with their rule numbers, highest volume first."""
    products = get(f"{BASE_URL}?product_type=SPOT&limit=1000").get("products", [])
    pairs = [p for p in products if p.get("quote_currency_id") == "USDC" and p.get("status") == "online"
             and not p.get("trading_disabled") and p.get("base_currency_id") not in STABLES]
    pairs = sorted(pairs, key=lambda p: -_volume(p))[:settings.top_n]
    now = now or int(time.time())
    end = now - now % DAY  # today's candle is still forming: closed candles only
    rows = []
    for p in pairs:
        try:
            candles = get(f"{BASE_URL}/{p['product_id']}/candles?start={end - (settings.slow + 60) * DAY}&end={end}"
                          f"&granularity=ONE_DAY").get("candles", [])
        except Exception:
            continue
        closes = [float(c["close"]) for c in sorted(candles, key=lambda c: int(c["start"])) if int(c["start"]) < end]
        if len(closes) < settings.slow or not p.get("price"):
            continue
        rows.append(Row(p["product_id"], _volume(p), float(p["price"]),
                        sum(closes[-settings.fast:]) / settings.fast, sum(closes[-settings.slow:]) / settings.slow))
    return rows


def candidates(rows: list[Row], held: set[str], settings: BotSettings) -> list[Row]:
    held_bases = {h.split("-")[0] for h in held}
    return [r for r in rows if r.signal == "LONG" and r.product_id not in settings.skip
            and r.product_id.split("-")[0] not in held_bases]


# ---------- account side (needs the key) ----------

def is_bracket(order: dict) -> bool:
    return order.get("order_type") == "BRACKET" or "trigger_bracket_gtc" in (order.get("order_configuration") or {})


def _round(value: Decimal, step: Decimal, rounding=ROUND_HALF_UP) -> Decimal:
    return (value / step).quantize(Decimal("1"), rounding=rounding) * step


class Account:
    """Thin wrapper over the Coinbase REST client (coinbase-advanced-py), using raw endpoints."""

    def __init__(self, client):
        self.client = client

    def open_brackets(self) -> list[dict]:
        data = self.client.get(f"{API}/orders/historical/batch",
                               params={"order_status": ["OPEN"], "order_side": "SELL", "limit": 250})
        return [o for o in data.get("orders", []) if is_bracket(o)]

    def last_filled_sell(self, product_id: str) -> dict | None:
        data = self.client.get(f"{API}/orders/historical/batch",
                               params={"order_status": ["FILLED"], "order_side": "SELL", "product_ids": [product_id],
                                       "limit": 5})
        orders = data.get("orders", [])
        return orders[0] if orders else None

    def usdc(self) -> Decimal:
        accounts = self.client.get(f"{API}/accounts", params={"limit": 250}).get("accounts", [])
        for a in accounts:
            if a.get("currency") == "USDC":
                return Decimal(a["available_balance"]["value"])
        return Decimal("0")

    def product(self, product_id: str) -> dict:
        return self.client.get(f"{API}/products/{product_id}")

    def ask(self, product_id: str) -> Decimal:
        books = self.client.get(f"{API}/best_bid_ask", params={"product_ids": [product_id]}).get("pricebooks", [])
        return Decimal(books[0]["asks"][0]["price"])

    def buy_with_bracket(self, product_id: str, spend: Decimal, take_profit: Decimal, stop: Decimal) -> dict:
        return self.client.post(f"{API}/orders", data={
            "client_order_id": f"cbagent-trend-{uuid.uuid4()}",
            "product_id": product_id,
            "side": "BUY",
            "order_configuration": {"market_market_ioc": {"quote_size": str(spend)}},
            "attached_order_configuration": {"trigger_bracket_gtc": {"limit_price": str(take_profit),
                                                                     "stop_trigger_price": str(stop)}},
        })

    def order(self, order_id: str) -> dict:
        return self.client.get(f"{API}/orders/historical/{order_id}").get("order", {})


def make_account(cfg: Config) -> Account:
    from .brokers.coinbase_live import make_client, require_safe_key

    client = make_client(cfg)
    require_safe_key(client)
    return Account(client)


def check_gates(cfg: Config, live_flag: bool) -> None:
    from .brokers.coinbase_live import LiveTradingNotAllowed

    if cfg.mode != "live":
        raise LiveTradingNotAllowed('config mode is not "live" (set mode = "live" in config/local.toml)')
    if not live_flag:
        raise LiveTradingNotAllowed("the --live flag was not passed")
    if cfg.env.get("CBAGENT_LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
        raise LiveTradingNotAllowed("CBAGENT_LIVE_CONFIRM is not set to the confirmation phrase in config/.env")


# ---------- the bot ----------

@dataclass
class TrendBot:
    settings: BotSettings
    account: Account | None          # None: dry run with no key, assumes no open positions and full USDC
    live: bool                        # False: never sends an order
    state_path: Path
    journal_path: Path
    kill_switch: Path
    scan_fn: Callable[[BotSettings], list[Row]] = scan
    out: Callable[[str], None] = print
    state: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.state.setdefault("positions", {})

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def _log(self, event: str, **data) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "event": event, **data}) + "\n")

    def _say(self, msg: str) -> None:
        self.out(time.strftime("%Y-%m-%d %H:%M:%S ") + msg)

    def tick(self) -> None:
        if self.kill_switch.exists():
            self._say(f"Kill switch is on ({self.kill_switch}); not trading. Run `cbagent unkill` after reviewing.")
            return
        held = self._sync_positions()
        free = self.settings.max_positions - len(held)
        if free <= 0:
            self._say(f"Both slots full: {', '.join(sorted(held))}. Brackets on Coinbase handle the exits.")
            return
        rows = self.scan_fn(self.settings)
        picks = candidates(rows, held, self.settings)
        if not picks:
            self._say(f"{len(held)}/{self.settings.max_positions} slots used; no coin is above both averages. Waiting.")
            return
        for row in picks[:free]:
            if not self._buy(row):
                break

    def _sync_positions(self) -> set[str]:
        if self.account is None:
            return set(self.state["positions"])
        brackets = self.account.open_brackets()
        open_now = {o["product_id"] for o in brackets}
        for product_id, pos in list(self.state["positions"].items()):
            if product_id in open_now:
                continue
            exit_order = self.account.last_filled_sell(product_id)
            if exit_order:
                value = Decimal(exit_order.get("filled_value") or "0")
                fees = Decimal(exit_order.get("total_fees") or "0")
                pnl = value - fees - Decimal(pos["cost"])
                self._say(f"EXIT {product_id} at {exit_order.get('average_filled_price')}: P&L {pnl:+.2f} USDC "
                          f"after fees.")
                self._log("exit", product=product_id, price=exit_order.get("average_filled_price"), pnl=str(pnl))
            else:
                self._say(f"{product_id} bracket is gone with no filled sell found; check it in Coinbase.")
                self._log("bracket_missing", product=product_id)
            del self.state["positions"][product_id]
        self._save()
        return open_now

    def _buy(self, row: Row) -> bool:
        s = self.settings
        usdc = self.account.usdc() if self.account else s.per_order_usdc + s.reserve_usdc
        spend = min(usdc - s.reserve_usdc, s.per_order_usdc).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if spend < s.min_order_usdc:
            self._say(f"USDC {usdc:.2f} is too low for another ${s.min_order_usdc}+ slot. Waiting.")
            return False
        numbers = (f"live {row.live:.6g}, 50d {row.fast_avg:.6g}, 200d {row.slow_avg:.6g}, "
                   f"24h vol ${row.volume_usd / 1e6:.1f}M")
        if self.account:
            ask = self.account.ask(row.product_id)
            step = Decimal(self.account.product(row.product_id)["quote_increment"])
        else:
            ask = Decimal(str(row.live))
            try:
                step = Decimal(_get(f"{BASE_URL}/{row.product_id}")["quote_increment"])
            except Exception:
                step = Decimal("1e-12")
        size = spend / ask
        tp = _round(ask + s.bracket_usd / size, step)
        sl = _round(ask - s.bracket_usd / size, step)
        if sl <= 0:
            self._say(f"{row.product_id}: a ${s.bracket_usd} stop would be below zero at this size; skipping.")
            return True
        plan = f"BUY ${spend} {row.product_id} at ~{ask}, take-profit {tp}, stop {sl} ({numbers})"
        if not self.live:
            self._say("DRY RUN, nothing sent: " + plan)
            return True
        resp = self.account.buy_with_bracket(row.product_id, spend, tp, sl)
        if not resp.get("success"):
            self._say(f"REJECTED {row.product_id}: {resp.get('error_response') or resp}")
            self._log("rejected", product=row.product_id, response=resp)
            return True  # try the next candidate, never this coin again this tick
        order_id = resp["success_response"]["order_id"]
        order = {}
        for _ in range(10):
            order = self.account.order(order_id)
            if order.get("status") == "FILLED":
                break
            time.sleep(1)
        filled = Decimal(order.get("filled_size") or "0")
        cost = Decimal(order.get("filled_value") or spend) + Decimal(order.get("total_fees") or "0")
        self.state["positions"][row.product_id] = {"order_id": order_id, "size": str(filled), "cost": str(cost),
                                                   "fill": order.get("average_filled_price"), "tp": str(tp),
                                                   "sl": str(sl), "opened": int(time.time())}
        self._save()
        self._log("buy", product=row.product_id, order_id=order_id, spend=str(spend), tp=str(tp), sl=str(sl),
                  status=order.get("status"), rule=numbers)
        self._say(f"BOUGHT {filled} {row.product_id} at {order.get('average_filled_price')} "
                  f"({order.get('status')}), take-profit {tp}, stop {sl}. {numbers}")
        return True

    def run(self, once: bool) -> int:
        mode = "LIVE, real orders" if self.live else "DRY RUN, no orders"
        self._say(f"Trend bot ({mode}): {self.settings.max_positions} x ${self.settings.per_order_usdc} slots, "
                  f"+/-${self.settings.bracket_usd} brackets, every {self.settings.every_minutes:g} min. Ctrl+C stops.")
        while True:
            try:
                self.tick()
            except Exception as e:  # a network blip should not stop the loop
                self._say(f"tick failed: {e}")
                self._log("error", message=str(e))
            if once:
                return 0
            time.sleep(self.settings.every_minutes * 60)


def build(cfg: Config, live_flag: bool, out=print) -> TrendBot:
    settings = BotSettings.from_config(cfg)
    account = None
    if live_flag:
        check_gates(cfg, live_flag)
        account = make_account(cfg)
    elif cfg.env.get("COINBASE_API_KEY"):
        try:
            account = make_account(cfg)  # read-only use: real positions and balance in the dry run
        except Exception as e:
            print(f"(dry run without the account: {e})", file=sys.stderr)
    return TrendBot(settings, account, live_flag, cfg.data_dir / "trend_bot.json", cfg.data_dir / "trend_bot.jsonl",
                    cfg.kill_switch_path, out=out)
