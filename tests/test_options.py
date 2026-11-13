"""Offline tests for the options-idea screen and the Telegram sender."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cbagent.models import Candle
from cbagent.options import screen, stock_data, telegram

DAY = 86400


def trend(n: int, start: float, daily_pct: float) -> list[Candle]:
    out, p = [], start
    for i in range(n):
        wiggle = 1 + (0.004 if i % 2 else -0.004)
        c = p * (1 + daily_pct / 100) * wiggle
        out.append(Candle(i * DAY, p, max(p, c) * 1.002, min(p, c) * 0.998, c, 1_000_000))
        p = c / wiggle
    return out


class RegimeTest(unittest.TestCase):
    def test_rule_and_hysteresis(self):
        self.assertEqual(screen.regime(110, 100, 105), screen.BULL)
        self.assertEqual(screen.regime(90, 100, 95), screen.BEAR)
        self.assertEqual(screen.regime(102, 100, 105), screen.FLAT)
        # Flat needs to clear the far average by the buffer; an existing idea holds until it crosses back.
        self.assertEqual(screen.regime(105.5, 100, 105, screen.FLAT, 1.0), screen.FLAT)
        self.assertEqual(screen.regime(105.5, 100, 105, screen.BULL, 1.0), screen.BULL)
        self.assertEqual(screen.regime(104.9, 100, 105, screen.BULL, 1.0), screen.FLAT)
        self.assertEqual(screen.regime(99.5, 100, 105, screen.BEAR, 1.0), screen.BEAR)

    def test_read_needs_history_and_describes(self):
        up = trend(260, 50, 0.3)
        r = screen.read("XYZ", "stock", up, up[-1].close * 1.01)
        self.assertEqual(r.regime, screen.BULL)
        self.assertGreater(r.move30, 0)
        text = screen.describe(r)
        self.assertIn("CALLS", text)
        self.assertIn("ends if the price goes back below", text)
        with self.assertRaises(ValueError):
            screen.read("XYZ", "stock", up[:150], 1.0)


class WatchTest(unittest.TestCase):
    def test_first_run_quiet_then_change_alert_then_daily_digest(self):
        data = {"UP": trend(260, 50, 0.3), "DOWN": trend(260, 50, -0.3)}
        price = {"UP": data["UP"][-1].close, "DOWN": data["DOWN"][-1].close}
        fetch = lambda sym, kind: (data[sym], price[sym])
        settings = {"stocks": ["UP", "DOWN"], "crypto": [], "min_dollar_volume": 0, "digest_hour_utc": 14}
        t0 = 1_794_000_000 - 1_794_000_000 % DAY + 10 * 3600  # 10:00 UTC
        with tempfile.TemporaryDirectory() as d:
            first = screen.watch(Path(d), settings, fetch, now=t0)
            self.assertEqual(len(first), 1)          # just the digest, no "new idea" spam
            self.assertIn("CALLS (1)", first[0])
            self.assertIn("PUTS (1)", first[0])
            self.assertEqual(screen.watch(Path(d), settings, fetch, now=t0 + 1800), [])
            price["UP"] = data["UP"][-1].close * 0.5     # UP falls below both averages
            msgs = screen.watch(Path(d), settings, fetch, now=t0 + 3600)
            self.assertEqual(len(msgs), 1)
            self.assertIn("UP: new PUTS idea (was CALLS)", msgs[0])
            self.assertEqual(screen.watch(Path(d), settings, fetch, now=t0 + 7200), [])
            msgs = screen.watch(Path(d), settings, fetch, now=t0 + 3600 * 5)  # 15:00: digest due once
            self.assertEqual(len(msgs), 1)
            self.assertIn("Options ideas", msgs[0])
            self.assertEqual(screen.watch(Path(d), settings, fetch, now=t0 + 3600 * 6), [])

    def test_failed_symbol_keeps_prior_state(self):
        data = trend(260, 50, 0.3)
        ok = {"v": True}

        def fetch(sym, kind):
            if not ok["v"]:
                raise OSError("down")
            return data, data[-1].close

        settings = {"stocks": ["UP"], "crypto": [], "digest_hour_utc": 23}
        with tempfile.TemporaryDirectory() as d:
            screen.watch(Path(d), settings, fetch, now=1_794_000_000)
            ok["v"] = False
            screen.watch(Path(d), settings, fetch, now=1_794_000_600)
            ok["v"] = True
            self.assertEqual(screen.watch(Path(d), settings, fetch, now=1_794_001_200), [])


class StockDataTest(unittest.TestCase):
    def payload(self, market_time: int):
        ts = [1_000_000 + i * DAY for i in range(3)]
        return {"chart": {"result": [{
            "meta": {"regularMarketPrice": 12.5, "regularMarketTime": market_time,
                     "currentTradingPeriod": {"regular": {"start": ts[-1], "end": ts[-1] + 23400}}},
            "timestamp": ts,
            "indicators": {"quote": [{"open": [10, 11, None], "high": [11, 12, 13], "low": [9, 10, 11],
                                      "close": [10.5, 11.5, 12.5], "volume": [5, 6, 7]}]}}]}}

    def test_drops_forming_bar_only_while_open(self):
        candles, price, is_open = stock_data.parse_chart(self.payload(1_000_000 + 2 * DAY + 600))
        self.assertTrue(is_open)
        self.assertEqual(price, 12.5)
        self.assertEqual([c.close for c in candles], [10.5, 11.5])   # bar with a None open is skipped anyway
        p = self.payload(1_000_000 + 2 * DAY + 23400)
        p["chart"]["result"][0]["indicators"]["quote"][0]["open"][2] = 12
        candles, _, is_open = stock_data.parse_chart(p)
        self.assertFalse(is_open)
        self.assertEqual(len(candles), 3)


class TelegramTest(unittest.TestCase):
    def test_send_splits_and_needs_credentials(self):
        calls = []
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"}
        n = telegram.send("\n".join(["x" * 100] * 100), env, call=lambda *a: calls.append(a) or {})
        self.assertEqual(n, len(calls))
        self.assertGreater(n, 1)
        self.assertTrue(all(len(c[2]["text"]) <= telegram.MAX_LEN for c in calls))
        self.assertEqual(calls[0][2]["chat_id"], "42")
        with self.assertRaises(RuntimeError):
            telegram.send("hi", {"TELEGRAM_BOT_TOKEN": "t"}, call=lambda *a: {})

    def test_credentials_cleanup(self):
        tok, chat = telegram.credentials({"TELEGRAM_BOT_TOKEN": ' bot123:ABC\n', "TELEGRAM_CHAT_ID": '"42" '})
        self.assertEqual((tok, chat), ("123:ABC", "42"))

    def test_find_chat_ids(self):
        upd = {"result": [{"message": {"chat": {"id": 7, "first_name": "Ram"}}},
                          {"message": {"chat": {"id": 7, "first_name": "Ram"}}}]}
        self.assertEqual(telegram.find_chat_ids({"TELEGRAM_BOT_TOKEN": "t"}, call=lambda *a: upd), ["7  Ram"])


if __name__ == "__main__":
    unittest.main()
