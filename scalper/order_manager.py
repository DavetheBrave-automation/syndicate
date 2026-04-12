"""
order_manager.py — Order execution layer for The Syndicate scalper.

Responsibilities:
  - place_order()   : Entry execution (paper sim or live Kalshi limit buy)
  - close_position(): Exit execution (paper sim or live Kalshi limit sell)
  - cancel_all()    : Emergency cancel of all open orders
  - get_open_orders(): Live order list (empty in paper mode)

Paper mode is read from syndicate_config.yaml → syndicate.paper_mode.
Defaults to True (safe).

P&L convention (mirrors Atlas banker.py):
  YES side: (exit_dollars - entry_dollars) * quantity
  NO  side: ((1 - exit_dollars) - (100 - entry_cents)/100) * quantity
"""

import os
import sys
import time
import logging
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup — must come before any local imports
# ---------------------------------------------------------------------------

_SCALPER_DIR   = os.path.dirname(os.path.abspath(__file__))
_SYNDICATE_ROOT = os.path.dirname(_SCALPER_DIR)
_ATLAS_ROOT    = os.path.join(os.path.dirname(_SYNDICATE_ROOT), "atlas")

sys.path.insert(0, _ATLAS_ROOT)
sys.path.insert(0, _SYNDICATE_ROOT)

logger = logging.getLogger("syndicate.orders")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_PAPER_MODE: Optional[bool] = None  # cached at first call, restart to change


def _is_paper_mode() -> bool:
    """Read paper_mode once from config and cache. Restart to change. Defaults to True."""
    global _PAPER_MODE
    if _PAPER_MODE is None:
        try:
            import yaml
            cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            _PAPER_MODE = bool(cfg.get("syndicate", {}).get("paper_mode", True))
        except Exception:
            _PAPER_MODE = True
    return _PAPER_MODE


# ---------------------------------------------------------------------------
# P&L computation
# ---------------------------------------------------------------------------

def _compute_pnl(position, exit_price: float, spread: float = 0.0) -> float:
    """
    Compute realised P&L for a closed position.

    entry_price on Position is cents integer (e.g. 45).
    exit_price passed here is decimal dollars (e.g. 0.62).
    spread is the bid-ask spread in decimal dollars — deducted as round-trip cost.

    YES: (exit_dollars - entry_dollars - spread) * quantity
    NO : ((1 - exit_dollars) - (100 - entry_cents)/100 - spread) * quantity
    """
    entry_dollars = position.entry_price / 100.0
    if position.side == "yes":
        return (exit_price - entry_dollars - spread) * position.quantity
    else:
        no_entry = (100 - position.entry_price) / 100.0
        no_exit  = 1.0 - exit_price
        return (no_exit - no_entry - spread) * position.quantity


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------

def place_order(
    ticker: str,
    side: str,
    quantity: int,
    price: float,
    rule: dict,
    rule_id: str,
    agent_name: str,
    contract_class: str,
    max_size: int,
) -> Optional[str]:
    """
    Execute an entry order.

    Parameters
    ----------
    ticker         : Kalshi market ticker
    side           : 'yes' or 'no'
    quantity       : number of contracts (capped to max_size)
    price          : current market price in decimal dollars (0.0–1.0)
    rule           : rule dict containing stop_price and target_price (cents)
    rule_id        : identifier for the triggering rule
    agent_name     : name of the agent placing the order
    contract_class : SCALP / SWING / POSITION / WATCH
    max_size       : hard cap on contracts per order

    Returns
    -------
    order_id string on success, None on failure.
    """
    from core.shared_state import state, Position
    import connectors.kalshi_rest as kalshi_rest

    quantity = min(quantity, max_size)

    stop_price   = float(rule.get("stop_price",   0.0))
    target_price = float(rule.get("target_price", 0.0))

    try:
        # ── Paper mode ──────────────────────────────────────────────────────
        if _is_paper_mode():
            order_id    = f"PAPER-{ticker}-{int(time.time())}"
            entry_cents = int(round(price * 100))

            position = Position(
                ticker         = ticker,
                side           = side,
                quantity       = quantity,
                entry_price    = entry_cents,
                entry_time     = time.time(),
                stop_price     = stop_price,
                target_price   = target_price,
                order_id       = order_id,
                rule_id        = rule_id,
                agent_name     = agent_name,
                contract_class = contract_class,
            )
            state.add_position(position)
            state.remove_pending(ticker)

            logger.info(
                "[PAPER] Simulated fill: %s %s %dx @ %d¢ | stop=%.0f¢ target=%.0f¢ | rule=%s",
                side.upper(), ticker, quantity, entry_cents,
                stop_price, target_price, rule_id,
            )
            try:
                from notifications.discord import post as _discord_post
                _discord_post(f"PAPER FILL: {ticker} {side} @ {entry_cents}¢")
            except Exception:
                pass
            try:
                from notifications.telegram import post as _tg_post
                _tg_post(f"PAPER FILL: {ticker} {side} @ {entry_cents}¢")
            except Exception:
                pass
            return order_id

        # ── Live mode ────────────────────────────────────────────────────────
        # Aggressive limit: price + 0.03, capped at 0.99
        aggressive_price = min(0.99, price + 0.03)

        result = kalshi_rest.place_limit_buy(ticker, side, quantity, aggressive_price)
        if not result or "error" in result:
            logger.error(
                "[OrderManager] Live buy failed for %s: %s", ticker, result
            )
            state.remove_pending(ticker)
            return None

        order    = result.get("order", result)
        order_id = order.get("order_id", "")
        if not order_id:
            logger.error("[OrderManager] No order_id in response for %s: %s", ticker, result)
            state.remove_pending(ticker)
            return None

        entry_cents = int(round(aggressive_price * 100))

        position = Position(
            ticker         = ticker,
            side           = side,
            quantity       = quantity,
            entry_price    = entry_cents,
            entry_time     = time.time(),
            stop_price     = stop_price,
            target_price   = target_price,
            order_id       = order_id,
            rule_id        = rule_id,
            agent_name     = agent_name,
            contract_class = contract_class,
        )
        state.add_position(position)
        state.remove_pending(ticker)

        logger.info(
            "[OrderManager] Live buy placed: %s %s %dx @ %.3f | order_id=%s | rule=%s",
            side.upper(), ticker, quantity, aggressive_price, order_id, rule_id,
        )
        return order_id

    except Exception as exc:
        logger.exception("[OrderManager] place_order exception for %s: %s", ticker, exc)
        state.remove_pending(ticker)
        return None


# ---------------------------------------------------------------------------
# close_position
# ---------------------------------------------------------------------------

def close_position(position, exit_price: float, exit_reason: str) -> bool:
    """
    Close an open position.

    Parameters
    ----------
    position    : Position dataclass instance
    exit_price  : exit price in decimal dollars (0.0–1.0)
    exit_reason : human-readable reason string (e.g. 'stop_hit', 'target_hit')

    Returns
    -------
    True on success, False on failure.
    """
    from core.shared_state import state

    # Lazy import to avoid circular dependency
    def _record_outcome(pos, ep, er, pnl, spread=0.0):
        try:
            from core.outcome_reporter import outcome_reporter
            outcome_reporter.record_outcome(pos, ep, er, pnl, spread=spread)
        except Exception as exc:
            logger.warning("[OrderManager] outcome_reporter.record_outcome failed: %s", exc)

    ticker   = position.ticker
    side     = position.side
    quantity = position.quantity

    try:
        # ── Paper mode ──────────────────────────────────────────────────────
        if _is_paper_mode():
            market = state.get_market(ticker)
            spread = market.spread if market else 0.0
            pnl = _compute_pnl(position, exit_price, spread)
            state.record_trade_pnl(pnl)
            state.remove_position(ticker)

            logger.info(
                "[PAPER] Simulated exit: %s %s %dx @ %.3f | spread=%.3f pnl=$%.2f | %s",
                side.upper(), ticker, quantity, exit_price, spread, pnl, exit_reason,
            )
            try:
                from notifications.discord import post as _discord_post
                _discord_post(f"PAPER EXIT: {ticker} pnl={pnl:+.2f}")
            except Exception:
                pass
            try:
                from notifications.telegram import post as _tg_post
                _tg_post(f"PAPER EXIT: {ticker} pnl={pnl:+.2f}")
            except Exception:
                pass
            _record_outcome(position, exit_price, exit_reason, pnl, spread=spread)
            return True

        # ── Live mode ────────────────────────────────────────────────────────
        import connectors.kalshi_rest as kalshi_rest

        # Aggressive limit: price - 0.03, floored at 0.01
        aggressive_price = max(0.01, exit_price - 0.03)

        result = kalshi_rest.place_limit_sell(ticker, side, quantity, aggressive_price)
        if not result or "error" in result:
            logger.error(
                "[OrderManager] Live sell failed for %s: %s", ticker, result
            )
            return False

        # Use the aggressive limit as the effective exit price
        # (fills immediately at market or better for aggressive limit)
        effective_exit = aggressive_price

        pnl = _compute_pnl(position, effective_exit)
        state.record_trade_pnl(pnl)
        state.remove_position(ticker)

        logger.info(
            "[OrderManager] Live sell placed: %s %s %dx @ %.3f | pnl=$%.2f | %s",
            side.upper(), ticker, quantity, effective_exit, pnl, exit_reason,
        )
        _record_outcome(position, effective_exit, exit_reason, pnl)
        return True

    except Exception as exc:
        logger.exception("[OrderManager] close_position exception for %s: %s", ticker, exc)
        return False


# ---------------------------------------------------------------------------
# cancel_all
# ---------------------------------------------------------------------------

def cancel_all() -> int:
    """
    Cancel all open orders via Kalshi REST.

    Paper mode: returns 0 (no real orders exist).
    Live mode : fetches open orders and cancels each by order_id.

    Returns
    -------
    Count of successfully cancelled orders.
    """
    if _is_paper_mode():
        logger.info("[PAPER] cancel_all called — no real orders to cancel.")
        return 0

    import connectors.kalshi_rest as kalshi_rest

    cancelled = 0
    orders = get_open_orders()
    for order in orders:
        order_id = order.get("order_id") or order.get("id", "")
        if not order_id:
            continue
        success = kalshi_rest.cancel_order(order_id)
        if success:
            cancelled += 1
            logger.info("[OrderManager] Cancelled order %s", order_id)
        else:
            logger.warning("[OrderManager] Failed to cancel order %s", order_id)

    logger.info("[OrderManager] cancel_all: %d cancelled.", cancelled)
    return cancelled


# ---------------------------------------------------------------------------
# get_open_orders
# ---------------------------------------------------------------------------

def get_open_orders() -> list:
    """
    Return list of open orders from Kalshi.

    Paper mode: returns [] (no real orders).
    Live mode : delegates to kalshi_rest.get_positions() for active positions.
    """
    if _is_paper_mode():
        return []

    try:
        import connectors.kalshi_rest as kalshi_rest
        return kalshi_rest.get_positions()
    except Exception as exc:
        logger.error("[OrderManager] get_open_orders failed: %s", exc)
        return []
