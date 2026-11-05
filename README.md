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

## Robustness test

`cbagent robustness` replays every candidate over rolling one-year windows of daily data (default: 6 years, a
window every 90 days) and reports median and worst return, how often it was positive, how often it beat
buy-and-hold, and kill-switch trips. Use it before trusting any single backtest.

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
