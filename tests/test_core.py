"""Offline tests for risk limits, the paper broker, the strategy and the live-trading gates."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from cbagent.brokers.coinbase_live import CoinbaseLiveBroker, LiveTradingNotAllowed
from cbagent.brokers.paper import PaperBroker
from cbagent.config import LIVE_CONFIRM_PHRASE, PaperConfig, RiskConfig, load_config
from cbagent.engine import Engine, run_backtest
from cbagent.models import Candle, OrderRequest, Position
from cbagent.risk import Account, RiskManager
from cbagent.strategies import build_strategy

RISK = RiskConfig(
    max_order_quote=D("50"),
    max_position_quote=D("200"),
    max_total_exposure_quote=D("500"),
    max_daily_loss_quote=D("50"),
    max_drawdown_pct=D("10"),
    max_orders_per_day=3,
    kill_switch_file="KILL_SWITCH",
)
PAPER = PaperConfig(starting_cash=D("1000"), fee_pct=D("0.6"), slippage_pct=D("0.05"))
NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def buy(quote="50", product="BTC-USD", price="100"):
    return OrderRequest(product, "BUY", D(quote), D(price), "test")


def account(cash="1000", base="0", price="100", product="BTC-USD"):
    return Account(D(cash), {product: Position(D(base))}, {product: D(price)})


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.risk = RiskManager(RISK, self.tmp / "KILL_SWITCH", self.tmp / "risk.json")
        self.risk.update(D("1000"), NOW)

    def test_clamps_to_max_order(self):
        d = self.risk.check(buy("500"), account())
        self.assertTrue(d.approved)
        self.assertEqual(d.quote_size, D("50"))

    def test_position_cap(self):
        d = self.risk.check(buy("50"), account(base="1.8"))  # holding 180 of a 200 cap
        self.assertEqual(d.quote_size, D("20"))
        self.assertFalse(self.risk.check(buy("50"), account(base="2")).approved)

    def test_cash_cap(self):
        self.risk = RiskManager(RISK)
        self.risk.update(D("30"), NOW)
        self.assertEqual(self.risk.check(buy("50"), account(cash="30")).quote_size, D("30"))

    def test_kill_switch_file_blocks_everything(self):
        (self.tmp / "KILL_SWITCH").write_text("x")
        self.assertFalse(self.risk.check(buy(), account()).approved)
        sell = OrderRequest("BTC-USD", "SELL", D("10"), D("100"), "t")
        self.assertFalse(self.risk.check(sell, account(base="1")).approved)

    def test_drawdown_trips_kill_switch_and_writes_file(self):
        self.risk.update(D("899"), NOW)
        self.assertTrue(self.risk.kill_switch_active())
        self.assertTrue((self.tmp / "KILL_SWITCH").exists())
        self.risk.reset_kill_switch()
        self.assertFalse(self.risk.kill_switch_active())

    def test_daily_loss_blocks_buys_not_sells(self):
        self.risk.update(D("950"), NOW)
        self.assertFalse(self.risk.check(buy(), account(cash="900", base="0.5")).approved)
        sell = OrderRequest("BTC-USD", "SELL", D("50"), D("100"), "t")
        self.assertTrue(self.risk.check(sell, account(cash="900", base="0.5")).approved)

    def test_daily_counters_reset_next_day(self):
        self.risk.update(D("950"), NOW)
        self.risk.update(D("950"), NOW.replace(day=6))
        self.assertTrue(self.risk.check(buy(), account(cash="950")).approved)

    def test_order_count_limit(self):
        for _ in range(3):
            self.risk.record_order()
        self.assertFalse(self.risk.check(buy(), account()).approved)

    def test_state_survives_restart(self):
        self.risk.trip_kill_switch("test")
        (self.tmp / "KILL_SWITCH").unlink()
        again = RiskManager(RISK, self.tmp / "KILL_SWITCH", self.tmp / "risk.json")
        self.assertTrue(again.kill_switch_active())


class PaperBrokerTests(unittest.TestCase):
    def test_round_trip_costs_fees_and_slippage(self):
        b = PaperBroker(PAPER)
        b.market_order("BTC-USD", "BUY", D("100"), D("100"), 0, "t")
        self.assertEqual(b.cash(), D("900"))
        b.market_order("BTC-USD", "SELL", D("1000"), D("100"), 0, "t")
        self.assertEqual(b.positions()["BTC-USD"].base, 0)
        self.assertLess(b.cash(), D("1000"))
        self.assertGreater(b.cash(), D("998"))

    def test_state_persists(self):
        path = Path(tempfile.mkdtemp()) / "paper.json"
        PaperBroker(PAPER, path).market_order("ETH-USD", "BUY", D("40"), D("2000"), 0, "t")
        again = PaperBroker(PAPER, path)
        self.assertEqual(again.cash(), D("960"))
        self.assertGreater(again.positions()["ETH-USD"].base, 0)


def candles(closes):
    return [Candle(i * 3600, c, c, c, c, 1.0) for i, c in enumerate(closes)]


class StrategyAndBacktestTests(unittest.TestCase):
    def setUp(self):
        self.strategy = build_strategy({"name": "sma_crossover", "fast": 3, "slow": 6, "order_quote": 50})

    def test_crossover_signals(self):
        up = [10.0] * 7 + [20.0]
        self.assertEqual(self.strategy.on_candles("X", candles(up), D("0")).action, "buy")
        down = [10.0] * 7 + [1.0]
        self.assertEqual(self.strategy.on_candles("X", candles(down), D("1")).action, "sell")
        self.assertEqual(self.strategy.on_candles("X", candles([10.0] * 8), D("0")).action, "hold")

    def test_backtest_trades_and_respects_limits(self):
        closes = [100.0] * 10 + [110.0 + i for i in range(10)] + [90.0 - i for i in range(10)]
        engine = Engine(self.strategy, RiskManager(RISK), PaperBroker(PAPER), log=lambda _m: None)
        result = run_backtest(engine, {"BTC-USD": candles(closes)})
        self.assertEqual([f.side for f in result.fills], ["BUY", "SELL"])
        self.assertLessEqual(result.fills[0].base_size * result.fills[0].price, D("50"))


class IndicatorAndConfluenceTests(unittest.TestCase):
    def test_rsi_extremes(self):
        from cbagent.indicators import rsi
        self.assertEqual(rsi([float(i) for i in range(50)]), 100.0)
        self.assertLess(rsi([float(50 - i) for i in range(50)]), 1)

    def test_confluence_buys_on_agreement_and_holds_otherwise(self):
        s = build_strategy({"name": "confluence", "order_quote": 50})
        flat = candles([100.0] * (s.warmup + 5))
        self.assertEqual(s.on_candles("X", flat, D("0")).action, "hold")
        rising = candles([100.0 + i * 0.5 for i in range(s.warmup + 5)])
        sig = s.on_candles("X", rising, D("1"))
        self.assertIn("score", sig.reason)


class AtrStopTests(unittest.TestCase):
    def test_stop_sells_after_drop_from_high(self):
        from cbagent.exits import AtrTrailingStop
        stop = AtrTrailingStop(period=3, mult=2)
        bars = [Candle(i, c, c + 1, c - 1, c, 1) for i, c in enumerate([100.0, 101, 102, 103, 104])]
        stop.on_entry("X", 100)
        self.assertIsNone(stop.check("X", bars))
        bars.append(Candle(5, 104, 104, 95, 95, 1))
        self.assertIn("ATR stop", stop.check("X", bars))

    def test_engine_uses_stop_over_strategy(self):
        from cbagent.exits import AtrTrailingStop
        closes = [100.0] * 10 + [110.0 + i for i in range(10)] + [129.0, 100.0]
        strategy = build_strategy({"name": "sma_crossover", "fast": 3, "slow": 6, "order_quote": 50})
        engine = Engine(strategy, RiskManager(RISK), PaperBroker(PAPER), log=lambda _m: None, stop=AtrTrailingStop(3, 1))
        result = run_backtest(engine, {"BTC-USD": candles(closes)})
        self.assertTrue(any(f.reason.startswith("ATR stop") for f in result.fills))


def daily(closes, start=1767225600):  # 2026-01-01, a Thursday
    return [Candle(start + i * 86400, c, c, c, c, 1.0) for i, c in enumerate(closes)]


class DailyStrategyTests(unittest.TestCase):
    def test_trend_buys_above_band_and_sells_below(self):
        s = build_strategy({"name": "trend", "period": 5, "band_pct": 2, "order_quote": 100})
        self.assertEqual(s.on_candles("X", daily([100.0] * 4 + [110.0]), D("0")).action, "buy")
        self.assertEqual(s.on_candles("X", daily([100.0] * 4 + [101.0]), D("0")).action, "hold")
        self.assertEqual(s.on_candles("X", daily([100.0] * 4 + [90.0]), D("1")).action, "sell")

    def test_dca_buys_on_weekday_and_doubles_on_dips(self):
        s = build_strategy({"name": "dca", "weekday": "monday", "dip_lookback": 3, "order_quote": 10})
        monday = daily([100.0] * 5)  # last candle is 2026-01-05, a Monday
        self.assertEqual(s.on_candles("X", monday, D("0")).quote_size, D("10"))
        dip = daily([100.0] * 4 + [80.0])
        self.assertEqual(s.on_candles("X", dip, D("0")).quote_size, D("20"))
        self.assertEqual(s.on_candles("X", daily([100.0] * 4), D("0")).action, "hold")

    def test_engine_acts_once_per_candle(self):
        s = build_strategy({"name": "dca", "dip_lookback": 3, "order_quote": 10})
        engine = Engine(s, RiskManager(RISK), PaperBroker(PAPER), log=lambda _m: None)
        bars = daily([100.0] * 5)
        self.assertEqual(len(engine.tick({"BTC-USD": bars}, bars[-1].start)), 1)
        self.assertEqual(len(engine.tick({"BTC-USD": bars}, bars[-1].start + 300)), 0)


class DrawdownFlattenTests(unittest.TestCase):
    def run_crash(self, on_drawdown):
        risk = replace(RISK, max_order_quote=D("500"), max_position_quote=D("1000"),
                       max_total_exposure_quote=D("1000"), max_daily_loss_quote=D("1000"), on_drawdown=on_drawdown)
        strategy = build_strategy({"name": "dca", "dip_lookback": 3, "order_quote": 500, "weekday": "sunday"})
        broker = PaperBroker(PAPER)
        engine = Engine(strategy, RiskManager(risk), broker, log=lambda _m: None)
        run_backtest(engine, {"BTC-USD": daily([100.0] * 3 + [100.0, 50.0, 40.0])})
        return engine, broker

    def test_flatten_sells_everything_once(self):
        engine, broker = self.run_crash("flatten")
        self.assertEqual(broker.positions()["BTC-USD"].base, 0)
        self.assertTrue(engine.risk.kill_switch_active())
        self.assertFalse(engine.risk.state.flatten_pending)
        self.assertTrue(broker.fills[-1].reason.startswith("kill switch flatten"))

    def test_freeze_keeps_holdings(self):
        engine, broker = self.run_crash("freeze")
        self.assertGreater(broker.positions()["BTC-USD"].base, 0)
        self.assertTrue(engine.risk.kill_switch_active())

    def test_cooloff_restarts_auto_trip_at_half_size(self):
        risk = RiskManager(replace(RISK, cooloff_days=30, reentry_size_pct=D("50")))
        risk.update(D("1000"), NOW)
        risk.update(D("800"), NOW)
        self.assertTrue(risk.kill_switch_active())
        risk.flatten_done()  # the engine does this after selling
        risk.update(D("800"), NOW.replace(day=20))
        self.assertTrue(risk.kill_switch_active())
        later = NOW.replace(month=12, day=6)
        risk.update(D("800"), later)
        self.assertFalse(risk.kill_switch_active())
        self.assertEqual(risk.check(buy("50"), account(cash="800")).quote_size, D("25"))
        risk.update(D("800"), later.replace(year=2027, month=1, day=10))
        self.assertEqual(risk.check(buy("50"), account(cash="800")).quote_size, D("50"))

    def test_manual_kill_never_restarts(self):
        risk = RiskManager(replace(RISK, cooloff_days=1))
        risk.update(D("1000"), NOW)
        risk.trip_kill_switch("manual")
        risk.update(D("1000"), NOW.replace(month=12))
        self.assertTrue(risk.kill_switch_active())

    def test_manual_kill_does_not_flatten(self):
        risk = RiskManager(RISK)
        risk.trip_kill_switch("manual")
        self.assertFalse(risk.state.flatten_pending)


class _Resp(dict):
    def to_dict(self):
        return dict(self)


class FakeClient:
    """Stands in for coinbase.rest.RESTClient; fills every market order at 100000 with a 0.6% fee."""

    def __init__(self, can_transfer=False, usd="100", btc="0"):
        self.can_transfer = can_transfer
        self.bal = {"USD": D(usd), "BTC": D(btc), "ETH": D("0")}
        self.orders, self.fills = [], {}

    def get_api_key_permissions(self):
        return _Resp(can_view=True, can_trade=True, can_transfer=self.can_transfer, portfolio_type="DEFAULT")

    def get_accounts(self, limit=250):
        return _Resp(accounts=[{"currency": c, "available_balance": {"value": str(v)}} for c, v in self.bal.items()])

    def get_product(self, product_id):
        return _Resp(base_increment="0.00000001")

    def _fill(self, side, base):
        oid = f"o{len(self.orders)}"
        price = D("100000")
        self.fills[oid] = (base, price, base * price * D("0.006"))
        return _Resp(success=True, success_response={"order_id": oid})

    def market_order_buy(self, client_order_id, product_id, quote_size):
        self.orders.append(("BUY", quote_size))
        base = D(quote_size) / D("100000")
        self.bal["BTC"] += base
        return self._fill("BUY", base)

    def market_order_sell(self, client_order_id, product_id, base_size):
        self.orders.append(("SELL", base_size))
        self.bal["BTC"] -= D(base_size)
        return self._fill("SELL", D(base_size))

    def get_order(self, order_id):
        base, price, fee = self.fills[order_id]
        return _Resp(order={"status": "FILLED", "filled_size": str(base), "average_filled_price": str(price),
                            "total_fees": str(fee)})


class ScannerTests(unittest.TestCase):
    def test_measure_uptrend_scores_above_downtrend(self):
        from cbagent.scanner import measure
        up = daily([100.0 * 1.01 ** i for i in range(220)])
        down = daily([100.0 * 0.99 ** i for i in range(220)])
        btc = daily([100.0] * 220)
        a = measure({"product_id": "UP-USDC"}, up, btc)
        b = measure({"product_id": "DN-USDC"}, down, btc)
        self.assertTrue(a.above_sma200 and a.sma50_over_200)
        self.assertGreater(a.trend_score, b.trend_score)
        self.assertGreater(a.vs_btc_30d, 0)
        self.assertIsNone(measure({"product_id": "NEW-USDC"}, up[:30], btc))


class NewsTests(unittest.TestCase):
    def test_matches_tickers_and_names_not_substrings(self):
        from cbagent.news import Article, match
        when = datetime(2026, 11, 5, tzinfo=timezone.utc)
        arts = [Article("x", t, "", when, "") for t in
                ["Arbitrum DAO votes on grant", "$HBAR jumps 10%", "Quant funds pile into bitcoin", "BARB token launches"]]
        found = match(arts, ["ARB", "HBAR", "QNT", "BTC"])
        self.assertEqual([a.title for a in found["ARB"]], ["Arbitrum DAO votes on grant"])
        self.assertEqual(len(found["HBAR"]), 1)
        self.assertEqual(found["QNT"], [])
        self.assertEqual(len(found["BTC"]), 1)


class LabTests(unittest.TestCase):
    def test_rotation_picks_the_rising_coin_and_pays_fees(self):
        from cbagent.lab import DAY, Rotation, simulate
        days = [1700000000 + i * DAY for i in range(400)]
        closes = {
            "BTC-USDC": {t: 100.0 for t in days},
            "UP-USDC": {t: 100.0 * 1.003 ** i * (1.01 if i % 2 else 1.0) for i, t in enumerate(days)},
            "FLAT-USDC": {t: 100.0 * (1.01 if i % 2 else 1.0) for i, t in enumerate(days)},
        }
        cfg = Rotation(top_n=1, lookback=30, btc_filter=False, fee_pct=0.5)
        ret, dd = simulate(closes, cfg, days[200], days[-1] + 1, "BTC-USDC")
        up = (closes["UP-USDC"][days[-1]] / closes["UP-USDC"][days[200]] - 1) * 100
        self.assertGreater(ret, 0)
        self.assertLess(ret, up + 2)  # can't beat simply holding the winner by much once fees are paid
        flat_btc = simulate(closes, Rotation(top_n=1, lookback=30, btc_filter=True), days[200], days[-1] + 1, "BTC-USDC")
        self.assertEqual(flat_btc[0], 0.0)  # BTC never above its 200-day average (flat) -> stays in cash


class PaperRotationTests(unittest.TestCase):
    def test_rebalances_into_targets_then_rotates_out(self):
        from unittest import mock
        from cbagent import rotation_paper as rp
        book = rp.Book("t", "ONE_DAY", top_n=2, lookback=3, vol_bars=3, trend_bars=3, btc_filter_bars=0,
                       rebalance_hours=24, description="test")
        r = rp.PaperRotation(book, Path(tempfile.mkdtemp()))
        prices = {"A-USDC": 10.0, "B-USDC": 20.0, "C-USDC": 5.0}
        with mock.patch.object(rp, "pick", return_value=(prices, {"A-USDC": 0.5, "B-USDC": 0.5}, "x")):
            r.run(now=1_000_000)
        self.assertEqual(set(r.state["holdings"]), {"A-USDC", "B-USDC"})
        self.assertAlmostEqual(r.equity(prices), 1000 * (1 - 0.0055), delta=1)
        with mock.patch.object(rp, "pick", return_value=(prices, {"A-USDC": 0.5, "C-USDC": 0.5}, "x")),              mock.patch.object(r, "_prices", return_value=prices):
            r.run(now=1_000_000 + 3600)  # not due yet: no trades
            self.assertIn("B-USDC", r.state["holdings"])
            r.run(now=1_000_000 + 86400)
        self.assertEqual(set(r.state["holdings"]), {"A-USDC", "C-USDC"})
        self.assertGreater(r.state["cash"], -0.01)
        self.assertEqual(r.state["prices"], {"A-USDC": 10.0, "C-USDC": 5.0})

    def test_score_needs_uptrend_and_positive_momentum(self):
        from cbagent.rotation_paper import BOOKS, score
        book = BOOKS["daily"]
        self.assertIsNotNone(score([100 * 1.01 ** i + (i % 2) for i in range(60)], book))
        self.assertIsNone(score([100 * 0.99 ** i + (i % 2) for i in range(60)], book))


class DashboardTests(unittest.TestCase):
    def _data(self):
        import json
        d = Path(tempfile.mkdtemp())
        rows = [
            {"ts": "2026-11-05T05:00:00+00:00", "book": "fast", "event": "fill", "product": "A-USDC", "side": "BUY", "qty": 10, "price": 10, "fee": 0.5},
            {"ts": "2026-11-05T05:00:00+00:00", "book": "fast", "event": "rebalance", "note": "picks: A-USDC (1.00)", "equity": 999.5},
            {"ts": "2026-11-05T09:00:00+00:00", "book": "fast", "event": "fill", "product": "A-USDC", "side": "SELL", "qty": 5, "price": 12, "fee": 0.3},
            {"ts": "2026-11-05T09:00:00+00:00", "book": "fast", "event": "rebalance", "note": "picks: </script>", "equity": 1009},
            {"ts": "2026-11-05T10:00:00+00:00", "book": "fast", "event": "error", "error": "boom"},
        ]
        (d / "journal_rotation.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        state = {"cash": 959.7, "holdings": {"A-USDC": 5}, "prices": {"A-USDC": 14.0}, "fees": 0.8,
                 "last_rebalance": 1793869200, "equity": [[1793854800, 999.5], [1793869200, 1019.7]]}
        (d / "rotation_fast.json").write_text(json.dumps(state))
        return d

    def test_cost_basis_pnl_and_trades(self):
        from cbagent import dashboard
        data = dashboard.collect(self._data(), now=1793872800)
        b = data["books"][0]
        self.assertEqual(b["name"], "fast")
        self.assertAlmostEqual(b["equity"], 959.7 + 70)
        self.assertAlmostEqual(b["holdings"][0]["cost"], 50.25)          # half of 100.5
        self.assertAlmostEqual(b["realized"], 60 - 0.3 - 50.25)
        self.assertAlmostEqual(b["unrealized"], 70 - 50.25)
        self.assertEqual([r["trades"] for r in data["rebalances"]], [1, 1])
        self.assertEqual(len(data["fills"]), 2)
        self.assertEqual(data["errors"][0]["error"], "boom")
        self.assertNotIn("picks: </script>", dashboard.render(data))

    def test_health_counts_missed_hours_and_failures(self):
        from cbagent import dashboard
        h0 = 1793869200 - 1793869200 % 3600
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        runs = [{"run_number": n, "event": "schedule", "status": "completed", "conclusion": c,
                 "run_started_at": iso(h0 + m * 60), "updated_at": iso(h0 + m * 60 + 30)}
                for n, m, c in [(1, 7, "success"), (2, 27, "success"), (3, 67, "success"), (4, 247, "failure")]]
        marks = [h0 + 7 * 60 + 20, h0 + 67 * 60 + 20]   # runs 1 and 3 ran the books, run 2 found nothing due
        parsed = dashboard._runs({"workflow_runs": runs})
        h = dashboard.health(parsed, marks, now=h0 + 5 * 3600 + 60)
        self.assertEqual([r["ran_books"] for r in reversed(parsed)], [True, False, True, False])
        self.assertEqual(h["level"], "critical")      # last run failed
        self.assertEqual(h["hours_with_run"], 2)
        self.assertEqual(h["missed_24h"], 3)          # hours 2 and 3 missed, hour 4 failed
        self.assertEqual([c["state"] for c in h["cells"][-6:]], ["ok", "ok", "missed", "missed", "fail", "pending"])
        self.assertEqual(h["median_duration"], 30)
        ok = dashboard.health(dashboard._runs({"workflow_runs": runs[:3]}), marks, now=h0 + 2 * 3600 + 60)
        self.assertEqual(ok["level"], "good")

    def test_due_runs_books_once_an_hour_and_brief_once_a_day(self):
        import json
        from cbagent.rotation_paper import due
        d = Path(tempfile.mkdtemp())
        self.assertTrue(due(d, now=1793869200)["rotate"])
        (d / "rotation_fast.json").write_text(json.dumps({"equity": [[1793869200, 1000]], "last_run": 1793869200}))
        self.assertFalse(due(d, now=1793869200 + 20 * 60)["rotate"])
        self.assertTrue(due(d, now=1793869200 + 46 * 60)["rotate"])
        at_15 = int(datetime(2026, 11, 5, 15, 7, tzinfo=timezone.utc).timestamp())
        self.assertFalse(due(d, now=at_15 - 3600)["brief"])
        self.assertTrue(due(d, now=at_15)["brief"])
        (d / "briefs").mkdir()
        (d / "briefs" / "brief_2026-11-05.md").write_text("x")
        self.assertFalse(due(d, now=at_15)["brief"])


class LiveGateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_paper_mode_refuses_live(self):
        with self.assertRaises(LiveTradingNotAllowed):
            CoinbaseLiveBroker(replace(self.cfg, mode="paper"), live_flag=True)

    def test_needs_flag(self):
        cfg = replace(self.cfg, mode="live", env={"CBAGENT_LIVE_CONFIRM": LIVE_CONFIRM_PHRASE})
        with self.assertRaises(LiveTradingNotAllowed):
            CoinbaseLiveBroker(cfg, live_flag=False)

    def test_needs_confirm_phrase(self):
        cfg = replace(self.cfg, mode="live", env={"COINBASE_API_KEY": "k", "COINBASE_API_SECRET": "s"})
        with self.assertRaises(LiveTradingNotAllowed):
            CoinbaseLiveBroker(cfg, live_flag=True)

    def live_cfg(self):
        tmp = Path(tempfile.mkdtemp())
        return replace(self.cfg, mode="live", data_dir=tmp, live={"allocation_quote": 100},
                       env={"CBAGENT_LIVE_CONFIRM": LIVE_CONFIRM_PHRASE})

    def test_refuses_key_that_can_transfer(self):
        with self.assertRaises(LiveTradingNotAllowed):
            CoinbaseLiveBroker(self.live_cfg(), live_flag=True, client=FakeClient(can_transfer=True))

    def test_allocation_caps_cash_and_ignores_other_holdings(self):
        client = FakeClient(usd="5000", btc="3")
        b = CoinbaseLiveBroker(self.live_cfg(), live_flag=True, client=client)
        self.assertEqual(b.cash(), D("100"))
        self.assertEqual(b.positions()["BTC-USD"].base, 0)  # the 3 BTC weren't bought by the bot
        fill = b.market_order("BTC-USD", "BUY", D("60"), D("100000"), 0, "t")
        self.assertEqual(fill.base_size, D("0.0006"))
        self.assertEqual(b.cash(), D("40") - D("0.36"))
        self.assertEqual(b.positions()["BTC-USD"].base, D("0.0006"))
        b.market_order("BTC-USD", "SELL", D("1000"), D("100000"), 0, "t")
        self.assertEqual(client.orders[-1], ("SELL", "0.00060000"))
        self.assertEqual(b.positions()["BTC-USD"].base, 0)

    def test_default_config_is_paper(self):
        self.assertEqual(self.cfg.mode, "paper")


if __name__ == "__main__":
    unittest.main()
