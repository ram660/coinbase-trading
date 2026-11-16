# Wealthsimple alerts

Telegram alerts for trades you place by hand in Wealthsimple. Nothing here trades.
`.github/workflows/ws-alerts.yml` runs `scripts/alerts.ts` every 15 min (secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

```
npm install --no-save tsx
npx tsx scripts/alerts.ts --dry     # print alerts, send nothing
npx tsx scripts/backtest.ts         # signal + Monday-gap tests, 2y hourly
npx tsx scripts/patterns.ts         # day-of-week / hour / gap scan
```

Paper book: every alert also opens a simulated $600 CAD position (start $2,000) that is closed on stop, target, Monday's close
(gap trades) or after 7 days (signal trades), with a Telegram message per close and a recap after 16:05 ET. Put alerts are tracked
as a short of the underlying, not the option premium. `npx tsx scripts/alerts.ts --report` prints the book (state lives on the
`ws-alerts-state` branch).

Optional env: `POSITION_CAD` (600), `START_CAD` (2000), `MAX_HOLD_DAYS` (7), `CRYPTO_FEE_PCT` (1.5), `STOCK_FEE_PCT` (0),
`MIN_CONFIDENCE` (60), `COOLDOWN_HOURS` (6), `MAX_PREMIUM_CAD` (350), `EARNINGS_DAYS` (21).

## What the alerts do, and why (backtest Nov 2024 - Nov 2026, 6 stocks/ETFs + 3 coins)

| Alert / filter | Evidence |
|---|---|
| Indicator signal, only when aligned with the ~50-day trend, no puts at RSI<30 / calls at RSI>70 | +0.08R/trade vs +0.03R unfiltered. Weak: treat as a watchlist, not a trigger. |
| Monday gap-down >0.5%, buy shares after the first hour, exit at close | +1.0% avg, 66% up, 80 Mondays, both years and all 6 names positive. Gaps >4%: +2.2%. |
| No single-leg options when earnings are within 21 days (Nasdaq calendar) | IV crush + gap risk. |
| Real contract quote (30-50 DTE, near the money), cost in CAD, implied vs realized vol | Flags contracts over budget, wide spreads, expensive IV. |

Tested and dropped: SPY market-regime filter (no help), ADX>=25, R:R>=1.5, hour-of-day timing (stocks and crypto: nothing above the spread), crypto weekday/weekend effects (noise).

Caveats: rising-market sample; the names are correlated so effective sample sizes are much smaller than trade counts; no costs modelled on the stock side.
