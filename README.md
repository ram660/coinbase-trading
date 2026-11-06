# Coinbase trading agent

A small, rule-based crypto trading bot for Coinbase Advanced Trade. Strategies propose trades, a risk manager
approves or shrinks them, and a broker executes them. **Paper trading is the default**; live trading has three
separate locks.

Nothing here is financial advice. Crypto is volatile, and a bot can lose money faster than a person can.

## Quick start (Windows, Python 3.11+)

```powershell
cd C:\dev\coinbase-trading-agent
python -m venv .venv
.venv\Scripts\activate
pip install -e .
python -m unittest discover -s tests      # offline tests
cbagent backtest --bars 2000              # replay ~83 days of hourly candles, no keys needed
cbagent paper                             # paper trade against live prices
cbagent status
```

Without installing, `$env:PYTHONPATH="src"; python -m cbagent backtest` also works.

## How it fits together

```
market_data.py  public Coinbase candles (no key)
      │
strategies/     on_candles(...) -> Signal(buy | sell | hold)
      │
risk.py         RiskManager.check(order, account) -> approve / clamp / reject
      │
brokers/        paper.py (simulated fills, fees, slippage)  |  coinbase_live.py (real orders, gated)
      │
engine.py       the loop, plus run_backtest(); every decision and fill goes to data/journal_*.jsonl
```

| Path | What it is |
|---|---|
| `config/default.toml` | All settings: mode, products, candle size, strategy, risk limits, paper fees |
| `config/local.toml` | Your overrides (git-ignored) |
| `config/.env.example` | Template for API keys; copy to `config/.env` only for live mode |
| `src/cbagent/strategies/` | One file per strategy, registered in `__init__.py` |
| `data/` | Paper state, risk state, journals, kill switch file (git-ignored) |

## Risk controls

All limits are in the quote currency (USD for `BTC-USD`) and live in `[risk]` in `config/default.toml`.

| Control | Default | Effect |
|---|---|---|
| `max_order_quote` | 50 | Buys are shrunk to this size |
| `max_position_quote` | 200 | Cap per product |
| `max_total_exposure_quote` | 500 | Cap across all products |
| `max_daily_loss_quote` | 50 | No new buys for the rest of the UTC day; sells still allowed |
| `max_drawdown_pct` | 30 | Trips the kill switch automatically |
| `on_drawdown` | flatten | On that trip, sell everything to cash (`freeze` keeps holdings) |
| `max_orders_per_day` | 20 | Stops churn |
| Kill switch | `data/KILL_SWITCH` | If the file exists, nothing trades and the loop exits |

`cbagent kill` turns the kill switch on, `cbagent unkill` turns it off after you've looked at what happened. You can
also just create the file. A manual kill freezes holdings so you decide what to do; an automatic drawdown trip
sells to cash first (unless `on_drawdown = "freeze"`). Either way the bot stays off until `cbagent unkill`.

## Dashboard

The `paper-trading` workflow builds a dashboard of the paper books (equity over time, holdings with cost and
P&L, every rebalance and fill) and of the workflow itself (hour-by-hour run strip, missed hours, success rate,
recent runs) and publishes it to GitHub Pages at https://ram660.github.io/coinbase-trading/. One-time setup:
Settings > Pages > Source = "GitHub Actions". The page is public because the repo is; it only shows the simulated
books.

GitHub skips and delays scheduled runs when it is busy, so the workflow is scheduled every 5 minutes. `cbagent due`
runs the books only when they haven't run in the last 12 minutes, so they run about every 15 minutes. Every run also
rebalances each book. The daily and perps books read closed daily candles, so most of their runs change nothing; the
fast book reads hourly candles. Adjustments under 2% of equity (5% for perps) are skipped to save fees. The brief runs on the first run after
15:00 UTC. To run by hand: Actions > paper-trading > Run workflow (tick "force" to run the books anyway).

Locally, `cbagent dashboard` writes `data/dashboard/index.html` from `data/`, using live public prices if the state has
no marks yet. Add `--runs file.json` (the GitHub runs API response) to include the workflow section.

## Robustness test

`cbagent robustness` replays every candidate over rolling one-year windows of daily data (default: 6 years, a
window every 90 days) and reports median and worst return, how often it was positive, how often it beat
buy-and-hold, and kill-switch trips. Use it before trusting any single backtest.

## Perps paper book

`cbagent rotate` also runs a third book, `perps`, on Coinbase's USD-margined nano perps (`BIP-20DEC30-CDE` for BTC,
`ETP-20DEC30-CDE` for ETH). It is simulated money only: there is no live perps broker, and building one is a
separate decision.

- Every 15 minutes it checks the signal: long when the daily close is above both its 50- and 200-day averages, short when below both,
  flat otherwise. Each position is sized to about 50% annual volatility, at most 2x leverage in total, in whole
  contracts (0.01 BTC, 0.1 ETH).
- Every hour it charges funding, estimated from the perp's premium over spot because the public API doesn't publish
  the rate. It checks liquidation against the worst hourly price since the last run, with 5% maintenance margin.
- Fees are 0.02% plus $0.11 per contract, Coinbase's introductory fee in Canada. The dashboard also shows equity
  as if fees were 0.05%.
- It stops for good at 40% below its peak, and doesn't rebalance while `data/KILL_SWITCH` exists.

`cbagent perps-backtest --years 4` replays the same rules over daily spot closes. Perps are assumed to track spot,
longs pay a flat funding rate (10% a year by default) and shorts receive it, and liquidation is checked on daily
closes only. Treat it as optimistic.

Access: Coinbase opened perps to eligible Canadians in September 2026 through Coinbase Financial Markets. Reports
put eligibility at investors with at least $5M in net financial assets; check the Futures tab in the app.

## Intraday tools

A crypto version of the ideas in the day-trader skill pack (github.com/Jakeschincariol/day-trader-skill), written
fresh for Coinbase's 24/7 markets. They read public 5-minute candles and local files. None of them places an order.

```
cbagent setups                                  # liquid USDC pairs: range_break, break_24h, volume_spike, rsi_bounce, rsi_fade
cbagent entry QNT-USDC --setup range_break      # waiting / triggered / expired; --watch keeps checking
cbagent plan QNT-USDC                           # live price, stop loss:5, target gain:5, spend $75 -> data/daytrade/plan.json
cbagent plan BTC-USDC --stop atr:2 --target R:2 --max-loss 4
cbagent odds                                    # Time Machine on the saved plan
cbagent ticket                                  # the plan as a ticket with the exact orders_create arguments
cbagent ticket --reply "yes QNT"                # records Ram's reply word for word; only an explicit yes counts
cbagent alerts add --from-plan                  # stop and target alerts; `cbagent alerts check` rings each once
```

- **Setups**: crypto has no opening bell, so the opening range is the first hour of the UTC day.
- **Plan** rules: stop `loss:5`, `pct:3`, `atr:2` (1-hour ATR) or `price:X`; target `gain:5`, `R:1.5`, `pct:4`,
  `atr:3` or `price:X`. `loss:` and `gain:` are dollars on the position with fees in and out included. Prices
  are rounded to the product's increment.
- **Time Machine** (`odds`): rebuilds the next 24 hours 10,000 times from the coin's own 5-minute candles at the
  same time of day, and tests the same % distances from every past close in the lookback (30 days by default).
  It reports target first / stop first / still open and the average result in R after fees. If one candle touches
  both levels it counts as a stop. It replays the recent past; it doesn't forecast.
- **Quiet mode** for schedules: `cbagent setups --quiet` prints the full report on the first run of the UTC day,
  then only hits (coin, setup, side) not yet reported today, and nothing otherwise. `cbagent alerts check --quiet`
  prints only what rang, so a price that can't be fetched shows once a day instead of every run. The state is
  `data/daytrade/seen_today.json`.
- **Ticket**: shows whether the order fits Ram's standing approval (one spot BUY in USDC, at most $75, with TP and
  stop, no live position open) or needs his yes. These notes inform; they don't block. Sending the order stays a
  separate step under CLAUDE.md rule 1.

## Live trading

Step-by-step for the $100 test: [docs/GOING_LIVE.md](docs/GOING_LIVE.md). `cbagent live-check` is a read-only check
of the key, balances and products. The live broker refuses keys that can transfer funds and never spends more than
`[live] allocation_quote`.


Live orders are refused unless **all** of these are true:

1. `mode = "live"` in `config/local.toml`
2. You run `cbagent live --live`
3. `CBAGENT_LIVE_CONFIRM=I accept real money risk` is in `config/.env`

Then `pip install -e ".[live]"` for Coinbase's official `coinbase-advanced-py` SDK, and create a CDP API key with
**View and Trade only, never Transfer**, restricted to your IP. The live broker has not been run against a real
account yet: start with tiny limits (say `max_order_quote = 5`) and check each fill in Coinbase.

## Strategies and exits

| Name | Idea |
|---|---|
| `sma_crossover` | Buy when the fast moving average crosses above the slow one, sell on the cross back |
| `confluence` | RSI, MACD, Bollinger, EMA trend and volume each vote; buy at net +3, sell at net -2 |
| `trend` | Daily: hold while the close is above its N-day average (with a band), cash otherwise |
| `dca` | Daily: buy a fixed amount each week, more after a big drop; never sells |

The engine acts once per closed candle, so a 5-minute poll on daily candles doesn't repeat orders. After a
restart it re-checks the latest candle once, which can repeat that day's `dca` buy.

An ATR trailing stop (`[exits]`) runs on top of any strategy and sells when the close drops `atr_mult` x ATR below
the highest close since entry. Compare with `cbagent backtest --strategy confluence` and `--no-stop`.

## Adding a strategy

1. Copy `strategies/sma_crossover.py`, give it a new `name`, set `warmup`, implement `on_candles`.
2. Register it in `strategies/__init__.py`.
3. Set `[strategy] name = "..."` plus its parameters in `config/local.toml`.
4. `cbagent backtest`, compare against buy-and-hold and fees, then paper trade for a few weeks before anything else.

## Known gaps

- Backtests fill at the candle close with a flat fee and slippage. Real fills differ.
- Live positions come from account balances, so cost basis and P&L for live mode are not tracked yet.
- Only market orders. Limit (maker) orders would cut fees a lot and are the obvious next step.
