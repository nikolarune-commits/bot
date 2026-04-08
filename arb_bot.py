"""
Polymarket Paper Trading Bot
=============================
Real prices from Polymarket's public API.
Strategy: BTC momentum signals on Bitcoin Up/Down 5-minute markets.
Balance is fake (paper trading) — no real money moves.
"""

import time
import csv
import os
import json
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
STARTING_BALANCE   = 100.00   # Starting balance (first run only)
BET_PCT            = 0.05     # Bet 5% of balance per trade
MAX_BET_SIZE       = 6.00     # Hard cap per trade
MIN_BET_SIZE       = 1.00     # Minimum to place a trade
MAX_OPEN_TRADES    = 1        # One trade at a time
FEE_RATE           = 0.02     # 2% fee (matches real Polymarket)
ENTRY_MIN_PRICE    = 0.38     # Skip if market has priced out the move
ENTRY_MAX_PRICE    = 0.70     # Skip near-certainties (low payout)
WINDOW_MIN_AGE     = 20       # Don't trade in the first 20s (market still adjusting)
WINDOW_MAX_AGE     = 150      # Don't trade after 2.5 min (move already priced in)
CONFIDENCE_MIN     = 0.57     # ~4/7 minimum score — strong signal required
SCAN_INTERVAL      = 2.0      # Seconds between loop iterations
SCANS_PER_SESSION  = 300      # ~10 min per session
API_REFRESH_SCANS  = 30       # Re-fetch markets every N scans (~60 s)
BTC_CACHE_SECONDS  = 8        # Refresh BTC data every 8 seconds
RESOLVE_TIMEOUT    = 8        # Minutes before forcing resolution via BTC price
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
    market_id:  str
    name:       str
    up_price:   float
    down_price: float
    liquidity:  float
    closed:     bool = False


@dataclass
class Trade:
    """A single paper trade."""
    trade_id:          int
    market:            str
    market_id:         str
    side:              str           # "UP" or "DOWN"
    entry_price:       float         # Polymarket price paid per share
    shares:            float         # units bought = bet_size / entry_price
    bet_size:          float         # USD staked
    btc_entry_price:   float = 0.0   # BTC/USD at trade open (for resolution fallback)
    actual_profit:     Optional[float] = None
    status:            str = "OPEN"
    open_time:         datetime = field(default_factory=datetime.now)
    close_time:        Optional[datetime] = None
    exit_price:        float = 0.0


# ─────────────────────────────────────────────
# BTC PRICE DATA  (Binance → Kraken fallback)
# ─────────────────────────────────────────────

def get_btc_candles(limit: int = 60) -> list:
    """
    Fetch recent 1-minute OHLCV candles.
    Tries Binance first; falls back to Kraken if Binance is unavailable.
    Returns list of [ts_ms, open, high, low, close, ...] rows (Binance-style).
    """
    try:
        r = requests.get(f"{BINANCE_API}/klines",
                         params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
                         timeout=5)
        if r.ok and isinstance(r.json(), list) and r.json():
            return r.json()
    except Exception:
        pass

    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC",
                         params={"pair": "XBTUSD", "interval": 1},
                         timeout=5)
        if r.ok:
            result = r.json().get("result", {})
            rows   = result.get("XXBTZUSD", result.get("XBTUSD", []))
            if rows:
                out = []
                for row in rows[-limit:]:
                    out.append([int(row[0]) * 1000, row[1], row[2], row[3], row[4]])
                print("  [BTC] Using Kraken data")
                return out
    except Exception:
        pass

    print("  [BTC] All price sources failed")
    return []


def get_order_book(limit: int = 20) -> tuple:
    """Fetch top bid/ask levels. Binance → Kraken fallback."""
    try:
        r = requests.get(f"{BINANCE_API}/depth",
                         params={"symbol": "BTCUSDT", "limit": limit},
                         timeout=5)
        if r.ok:
            d = r.json()
            return d["bids"], d["asks"]
    except Exception:
        pass

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
    k   = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calc_rsi(closes: list, period: int = 14) -> float:
    """RSI (0–100). Returns 50 if not enough data."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0)     for d in deltas[-period:]]
    losses = [abs(min(d, 0)) for d in deltas[-period:]]
    avg_g  = sum(gains)  / period
    avg_l  = sum(losses) / period
    if avg_l == 0:
        return 100.0
    return 100 - (100 / (1 + avg_g / avg_l))


def calc_obi(bids: list, asks: list, levels: int = 10) -> float:
    """Order Book Imbalance: +1 = pure buy pressure, -1 = pure sell pressure."""
    bid_vol = sum(float(b[1]) for b in bids[:levels])
    ask_vol = sum(float(a[1]) for a in asks[:levels])
    total   = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0


def calc_window_delta(candles: list) -> float:
    """% BTC price moved since the current 5-minute Polymarket window opened."""
    if not candles:
        return 0.0
    now          = int(time.time())
    window_ts_ms = (now - now % 300) * 1000
    # Find first candle at or after window start
    window_candle = next((c for c in candles if c[0] >= window_ts_ms), candles[-1])
    window_open   = float(window_candle[1])
    current       = float(candles[-1][4])
    if window_open == 0:
        return 0.0
    return (current - window_open) / window_open * 100


def calc_sustained_momentum(closes: list) -> float:
    """
    Checks if price is moving consistently in one direction.
    Returns +1 if last 4 minutes consistently UP, -1 if DOWN, 0 if mixed.
    """
    if len(closes) < 5:
        return 0.0
    # Look at 4 consecutive 1-min changes
    changes = [closes[i] - closes[i - 1] for i in range(len(closes) - 4, len(closes))]
    ups   = sum(1 for c in changes if c > 0)
    downs = sum(1 for c in changes if c < 0)
    if ups >= 3:    return 1.0   # 3+ of last 4 minutes were up
    if downs >= 3:  return -1.0  # 3+ of last 4 minutes were down
    return 0.0


def seconds_into_window() -> int:
    """How many seconds have elapsed since the current 5-min Polymarket window opened."""
    return int(time.time()) % 300


# ─────────────────────────────────────────────
# BTC SIGNAL SYSTEM
# ─────────────────────────────────────────────

_btc_cache: dict       = {}
_btc_cache_time: float = 0.0


def _refresh_btc_cache():
    """Fetch and cache BTC candles + order book. Called once per BTC_CACHE_SECONDS."""
    global _btc_cache, _btc_cache_time
    now = time.time()
    if now - _btc_cache_time > BTC_CACHE_SECONDS:
        candles    = get_btc_candles(60)   # 60 min of 1-min candles
        bids, asks = get_order_book(20)
        if candles:
            _btc_cache      = {"candles": candles, "bids": bids, "asks": asks}
            _btc_cache_time = now


def btc_signal() -> Optional[tuple[str, float]]:
    """
    5 independent BTC indicators → directional signal + confidence.
    Returns (direction, confidence) or None if confidence < CONFIDENCE_MIN.

    Scoring:
      Window Delta    weight 5–7   — % BTC moved since current 5-min window opened
      30-min Trend    weight 2     — EMA15 vs EMA30 macro direction
      EMA 9/21        weight 1     — 5-min micro trend
      RSI 14          weight 1     — directional momentum (no counter-trend traps)
      Order Book Imb  weight 1–2   — real-time buy vs sell pressure
      Sustained Mom   weight 1     — consistent direction over last 4 min
    """
    _refresh_btc_cache()

    candles = _btc_cache.get("candles", [])
    bids    = _btc_cache.get("bids",    [])
    asks    = _btc_cache.get("asks",    [])
    if not candles:
        return None

    closes = [float(c[4]) for c in candles]
    score  = 0.0

    # ── Signal 1: Window Delta (weight 5–7) — core signal
    delta = calc_window_delta(candles)
    if   delta >  0.15: score += 7
    elif delta >  0.05: score += 5
    elif delta < -0.15: score -= 7
    elif delta < -0.05: score -= 5

    # ── Signal 2: 30-minute macro trend (weight 2) — anti-trend-fight filter
    ema15 = calc_ema(closes, 15)
    ema30 = calc_ema(closes, 30)
    gap   = (ema15 - ema30) / ema30 * 100   # % gap
    if   gap >  0.05: score += 2    # clear uptrend
    elif gap < -0.05: score -= 2    # clear downtrend
    # gap between ±0.05% = ranging, add nothing

    # ── Signal 3: Short-term EMA 9/21 crossover (weight 1)
    ema9  = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    score += 1 if ema9 > ema21 else -1

    # ── Signal 4: RSI directional bias (weight 1)
    # NOTE: we use directional RSI only, NOT extremes.
    # Oversold RSI in a strong downtrend is NOT a reversal — it's a trap.
    rsi = calc_rsi(closes)
    if   rsi > 60: score += 1    # upward momentum
    elif rsi < 40: score -= 1    # downward momentum
    # RSI 40–60 (neutral) = no contribution

    # ── Signal 5: Order Book Imbalance (weight 1–2)
    obi = calc_obi(bids, asks)
    if   obi >  0.3: score += 2
    elif obi >  0.1: score += 1
    elif obi < -0.3: score -= 2
    elif obi < -0.1: score -= 1

    # ── Signal 6: Sustained momentum over last 4 min (weight 1)
    momentum = calc_sustained_momentum(closes)
    score += momentum

    # Max possible score = 7+2+1+1+2+1 = 14 (perfectly aligned)
    # Normalize to 0–1 against a typical strong signal of 7
    confidence = min(abs(score) / 7.0, 1.0)
    direction  = "UP" if score > 0 else "DOWN"

    trend_str = f"+{gap:.2f}%" if gap > 0 else f"{gap:.2f}%"
    print(f"  [BTC] Δ={delta:+.3f}%  trend={trend_str}  EMA={'↑' if ema9>ema21 else '↓'}"
          f"  RSI={rsi:.0f}  OBI={obi:+.2f}  mom={momentum:+.0f}"
          f"  score={score:+.0f}  conf={confidence:.0%}  → {direction}")

    if confidence < CONFIDENCE_MIN:
        return None

    return (direction, confidence)


# ─────────────────────────────────────────────
# POLYMARKET API
# ─────────────────────────────────────────────

_market_cache:    dict[str, Market] = {}
_cache_last_scan: int = -9999


def _parse_market(m: dict) -> Optional[Market]:
    """Parse a single Polymarket market dict into a Market object."""
    try:
        question = m.get("question", "")
        prices   = m.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = json.loads(prices)
        if len(prices) != 2:
            return None
        up_p   = float(prices[0])
        down_p = float(prices[1])
        if up_p <= 0 or down_p <= 0:
            return None
        mid = str(m.get("id") or m.get("conditionId") or question)
        return Market(
            market_id  = mid,
            name       = question[:60],
            up_price   = up_p,
            down_price = down_p,
            liquidity  = float(m.get("liquidity") or 0),
            closed     = bool(m.get("closed", False)),
        )
    except (ValueError, TypeError, KeyError):
        return None


def _fetch_by_slug() -> dict[str, Market]:
    """
    Fetch the current Bitcoin Up/Down 5-min window market via deterministic slug.
    Checks current window and two previous windows (handles boundary edge cases).
    """
    results = {}
    now    = int(time.time())
    slugs  = [f"btc-updown-5m-{now - (now % 300) - i * 300}" for i in range(3)]
    for slug in slugs:
        try:
            resp = requests.get(f"{POLYMARKET_API}/events",
                                params={"slug": slug}, timeout=8)
            if not resp.ok:
                continue
            data   = resp.json()
            events = data if isinstance(data, list) else [data]
            for event in events:
                if not event:
                    continue
                for m in event.get("markets", []):
                    market = _parse_market(m)
                    if market:
                        results[market.market_id] = market
                        print(f"  [API] Slug hit: {market.name[:45]}"
                              f"  UP:{market.up_price:.2f}  DN:{market.down_price:.2f}")
        except Exception:
            continue
    return results


def _fetch_markets() -> dict[str, Market]:
    """Primary: slug lookup. Fallback: text search."""
    results = _fetch_by_slug()
    if results:
        return results
    for query in ["Bitcoin Up or Down", "BTC Up or Down"]:
        try:
            resp = requests.get(f"{POLYMARKET_API}/markets",
                                params={"limit": 50, "q": query}, timeout=10)
            if not resp.ok:
                continue
            for m in resp.json():
                q = m.get("question", "").lower()
                if ("bitcoin" not in q and "btc" not in q):
                    continue
                if not any(k in q for k in ("up or down", "5 min", "5-min", "updown")):
                    continue
                market = _parse_market(m)
                if market:
                    results[market.market_id] = market
        except Exception as e:
            print(f"  [API] Search failed ({query}): {e}")
    return results


def refresh_cache(scan_num: int) -> dict[str, Market]:
    """Refresh market cache every API_REFRESH_SCANS iterations."""
    global _market_cache, _cache_last_scan
    if scan_num - _cache_last_scan >= API_REFRESH_SCANS or not _market_cache:
        markets = _fetch_markets()
        if markets:
            _market_cache = markets
            active = sum(1 for m in markets.values() if not m.closed)
            print(f"  [API] {active} active BTC market(s) loaded")
        _cache_last_scan = scan_num
    return _market_cache


# ─────────────────────────────────────────────
# TRADING SIGNAL
# ─────────────────────────────────────────────

def find_signal(market: Market) -> Optional[tuple[str, float]]:
    """
    Gate 1 — Window timing: only trade in seconds WINDOW_MIN_AGE to WINDOW_MAX_AGE.
    Gate 2 — BTC signal: 6-indicator system must pass confidence threshold.
    Gate 3 — Price range: Polymarket price for our direction must be in range.
    """
    if market.closed:
        return None

    # Gate 1: Window timing
    age = seconds_into_window()
    if age < WINDOW_MIN_AGE:
        print(f"  [WINDOW] Too early in window ({age}s) — waiting for market to settle")
        return None
    if age > WINDOW_MAX_AGE:
        print(f"  [WINDOW] Too late in window ({age}s) — move already priced in")
        return None

    # Gate 2: BTC momentum signal
    sig = btc_signal()
    if not sig:
        return None
    direction, confidence = sig

    # Gate 3: Polymarket price sanity check
    price = market.up_price if direction == "UP" else market.down_price
    if not (ENTRY_MIN_PRICE <= price <= ENTRY_MAX_PRICE):
        print(f"  [PRICE] {direction} @ {price:.2f} outside range "
              f"[{ENTRY_MIN_PRICE:.2f}-{ENTRY_MAX_PRICE:.2f}]")
        return None

    return (direction, confidence)


# ─────────────────────────────────────────────
# TRADE RESOLUTION
# ─────────────────────────────────────────────

def check_resolution(trade: Trade, markets: dict[str, Market]) -> Optional[tuple[float, str, float]]:
    """
    Resolve a trade by checking Polymarket's market price.
    Winning side reaches 1.00, losing side reaches 0.00.

    Timeout (RESOLVE_TIMEOUT minutes): Instead of random fallback, we compare
    the current BTC price to BTC price at trade entry. This mirrors how Chainlink
    resolves these markets (price at T+300 vs price at T+0).
    """
    m = markets.get(trade.market_id)

    if m:
        current = m.up_price if trade.side == "UP" else m.down_price
        if current >= 0.97:
            gross  = trade.shares * 1.0
            profit = round(gross - trade.bet_size - FEE_RATE * trade.bet_size, 4)
            return (profit, "WIN", current)
        if current <= 0.03:
            return (round(-trade.bet_size, 4), "LOSS", current)

    age_min = (datetime.now() - trade.open_time).total_seconds() / 60
    if age_min >= RESOLVE_TIMEOUT:
        # Use BTC price movement as resolution proxy (matches Chainlink oracle)
        candles = _btc_cache.get("candles", [])
        if candles and trade.btc_entry_price > 0:
            current_btc = float(candles[-1][4])
            btc_went_up = current_btc >= trade.btc_entry_price
            won = (trade.side == "UP" and btc_went_up) or \
                  (trade.side == "DOWN" and not btc_went_up)
            if won:
                gross  = trade.shares * 1.0
                profit = round(gross - trade.bet_size - FEE_RATE * trade.bet_size, 4)
                return (profit, "WIN", 1.0)
            else:
                return (round(-trade.bet_size, 4), "LOSS", 0.0)
        else:
            # No BTC data → conservative: count as loss
            return (round(-trade.bet_size, 4), "LOSS", 0.0)

    return None   # Still open


# ─────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────

SEP = "─" * 52

def print_header(scan_num: int, total: int):
    age = seconds_into_window()
    remaining = 300 - age
    print(f"\n{'═' * 52}")
    print(f"  SCAN #{scan_num:03d}/{total}  |  {datetime.now().strftime('%H:%M:%S')}"
          f"  |  Window: {age}s in / {remaining}s left")
    print(f"{'═' * 52}")

def print_market(m: Market):
    print(f"  Market : {m.name}")
    print(f"  UP     : {m.up_price:.4f}  |  DOWN: {m.down_price:.4f}")
    print(f"  Liq    : ${m.liquidity:,.2f}")

def print_trade_open(side: str, price: float, bet: float, shares: float,
                     profit_if_win: float, confidence: float):
    print(SEP)
    print(f"  ACTION         : BUY {side}  (confidence {confidence:.0%})")
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

def print_balance(balance: float, open_count: int, session_pnl: float):
    sign = "+" if session_pnl >= 0 else ""
    print(f"\n  BALANCE    : ${balance:.2f}  |  Open: {open_count}"
          f"  |  Session P&L: {sign}${session_pnl:.2f}")

def print_session_summary(balance, session_start, original_balance, trades, skipped,
                          consec_losses):
    closed = [t for t in trades if t.status in ("WIN", "LOSS")]
    wins   = [t for t in closed if t.status == "WIN"]
    losses = [t for t in closed if t.status == "LOSS"]

    win_rate    = (len(wins) / len(closed) * 100) if closed else 0
    total_profit = sum(t.actual_profit for t in closed)

    print(f"\n{'═' * 52}")
    print(f"  SESSION SUMMARY")
    print(f"{'═' * 52}")
    print(f"  Balance        : ${balance:.2f}")
    print(f"  Session P&L    : ${balance - session_start:+.2f}")
    print(f"  All-time P&L   : ${balance - original_balance:+.2f}"
          f"  (started ${original_balance:.2f})")
    print(f"{'─' * 52}")
    print(f"  Trades         : {len(closed)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate       : {win_rate:.1f}%")
    print(f"  Skipped        : {skipped}")
    print(f"  Total P&L      : ${total_profit:+.4f}")
    print(f"  Loss streak    : {consec_losses}")
    print(f"{'═' * 52}\n")


# ─────────────────────────────────────────────
# CSV LOGGER
# ─────────────────────────────────────────────

def init_csv(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow([
                "trade_id", "market", "side", "entry_price", "shares",
                "bet_size", "btc_entry_price", "exit_price",
                "actual_profit", "status", "open_time", "close_time",
            ])

def append_csv(path: str, trade: Trade):
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow([
            trade.trade_id, trade.market, trade.side,
            trade.entry_price, trade.shares, trade.bet_size,
            trade.btc_entry_price, trade.exit_price,
            trade.actual_profit, trade.status,
            trade.open_time.strftime("%Y-%m-%d %H:%M:%S"),
            trade.close_time.strftime("%Y-%m-%d %H:%M:%S") if trade.close_time else "",
        ])


# ─────────────────────────────────────────────
# BALANCE PERSISTENCE
# ─────────────────────────────────────────────

def trades_to_json(trades: list) -> list:
    return [{
        "trade_id":        t.trade_id,
        "market":          t.market,
        "market_id":       t.market_id,
        "side":            t.side,
        "entry_price":     t.entry_price,
        "shares":          t.shares,
        "bet_size":        t.bet_size,
        "btc_entry_price": t.btc_entry_price,
        "open_time":       t.open_time.isoformat(),
    } for t in trades]

def trades_from_json(data: list) -> list:
    result = []
    for d in (data or []):
        try:
            result.append(Trade(
                trade_id        = d["trade_id"],
                market          = d["market"],
                market_id       = d["market_id"],
                side            = d["side"],
                entry_price     = d["entry_price"],
                shares          = d["shares"],
                bet_size        = d["bet_size"],
                btc_entry_price = d.get("btc_entry_price", 0.0),
                open_time       = datetime.fromisoformat(d["open_time"]),
            ))
        except (KeyError, ValueError):
            continue
    return result

def load_state() -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILE)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "balance":          STARTING_BALANCE,
        "original_balance": STARTING_BALANCE,
        "total_sessions":   0,
        "open_trades":      [],
        "consec_losses":    0,
        "cooldown_until":   None,
    }

def save_state(balance: float, original_balance: float, total_sessions: int,
               open_trades: list, consec_losses: int = 0,
               cooldown_until: Optional[datetime] = None):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILE)
    with open(path, "w") as f:
        json.dump({
            "balance":          balance,
            "original_balance": original_balance,
            "total_sessions":   total_sessions,
            "open_trades":      trades_to_json(open_trades),
            "consec_losses":    consec_losses,
            "cooldown_until":   cooldown_until.isoformat() if cooldown_until else None,
        }, f)


# ─────────────────────────────────────────────
# COOLDOWN CALCULATION
# ─────────────────────────────────────────────

def cooldown_for_streak(n: int) -> int:
    """
    Escalating cooldown minutes based on consecutive loss count.
    2 losses →  5 min   (brief pause)
    3 losses → 10 min   (something is wrong with signals)
    4 losses → 20 min   (strong trend against us — sit out)
    5+ losses → 30 min  (market regime clearly against our model)
    """
    if n < 2:  return 0
    if n == 2: return 5
    if n == 3: return 10
    if n == 4: return 20
    return 30


# ─────────────────────────────────────────────
# MAIN BOT LOOP
# ─────────────────────────────────────────────

def run_bot(balance: float, original_balance: float, carried_trades: list,
            consec_losses_in: int = 0,
            cooldown_until_in: Optional[datetime] = None) -> tuple:
    session_start = balance
    open_trades   = carried_trades
    all_trades    = []
    trade_id_seq  = max((t.trade_id for t in open_trades), default=0)
    skipped       = 0
    session_pnl   = 0.0
    consec_losses = consec_losses_in

    # Restore cooldown timestamp from previous session (only if still in the future)
    cooldown_until = cooldown_until_in
    if cooldown_until and datetime.now() >= cooldown_until:
        cooldown_until = None   # already expired — don't block trading
    if cooldown_until:
        remaining_min = int((cooldown_until - datetime.now()).total_seconds() / 60)
        print(f"  [STRATEGY] Loss streak {consec_losses} — cooldown "
              f"{remaining_min} min remaining")

    if SAVE_CSV:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CSV_FILENAME)
        init_csv(csv_path)

    print(f"  Balance  : ${balance:.2f}")
    print(f"  Entry    : {ENTRY_MIN_PRICE:.2f}–{ENTRY_MAX_PRICE:.2f}"
          f"  |  Confidence min: {CONFIDENCE_MIN:.0%}"
          f"  |  Window: {WINDOW_MIN_AGE}–{WINDOW_MAX_AGE}s\n")

    for scan_num in range(1, SCANS_PER_SESSION + 1):
        markets = refresh_cache(scan_num)

        # ── 1. Resolve any open trades
        still_open = []
        for t in open_trades:
            result = check_resolution(t, markets)
            if result:
                profit, status, exit_p = result
                t.actual_profit = profit
                t.status        = status
                t.exit_price    = exit_p
                t.close_time    = datetime.now()
                balance     += t.bet_size + profit
                session_pnl += profit
                print_resolution(t)
                all_trades.append(t)
                # Update streak and cooldown
                if status == "LOSS":
                    consec_losses += 1
                    cd_min = cooldown_for_streak(consec_losses)
                    if cd_min > 0:
                        cooldown_until = datetime.now() + timedelta(minutes=cd_min)
                        print(f"  [STRATEGY] Streak={consec_losses}"
                              f" — cooldown {cd_min} min")
                else:
                    consec_losses = 0
                    cooldown_until = None
                if SAVE_CSV:
                    append_csv(csv_path, t)
            else:
                still_open.append(t)
        open_trades = still_open

        # ── 2. Find an active market
        active_markets = [m for m in markets.values() if not m.closed]
        if not active_markets:
            print_header(scan_num, SCANS_PER_SESSION)
            print("  [API] No active markets — waiting for next window...")
            time.sleep(SCAN_INTERVAL)
            continue

        market = active_markets[0]   # always one 5-min market at a time
        print_header(scan_num, SCANS_PER_SESSION)
        print_market(market)

        # ── 3. Pre-trade gates
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

        if cooldown_until and datetime.now() < cooldown_until:
            remaining = int((cooldown_until - datetime.now()).total_seconds())
            print_skipped(f"Loss-streak cooldown ({remaining}s remaining"
                          f", streak={consec_losses})")
            skipped += 1
            time.sleep(SCAN_INTERVAL)
            continue

        # ── 4. Signal check
        signal = find_signal(market)
        if not signal:
            print_skipped("No signal")
            skipped += 1
            time.sleep(SCAN_INTERVAL)
            continue

        side, confidence = signal
        price = market.up_price if side == "UP" else market.down_price

        # ── 5. Confidence-scaled bet sizing
        # Higher confidence = bet closer to the 5% cap
        # Confidence 57–70%  → 3% of balance
        # Confidence 70–85%  → 4% of balance
        # Confidence 85%+    → 5% of balance
        if confidence >= 0.85:
            pct = BET_PCT          # 5%
        elif confidence >= 0.70:
            pct = BET_PCT * 0.80   # 4%
        else:
            pct = BET_PCT * 0.60   # 3%

        bet = round(min(MAX_BET_SIZE, max(MIN_BET_SIZE, balance * pct)), 2)
        if bet < MIN_BET_SIZE:
            print_skipped("Insufficient balance for minimum bet")
            skipped += 1
            time.sleep(SCAN_INTERVAL)
            continue

        shares        = round(bet / price, 4)
        profit_if_win = round(shares * 1.0 - bet - FEE_RATE * bet, 4)

        # Get current BTC price for resolution tracking
        btc_candles = _btc_cache.get("candles", [])
        btc_now     = float(btc_candles[-1][4]) if btc_candles else 0.0

        # ── 6. Open the trade
        trade_id_seq += 1
        trade = Trade(
            trade_id        = trade_id_seq,
            market          = market.name,
            market_id       = market.market_id,
            side            = side,
            entry_price     = price,
            shares          = shares,
            bet_size        = bet,
            btc_entry_price = btc_now,
        )

        balance -= bet
        open_trades.append(trade)

        print_trade_open(side, price, bet, shares, profit_if_win, confidence)
        print_balance(balance, len(open_trades), session_pnl)

        time.sleep(SCAN_INTERVAL)

    # ── End of session
    if open_trades:
        print(f"\n{'─' * 52}")
        print(f"  {len(open_trades)} trade(s) carrying to next session:")
        for t in open_trades:
            age = (datetime.now() - t.open_time).total_seconds() / 60
            print(f"    #{t.trade_id} {t.side} @ {t.entry_price:.4f}"
                  f"  bet=${t.bet_size:.2f}  age={age:.1f}min")

    print_session_summary(balance, session_start, original_balance,
                          all_trades, skipped, consec_losses)
    return balance, open_trades, consec_losses, cooldown_until


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    state       = load_state()
    session     = state["total_sessions"] + 1
    balance     = state["balance"]
    open_trades = trades_from_json(state.get("open_trades", []))

    if balance < MIN_BET_SIZE:
        print(f"\n  Balance ${balance:.2f} too low — resetting to ${STARTING_BALANCE:.2f}\n")
        balance     = STARTING_BALANCE
        open_trades = []
        state["original_balance"] = STARTING_BALANCE

    consec_losses = state.get("consec_losses", 0)
    cd_str        = state.get("cooldown_until")
    cooldown_until = datetime.fromisoformat(cd_str) if cd_str else None

    while True:
        streak_str = f"  |  Loss streak: {consec_losses}" if consec_losses > 0 else ""
        print(f"\n  {'═' * 50}")
        print(f"  SESSION #{session}"
              f"  |  Balance: ${balance:.2f}"
              f"  |  Open: {len(open_trades)}{streak_str}")
        print(f"  {'═' * 50}")

        balance, open_trades, consec_losses, cooldown_until = run_bot(
            balance, state["original_balance"], open_trades, consec_losses, cooldown_until)
        save_state(balance, state["original_balance"], session, open_trades,
                   consec_losses, cooldown_until)
        session += 1
        print("  Restarting in 5 seconds...\n")
        time.sleep(5)
