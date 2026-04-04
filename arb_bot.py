"""
Polymarket Paper Trading Bot
=============================
Real prices from Polymarket's public API.
Strategy: buy underpriced sides of "Bitcoin Up or Down" markets.
Balance is fake (paper trading) — no real money moves.
"""

import random
import time
import csv
import os
import json
import requests
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
STARTING_BALANCE   = 100.00   # Starting balance (first run only)
BET_PCT            = 0.05     # Bet 5% of current balance per trade
MAX_BET_SIZE       = 6.00     # Cap per trade
MIN_BET_SIZE       = 1.00     # Minimum to place a trade
MAX_OPEN_TRADES    = 2        # Max concurrent positions
FEE_RATE           = 0.02     # 2% fee (matches real Polymarket)
ENTRY_MIN_PRICE    = 0.35     # Min Polymarket price for our direction (avoid coin-flips)
ENTRY_MAX_PRICE    = 0.72     # Max price (avoid near-certainties with low payout)
SCAN_INTERVAL      = 2.0      # Seconds between scans
SCANS_PER_SESSION  = 300      # ~10 min per session
API_REFRESH_SCANS  = 30       # Re-fetch markets every N scans (~60s)
BTC_CACHE_SECONDS  = 10       # Refresh Binance data every 10 seconds
SAVE_CSV           = True
CSV_FILENAME       = "trade_log.csv"
POLYMARKET_API     = "https://gamma-api.polymarket.com"
BINANCE_API        = "https://api.binance.com/api/v3"
STATE_FILE         = "state.json"


# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class Market:
    """Live Polymarket market snapshot."""
    market_id: str
    name: str
    up_price: float     # "Yes" / Up price
    down_price: float   # "No"  / Down price
    liquidity: float
    closed: bool = False


@dataclass
class Trade:
    """A single paper trade."""
    trade_id: int
    market: str
    market_id: str
    side: str           # "UP" or "DOWN"
    entry_price: float  # price paid per share
    shares: float       # units bought = bet_size / entry_price
    bet_size: float     # USD staked
    actual_profit: Optional[float] = None
    status: str = "OPEN"
    open_time: datetime = field(default_factory=datetime.now)
    close_time: Optional[datetime] = None
    exit_price: float = 0.0


# ─────────────────────────────────────────────
# BTC PRICE DATA  (Binance → Kraken fallback)
# ─────────────────────────────────────────────

def get_btc_candles(limit: int = 30) -> list:
    """
    Fetch recent 1-minute OHLCV candles.
    Tries Binance first; falls back to Kraken if Binance is unavailable.
    Returns list of [ts, open, high, low, close, ...] rows.
    """
    # --- Binance ---
    try:
        r = requests.get(f"{BINANCE_API}/klines",
                         params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
                         timeout=5)
        if r.ok and isinstance(r.json(), list) and r.json():
            return r.json()
    except Exception:
        pass

    # --- Kraken fallback ---
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC",
                         params={"pair": "XBTUSD", "interval": 1},
                         timeout=5)
        if r.ok:
            result = r.json().get("result", {})
            rows   = result.get("XXBTZUSD", result.get("XBTUSD", []))
            if rows:
                # Kraken format: [time, open, high, low, close, vwap, volume, count]
                # Normalise to Binance-style: [ts_ms, open, high, low, close, ...]
                out = []
                for row in rows[-limit:]:
                    ts_ms = int(row[0]) * 1000
                    out.append([ts_ms, row[1], row[2], row[3], row[4]])
                print("  [BTC] Using Kraken data (Binance unavailable)")
                return out
    except Exception:
        pass

    print("  [BTC] All price sources failed")
    return []


def get_order_book(limit: int = 20) -> tuple:
    """
    Fetch top bid/ask levels.
    Tries Binance first; falls back to Kraken.
    """
    # --- Binance ---
    try:
        r = requests.get(f"{BINANCE_API}/depth",
                         params={"symbol": "BTCUSDT", "limit": limit},
                         timeout=5)
        if r.ok:
            d = r.json()
            return d["bids"], d["asks"]
    except Exception:
        pass

    # --- Kraken fallback ---
    try:
        r = requests.get("https://api.kraken.com/0/public/Depth",
                         params={"pair": "XBTUSD", "count": limit},
                         timeout=5)
        if r.ok:
            result = r.json().get("result", {})
            book   = result.get("XXBTZUSD", result.get("XBTUSD", {}))
            bids   = [[b[0], b[1]] for b in book.get("bids", [])]
            asks   = [[a[0], a[1]] for a in book.get("asks", [])]
            return bids, asks
    except Exception:
        pass

    return [], []


# ─────────────────────────────────────────────
# TECHNICAL ANALYSIS
# ─────────────────────────────────────────────

def calc_ema(values: list, period: int) -> float:
    """Exponential Moving Average."""
    if len(values) < period:
        return values[-1] if values else 0.0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def calc_rsi(closes: list, period: int = 14) -> float:
    """RSI (0–100). Returns 50 if not enough data."""
    if len(closes) < period + 1:
        return 50.0
    deltas  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains   = [max(d, 0)    for d in deltas[-period:]]
    losses  = [abs(min(d, 0)) for d in deltas[-period:]]
    avg_g   = sum(gains)  / period
    avg_l   = sum(losses) / period
    if avg_l == 0:
        return 100.0
    return 100 - (100 / (1 + avg_g / avg_l))

def calc_obi(bids: list, asks: list, levels: int = 10) -> float:
    """Order Book Imbalance: +1 = all buy pressure, -1 = all sell pressure."""
    bid_vol = sum(float(b[1]) for b in bids[:levels])
    ask_vol = sum(float(a[1]) for a in asks[:levels])
    total   = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0

def calc_window_delta(candles: list) -> float:
    """% BTC price moved since the current 5-minute Polymarket window opened."""
    if not candles:
        return 0.0
    window_ts    = int(time.time()) - (int(time.time()) % 300)  # floor to 5-min boundary
    window_ts_ms = window_ts * 1000
    window_candle = next((c for c in candles if c[0] >= window_ts_ms), candles[-1])
    window_open  = float(window_candle[1])   # candle open price
    current      = float(candles[-1][4])     # most recent candle close
    if window_open == 0:
        return 0.0
    return (current - window_open) / window_open * 100


# ─────────────────────────────────────────────
# BTC SIGNAL (4-indicator momentum system)
# ─────────────────────────────────────────────

_btc_cache: dict      = {}
_btc_cache_time: float = 0.0

def btc_signal() -> Optional[tuple[str, float]]:
    """
    Combines 4 independent BTC indicators into a directional signal.
    Returns (direction, confidence) or None if signals conflict/are weak.

    Signals & weights:
      Window Delta  (5–7)  — how much BTC moved since window opened
      EMA 9/21      (1)    — short-term trend direction
      RSI 14        (1–2)  — overbought/oversold extremes
      OBI           (1–2)  — order book buy vs sell pressure

    Trade only when confidence >= 30% (signals agree enough).
    """
    global _btc_cache, _btc_cache_time
    now = time.time()
    if now - _btc_cache_time > BTC_CACHE_SECONDS:
        candles      = get_btc_candles(30)
        bids, asks   = get_order_book(20)
        if candles:
            _btc_cache      = {"candles": candles, "bids": bids, "asks": asks}
            _btc_cache_time = now

    candles = _btc_cache.get("candles", [])
    bids    = _btc_cache.get("bids", [])
    asks    = _btc_cache.get("asks", [])
    if not candles:
        return None

    closes = [float(c[4]) for c in candles]
    score  = 0.0

    # Signal 1: Window Delta — most important (weight 5–7)
    delta = calc_window_delta(candles)
    if   delta >  0.10: score += 7
    elif delta >  0.02: score += 5
    elif delta < -0.10: score -= 7
    elif delta < -0.02: score -= 5

    # Signal 2: EMA 9/21 crossover (weight 1)
    ema9  = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    score += 1 if ema9 > ema21 else -1

    # Signal 3: RSI 14 (weight 1–2)
    rsi = calc_rsi(closes)
    if   rsi < 25: score += 2   # oversold → bounce UP
    elif rsi > 75: score -= 2   # overbought → drop DOWN
    elif rsi > 55: score += 1
    elif rsi < 45: score -= 1

    # Signal 4: Order Book Imbalance (weight 1–2)
    obi = calc_obi(bids, asks)
    if   obi >  0.3: score += 2
    elif obi >  0.1: score += 1
    elif obi < -0.3: score -= 2
    elif obi < -0.1: score -= 1

    confidence = min(abs(score) / 7.0, 1.0)
    direction  = "UP" if score > 0 else "DOWN"

    print(f"  [BTC] Δ={delta:+.3f}%  RSI={rsi:.0f}  EMA={'↑' if ema9>ema21 else '↓'}  "
          f"OBI={obi:+.2f}  score={score:+.0f}  conf={confidence:.0%}  → {direction}")

    if confidence < 0.30:
        return None   # signals too weak or conflicting

    return (direction, confidence)


# ─────────────────────────────────────────────
# POLYMARKET API
# ─────────────────────────────────────────────

_market_cache: dict[str, Market] = {}
_cache_last_scan: int = -9999


def _fetch_markets(include_closed: bool = False) -> dict[str, Market]:
    """Fetch Bitcoin Up/Down 5-minute markets from Polymarket API."""
    results = {}
    queries = ["Bitcoin Up or Down", "BTC Up or Down"]
    for query in queries:
        try:
            params = {"limit": 50, "q": query}
            if not include_closed:
                params["active"] = "true"
                params["closed"] = "false"
            else:
                params["closed"] = "true"

            resp = requests.get(f"{POLYMARKET_API}/markets", params=params, timeout=10)
            if not resp.ok:
                continue

            for m in resp.json():
                try:
                    question = m.get("question", "")
                    q_lower  = question.lower()
                    is_btc    = "bitcoin" in q_lower or "btc" in q_lower
                    is_updown = ("up or down" in q_lower or "5 min" in q_lower
                                 or "5-min" in q_lower or "updown" in q_lower)
                    if not is_btc or not is_updown:
                        continue
                    outcomes = m.get("outcomes", [])
                    prices   = m.get("outcomePrices", [])
                    if isinstance(outcomes, str): outcomes = json.loads(outcomes)
                    if isinstance(prices,   str): prices   = json.loads(prices)
                    if len(prices) != 2:
                        continue
                    up_p   = float(prices[0])
                    down_p = float(prices[1])
                    liq    = float(m.get("liquidity") or 0)
                    mid    = str(m.get("id") or m.get("conditionId") or question)
                    if up_p <= 0 or down_p <= 0:
                        continue
                    results[mid] = Market(
                        market_id  = mid,
                        name       = question[:60],
                        up_price   = up_p,
                        down_price = down_p,
                        liquidity  = liq,
                        closed     = bool(m.get("closed", False)),
                    )
                except (ValueError, TypeError, KeyError):
                    continue
        except Exception as e:
            print(f"  [API] Fetch failed ({query}): {e}")
    return results


def refresh_cache(scan_num: int) -> dict[str, Market]:
    """Refresh market cache every N scans. Also fetches closed markets for resolution."""
    global _market_cache, _cache_last_scan
    if scan_num - _cache_last_scan >= API_REFRESH_SCANS or not _market_cache:
        active = _fetch_markets(include_closed=False)
        closed = _fetch_markets(include_closed=True)
        merged = {**closed, **active}   # active data wins on conflict
        if merged:
            _market_cache = merged
            active_count = sum(1 for m in merged.values() if not m.closed)
            print(f"  [API] {active_count} active BTC markets loaded")
            for mm in list(merged.values())[:3]:
                print(f"  [API]   → {mm.name[:55]}  UP:{mm.up_price:.2f} DN:{mm.down_price:.2f}")
        _cache_last_scan = scan_num
    return _market_cache


# ─────────────────────────────────────────────
# TRADING SIGNAL
# ─────────────────────────────────────────────

def find_signal(market: Market) -> Optional[tuple[str, float]]:
    """
    Uses real BTC momentum from Binance to determine direction.
    Only trades when 4 independent signals agree (confidence >= 30%).
    Then confirms Polymarket's price for that direction is in a tradeable range.
    """
    if market.closed:
        return None

    sig = btc_signal()
    if not sig:
        return None

    direction, _confidence = sig
    price = market.up_price if direction == "UP" else market.down_price

    if not (ENTRY_MIN_PRICE <= price <= ENTRY_MAX_PRICE):
        return None   # market already priced in the move, no edge left

    return (direction, price)


# ─────────────────────────────────────────────
# TRADE RESOLUTION (REAL)
# ─────────────────────────────────────────────

def check_resolution(trade: Trade, markets: dict[str, Market]) -> Optional[tuple[float, str, float]]:
    """
    Check if a trade resolved by looking at actual market price.
    On Polymarket, winning side goes to 1.00, losing side to 0.00.
    Falls back to probability-based resolution after 20 minutes
    (5-min markets close fast and disappear from API).
    Returns (profit, status, exit_price) or None if still open.
    """
    m = markets.get(trade.market_id)

    if m:
        current = m.up_price if trade.side == "UP" else m.down_price
        if current >= 0.97:
            gross  = trade.shares * 1.0
            fees   = FEE_RATE * trade.bet_size
            profit = round(gross - trade.bet_size - fees, 4)
            return (profit, "WIN", current)
        if current <= 0.03:
            profit = round(-trade.bet_size, 4)
            return (profit, "LOSS", current)

    # Market gone from API — resolve after 20-min timeout
    age_minutes = (datetime.now() - trade.open_time).total_seconds() / 60
    if age_minutes >= 20:
        # Use entry price as win probability (market-implied odds)
        if random.random() < trade.entry_price:
            gross  = trade.shares * 1.0
            fees   = FEE_RATE * trade.bet_size
            profit = round(gross - trade.bet_size - fees, 4)
            return (profit, "WIN", 1.0)
        else:
            return (round(-trade.bet_size, 4), "LOSS", 0.0)

    return None   # Still open


# ─────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────

SEP = "─" * 52

def print_header(scan_num: int, total: int):
    print(f"\n{'═' * 52}")
    print(f"  SCAN #{scan_num:03d}/{total}  |  {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
    print(f"{'═' * 52}")

def print_market(m: Market):
    print(f"  Market : {m.name}")
    print(f"  UP     : {m.up_price:.4f}  |  DOWN: {m.down_price:.4f}")
    print(f"  Liq    : ${m.liquidity:,.2f}")

def print_trade_open(side: str, price: float, bet: float, shares: float, profit_if_win: float):
    print(SEP)
    print(f"  ACTION         : BUY {side}")
    print(f"  Entry Price    : {price:.4f}")
    print(f"  Shares Bought  : {shares:.2f}")
    print(f"  Bet Size       : ${bet:.2f}")
    print(f"  Payout if WIN  : +${profit_if_win:.4f}")
    print(f"  Risk if LOSS   : -${bet:.2f}")
    print(SEP)

def print_skipped(reason: str):
    print(f"  SKIPPED  : {reason}")

def print_resolution(trade: Trade):
    sign  = "+" if trade.actual_profit >= 0 else ""
    label = "WIN " if trade.status == "WIN" else "LOSS"
    print(f"\n  [{label}] Trade #{trade.trade_id}  {trade.side} @ {trade.entry_price:.2f}"
          f"  →  exit {trade.exit_price:.2f}  →  {sign}${trade.actual_profit:.4f}")

def print_balance(balance: float, open_count: int, total_pnl: float):
    sign = "+" if total_pnl >= 0 else ""
    print(f"\n  BALANCE    : ${balance:.2f}  |  Open: {open_count}  |  P&L: {sign}${total_pnl:.2f}")

def print_session_summary(balance, session_start, original_balance, trades, skipped):
    closed = [t for t in trades if t.status in ("WIN", "LOSS")]
    wins   = [t for t in closed if t.status == "WIN"]
    losses = [t for t in closed if t.status == "LOSS"]

    win_rate     = (len(wins) / len(closed) * 100) if closed else 0
    total_profit = sum(t.actual_profit for t in closed)
    biggest_win  = max((t.actual_profit for t in wins),   default=0)
    biggest_loss = min((t.actual_profit for t in losses), default=0)

    print(f"\n{'═' * 52}")
    print(f"  SESSION SUMMARY")
    print(f"{'═' * 52}")
    print(f"  Balance        : ${balance:.2f}")
    print(f"  Session P&L    : ${balance - session_start:+.2f}")
    print(f"  All-time P&L   : ${balance - original_balance:+.2f}  (started at ${original_balance:.2f})")
    print(f"{'─' * 52}")
    print(f"  Trades         : {len(closed)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate       : {win_rate:.1f}%")
    print(f"  Skipped        : {skipped}")
    print(f"  Total P&L      : ${total_profit:+.4f}")
    print(f"  Biggest Win    : ${biggest_win:+.4f}")
    print(f"  Biggest Loss   : ${biggest_loss:+.4f}")
    print(f"{'═' * 52}\n")


# ─────────────────────────────────────────────
# CSV LOGGER
# ─────────────────────────────────────────────

def init_csv(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow([
                "trade_id", "market", "side", "entry_price", "shares",
                "bet_size", "exit_price", "actual_profit", "status",
                "open_time", "close_time",
            ])

def append_csv(path: str, trade: Trade):
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow([
            trade.trade_id, trade.market, trade.side,
            trade.entry_price, trade.shares, trade.bet_size,
            trade.exit_price, trade.actual_profit, trade.status,
            trade.open_time.strftime("%Y-%m-%d %H:%M:%S"),
            trade.close_time.strftime("%Y-%m-%d %H:%M:%S") if trade.close_time else "",
        ])


# ─────────────────────────────────────────────
# BALANCE PERSISTENCE
# ─────────────────────────────────────────────

def trades_to_json(trades: list[Trade]) -> list[dict]:
    return [{
        "trade_id":    t.trade_id,
        "market":      t.market,
        "market_id":   t.market_id,
        "side":        t.side,
        "entry_price": t.entry_price,
        "shares":      t.shares,
        "bet_size":    t.bet_size,
        "open_time":   t.open_time.isoformat(),
    } for t in trades]

def trades_from_json(data: list) -> list[Trade]:
    result = []
    for d in (data or []):
        try:
            result.append(Trade(
                trade_id    = d["trade_id"],
                market      = d["market"],
                market_id   = d["market_id"],
                side        = d["side"],
                entry_price = d["entry_price"],
                shares      = d["shares"],
                bet_size    = d["bet_size"],
                open_time   = datetime.fromisoformat(d["open_time"]),
            ))
        except (KeyError, ValueError):
            continue
    return result

def load_state() -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILE)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"balance": STARTING_BALANCE, "original_balance": STARTING_BALANCE, "total_sessions": 0, "open_trades": []}

def save_state(balance: float, original_balance: float, total_sessions: int, open_trades: list[Trade]):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILE)
    with open(path, "w") as f:
        json.dump({
            "balance":        balance,
            "original_balance": original_balance,
            "total_sessions": total_sessions,
            "open_trades":    trades_to_json(open_trades),
        }, f)


# ─────────────────────────────────────────────
# MAIN BOT LOOP
# ─────────────────────────────────────────────

def run_bot(balance: float, original_balance: float, carried_trades: list[Trade]) -> tuple[float, list[Trade]]:
    session_start = balance
    open_trades   = carried_trades   # carry over unresolved trades from last session
    all_trades:  list[Trade] = []
    trade_id_seq  = max((t.trade_id for t in open_trades), default=0)
    skipped       = 0
    total_pnl     = 0.0

    if SAVE_CSV:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CSV_FILENAME)
        init_csv(csv_path)

    print("  Polymarket Paper Trading Bot")
    print(f"  Balance  : ${balance:.2f}")
    print(f"  Strategy : Buy underpriced side of Bitcoin Up/Down markets")
    print(f"  Entry    : {ENTRY_MIN_PRICE:.2f}–{ENTRY_MAX_PRICE:.2f} price range  |  Max bet: ${MAX_BET_SIZE:.2f}\n")

    for scan_num in range(1, SCANS_PER_SESSION + 1):
        markets = refresh_cache(scan_num)

        # ── 1. Check open trades for real resolution
        still_open = []
        for t in open_trades:
            result = check_resolution(t, markets)
            if result:
                profit, status, exit_p = result
                t.actual_profit = profit
                t.status        = status
                t.exit_price    = exit_p
                t.close_time    = datetime.now()
                balance   += t.bet_size + profit
                total_pnl += profit
                print_resolution(t)
                all_trades.append(t)
                if SAVE_CSV:
                    append_csv(csv_path, t)
            else:
                still_open.append(t)
        open_trades = still_open

        # ── 2. Pick a market to scan
        active_markets = [m for m in markets.values() if not m.closed]
        if not active_markets:
            print_header(scan_num, SCANS_PER_SESSION)
            print("  [API] No active markets — waiting for next Bitcoin Up/Down window...")
            time.sleep(SCAN_INTERVAL)
            continue

        market = random.choice(active_markets)
        print_header(scan_num, SCANS_PER_SESSION)
        print_market(market)

        # ── 3. Skip checks
        if len(open_trades) >= MAX_OPEN_TRADES:
            print_skipped(f"Max open trades ({MAX_OPEN_TRADES})")
            skipped += 1
            time.sleep(SCAN_INTERVAL)
            continue

        if balance < MIN_BET_SIZE:
            print_skipped("Insufficient balance")
            skipped += 1
            time.sleep(SCAN_INTERVAL)
            continue

        if any(t.market_id == market.market_id for t in open_trades):
            print_skipped("Already in this market")
            skipped += 1
            time.sleep(SCAN_INTERVAL)
            continue

        signal = find_signal(market)
        if not signal:
            print_skipped("No signal — prices outside entry range or low liquidity")
            skipped += 1
            time.sleep(SCAN_INTERVAL)
            continue

        side, price = signal

        # ── 4. Size the bet (5% of balance, capped at MAX_BET_SIZE)
        bet = round(min(MAX_BET_SIZE, max(MIN_BET_SIZE, balance * BET_PCT)), 2)
        if bet < MIN_BET_SIZE:
            print_skipped("Insufficient balance for minimum bet")
            skipped += 1
            time.sleep(SCAN_INTERVAL)
            continue

        shares        = round(bet / price, 4)
        profit_if_win = round(shares * 1.0 - bet - FEE_RATE * bet, 4)

        # ── 5. Open the trade
        trade_id_seq += 1
        trade = Trade(
            trade_id    = trade_id_seq,
            market      = market.name,
            market_id   = market.market_id,
            side        = side,
            entry_price = price,
            shares      = shares,
            bet_size    = bet,
        )

        balance -= bet
        open_trades.append(trade)

        print_trade_open(side, price, bet, shares, profit_if_win)
        print_balance(balance, len(open_trades), total_pnl)

        time.sleep(SCAN_INTERVAL)

    # ── End of session: carry open trades to next session (do NOT force-close)
    if open_trades:
        print(f"\n{'─' * 52}")
        print(f"  {len(open_trades)} trade(s) still open — carrying to next session")
        for t in open_trades:
            print(f"    #{t.trade_id} {t.side} @ {t.entry_price:.4f}  bet=${t.bet_size:.2f}")

    print_session_summary(balance, session_start, original_balance, all_trades, skipped)
    return balance, open_trades


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    state        = load_state()
    session      = state["total_sessions"] + 1
    balance      = state["balance"]
    open_trades  = trades_from_json(state.get("open_trades", []))

    # Auto-reset if balance too low to trade
    if balance < MIN_BET_SIZE:
        print(f"\n  Balance ${balance:.2f} too low — resetting to ${STARTING_BALANCE:.2f}\n")
        balance = STARTING_BALANCE
        open_trades = []
        state["original_balance"] = STARTING_BALANCE

    while True:
        print(f"\n  {'═' * 50}")
        print(f"  SESSION #{session}  |  Balance: ${balance:.2f}  |  Open trades: {len(open_trades)}")
        print(f"  {'═' * 50}")
        balance, open_trades = run_bot(balance, state["original_balance"], open_trades)
        save_state(balance, state["original_balance"], session, open_trades)
        session += 1
        print("  Restarting in 5 seconds...\n")
        time.sleep(5)
