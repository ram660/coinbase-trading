"""Offline tests for the stock paper book: the trend rule, picking, market hours, the book run and the backtest."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cbagent import stocks_paper as sp

DAY = 86400
T0 = 1_700_000_000 - 1_700_000_000 % DAY + 14 * 3600 + 1800   # a 14:30 UTC session start


def series(n: int, start: float, step: float) -> list[float]:
    return [start * (1 + step) ** i for i in range(n)]


def daily(symbol: str, closes: list[float], volume: float = 1e6, price: float | None = None,
          session_open: bool = True) -> sp.Daily:
    times = [T0 + i * DAY for i in range(len(closes))]
    session = (times[-1], times[-1] + 6 * 3600 + 1800)
    if not session_open:
        session = (times[-1] - DAY, times[-1] - DAY + 6 * 3600 + 1800)
    return sp.Daily(symbol, times, closes, closes, [volume] * len(closes), price=price or closes[-1], session=session)


class TrendRuleTests(unittest.TestCase):
    def test_qualifies_only_above_both_averages(self):
        up = series(250, 100, 0.002)
        self.assertTrue(sp.qualifies(up[-1], up))
        self.assertFalse(sp.qualifies(up[-1] * 0.8, up))          # dropped below the 50-day
        self.assertFalse(sp.qualifies(1.0, series(100, 1, 0.01)))  # not enough history for the 200-day

    def test_choose_ranks_by_dollar_volume_keeps_holdings_and_respects_spy(self):
        book = replace(sp.BOOK, top_n=2)
        up = series(250, 100, 0.002)
        down = series(250, 100, -0.002)
        rows = {"A": (up[-1], up, [1e6] * 250), "B": (up[-1], up, [3e6] * 250),
                "C": (up[-1], up, [2e6] * 250), "D": (down[-1], down, [9e9] * 250)}
        target, ranked = sp.choose(rows, book, (up[-1], up))
        self.assertEqual(ranked, ["B", "C", "A"])                  # D is in a downtrend however big its volume
        self.assertEqual(target, {"B": 0.5, "C": 0.5})
        target, _ = sp.choose(rows, book, (up[-1], up), held={"A"})
        self.assertEqual(set(target), {"A", "B"})                  # A keeps its place while it qualifies
        self.assertEqual(sp.choose(rows, book, (down[-1], down))[0], {})  # SPY below its 200-day: cash

    def test_todays_bar_is_dropped_while_the_session_runs(self):
        d = daily("X", series(10, 100, 0.01))
        self.assertEqual(sp.closed_bars(d, d.session[0] + 60), 9)
        self.assertEqual(sp.closed_bars(d, d.session[1] + 60), 10)
        self.assertTrue(sp.market_open(d, d.session[0] + 60))
        self.assertFalse(sp.market_open(d, d.session[1] + 60))


class PaperStocksTests(unittest.TestCase):
    def _book(self, session_open: bool, now_offset: int = 3600):
        up, down = series(260, 100, 0.002), series(260, 100, -0.002)
        data = {"SPY": daily("SPY", up, 5e6, session_open=session_open),
                "AAA": daily("AAA", up, 2e6, session_open=session_open),
                "BBB": daily("BBB", down, 9e6, session_open=session_open)}
        book = replace(sp.BOOK, universe=("SPY", "AAA", "BBB"), top_n=2)
        now = data["SPY"].session[0] + now_offset
        d = Path(tempfile.mkdtemp())
        return sp.PaperStocks(d, book, fetch=lambda s: data[s], clock=lambda: now), d, now

    def test_trades_in_market_hours_and_tracks_spy(self):
        b, d, now = self._book(session_open=True)
        out = b.run()
        self.assertEqual(set(b.state["holdings"]), {"SPY", "AAA"})
        self.assertGreater(b.state["cash"], -0.01)
        self.assertIn("SPY held since start", out)
        self.assertEqual(b.state["benchmark"]["symbol"], "SPY")
        self.assertTrue((d / "rotation_stocks.json").exists())
        from cbagent import dashboard
        book = dashboard.collect(d, now=int(now), fallback_prices=None)["books"][0]
        self.assertEqual(book["name"], "stocks")
        self.assertIn("50- and 200-day", book["description"])

    def test_market_closed_only_marks(self):
        b, _, _ = self._book(session_open=False, now_offset=12 * 3600)   # after that session closed
        out = b.run()
        self.assertEqual(b.state["holdings"], {})
        self.assertEqual(b.state["cash"], 1000)
        self.assertEqual(len(b.state["equity"]), 1)
        self.assertNotIn("rebalanced", out)

    def test_backtest_beats_cash_on_a_rising_market_and_reports(self):
        n = 400
        up = series(n, 100, 0.001)
        hist = {"SPY": daily("SPY", up), "AAA": daily("AAA", series(n, 50, 0.002), 3e6),
                "BBB": daily("BBB", series(n, 80, -0.001), 9e6)}
        book = replace(sp.BOOK, universe=("SPY", "AAA", "BBB"), top_n=2)
        r = sp.backtest(hist, book)
        self.assertGreater(r["book"]["end"], 1000)
        self.assertGreater(r["trades"], 0)
        self.assertGreater(r["spy"]["cagr_pct"], 0)
        report = sp.backtest_report(hist, {"test": book})
        self.assertIn("SPY bought and held", report)
        self.assertIn("survivorship", report)


if __name__ == "__main__":
    unittest.main()
