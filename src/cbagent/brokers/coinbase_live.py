"""Live Coinbase Advanced Trade broker. Sends REAL orders with REAL money.

Safety, in order:
- It only constructs when all three gates pass: mode = "live" in config, the --live CLI flag, and
  CBAGENT_LIVE_CONFIRM set to the exact phrase in config/.env.
- It refuses any API key that is allowed to transfer (withdraw) funds.
- It keeps its own ledger of the allocation ([live] allocation_quote), so it never spends more than that even if the
  account holds more, and only counts coins it bought itself. Use a dedicated Coinbase portfolio for the bot anyway.

Not yet exercised against a real account: start with the smallest sizes and check each fill in Coinbase.
"""
from __future__ import annotations

import json
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from ..config import LIVE_CONFIRM_PHRASE, Config
from ..models import Fill, Position, Side
from .base import Broker


class LiveTradingNotAllowed(RuntimeError):
    pass


def make_client(config: Config):
    if not config.env.get("COINBASE_API_KEY") or not config.env.get("COINBASE_API_SECRET"):
        raise LiveTradingNotAllowed("COINBASE_API_KEY / COINBASE_API_SECRET missing from config/.env")
    try:
        from coinbase.rest import RESTClient  # type: ignore[import-not-found]
    except ImportError as e:
        raise LiveTradingNotAllowed('install the live extra first: pip install -e ".[live]"') from e
    return RESTClient(api_key=config.env["COINBASE_API_KEY"], api_secret=config.env["COINBASE_API_SECRET"])


def key_permissions(client) -> dict:
    return client.get_api_key_permissions().to_dict()


def require_safe_key(client) -> dict:
    perms = key_permissions(client)
    if perms.get("can_transfer"):
        raise LiveTradingNotAllowed("this API key can transfer funds; create one with View and Trade only")
    if not perms.get("can_trade"):
        raise LiveTradingNotAllowed("this API key cannot trade; give it the Trade permission")
    return perms


class CoinbaseLiveBroker(Broker):
    def __init__(self, config: Config, live_flag: bool, client=None):
        if config.mode != "live":
            raise LiveTradingNotAllowed('config mode is not "live"')
        if not live_flag:
            raise LiveTradingNotAllowed("the --live flag was not passed")
        if config.env.get("CBAGENT_LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
            raise LiveTradingNotAllowed("CBAGENT_LIVE_CONFIRM is not set to the confirmation phrase")
        allocation = Decimal(str(config.live.get("allocation_quote", 0)))
        if allocation <= 0:
            raise LiveTradingNotAllowed("set [live] allocation_quote to the most the bot may use")
        quotes = {p.split("-")[1] for p in config.products}
        if len(quotes) != 1:
            raise LiveTradingNotAllowed("all products must share one quote currency (e.g. all -USD)")

        self.client = client or make_client(config)
        require_safe_key(self.client)
        self.products = config.products
        self.quote_currency = quotes.pop()
        self._increments: dict[str, Decimal] = {}
        self.ledger_path: Path = config.data_dir / "live_ledger.json"
        self.ledger = {"cash": allocation, "base": {p: Decimal("0") for p in self.products}}
        if self.ledger_path.exists():
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            self.ledger = {"cash": Decimal(data["cash"]), "base": {p: Decimal(v) for p, v in data["base"].items()}}

    def _save(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"cash": str(self.ledger["cash"]), "base": {p: str(v) for p, v in self.ledger["base"].items()}}
        self.ledger_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _balances(self) -> dict[str, Decimal]:
        accounts = self.client.get_accounts(limit=250).to_dict().get("accounts", [])
        return {a["currency"]: Decimal(a["available_balance"]["value"]) for a in accounts}

    def cash(self) -> Decimal:
        real = self._balances().get(self.quote_currency, Decimal("0"))
        return max(Decimal("0"), min(self.ledger["cash"], real))

    def positions(self) -> dict[str, Position]:
        balances = self._balances()
        out = {}
        for p in self.products:
            base = min(self.ledger["base"].get(p, Decimal("0")), balances.get(p.split("-")[0], Decimal("0")))
            out[p] = Position(base=base if base >= self._base_increment(p) else Decimal("0"))
        return out

    def _base_increment(self, product_id: str) -> Decimal:
        if product_id not in self._increments:
            product = self.client.get_product(product_id).to_dict()
            self._increments[product_id] = Decimal(product["base_increment"])
        return self._increments[product_id]

    def _actual_fill(self, order_id: str) -> tuple[Decimal, Decimal, Decimal] | None:
        """(filled base, average price, fees) once Coinbase reports the order filled, else None."""
        for _ in range(10):
            order = self.client.get_order(order_id).to_dict().get("order", {})
            if order.get("status") == "FILLED":
                return Decimal(order["filled_size"]), Decimal(order["average_filled_price"]), Decimal(order["total_fees"])
            time.sleep(1)
        return None

    def market_order(self, product_id: str, side: Side, quote_size: Decimal, price: Decimal, ts: int, reason: str) -> Fill:
        client_order_id = f"cbagent-{uuid.uuid4()}"
        if side == "BUY":
            size = min(quote_size, self.ledger["cash"]).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            resp = self.client.market_order_buy(client_order_id=client_order_id, product_id=product_id, quote_size=str(size))
            base, fee = size / price, Decimal("0")
        else:
            held = self.positions()[product_id].base
            base = min(held, quote_size / price).quantize(self._base_increment(product_id), rounding=ROUND_DOWN)
            resp = self.client.market_order_sell(client_order_id=client_order_id, product_id=product_id, base_size=str(base))
            fee = Decimal("0")
        data = resp.to_dict()
        if not data.get("success"):
            raise RuntimeError(f"Coinbase rejected the order: {data.get('error_response')}")
        order_id = data.get("success_response", {}).get("order_id", client_order_id)

        actual = self._actual_fill(order_id)
        if actual:
            base, price, fee = actual
        if side == "BUY":
            self.ledger["cash"] -= size if not actual else base * price + fee
            self.ledger["base"][product_id] = self.ledger["base"].get(product_id, Decimal("0")) + base
        else:
            self.ledger["cash"] += base * price - fee
            self.ledger["base"][product_id] = max(Decimal("0"), self.ledger["base"].get(product_id, Decimal("0")) - base)
        self._save()
        return Fill(product_id, side, base, price, fee, ts, order_id, reason + ("" if actual else " (fill not confirmed yet)"))
