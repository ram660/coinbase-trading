# Going live with $100

You do these steps yourself. Claude prepares the code but does not create keys, move money or start live trading.

## 1. Have about 100 USDC in the account

The bot trades BTC-USDC and ETH-USDC, so it needs USDC as cash (CAD pairs aren't offered on this account). How you
get the USDC is your call. The bot only ever uses `allocation_quote` (100) of it and ignores coins it didn't buy,
so it can share your Default portfolio. A separate portfolio is optional extra isolation.

## 2. Create an API key

1. Go to https://portal.cdp.coinbase.com/ (Coinbase Developer Platform), then **API keys**, then **Create API key**.
2. Choose your portfolio (Default is fine). Tick **View** and **Trade**. Leave **Transfer** off. The bot refuses keys that can transfer.
3. Under IP allowlist, add your home IP if you can.
4. Download the key. Keep it out of chat, email and git.

## 3. Configure

```powershell
cd C:\dev\coinbase-trading-agent
pip install -e ".[live]"
copy config\live.example.toml config\local.toml
copy config\.env.example config\.env
notepad config\.env
```

In `config\.env`, paste the key name into `COINBASE_API_KEY`, the private key into `COINBASE_API_SECRET`, and set
`CBAGENT_LIVE_CONFIRM=I accept real money risk`.

## 4. Check, read-only

```powershell
cbagent live-check
```

This places no orders. It should show `transfer=False`, about 100 available, and each product's minimum order and
what the strategy would do right now. Fix anything marked NOT OK first.

## 5. Start

```powershell
cbagent live --live
```

Leave the window open; it checks every hour and acts once per daily candle. Every decision and fill goes to
`data\journal_live.jsonl`. The 50/200 cross trades rarely, so days or weeks with no orders are normal.

## Stopping

- `Ctrl+C` stops the bot and leaves holdings as they are.
- `cbagent kill` stops it from trading until `cbagent unkill`.
- A 30% fall from peak sells to cash, waits 30 days, then restarts at half size.
- To exit completely, stop the bot and sell in the Coinbase app.
