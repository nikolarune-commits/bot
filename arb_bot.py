"""
Polymarket-style Arbitrage Bot Simulation
==========================================
Simulates a short-term binary prediction market arbitrage strategy.
Markets focus on Bitcoin price direction (5-15 minute windows).

This is a SIMULATION ONLY. No real money is used.
"""

import random
import time
import csv
import os
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
RANDOM_SEED         = 42          # For reproducibility
STARTING_BALANCE    = 100.00      # Initial wallet balance in USD
MAX_RISK_PER_TRADE  = 10.00       # Hard cap per arbitrage position
MIN_POSITION_SIZE   = 5.00        # Minimum bet size
MAX_OPEN_TRADES     = 3           # Maximum concurrent open positions
FEE_RATE            = 0.02        # 2% total round-trip fee (per the spec)
ARB_THRESHOLD       = 0.99        # YES + NO must be below this to trigger arb
TARGET_TRADES       = 500         # Number of market scans to simulate
SCAN_INTERVAL       = 0.12        # Seconds between scans (simulated latency)
SAVE_CSV            = True        # Save trade log to CSV
CSV_FILENAME        = "trade_log.csv"

# Bitcoin market window labels — rotated to simulate multiple markets
BTC_MARKETS = [
    "BTC > $68k (5m)",
    "BTC > $69k (10m)",
    "BTC > $70k (15m)",
    "BTC < $67.5k (5m)",
    "BTC > $70.5k (10m)",
    "BTC above $69.5k (15m)",
]

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
# MARKET DATA GENERATOR
# ─────────────────────────────────────────────

def generate_market_snapshot(name: str) -> MarketSnapshot:
    """
    Simulates a realistic prediction market tick.

    Most of the time YES + NO ≈ 1.00 (efficient market).
    ~20% of the time there is a small gap (arbitrage window).
    ~5% of the time there is a LARGE gap (fat opportunity).
    """
    # Base YES probability, slightly randomised per tick
    base_yes = random.uniform(0.35, 0.65)

    roll = random.random()

    if roll < 0.05:
        # Large arb gap: 3-6% spread
        gap = random.uniform(0.03, 0.06)
        yes = round(base_yes - gap / 2, 4)
        no  = round((1 - base_yes) - gap / 2, 4)
    elif roll < 0.20:
        # Small arb gap: 1-3% spread
        gap = random.uniform(0.01, 0.03)
        yes = round(base_yes - gap / 2, 4)
        no  = round((1 - base_yes) - gap / 2, 4)
    else:
        # Efficient market: slight noise around 1.00
        noise = random.uniform(-0.005, 0.005)
        yes = round(base_yes + noise, 4)
        no  = round(1.0 - yes + random.uniform(-0.004, 0.004), 4)

    # Clamp prices to valid range
    yes = max(0.01, min(0.99, yes))
    no  = max(0.01, min(0.99, no))
    total = round(yes + no, 4)

    # Simulate order-book liquidity (USD available to trade against)
    liquidity = round(random.uniform(20, 500), 2)

    return MarketSnapshot(
        name=name,
        yes_price=yes,
        no_price=no,
        total=total,
        liquidity=liquidity,
        spread_gap=round(1.0 - total, 4),
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
        # Partial loss: market corrected before resolution
        actual = round(-random.uniform(0.05, 0.50) * trade.position_size, 4)
        status = "LOSS"
    else:
        # Big correction: both legs expire out of the money
        actual = round(-random.uniform(0.50, 1.00) * trade.position_size, 4)
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
    print(f"  Final Balance   : ${balance:.2f}")
    print(f"  Starting Balance: ${STARTING_BALANCE:.2f}")
    print(f"  Net P&L         : ${balance - STARTING_BALANCE:+.2f}")
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
    """Creates the CSV file and writes the header row."""
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
# MAIN BOT LOOP
# ─────────────────────────────────────────────

def run_bot():
    random.seed(RANDOM_SEED)

    balance      = STARTING_BALANCE
    open_trades: list[Trade] = []
    all_trades:  list[Trade] = []
    trade_id_seq = 0
    skipped_count = 0
    failed_count  = 0
    total_profit  = 0.0

    if SAVE_CSV:
        csv_path = os.path.join(os.path.dirname(__file__), CSV_FILENAME)
        init_csv(csv_path)
        print(f"  CSV logging → {csv_path}\n")

    print("  Polymarket Arbitrage Bot — SIMULATION")
    print(f"  Starting Balance : ${STARTING_BALANCE:.2f}")
    print(f"  Target Scans     : {TARGET_TRADES}")
    print(f"  Arb Threshold    : {ARB_THRESHOLD}")
    print(f"  Fee Rate         : {FEE_RATE * 100:.0f}% total round-trip\n")

    for scan_num in range(1, TARGET_TRADES + 1):

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

        # ── 2. Pick a market to scan
        market_name = random.choice(BTC_MARKETS)
        snap        = generate_market_snapshot(market_name)

        print_header(scan_num, TARGET_TRADES)
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
    print_session_summary(balance, all_trades, skipped_count, failed_count)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    run_bot()
