"""Order execution + position monitoring.

Modes:
  PAPER — simulate fills against current bid/ask, update SQLite portfolio
  LIVE  — call kalshi.place_order / kalshi.cancel_order

Hard exit rules (T-minus expiry):
  T-6h: profitable -> limit sell at bid-2¢
  T-3h: at loss/flat -> market sell
  T-1h: emergency market sell regardless
"""
import time
from app import db
from app.config import (
    TRADING_MODE, EXIT_T_MINUS_HOURS, PAPER_STARTING_CASH,
)
from engine import kalshi, allowlist


# -------- portfolio state (works for both PAPER and LIVE) --------

def get_cash() -> float:
    if TRADING_MODE == "LIVE":
        bal = kalshi.get_balance()
        if bal:
            return bal.get("balance", 0) / 100.0
        return 0.0
    return float(db.state_get("paper_cash", PAPER_STARTING_CASH))


def set_cash(v: float):
    db.state_set("paper_cash", round(float(v), 4))


def session_pnl() -> float:
    return float(db.state_get("session_pnl", 0.0))


def add_session_pnl(delta: float):
    db.state_set("session_pnl", round(session_pnl() + delta, 4))


def total_exposure() -> float:
    return sum(p["cost_basis_dollars"] for p in db.all_positions())


def sports_exposure() -> float:
    return sum(p["cost_basis_dollars"] for p in db.all_positions()
               if p.get("asset_class") == "sports")


def portfolio_value() -> float:
    """Cash + sum of current market values of positions (using last price)."""
    cash = get_cash()
    pv = cash
    for p in db.all_positions():
        # We don't refresh per-position market value here; use cost basis as proxy.
        # For more accuracy, the scheduler refreshes positions periodically.
        pv += p["cost_basis_dollars"]
    return round(pv, 2)


# -------- entry --------

def open_position(ticker: str, contracts: int, ask_cents: float,
                  asset_class: str, expires_at: int = None) -> bool:
    """Buy YES at ask. Returns True on success."""
    if contracts <= 0:
        return False
    if not allowlist.is_tradeable(ticker):
        db.log("BLOCKED", f"Entry blocked — {ticker} not tradeable")
        return False

    cost = round(contracts * ask_cents / 100.0, 4)
    target = round(ask_cents * 2, 2)
    stop = round(ask_cents * 0.4, 2)

    if TRADING_MODE == "LIVE":
        res = kalshi.place_order(ticker, side="yes", action="buy",
                                 count=contracts, yes_price=int(ask_cents))
        if not res:
            db.log("ERROR", f"LIVE order failed — {ticker}")
            return False
        # The actual fill price comes from a fills callback; for v1 we trust the limit.

    else:  # PAPER
        cash = get_cash()
        if cost > cash:
            db.log("REJECTED", f"Insufficient paper cash — {ticker}",
                   f"need ${cost:.2f}, have ${cash:.2f}")
            return False
        set_cash(cash - cost)

    db.upsert_position({
        "ticker": ticker,
        "qty": contracts,
        "entry_cents": ask_cents,
        "target_cents": target,
        "stop_cents": stop,
        "cost_basis_dollars": cost,
        "asset_class": asset_class,
        "opened_at": int(time.time()),
        "expires_at": expires_at,
    })
    db.log("ENTRY", f"BUY {contracts} {ticker} @ {ask_cents:.1f}¢",
           f"COST: ${cost:.2f} | TARGET: {target:.0f}¢ | STOP: {stop:.0f}¢")
    return True


# -------- exit --------

def close_position(ticker: str, exit_cents: float, reason: str) -> bool:
    pos = next((p for p in db.all_positions() if p["ticker"] == ticker), None)
    if pos is None:
        return False

    proceeds = round(pos["qty"] * exit_cents / 100.0, 4)
    pnl = round(proceeds - pos["cost_basis_dollars"], 4)

    if TRADING_MODE == "LIVE":
        kalshi.place_order(ticker, side="yes", action="sell",
                           count=pos["qty"], order_type="market")
    else:
        set_cash(get_cash() + proceeds)

    add_session_pnl(pnl)
    db.delete_position(ticker)

    sign = "+" if pnl >= 0 else ""
    db.log(reason, f"SELL {pos['qty']} {ticker} @ {exit_cents:.1f}¢",
           f"ENTRY: {pos['entry_cents']:.1f}¢ | EXIT: {exit_cents:.1f}¢ | "
           f"P&L: {sign}${pnl:.2f} | SESSION: ${session_pnl():.2f}")
    return True


# -------- monitor --------

def monitor_positions(market_lookup: dict):
    """Walk every open position, check exits.

    market_lookup: {ticker: market_dict} for fresh quotes & expiry.
    """
    now = time.time()

    for pos in db.all_positions():
        ticker = pos["ticker"]
        m = market_lookup.get(ticker)
        if m is None:
            continue

        bid = m.get("yes_bid", 0)
        hours_left = m.get("hours_to_expiry")
        if hours_left is None and m.get("close_time"):
            hours_left = max(0, (m["close_time"] - now) / 3600)

        # 1. T-1h emergency
        if hours_left is not None and hours_left < EXIT_T_MINUS_HOURS["emergency_exit"]:
            close_position(ticker, bid, "EMERGENCY_EXIT_T1H")
            continue

        # 2. T-3h hard loss exit
        if (hours_left is not None and
                hours_left < EXIT_T_MINUS_HOURS["hard_loss_exit"] and
                bid <= pos["entry_cents"]):
            close_position(ticker, bid, "HARD_EXIT_T3H_LOSS")
            continue

        # 3. T-6h soft profit exit
        if (hours_left is not None and
                hours_left < EXIT_T_MINUS_HOURS["soft_profit_exit"] and
                bid > pos["entry_cents"]):
            close_position(ticker, max(0, bid - 2), "SOFT_EXIT_T6H_PROFIT")
            continue

        # 4. Profit target
        if bid >= pos["target_cents"]:
            close_position(ticker, bid, "PROFIT_TARGET")
            continue

        # 5. Stop loss
        if bid <= pos["stop_cents"]:
            close_position(ticker, bid, "STOP_LOSS")
            continue
