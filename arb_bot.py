"""
Polymarket Paper Trading Bot
=============================
Uses REAL market data from Polymarket's public API.
Trades are simulated (fake balance) — no real money moves.
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
STARTING_BALANCE    = 100.00      # Initial wallet balance in USD (first run only)
MAX_RISK_PER_TRADE  = 10.00       # Hard cap per arbitrage position
MIN_POSITION_SIZE   = 5.00        # Minimum bet size
MAX_OPEN_TRADES     = 3           # Maximum concurrent open positions
FEE_RATE            = 0.02        # 2% total round-trip fee (per the spec)
ARB_THRESHOLD       = 0.96        # YES + NO must be below this (needs 4%+ gap)
TARGET_TRADES       = 0           # 0 = run forever (loop restarts session)
SCAN_INTERVAL       = 0.12        # Seconds between scans (simulated latency)
SAVE_CSV            = True        # Save trade log to CSV
CSV_FILENAME        = "trade_log.csv"

POLYMARKET_API = "https://gamma-api.polymarket.com"
CACHE_REFRESH_INTERVAL = 50   # re-fetch real markets every N scans

# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class MarketSnapshot:
    """Represents a single market's current state."""
    name: str
    yes_price: float
    no_price: float
    total: float
    liquidity: float          # Available USD liquidity in the order book
    spread_gap: float         # 1.00 - total  (positive = arb opportunity)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Trade:
    """Records an executed arbitrage trade."""
    trade_id: int
    market: str
    yes_price: float
    no_price: float
    total: float
    position_size: float
    expected_profit: float
    actual_profit: Optional[float] = None
    status: str = "OPEN"       # OPEN | WIN | LOSS | FAILED
    open_time: datetime = field(default_factory=datetime.now)
    close_time: Optional[datetime] = None
    slippage: float = 0.0
    reason: Optional[str] = None


# ─────────────────────────────────────────────
# REAL MARKET DATA (POLYMARKET API)
# ─────────────────────────────────────────────

_market_cache: list[dict] = []
_cache_last_scan: int = -9999


def _refresh_market_cache() -> bool:
    """Fetch live binary markets from Polymarket's public API."""
    global _market_cache
    try:
        resp = requests.get(
            f"{POLYMARKET_API}/markets",
            params={"active": "true", "closed": "false", "limit": 100},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        markets = []
        for m in raw:
            try:
                outcomes = m.get("outcomes", [])
                prices   = m.get("outcomePrices", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if len(outcomes) != 2 or len(prices) != 2:
                    continue
                question = m.get("question", "")
                if "Bitcoin Up or Down" not in question:
                    continue
                yes_p = float(prices[0])
                no_p  = float(prices[1])
                liq   = float(m.get("liquidity") or 0)
                if not (0.01 <= yes_p <= 0.99 and 0.01 <= no_p <= 0.99):
                    continue
                if liq < 10:
                    continue
                markets.append({
                    "name":      m.get("question", "Unknown")[:60],
                    "yes_price": yes_p,
                    "no_price":  no_p,
                    "liquidity": liq,
                })
            except (ValueError, TypeError, KeyError):
                continue
        if markets:
            _market_cache = markets
            print(f"  [API] {len(markets)} Bitcoin Up/Down markets loaded from Polymarket")
            return True
        return False
    except Exception as e:
        print(f"  [API] Fetch failed: {e} — using last cache")
        return False


def generate_market_snapshot(scan_num: int) -> MarketSnapshot:
    """Pick a real market from the cache; refresh cache every N scans."""
    global _cache_last_scan

    if scan_num - _cache_last_scan >= CACHE_REFRESH_INTERVAL or not _market_cache:
        _refresh_market_cache()
        _cache_last_scan = scan_num

    if _market_cache:
        m     = random.choice(_market_cache)
        yes   = round(m["yes_price"], 4)
        no    = round(m["no_price"],  4)
        total = round(yes + no, 4)
        return MarketSnapshot(
            name=m["name"],
            yes_price=yes,
            no_price=no,
            total=total,
            liquidity=round(m["liquidity"], 2),
            spread_gap=round(1.0 - total, 4),
        )

    # Fallback: simulate if API is completely unavailable
    yes = round(random.uniform(0.35, 0.65), 4)
    no  = round(1.0 - yes + random.uniform(-0.004, 0.004), 4)
    yes = max(0.01, min(0.99, yes))
    no  = max(0.01, min(0.99, no))
    return MarketSnapshot(
        name="[OFFLINE] Simulated Market",
        yes_price=yes,
        no_price=no,
        total=round(yes + no, 4),
        liquidity=round(random.uniform(20, 500), 2),
        spread_gap=round(1.0 - yes - no, 4),
    )


# ─────────────────────────────────────────────
# SLIPPAGE & FAILURE SIMULATOR
# ─────────────────────────────────────────────

def simulate_execution(snapshot: MarketSnapshot, position_size: float):
    """
    Determines whether a trade executes cleanly, slips, or fails outright.

    Returns (success: bool, fill_price_yes, fill_price_no, reason)
    """
    fail_roll = random.random()

    # 8% chance of outright failure (price moved, connection timeout, etc.)
    if fail_roll < 0.08:
        reasons = [
            "Price moved too fast",
            "Connection timeout",
            "Order rejected by exchange",
            "Market paused",
        ]
        return False, 0, 0, random.choice(reasons)

    # Simulate trade latency (50–200 ms)
    latency_ms = random.randint(50, 200)
    time.sleep(latency_ms / 1000)

    # 15% chance of partial fill / slippage (prices worsen slightly)
    if fail_roll < 0.23:
        slip_yes = random.uniform(0.001, 0.008)
        slip_no  = random.uniform(0.001, 0.008)
        fill_yes = round(snapshot.yes_price + slip_yes, 4)
        fill_no  = round(snapshot.no_price  + slip_no,  4)
        return True, fill_yes, fill_no, "Slippage"

    # Clean fill at quoted prices
    return True, snapshot.yes_price, snapshot.no_price, None


# ─────────────────────────────────────────────
# PROFIT CALCULATOR
# ─────────────────────────────────────────────

def calculate_profit(yes_price: float, no_price: float, size: float) -> float:
    """
    Arbitrage P&L formula:
      - You buy YES at yes_price and NO at no_price.
      - One side always wins (pays 1.00).
      - Cost = (yes_price + no_price) * size
      - Revenue = 1.00 * size
      - Gross profit = (1 - (yes + no)) * size
      - Net profit after 2% fees on each leg = gross - fee
    """
    gross = (1.0 - (yes_price + no_price)) * size
    fees  = FEE_RATE * size              # 2% total round-trip fee (spec formula)
    return round(gross - fees, 4)


# ─────────────────────────────────────────────
# OUTCOME RESOLVER
# ─────────────────────────────────────────────

def resolve_trade(trade: Trade) -> Trade:
    """
    Resolves an OPEN trade after a short holding period.

    In real arb both legs pay back at resolution.
    We model a small variance: ~80% of the time the arb pays as expected,
    ~15% the market corrects and we lose a little, ~5% a big correction hurts.
    """
    roll = random.random()

    if roll < 0.80:
        # Clean arb: expected profit materialises
        actual = trade.expected_profit
        status = "WIN"
    elif roll < 0.95:
        # Partial loss: market corrected — capped at $0.50 max loss
        actual = round(-min(random.uniform(0.01, 0.10) * trade.position_size, 0.50), 4)
        status = "LOSS"
    else:
        # Big correction — capped at $1.00 max loss
        actual = round(-min(random.uniform(0.10, 0.20) * trade.position_size, 1.00), 4)
        status = "LOSS"

    trade.actual_profit = actual
    trade.status        = status
    trade.close_time    = datetime.now()
    return trade


# ─────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────

SEPARATOR = "─" * 48

def print_header(scan_num: int, total: int):
    print(f"\n{'═' * 48}")
    print(f"  SCAN #{scan_num:03d} / {total}  |  {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
    print(f"{'═' * 48}")


def print_market(snap: MarketSnapshot):
    gap_flag = " ◄ ARB" if snap.spread_gap > (1 - ARB_THRESHOLD) else ""
    print(f"  Market    : {snap.name}")
    print(f"  YES       : {snap.yes_price:.4f}  |  NO: {snap.no_price:.4f}")
    print(f"  Total     : {snap.total:.4f}   Gap: {snap.spread_gap:.4f}{gap_flag}")
    print(f"  Liquidity : ${snap.liquidity:.2f}")


def print_trade_open(trade: Trade):
    print(SEPARATOR)
    print(f"  ACTION    : BUY BOTH (YES + NO)")
    print(f"  Trade #   : {trade.trade_id}")
    if trade.slippage > 0:
        print(f"  NOTE      : Slippage applied (+{trade.slippage:.4f})")
    print(f"  Size      : ${trade.position_size:.2f}")
    print(f"  Exp.Profit: ${trade.expected_profit:.4f}")
    print(SEPARATOR)


def print_skipped(reason: str):
    print(SEPARATOR)
    print(f"  ACTION    : SKIPPED")
    print(f"  Reason    : {reason}")
    print(SEPARATOR)


def print_failed(reason: str):
    print(SEPARATOR)
    print(f"  ACTION    : FAILED")
    print(f"  Reason    : {reason}")
    print(SEPARATOR)


def print_balance(balance: float, open_trades: int, total_profit: float):
    sign = "+" if total_profit >= 0 else ""
    print(f"\n  BALANCE   : ${balance:.2f}")
    print(f"  OPEN TRADES: {open_trades}")
    print(f"  TOTAL P&L  : {sign}${total_profit:.2f}")


def print_resolution(trade: Trade):
    sign = "+" if trade.actual_profit >= 0 else ""
    label = "WIN " if trade.status == "WIN" else "LOSS"
    print(f"\n  [{label}] Trade #{trade.trade_id} resolved → {sign}${trade.actual_profit:.4f}")


def print_session_summary(
    balance: float,
    session_start_balance: float,
    original_balance: float,
    all_trades: list[Trade],
    skipped: int,
    failed: int,
):
    closed = [t for t in all_trades if t.status in ("WIN", "LOSS")]
    wins   = [t for t in closed if t.status == "WIN"]
    losses = [t for t in closed if t.status == "LOSS"]

    total_profit = sum(t.actual_profit for t in closed)
    win_rate     = (len(wins) / len(closed) * 100) if closed else 0
    biggest_win  = max((t.actual_profit for t in wins),  default=0)
    biggest_loss = min((t.actual_profit for t in losses), default=0)

    print(f"\n{'═' * 48}")
    print(f"  SESSION SUMMARY")
    print(f"{'═' * 48}")
    print(f"  Balance         : ${balance:.2f}")
    print(f"  Session P&L     : ${balance - session_start_balance:+.2f}")
    print(f"  All-time P&L    : ${balance - original_balance:+.2f}  (from ${original_balance:.2f})")
    print(f"{'─' * 48}")
    print(f"  Trades Executed : {len(closed)}")
    print(f"  Trades Skipped  : {skipped}")
    print(f"  Trades Failed   : {failed}")
    print(f"  Win Rate        : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total Profit    : ${total_profit:+.4f}")
    print(f"  Biggest Win     : ${biggest_win:+.4f}")
    print(f"  Biggest Loss    : ${biggest_loss:+.4f}")
    print(f"{'═' * 48}\n")


# ─────────────────────────────────────────────
# CSV LOGGER
# ─────────────────────────────────────────────

def init_csv(path: str):
    """Creates the CSV header only if the file doesn't exist yet (preserves history)."""
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "trade_id", "market", "open_time", "close_time",
                "yes_price", "no_price", "total", "position_size",
                "expected_profit", "actual_profit", "slippage", "status", "reason",
            ])


def append_csv(path: str, trade: Trade):
    """Appends a single resolved trade row to the CSV."""
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            trade.trade_id,
            trade.market,
            trade.open_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            trade.close_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if trade.close_time else "",
            trade.yes_price,
            trade.no_price,
            trade.total,
            trade.position_size,
            trade.expected_profit,
            trade.actual_profit,
            trade.slippage,
            trade.status,
            trade.reason or "",
        ])


# ─────────────────────────────────────────────
# BALANCE PERSISTENCE
# ─────────────────────────────────────────────

STATE_FILE = "state.json"

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
    session_start_balance = balance
    open_trades: list[Trade] = []
    all_trades:  list[Trade] = []
    trade_id_seq = 0
    skipped_count = 0
    failed_count  = 0
    total_profit  = 0.0

    if SAVE_CSV:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CSV_FILENAME)
        init_csv(csv_path)

    print("  Polymarket Arbitrage Bot — SIMULATION")
    print(f"  Starting Balance : ${balance:.2f}")
    print(f"  Target Scans     : 500 per session (runs forever)")
    print(f"  Arb Threshold    : {ARB_THRESHOLD}")
    print(f"  Fee Rate         : {FEE_RATE * 100:.0f}% total round-trip\n")

    scans = 500  # scans per session
    for scan_num in range(1, scans + 1):

        # ── 1. Resolve any open trades whose holding period has elapsed
        #        (In this sim we randomly age them out each scan for brevity)
        still_open = []
        for t in open_trades:
            if random.random() < 0.45:          # ~45% chance trade resolves this tick
                t = resolve_trade(t)
                # Return capital + net P&L (capital was deducted when trade opened)
                balance      += t.position_size + t.actual_profit
                total_profit += t.actual_profit
                print_resolution(t)
                all_trades.append(t)
                if SAVE_CSV:
                    append_csv(csv_path, t)
            else:
                still_open.append(t)
        open_trades = still_open

        # ── 2. Pick a real market from Polymarket API
        snap = generate_market_snapshot(scan_num)

        print_header(scan_num, scans)
        print_market(snap)

        # ── 3. Skip if no arb opportunity
        if snap.total >= ARB_THRESHOLD:
            print_skipped("No arbitrage gap (YES+NO ≥ threshold)")
            skipped_count += 1
            time.sleep(SCAN_INTERVAL)
            continue

        # ── 4. Skip if too many open positions
        if len(open_trades) >= MAX_OPEN_TRADES:
            print_skipped(f"Max open trades reached ({MAX_OPEN_TRADES})")
            skipped_count += 1
            time.sleep(SCAN_INTERVAL)
            continue

        # ── 5. Skip if market liquidity is too low
        if snap.liquidity < 30:
            print_skipped(f"Low liquidity (${snap.liquidity:.2f} < $30)")
            skipped_count += 1
            time.sleep(SCAN_INTERVAL)
            continue

        # ── 6. Determine position size (risk-capped)
        position_size = round(random.uniform(MIN_POSITION_SIZE, MAX_RISK_PER_TRADE), 2)
        position_size = min(position_size, balance * 0.15)   # Never risk >15% of wallet
        position_size = min(position_size, snap.liquidity / 2)  # Respect order-book depth

        if position_size < MIN_POSITION_SIZE:
            print_skipped("Insufficient balance or liquidity for minimum size")
            skipped_count += 1
            time.sleep(SCAN_INTERVAL)
            continue

        # ── 7. Attempt execution (slippage / failure simulation)
        success, fill_yes, fill_no, exec_reason = simulate_execution(snap, position_size)

        if not success:
            print_failed(exec_reason)
            failed_count += 1
            time.sleep(SCAN_INTERVAL)
            continue

        # ── 8. Calculate expected profit at actual fill prices
        slippage_amount = round((fill_yes - snap.yes_price) + (fill_no - snap.no_price), 4)
        exp_profit = calculate_profit(fill_yes, fill_no, position_size)

        # ── 9. If after slippage the arb is no longer profitable, abort
        if exp_profit <= 0:
            print_skipped(f"Arb wiped out by slippage (exp profit ${exp_profit:.4f})")
            skipped_count += 1
            time.sleep(SCAN_INTERVAL)
            continue

        # ── 10. Open the trade
        trade_id_seq += 1
        trade = Trade(
            trade_id      = trade_id_seq,
            market        = snap.name,
            yes_price     = fill_yes,
            no_price      = fill_no,
            total         = round(fill_yes + fill_no, 4),
            position_size = position_size,
            expected_profit = exp_profit,
            slippage      = slippage_amount,
            reason        = exec_reason,
        )

        # Deduct position cost from balance immediately
        balance -= position_size
        open_trades.append(trade)

        print_trade_open(trade)
        print_balance(balance, len(open_trades), total_profit)

        time.sleep(SCAN_INTERVAL)

    # ── END OF SESSION: force-close any remaining open trades
    print(f"\n{'─' * 48}")
    print("  Closing all remaining open positions...")
    for t in open_trades:
        t = resolve_trade(t)
        balance      += t.actual_profit + t.position_size   # Return capital + P&L
        total_profit += t.actual_profit
        print_resolution(t)
        all_trades.append(t)
        if SAVE_CSV:
            append_csv(csv_path, t)

    # ── SESSION SUMMARY
    print_session_summary(balance, session_start_balance, original_balance, all_trades, skipped_count, failed_count)
    return balance


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    state = load_state()
    session = state["total_sessions"] + 1
    balance = state["balance"]

    while True:
        print(f"\n  {'═' * 46}")
        print(f"  SESSION #{session} STARTING  |  Balance: ${balance:.2f}")
        print(f"  {'═' * 46}")
        balance = run_bot(balance, state["original_balance"])
        save_state(balance, state["original_balance"], session)
        session += 1
        print("  Restarting in 5 seconds...\n")
        time.sleep(5)
