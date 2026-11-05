"""Portfolio report: what you hold, how concentrated and how risky it is, measured on daily Coinbase data.

Quantities come from [portfolio.quantities] in config/local.toml (git-ignored). Read-only, public prices.
Descriptive only: it shows exposures and history, it doesn't say what to do.
"""
from __future__ import annotations

import math
import statistics

from .market_data import fetch_candles
from .scanner import HISTORY_DAYS, _corr, _daily_returns

STRESS_BTC_MOVES = (-30, -20, -10, 10)


def _beta(coin: list[float], btc: list[float]) -> float | None:
    n = min(len(coin), len(btc))
    if n < 30:
        return None
    coin, btc = coin[-n:], btc[-n:]
    var = statistics.pvariance(btc)
    if not var:
        return None
    mc, mb = statistics.fmean(coin), statistics.fmean(btc)
    return sum((c - mc) * (b - mb) for c, b in zip(coin, btc)) / n / var


def _max_drawdown(values: list[float]) -> float:
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst * 100


def build(quantities: dict[str, float], quote: str = "USDC") -> str:
    btc = fetch_candles(f"BTC-{quote}", "ONE_DAY", HISTORY_DAYS)
    btc_close = {c.start: c.close for c in btc}
    rows, series, skipped = [], {}, []
    for coin, qty in quantities.items():
        if qty <= 0:
            continue
        try:
            candles = fetch_candles(f"{coin}-{quote}", "ONE_DAY", HISTORY_DAYS)
        except Exception:
            candles = []
        if len(candles) < 30:
            skipped.append(coin)
            continue
        closes = {c.start: c.close for c in candles}
        series[coin] = closes
        ts = sorted(t for t in closes if t in btc_close)
        coin_r = _daily_returns([closes[t] for t in ts][-91:])
        btc_r = _daily_returns([btc_close[t] for t in ts][-91:])
        last = candles[-1]
        vol = statistics.pstdev(_daily_returns([c.close for c in candles][-31:])) * math.sqrt(365) * 100
        avg_dollar_vol = statistics.fmean(c.volume * c.close for c in candles[-30:])
        rows.append({
            "coin": coin, "qty": qty, "price": last.close, "value": qty * last.close,
            "ret30": (last.close / candles[-31].close - 1) * 100 if len(candles) > 31 else float("nan"),
            "off_high": (last.close / max(c.close for c in candles) - 1) * 100,
            "vol": vol, "beta": _beta(coin_r, btc_r), "corr": _corr(coin_r, btc_r),
            "liq_days": qty * last.close / avg_dollar_vol if avg_dollar_vol else float("nan"),
        })
    if not rows:
        return "No holdings with Coinbase price data."
    total = sum(r["value"] for r in rows)
    rows.sort(key=lambda r: r["value"], reverse=True)

    lines = [f"Portfolio value about {total:,.2f} {quote} across {len(rows)} coins (prices from Coinbase daily closes).", "",
             f"{'coin':<8}{'value':>10}{'weight':>8}{'30d':>8}{'off high':>10}{'vol/yr':>8}{'beta':>6}{'corr':>6}{'% of daily vol':>16}"]
    for r in rows:
        beta = f"{r['beta']:.2f}" if r["beta"] is not None else "n/a"
        corr = f"{r['corr']:.2f}" if r["corr"] is not None else "n/a"
        lines.append(f"{r['coin']:<8}{r['value']:>10,.2f}{r['value'] / total * 100:>7.1f}%{r['ret30']:>+7.1f}%{r['off_high']:>+9.1f}%"
                     f"{r['vol']:>7.0f}%{beta:>6}{corr:>6}{r['liq_days'] * 100:>15.3f}%")

    weights = {r["coin"]: r["value"] / total for r in rows}
    hhi = sum(w * w for w in weights.values())
    lines += ["", f"Concentration: largest holding {max(weights.values()) * 100:.0f}%, "
                  f"top two {sum(sorted(weights.values())[-2:]) * 100:.0f}%, effective number of coins {1 / hhi:.1f}"]

    # Current mix replayed over the last HISTORY_DAYS, as if held unchanged the whole time.
    common = sorted(set(btc_close).intersection(*(set(s) for s in series.values())))
    if len(common) > 30:
        mix = [sum(r["qty"] * series[r["coin"]][t] for r in rows) for t in common]
        mix_r = _daily_returns(mix)
        btc_r = _daily_returns([btc_close[t] for t in common])
        mix_vol = statistics.pstdev(mix_r[-30:]) * math.sqrt(365) * 100
        beta = _beta(mix_r[-90:], btc_r[-90:]) or 0.0
        lines += [
            f"Current mix held unchanged for the last {len(common)} days: {(mix[-1] / mix[0] - 1) * 100:+.1f}% "
            f"(BTC {(btc_close[common[-1]] / btc_close[common[0]] - 1) * 100:+.1f}%), worst fall {_max_drawdown(mix):.1f}%, "
            f"volatility {mix_vol:.0f}%/yr, beta to BTC {beta:.2f}",
            "If BTC moved, on that beta the portfolio would typically move: "
            + ", ".join(f"BTC {m:+d}% -> {m * beta:+.0f}% ({total * m * beta / 100:+,.0f})" for m in STRESS_BTC_MOVES),
            "(beta is an average over 90 days; altcoins often fall harder than that in a sharp sell-off)",
        ]
        names = [r["coin"] for r in rows]
        if len(names) > 1:
            lines += ["", "90-day correlation of daily moves", "        " + "".join(f"{n:>8}" for n in names)]
            rets = {n: _daily_returns([series[n][t] for t in common])[-90:] for n in names}
            for a in names:
                lines.append(f"{a:<8}" + "".join(f"{(_corr(rets[a], rets[b]) or 0):>8.2f}" for b in names))
    if skipped:
        lines.append(f"\nNo {quote} price history for: {', '.join(skipped)} (left out of totals)")
    return "\n".join(lines)
