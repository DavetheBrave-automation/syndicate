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

KALSHI_MAX_PER_ORDER = 99   # Kalshi single-order contract cap — chunk larger positions


def _compute_chunks(total_quantity: int) -> list[int]:
    """Split total_quantity into groups of ≤ KALSHI_MAX_PER_ORDER contracts."""
    chunks, remaining = [], total_quantity
    while remaining > 0:
        c = min(remaining, KALSHI_MAX_PER_ORDER)
        chunks.append(c)
        remaining -= c
    return chunks


def _chunk_summary(chunks: list[int]) -> str:
    """Human-readable breakdown: '99+99+99+36'."""
    return "+".join(str(c) for c in chunks)


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
            # Convention: entry_price always stores YES price in cents.
            # For YES side: price = yes_price directly.
            # For NO  side: price = contract_price = 1 - yes_price,
            #               so yes_price = 1 - price.
            if side.lower() == "no":
                entry_cents = int(round((1.0 - price) * 100))
            else:
                entry_cents = int(round(price * 100))

            # Chunk into ≤99-contract orders (Kalshi per-order limit).
            # Paper mode simulates each chunk with 200ms delay and unified position.
            chunks  = _compute_chunks(quantity)
            t0      = time.time()
            order_ids: list[str] = []
            for i, chunk_qty in enumerate(chunks):
                if i > 0:
                    time.sleep(0.2)   # 200ms between chunks — mirrors live rate-limit spacing
                cid = f"PAPER-{ticker}-{int(t0)}-C{i + 1}of{len(chunks)}"
                order_ids.append(cid)
                if len(chunks) > 1:
                    logger.info(
                        "[PAPER] Chunk %d/%d: %s %s %dx @ %d¢ | order=%s",
                        i + 1, len(chunks), side.upper(), ticker, chunk_qty,
                        (100 - entry_cents) if side.lower() == "no" else entry_cents, cid,
                    )
            order_id    = order_ids[0]
            chunk_count = len(chunks)

            position = Position(
                ticker             = ticker,
                side               = side,
                quantity           = quantity,
                entry_price        = entry_cents,
                entry_time         = t0,
                stop_price         = stop_price,
                target_price       = target_price,
                order_id           = order_id,
                rule_id            = rule_id,
                agent_name         = agent_name,
                contract_class     = contract_class,
                edge_at_entry      = float(rule.get("edge_pct", 0.0)),
                hold_to_settlement = bool(rule.get("hold_to_settlement", False)),
                target_exit_pct    = float(rule.get("target_exit_pct",  0.20)),
                stop_loss_pct      = float(rule.get("stop_loss_pct",    0.30)),
                max_hold_minutes   = int(rule.get("max_hold_minutes",   60)),
            )
            state.add_position(position)
            state.remove_pending(ticker)

            display_cost = (100 - entry_cents) if side.lower() == "no" else entry_cents
            logger.info(
                "[PAPER] %s entry: %s %s %dx @ %d¢ (YES=%d¢)%s | stop=%.0f¢ target=%.0f¢ | htsr=%s | rule=%s",
                f"Chunked ({chunk_count} orders)" if chunk_count > 1 else "Simulated",
                side.upper(), ticker, quantity, display_cost, entry_cents,
                f" [{_chunk_summary(chunks)}]" if chunk_count > 1 else "",
                stop_price, target_price, rule.get("hold_to_settlement", False), rule_id,
            )

            try:
                from core.agent_tier_manager import get_agent_tier  # noqa: PLC0415
                _agent_tier = get_agent_tier(agent_name)
            except Exception:
                _agent_tier = "probation"
            _trade_data = {
                "ticker":          ticker,
                "agent_name":      agent_name,
                "agent_tier":      _agent_tier,
                "conviction_tier": rule.get("conviction_tier", "?"),
                "side":            side,
                "quantity":        quantity,
                "price_cents":     display_cost,
                "size_dollars":    display_cost / 100.0 * quantity,
                "edge_pct":        rule.get("edge_pct", 0.0),
                "paper":           True,
                "chunk_count":     chunk_count,
                "chunk_summary":   _chunk_summary(chunks) if chunk_count > 1 else None,
            }
            try:
                from notifications.discord import post_entry_embed as _disc_entry  # noqa: PLC0415
                _disc_entry(_trade_data)
            except Exception:
                pass
            try:
                from notifications.telegram import post_trade_entry as _tg_entry  # noqa: PLC0415
                _tg_entry(_trade_data)
            except Exception:
                pass
            return order_id

        # ── Live mode ────────────────────────────────────────────────────────
        # Aggressive limit: price + 0.03, capped at 0.99
        aggressive_price = min(0.99, price + 0.03)

        # Chunk into ≤99-contract orders; track fills across all chunks.
        chunks           = _compute_chunks(quantity)
        order_ids:  list[str]   = []
        total_filled            = 0
        weighted_price_sum      = 0.0
        first_chunk_price       = aggressive_price
        t0                      = time.time()

        for i, chunk_qty in enumerate(chunks):
            if i > 0:
                time.sleep(0.2)   # 200ms between chunks — Kalshi rate-limit safety

            result = kalshi_rest.place_limit_buy(ticker, side, chunk_qty, aggressive_price)
            if not result or "error" in result:
                logger.error(
                    "[OrderManager] Live buy chunk %d/%d failed for %s: %s",
                    i + 1, len(chunks), ticker, result,
                )
                if i == 0:
                    state.remove_pending(ticker)
                    return None
                logger.warning(
                    "[OrderManager] Halting after chunk %d/%d partial fill: %d/%d contracts for %s",
                    i, len(chunks), total_filled, quantity, ticker,
                )
                break

            order    = result.get("order", result)
            order_id = order.get("order_id", "")
            if not order_id:
                logger.error("[OrderManager] No order_id in chunk %d/%d response for %s: %s", i + 1, len(chunks), ticker, result)
                if i == 0:
                    state.remove_pending(ticker)
                    return None
                break

            chunk_filled = int(order.get("filled_count", chunk_qty))
            order_ids.append(order_id)
            total_filled        += chunk_filled
            weighted_price_sum  += aggressive_price * chunk_filled

            slippage_cents = abs(aggressive_price - first_chunk_price) * 100
            if slippage_cents > 3.0:
                logger.warning(
                    "[CHUNK SLIPPAGE WARN] %s chunk %d fill price drift %.1f¢ vs first chunk",
                    ticker, i + 1, slippage_cents,
                )
            if chunk_filled < chunk_qty:
                logger.warning(
                    "[OrderManager] Partial fill chunk %d/%d: %d/%d contracts | halting | %s",
                    i + 1, len(chunks), chunk_filled, chunk_qty, ticker,
                )
                break

        if total_filled == 0:
            state.remove_pending(ticker)
            return None

        avg_price = weighted_price_sum / total_filled
        order_id  = order_ids[0]
        filled_chunks = _compute_chunks(total_filled)  # re-chunk based on actual fills

        if side.lower() == "no":
            entry_cents = int(round((1.0 - avg_price) * 100))
        else:
            entry_cents = int(round(avg_price * 100))

        position = Position(
            ticker             = ticker,
            side               = side,
            quantity           = total_filled,
            entry_price        = entry_cents,
            entry_time         = t0,
            stop_price         = stop_price,
            target_price       = target_price,
            order_id           = order_id,
            rule_id            = rule_id,
            agent_name         = agent_name,
            contract_class     = contract_class,
            edge_at_entry      = float(rule.get("edge_pct", 0.0)),
            hold_to_settlement = bool(rule.get("hold_to_settlement", False)),
            target_exit_pct    = float(rule.get("target_exit_pct",  0.20)),
            stop_loss_pct      = float(rule.get("stop_loss_pct",    0.30)),
            max_hold_minutes   = int(rule.get("max_hold_minutes",   60)),
        )
        state.add_position(position)
        state.remove_pending(ticker)

        chunk_count = len(order_ids)
        logger.info(
            "[OrderManager] Live buy %s: %s %s %dx @ %.3f avg%s | order_id=%s | htsr=%s | rule=%s",
            f"chunked ({chunk_count} orders)" if chunk_count > 1 else "placed",
            side.upper(), ticker, total_filled, avg_price,
            f" [{_chunk_summary(filled_chunks)}]" if chunk_count > 1 else "",
            order_id, rule.get("hold_to_settlement", False), rule_id,
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

            exit_chunks = _compute_chunks(quantity)
            chunk_count = len(exit_chunks)
            logger.info(
                "[PAPER] %s exit: %s %s %dx @ %.3f%s | spread=%.3f pnl=$%.2f | %s",
                f"Chunked ({chunk_count} orders)" if chunk_count > 1 else "Simulated",
                side.upper(), ticker, quantity, exit_price,
                f" [{_chunk_summary(exit_chunks)}]" if chunk_count > 1 else "",
                spread, pnl, exit_reason,
            )

            if position.side == "no":
                display_entry = 100 - position.entry_price  # actual NO cost
                display_exit  = int((1.0 - exit_price) * 100)
            else:
                display_entry = position.entry_price
                display_exit  = int(exit_price * 100)

            hold_minutes = (time.time() - position.entry_time) / 60.0

            try:
                from core.agent_tier_manager import get_agent_tier  # noqa: PLC0415
                _agent_tier = get_agent_tier(position.agent_name)
            except Exception:
                _agent_tier = "probation"
            try:
                from core.fee_calculator import calculate_roundtrip_fee as _crf  # noqa: PLC0415
                _fees_paid = _crf(quantity, position.entry_price, int(exit_price * 100))
            except Exception:
                _fees_paid = 0.0
            _exit_data = {
                "ticker":             ticker,
                "agent_name":         position.agent_name,
                "agent_tier":         _agent_tier,
                "side":               side,
                "quantity":           quantity,
                "entry_price_cents":  display_entry,
                "exit_price_cents":   display_exit,
                "pnl":                round(pnl, 4),
                "fees_paid":          round(_fees_paid, 4),
                "net_pnl":            round(pnl - _fees_paid, 4),
                "hold_minutes":       round(hold_minutes, 1),
                "exit_reason":        exit_reason,
                "paper":              True,
                "chunk_count":        chunk_count,
                "chunk_summary":      _chunk_summary(exit_chunks) if chunk_count > 1 else None,
            }
            try:
                from notifications.discord import post_exit_embed as _disc_exit  # noqa: PLC0415
                _disc_exit(_exit_data)
            except Exception:
                pass
            try:
                from notifications.telegram import post_trade_exit as _tg_exit  # noqa: PLC0415
                _tg_exit(_exit_data)
            except Exception:
                pass
            _record_outcome(position, exit_price, exit_reason, pnl, spread=spread)
            return True

        # ── Live mode ────────────────────────────────────────────────────────
        import connectors.kalshi_rest as kalshi_rest

        # Aggressive limit: price - 0.03, floored at 0.01
        aggressive_price = max(0.01, exit_price - 0.03)

        # Chunk large exits into ≤99-contract sell orders
        exit_chunks      = _compute_chunks(quantity)
        total_sold       = 0
        weighted_exit    = 0.0

        for i, chunk_qty in enumerate(exit_chunks):
            if i > 0:
                time.sleep(0.2)
            result = kalshi_rest.place_limit_sell(ticker, side, chunk_qty, aggressive_price)
            if not result or "error" in result:
                logger.error(
                    "[OrderManager] Live sell chunk %d/%d failed for %s: %s",
                    i + 1, len(exit_chunks), ticker, result,
                )
                break
            chunk_filled  = int(result.get("order", result).get("filled_count", chunk_qty))
            total_sold   += chunk_filled
            weighted_exit += aggressive_price * chunk_filled
            if chunk_filled < chunk_qty:
                logger.warning(
                    "[OrderManager] Partial sell fill chunk %d/%d: %d/%d | %s",
                    i + 1, len(exit_chunks), chunk_filled, chunk_qty, ticker,
                )
                break

        if total_sold == 0:
            logger.error("[OrderManager] Live sell produced zero fills for %s", ticker)
            return False

        effective_exit = weighted_exit / total_sold
        pnl = _compute_pnl(position, effective_exit)
        state.record_trade_pnl(pnl)
        state.remove_position(ticker)

        chunk_count = len(exit_chunks)
        logger.info(
            "[OrderManager] Live sell %s: %s %s %dx @ %.3f%s | pnl=$%.2f | %s",
            f"chunked ({chunk_count} orders)" if chunk_count > 1 else "placed",
            side.upper(), ticker, total_sold, effective_exit,
            f" [{_chunk_summary(exit_chunks)}]" if chunk_count > 1 else "",
            pnl, exit_reason,
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
