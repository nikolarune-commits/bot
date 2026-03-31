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
MAX_BET_SIZE       = 8.00     # Max $ per trade
MIN_BET_SIZE       = 3.00     # Min $ per trade
MAX_OPEN_TRADES    = 3        # Max concurrent positions
FEE_RATE           = 0.02     # 2% fee (matches real Polymarket)
ENTRY_MIN_PRICE    = 0.28     # Don't buy below this (too risky)
ENTRY_MAX_PRICE    = 0.48     # Only buy the cheaper/underpriced side
SCAN_INTERVAL      = 2.0      # Seconds between scans
SCANS_PER_SESSION  = 300      # ~10 min per session
API_REFRESH_SCANS  = 30       # Re-fetch markets every N scans (~60s)
SAVE_CSV           = True
CSV_FILENAME       = "trade_log.csv"
POLYMARKET_API     = "https://gamma-api.polymarket.com"
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
# POLYMARKET API
# ─────────────────────────────────────────────

_market_cache: dict[str, Market] = {}
_cache_last_scan: int = -9999


def _fetch_markets(include_closed: bool = False) -> dict[str, Market]:
    """Fetch Bitcoin Up/Down markets from Polymarket API."""
    results = {}
    try:
        params = {"limit": 100}
        if not include_closed:
            params["active"] = "true"
            params["closed"] = "false"
        else:
            params["closed"] = "true"

        resp = requests.get(f"{POLYMARKET_API}/markets", params=params, timeout=10)
        resp.raise_for_status()

        for m in resp.json():
            try:
                question = m.get("question", "")
                if "Bitcoin Up or Down" not in question:
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
                if up_p < 0 or down_p < 0:
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
        print(f"  [API] Fetch failed: {e}")
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
            print(f"  [API] {active_count} active Bitcoin Up/Down markets loaded")
        _cache_last_scan = scan_num
    return _market_cache


# ─────────────────────────────────────────────
# TRADING SIGNAL
# ─────────────────────────────────────────────

def find_signal(market: Market) -> Optional[tuple[str, float]]:
    """
    Returns (side, price) to buy, or None if no signal.
    Strategy: buy the cheaper side when it's in the value zone.
    Lower price = bigger payout if correct, but harder to win.
    Only trade liquid markets.
    """
    if market.closed or market.liquidity < 200:
        return None

    down = market.down_price
    up   = market.up_price

    if ENTRY_MIN_PRICE <= down <= ENTRY_MAX_PRICE:
        return ("DOWN", down)
    if ENTRY_MIN_PRICE <= up <= ENTRY_MAX_PRICE:
        return ("UP", up)
    return None


# ─────────────────────────────────────────────
# TRADE RESOLUTION (REAL)
# ─────────────────────────────────────────────

def check_resolution(trade: Trade, markets: dict[str, Market]) -> Optional[tuple[float, str, float]]:
    """
    Check if a trade resolved by looking at actual market price.
    On Polymarket, winning side goes to 1.00, losing side to 0.00.
    Returns (profit, status, exit_price) or None if still open.
    """
    m = markets.get(trade.market_id)
    if not m:
        return None

    current = m.up_price if trade.side == "UP" else m.down_price

    if current >= 0.97:
        # Market resolved — our side WON
        gross  = trade.shares * 1.0
        fees   = FEE_RATE * trade.bet_size
        profit = round(gross - trade.bet_size - fees, 4)
        return (profit, "WIN", current)

    if current <= 0.03:
        # Market resolved — our side LOST
        profit = round(-trade.bet_size, 4)
        return (profit, "LOSS", current)

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

def load_state() -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILE)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"balance": STARTING_BALANCE, "original_balance": STARTING_BALANCE, "total_sessions": 0}

def save_state(balance: float, original_balance: float, total_sessions: int):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILE)
    with open(path, "w") as f:
        json.dump({
            "balance": balance,
            "original_balance": original_balance,
            "total_sessions": total_sessions,
        }, f)


# ─────────────────────────────────────────────
# MAIN BOT LOOP
# ─────────────────────────────────────────────

def run_bot(balance: float, original_balance: float) -> float:
    session_start = balance
    open_trades: list[Trade] = []
    all_trades:  list[Trade] = []
    trade_id_seq = 0
    skipped      = 0
    total_pnl    = 0.0

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

        # ── 4. Size the bet (max 10% of balance per trade)
        bet = round(min(MAX_BET_SIZE, balance * 0.10), 2)
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

    # ── End of session: close remaining positions at current market price (mark-to-market)
    print(f"\n{'─' * 52}")
    print("  Closing remaining positions at current price...")
    for t in open_trades:
        m = markets.get(t.market_id)
        if m:
            current = m.up_price if t.side == "UP" else m.down_price
            gross   = t.shares * current
            profit  = round(gross - t.bet_size - FEE_RATE * t.bet_size, 4)
            status  = "WIN" if profit > 0 else "LOSS"
            exit_p  = current
        else:
            profit = round(-t.bet_size * 0.5, 4)
            status = "LOSS"
            exit_p = 0.0

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

    print_session_summary(balance, session_start, original_balance, all_trades, skipped)
    return balance


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    state   = load_state()
    session = state["total_sessions"] + 1
    balance = state["balance"]

    while True:
        print(f"\n  {'═' * 50}")
        print(f"  SESSION #{session}  |  Balance: ${balance:.2f}")
        print(f"  {'═' * 50}")
        balance = run_bot(balance, state["original_balance"])
        save_state(balance, state["original_balance"], session)
        session += 1
        print("  Restarting in 5 seconds...\n")
        time.sleep(5)
