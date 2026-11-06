"""Offline tests for the intraday tools: Time Machine, setups, trade plan, ticket approval and alerts."""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from cbagent.daytrade import alerts, quiet, setups
from cbagent.daytrade import plan as tp
from cbagent.daytrade import timemachine as tm
from cbagent.models import Candle

DAY0 = 1_793_836_800  # 2026-11-05 00:00 UTC


def walk(n: int, start: float = 100.0, step_pct: float = 0.3, seed: int = 1, t0: int = DAY0 - 30 * 86400) -> list[Candle]:
    """A driftless random walk of 5-minute candles."""
    rng = random.Random(seed)
    out, price = [], start
    for i in range(n):
        o = price
        c = o * (1 + rng.gauss(0, step_pct / 100))
        h, l = max(o, c) * (1 + abs(rng.gauss(0, step_pct / 300))), min(o, c) * (1 - abs(rng.gauss(0, step_pct / 300)))
        out.append(Candle(t0 + i * 300, o, h, l, c, 100.0))
        price = c
    return out


def flat(n: int, price: float, t0: int, vol: float = 100.0) -> list[Candle]:
    return [Candle(t0 + i * 300, price, price * 1.001, price * 0.999, price, vol) for i in range(n)]


class TimeMachineTests(unittest.TestCase):
    candles = walk(30 * 288)

    def test_symmetric_bracket_on_a_driftless_walk_is_near_a_coin_flip(self):
        plan = tm.Plan("X-USDC", "long", 100, 98, 102, start=DAY0, hours=48)
        res = tm.run(plan, self.candles, runs=2000, seed=3, fee_pct=0)
        r = res.replays
        self.assertEqual(r.n, 2000)
        self.assertLess(abs(r.pct("target") - r.pct("stop")), 12)
        self.assertGreater(res.history.n, 1000)
        self.assertLess(abs(res.history.avg_r), 0.4)

    def test_same_seed_same_answer_and_fees_cost_r(self):
        plan = tm.Plan("X-USDC", "long", 100, 98, 102, start=DAY0, hours=12)
        a = tm.replay(plan, self.candles, 500, seed=9, fee_pct=0)
        b = tm.replay(plan, self.candles, 500, seed=9, fee_pct=0)
        c = tm.replay(plan, self.candles, 500, seed=9, fee_pct=0.5)
        self.assertEqual((a.target, a.stop, a.open), (b.target, b.stop, b.open))
        self.assertAlmostEqual(a.avg_r - c.avg_r, 2 * 0.005 * 100 / 2, places=6)  # 0.5R of fees on a 2% stop

    def test_a_close_target_and_far_stop_hit_the_target_far_more(self):
        plan = tm.Plan("X-USDC", "long", 100, 90, 100.5, start=DAY0, hours=24)
        r = tm.replay(plan, self.candles, 500, seed=1, fee_pct=0)
        self.assertGreater(r.pct("target"), 85)

    def test_short_mirrors_long(self):
        plan = tm.Plan("X-USDC", "short", 100, 110, 99.5, start=DAY0, hours=24)
        r = tm.replay(plan, self.candles, 500, seed=1, fee_pct=0)
        self.assertGreater(r.pct("target"), 85)

    def test_a_candle_touching_both_levels_counts_as_a_stop(self):
        plan = tm.Plan("X-USDC", "long", 100, 99, 101)
        t = tm.Tally()
        tm._walk(plan, 100, 99, 101, [(100, 102, 98, 100)], 0, t)
        self.assertEqual((t.stop, t.target, t.both_in_one_bar), (1, 0, 1))
        self.assertAlmostEqual(t.r_values[0], -1)

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        plan = tm.Plan("X-USDC", "long", 100, 99, 101)
        t = tm.Tally()
        tm._walk(plan, 100, 99, 101, [(100, 100.5, 99.5, 100), (97, 97.5, 96, 97)], 0, t)
        self.assertAlmostEqual(t.r_values[0], -3)

    def test_levels_on_the_wrong_side_are_refused(self):
        with self.assertRaises(ValueError):
            tm.Plan("X-USDC", "long", 100, 101, 105)
        with self.assertRaises(ValueError):
            tm.Plan("X-USDC", "short", 100, 98, 95)

    def test_report_mentions_both_answers(self):
        plan = tm.Plan("X-USDC", "long", 100, 98, 102, start=DAY0, hours=6)
        text = tm.report(tm.run(plan, self.candles, runs=300))
        self.assertIn("replays", text)
        self.assertIn("History", text)
        self.assertIn("not a forecast", text)


class SetupTests(unittest.TestCase):
    def test_range_break_long_then_expired(self):
        day = DAY0
        candles = flat(288, 100, day - 288 * 300)       # yesterday
        candles += flat(12, 100, day)                   # the first hour: range about 99.9 to 100.1
        candles += flat(5, 100.05, day + 3600)          # inside the range
        candles.append(Candle(day + 3600 + 5 * 300, 100.05, 100.6, 100.0, 100.5, 300))  # closes above
        hit = setups.check("X-USDC", candles, "range_break")
        self.assertEqual((hit.status, hit.side), ("triggered", "long"))
        self.assertAlmostEqual(hit.level, 100.1)
        later = candles + flat(5, 100.5, candles[-1].start + 300)
        self.assertEqual(setups.check("X-USDC", later, "range_break").status, "expired")

    def test_range_break_waits_while_the_range_forms_and_inside_it(self):
        candles = flat(288, 100, DAY0 - 288 * 300) + flat(6, 100, DAY0)
        self.assertEqual(setups.check("X-USDC", candles, "range_break").status, "waiting")
        candles = flat(288, 100, DAY0 - 288 * 300) + flat(20, 100, DAY0)
        hit = setups.check("X-USDC", candles, "range_break")
        self.assertEqual(hit.status, "waiting")
        self.assertIn("inside", hit.detail)

    def test_break_24h_short(self):
        candles = flat(300, 100, DAY0)
        candles.append(Candle(DAY0 + 300 * 300, 100, 100, 98, 98.5, 100))
        hit = setups.check("X-USDC", candles, "break_24h")
        self.assertEqual((hit.status, hit.side), ("triggered", "short"))

    def test_volume_spike(self):
        candles = flat(300, 100, DAY0)
        candles.append(Candle(DAY0 + 300 * 300, 100, 101.6, 100, 101.5, 500))
        hit = setups.check("X-USDC", candles, "volume_spike")
        self.assertEqual((hit.status, hit.side), ("triggered", "long"))
        self.assertAlmostEqual(hit.volume_x, 5)
        quiet = flat(300, 100, DAY0)
        self.assertEqual(setups.check("X-USDC", quiet, "volume_spike").status, "none")

    def test_rsi_bounce(self):
        prices = [100 - i * 0.5 for i in range(40)] + [83.5]
        candles = [Candle(DAY0 + i * 300, p, p + 0.1, p - 0.1, p, 100) for i, p in enumerate(prices)]
        hit = setups.check("X-USDC", candles, "rsi_bounce", fresh=3)
        self.assertEqual((hit.status, hit.side), ("triggered", "long"))

    def test_report_lists_hits(self):
        h = setups.Hit("X-USDC", "volume_spike", "triggered", "long", 101.5, 100, DAY0, "5.0x", 5.0)
        self.assertIn("X-USDC", setups.report([h], 10))


class PlanTests(unittest.TestCase):
    def test_loss_and_gain_rules_hit_their_dollar_amounts_with_fees(self):
        p = tp.make_plan("QNT-USDC", "long", 191.38, "loss:5", "gain:5", 75, fee_pct=0.5, now=DAY0)
        self.assertAlmostEqual(p.max_loss, 5, delta=0.02)
        self.assertAlmostEqual(p.max_gain, 5, delta=0.02)
        self.assertLess(p.stop, 191.38)
        self.assertGreater(p.target, 191.38)
        s = tp.make_plan("QNT-USDC", "short", 191.38, "loss:5", "gain:5", 75, fee_pct=0.5, now=DAY0)
        self.assertAlmostEqual(s.max_loss, 5, delta=0.02)
        self.assertGreater(s.stop, 191.38)

    def test_max_loss_cuts_the_spend(self):
        p = tp.make_plan("BTC-USDC", "long", 100, "pct:5", "R:2", 1000, max_loss=10, fee_pct=0)
        self.assertAlmostEqual(p.quote_size, 200, delta=0.01)
        self.assertAlmostEqual(p.target, 110)
        self.assertAlmostEqual(p.max_loss, 10, delta=0.01)

    def test_rounding_to_the_product_increments(self):
        p = tp.make_plan("X-USDC", "long", 1.23456, "pct:3", "pct:6", 75.555, price_increment="0.0001",
                         quote_increment="0.01")
        self.assertEqual(p.stop, 1.1975)
        self.assertEqual(p.quote_size, 75.55)

    def test_bad_rules_and_impossible_stops(self):
        with self.assertRaises(ValueError):
            tp.make_plan("X-USDC", "long", 100, "dollars:5", "R:1", 75)
        with self.assertRaises(ValueError):
            tp.make_plan("X-USDC", "long", 100, "loss:80", "R:1", 75)  # can't lose $80 on a $75 spend
        with self.assertRaises(ValueError):
            tp.make_plan("X-USDC", "long", 100, "atr:2", "R:1", 75)  # no ATR given

    def test_order_args_match_the_connector(self):
        p = tp.make_plan("QNT-USDC", "long", 191.38, "price:178.6", "price:204.2", 75, price_increment="0.01")
        self.assertEqual(tp.order_args(p), {"product_id": "QNT-USDC", "side": "BUY", "type": "market",
                                            "quote_size": "75.00", "attached_take_profit_price": "204.20",
                                            "attached_stop_loss_price": "178.60"})
        short = tp.make_plan("QNT-USDC", "short", 191.38, "pct:3", "pct:3", 75)
        self.assertIsNone(tp.order_args(short))
        self.assertIn("futures", tp.ticket(short))

    def test_checks_say_when_it_needs_a_yes(self):
        small = tp.make_plan("QNT-USDC", "long", 100, "pct:5", "pct:5", 75)
        self.assertIn("standing approval", tp.checks(small)[0])
        self.assertIn("already open", tp.checks(small, open_position=True)[0])
        big = tp.make_plan("QNT-USDC", "long", 100, "pct:5", "pct:5", 200)
        self.assertIn("explicit yes", tp.checks(big)[0])
        usd = tp.make_plan("QNT-USD", "long", 100, "pct:5", "pct:5", 50)
        self.assertIn("USD, not USDC", tp.checks(usd)[0])

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = tp.make_plan("QNT-USDC", "long", 100, "pct:5", "R:1", 75, now=DAY0)
            tp.save(p, Path(d))
            self.assertEqual(tp.load(Path(d)), p)


class ApprovalTests(unittest.TestCase):
    plan = tp.make_plan("QNT-USDC", "long", 191.38, "loss:5", "gain:5", 75, now=DAY0)

    def test_only_an_explicit_yes_for_this_ticket_counts(self):
        for reply in ("yes", "Yes QNT", "yes, place it", "y", "yes $75", "Approved"):
            self.assertTrue(tp.is_yes(reply, self.plan)[0], reply)
        for reply in ("sure", "ok", "yes to all", "yes but wait", "yes ETH", "yes $500", "not yet", "maybe yes",
                      "yes if it dips", "no"):
            self.assertFalse(tp.is_yes(reply, self.plan)[0], reply)

    def test_approve_records_every_reply_word_for_word(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(tp.approve(self.plan, "sure", Path(d))[0])
            self.assertFalse(tp.approved(self.plan, Path(d)))
            self.assertTrue(tp.approve(self.plan, "yes QNT", Path(d))[0])
            self.assertTrue(tp.approved(self.plan, Path(d)))
            lines = [json.loads(l) for l in (Path(d) / "daytrade" / "approvals.jsonl").read_text().splitlines()]
            self.assertEqual([l["reply"] for l in lines], ["sure", "yes QNT"])
            other = tp.make_plan("QNT-USDC", "long", 191.38, "loss:5", "gain:5", 70, now=DAY0)
            self.assertFalse(tp.approved(other, Path(d)))  # a yes covers one exact ticket


class AlertTests(unittest.TestCase):
    def test_alerts_ring_once(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            alerts.add(d, "btc-usdc", "above", 100, "breakout")
            alerts.add(d, "BTC-USDC", "below", 90)
            prices = {"BTC-USDC": 101.0}
            self.assertEqual(len(alerts.check(d, prices.__getitem__, now=DAY0)), 1)
            self.assertEqual(alerts.check(d, prices.__getitem__, now=DAY0), [])
            prices["BTC-USDC"] = 89
            self.assertIn("below 90", alerts.check(d, prices.__getitem__)[0])
            self.assertEqual(alerts.remove(d), 2)
            self.assertEqual(alerts.listing(d), "No alerts.")

    def test_a_failed_price_does_not_stop_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            alerts.add(d, "BAD-USDC", "above", 1)
            alerts.add(d, "BTC-USDC", "above", 1)

            def price(pid):
                if pid == "BAD-USDC":
                    raise RuntimeError("404")
                return 5.0
            rang = alerts.check(d, price)
            self.assertEqual(len(rang), 2)
            self.assertIn("unavailable", rang[0])
            self.assertFalse(alerts.load(d)[0]["fired"])


class QuietTests(unittest.TestCase):
    def test_first_run_of_the_day_then_only_new_keys(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.assertEqual(quiet.gate(d, ["a", "b"], now=DAY0 + 60), (["a", "b"], True))
            self.assertEqual(quiet.gate(d, ["a", "b"], now=DAY0 + 960), ([], False))
            self.assertEqual(quiet.gate(d, ["b", "c", "c"], now=DAY0 + 1860), (["c"], False))
            self.assertEqual(quiet.gate(d, [], now=DAY0 + 2760), ([], False))
            self.assertEqual(quiet.gate(d, ["a"], now=DAY0 + 86400 + 60), (["a"], True))  # a new UTC day

    def test_setup_keys_ignore_the_trigger_candle(self):
        a = setups.Hit("X-USDC", "break_24h", "triggered", "long", 101, 100, DAY0, "")
        b = setups.Hit("X-USDC", "break_24h", "triggered", "long", 103, 102, DAY0 + 3600, "")
        c = setups.Hit("X-USDC", "break_24h", "triggered", "short", 97, 98, DAY0 + 7200, "")
        self.assertEqual(setups.key(a), setups.key(b))
        self.assertNotEqual(setups.key(a), setups.key(c))


if __name__ == "__main__":
    unittest.main()
