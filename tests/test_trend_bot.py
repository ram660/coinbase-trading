"""Offline tests for the terminal trend bot: the scan rule, slots, brackets, exits and the live gates."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from cbagent import trend_bot as tb
from cbagent.brokers.coinbase_live import LiveTradingNotAllowed
from cbagent.config import load_config

DAY = 86400
NOW = 1_800_000_000


def fake_get(products: dict[str, tuple[float, float, list[float]]]):
    """products: id -> (volume, live price, daily closes oldest first)."""
    end = NOW - NOW % DAY

    def get(url: str) -> dict:
        if "/candles" in url:
            pid = url.split("/products/")[1].split("/")[0]
            closes = products[pid][2]
            return {"candles": [{"start": str(end - (len(closes) - i) * DAY), "close": str(c)}
                                for i, c in enumerate(closes)] + [{"start": str(end), "close": "1"}]}
        return {"products": [{"product_id": pid, "quote_currency_id": pid.split("-")[1], "base_currency_id": pid.split("-")[0],
                              "status": "online", "price": str(v[1]), "approximate_quote_24h_volume": str(v[0])}
                             for pid, v in products.items()]}
    return get


class FakeClient:
    def __init__(self, usdc="100", brackets=(), filled_sell=None):
        self.usdc, self.brackets, self.filled_sell, self.posts = usdc, list(brackets), filled_sell, []

    def get(self, path, params=None):
        if path.endswith("/accounts"):
            return {"accounts": [{"currency": "USDC", "available_balance": {"value": self.usdc}},
                                 {"currency": "QNT", "available_balance": {"value": "2.418"}}]}
        if path.endswith("/orders/historical/batch"):
            if params["order_status"] == ["OPEN"]:
                return {"orders": self.brackets}
            return {"orders": [self.filled_sell] if self.filled_sell else []}
        if path.endswith("/best_bid_ask"):
            return {"pricebooks": [{"asks": [{"price": "10"}]}]}
        if "/orders/historical/" in path:
            return {"order": {"status": "FILLED", "filled_size": "3", "average_filled_price": "10",
                              "filled_value": "30", "total_fees": "0.18"}}
        if "/products/" in path:
            return {"quote_increment": "0.001"}
        raise AssertionError(path)

    def post(self, path, data):
        self.posts.append(data)
        pid = data["product_id"]
        self.brackets.append({"product_id": pid, "order_type": "BRACKET", "side": "SELL"})
        return {"success": True, "success_response": {"order_id": f"o-{pid}"}}


UP = [100 * 1.002 ** i for i in range(260)]
DOWN = [100 * 0.998 ** i for i in range(260)]


class ScanTests(unittest.TestCase):
    def test_rule_and_ranking(self):
        get = fake_get({"AAA-USDC": (5e6, UP[-1] * 1.01, UP), "BBB-USDC": (9e6, DOWN[-1] * 0.99, DOWN),
                        "CCC-USDC": (7e6, UP[-1] * 1.01, UP), "USDT-USDC": (9e9, 1, [1] * 260),
                        "DDD-USD": (9e9, UP[-1], UP)})
        rows = tb.scan(tb.BotSettings(), get=get, now=NOW)
        self.assertEqual([r.product_id for r in rows], ["BBB-USDC", "CCC-USDC", "AAA-USDC"])  # stables, -USD dropped
        self.assertEqual([r.signal for r in rows], ["SHORT", "LONG", "LONG"])
        picks = tb.candidates(rows, {"CCC-USDC"}, replace(tb.BotSettings(), skip=()))
        self.assertEqual([r.product_id for r in picks], ["AAA-USDC"])


class BotTests(unittest.TestCase):
    def _bot(self, client, live=True, rows=None):
        d = Path(tempfile.mkdtemp())
        rows = rows if rows is not None else [tb.Row("AAA-USDC", 9e6, 10, 9, 8), tb.Row("CCC-USDC", 5e6, 10, 9, 8)]
        lines = []
        bot = tb.TrendBot(tb.BotSettings(), tb.Account(client) if client else None, live, d / "s.json", d / "j.jsonl",
                          d / "KILL", scan_fn=lambda s: rows, out=lines.append)
        return bot, lines, d

    def test_fills_both_slots_with_brackets(self):
        c = FakeClient(usdc="70")
        bot, lines, _ = self._bot(c)
        bot.tick()
        self.assertEqual([p["product_id"] for p in c.posts], ["AAA-USDC", "CCC-USDC"])
        order = c.posts[0]
        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["order_configuration"], {"market_market_ioc": {"quote_size": "30.00"}})
        bracket = order["attached_order_configuration"]["trigger_bracket_gtc"]
        self.assertEqual(Decimal(bracket["limit_price"]), Decimal("11"))         # 10 + 3 / 3 coins
        self.assertEqual(Decimal(bracket["stop_trigger_price"]), Decimal("9"))
        bot.tick()
        self.assertEqual(len(c.posts), 2)                                         # both slots full
        self.assertIn("slots full", lines[-1])

    def test_low_usdc_and_existing_positions(self):
        c = FakeClient(usdc="20")
        bot, lines, _ = self._bot(c)
        bot.tick()
        self.assertEqual(c.posts, [])
        self.assertIn("too low", lines[-1])
        c = FakeClient(usdc="100", brackets=[{"product_id": "AAA-USDC", "order_type": "BRACKET"}])
        bot, _, _ = self._bot(c)
        bot.tick()
        self.assertEqual([p["product_id"] for p in c.posts], ["CCC-USDC"])     # one slot left, not the same coin

    def test_reports_an_exit(self):
        c = FakeClient(usdc="100")
        bot, lines, _ = self._bot(c, rows=[tb.Row("AAA-USDC", 9e6, 10, 9, 8)])
        bot.tick()
        c.brackets = []
        c.filled_sell = {"average_filled_price": "11", "filled_value": "33", "total_fees": "0.2"}
        bot.tick()
        self.assertTrue(any("EXIT AAA-USDC" in l and "+2.62" in l for l in lines), lines)

    def test_dry_run_and_kill_switch_send_nothing(self):
        c = FakeClient()
        bot, lines, d = self._bot(c, live=False)
        bot.tick()
        self.assertEqual(c.posts, [])
        self.assertIn("DRY RUN", lines[-1])
        bot, lines, d = self._bot(FakeClient())
        (d / "KILL").write_text("x")
        bot.tick()
        self.assertEqual(bot.account.client.posts, [])
        self.assertIn("Kill switch", lines[-1])

    def test_gates(self):
        cfg = load_config()
        with self.assertRaises(LiveTradingNotAllowed):
            tb.check_gates(replace(cfg, mode="paper"), True)
        with self.assertRaises(LiveTradingNotAllowed):
            tb.check_gates(replace(cfg, mode="live", env={}), True)
        with self.assertRaises(ValueError):
            tb.BotSettings.from_config(replace(cfg, trend_bot={"per_order_usdc": 500}))


if __name__ == "__main__":
    unittest.main()
