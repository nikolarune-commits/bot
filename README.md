# bot

Polymarket paper-trading bot for Bitcoin Up/Down 5-minute markets.

## What it does
Watches BTC price via Binance (Kraken fallback) and places simulated trades on Polymarket's 5-minute prediction markets using a 6-signal momentum strategy. Balance and trade history are fake — no real money moves.

## Where it runs
Deployed as a Render worker via `render.yaml`. State (`state.json`) and trade log (`trade_log.csv`) live on a 1 GB Render persistent disk mounted at `/data`, so balance survives redeploys.

## Strategy at a glance
- Trade only Bitcoin Up/Down 5-minute markets
- 6 signals: window delta, 30-min trend, EMA 9/21, RSI, order-book imbalance, sustained momentum
- Entry gates: confidence ≥ 57%, Polymarket price 0.38–0.70, window age 20–150s
- Bet 3–5% of balance, capped at $6, one open trade at a time
- Escalating cooldowns on loss streaks (2L → 5 min, 3L → 10 min, 4L → 20 min, 5L+ → 30 min)

## Files
- `arb_bot.py` — all bot logic
- `render.yaml` — Render worker config with persistent disk
- `requirements.txt` — pinned Python dependencies
