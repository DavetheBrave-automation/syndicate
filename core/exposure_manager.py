"""
exposure_manager.py — Risk gatekeeper for The Syndicate.

All trade proposals pass through check_trade() before execution.
Reads live exposure from shared_state and enforces per-class and
total limits defined in syndicate_config.yaml.

Contract classes: SCALP / SWING / POSITION / WATCH
WATCH is always blocked — informational only, never traded.
"""

import os
import logging
from typing import Optional

import yaml

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from core.shared_state import state

logger = logging.getLogger("syndicate.exposure")


# ---------------------------------------------------------------------------
# Config loader — mtime-cached, same pattern as Atlas brain.py
# ---------------------------------------------------------------------------

_cfg_cache: Optional[dict] = None
_cfg_mtime: float = 0.0


def _load_config() -> dict:
    global _cfg_cache, _cfg_mtime
    cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
    try:
        mtime = os.path.getmtime(cfg_path)
        if _cfg_cache is None or mtime > _cfg_mtime:
            with open(cfg_path, encoding="utf-8") as f:
                _cfg_cache = yaml.safe_load(f)
            _cfg_mtime = mtime
            logger.debug("[ExposureManager] Config reloaded from %s", cfg_path)
    except Exception as e:
        logger.error("[ExposureManager] Config load error: %s", e)
    return _cfg_cache or {}


def _risk_cfg() -> dict:
    """Return the risk sub-section of config, with safe defaults."""
    cfg = _load_config()
    return cfg.get("risk", {})


# ---------------------------------------------------------------------------
# Per-class limit lookup
# ---------------------------------------------------------------------------

_CLASS_LIMIT_KEYS = {
    "SCALP":    "max_per_trade_scalp",
    "SWING":    "max_per_trade_swing",
    "POSITION": "max_per_trade_position",
}


def _per_class_max(contract_class: str, risk: dict) -> float:
    """Return the per-trade dollar cap for this contract class."""
    key = _CLASS_LIMIT_KEYS.get(contract_class.upper())
    if key is None:
        return 0.0  # unknown or WATCH → zero budget
    return float(risk.get(key, 0.0))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_trade(ticker: str, contract_class: str, proposed_dollars: float) -> tuple[bool, str]:
    """
    Gate a proposed trade.

    Returns:
        (True, "")          — trade is allowed
        (False, reason)     — trade is blocked; reason is a human-readable string

    Checks (in order):
        1. WATCH class → always blocked
        2. Hard stop already breached → block all new trades
        3. Per-class cap not exceeded by proposed_dollars
        4. Total exposure + proposed_dollars <= max_total_exposure
    """
    cc = contract_class.upper()
    risk = _risk_cfg()

    # 1. WATCH is never tradeable
    if cc == "WATCH":
        return False, "WATCH contracts are not traded"

    # 2. Hard stop already breached — no new entries
    hard_stop = float(risk.get("hard_stop_loss", 50.0))
    daily_loss = state.get_daily_loss()
    if daily_loss >= hard_stop:
        return False, (
            f"Hard stop breached: daily_loss={daily_loss:.2f} >= "
            f"hard_stop_loss={hard_stop:.2f}"
        )

    # 3. Per-class cap
    class_max = _per_class_max(cc, risk)
    if class_max <= 0.0:
        return False, f"Unknown contract class: {contract_class!r}"

    if proposed_dollars > class_max:
        return False, (
            f"{cc} per-trade cap exceeded: proposed=${proposed_dollars:.2f} > "
            f"max_per_trade_{cc.lower()}=${class_max:.2f}"
        )

    # 4. Total exposure headroom
    max_total = float(risk.get("max_total_exposure", 50.0))
    current_total = state.get_total_exposure()
    if current_total + proposed_dollars > max_total:
        headroom = max(0.0, max_total - current_total)
        return False, (
            f"Total exposure cap would be breached: current=${current_total:.2f}, "
            f"proposed=${proposed_dollars:.2f}, max=${max_total:.2f} "
            f"(headroom=${headroom:.2f})"
        )

    logger.debug(
        "[ExposureManager] ALLOW %s %s $%.2f | total_after=$%.2f",
        cc, ticker, proposed_dollars, current_total + proposed_dollars,
    )
    return True, ""


def get_available_size(contract_class: str) -> float:
    """
    How many dollars can still be deployed in this class right now.

    Returns the minimum of:
        - per-class per-trade max (from config)
        - remaining headroom in total exposure budget

    WATCH and unknown classes return 0.0.
    """
    cc = contract_class.upper()
    if cc == "WATCH":
        return 0.0

    risk = _risk_cfg()
    class_max = _per_class_max(cc, risk)
    if class_max <= 0.0:
        return 0.0

    max_total = float(risk.get("max_total_exposure", 50.0))
    current_total = state.get_total_exposure()
    headroom = max(0.0, max_total - current_total)

    return min(class_max, headroom)


def get_exposure_summary() -> dict:
    """
    Full exposure snapshot for context injection into agent prompts.

    Returns:
        {
            "total":              float,   # dollars currently deployed
            "by_class":           dict,    # {class: dollars}
            "headroom":           float,   # dollars until max_total_exposure
            "hard_stop_breached": bool,
            "positions":          dict,    # {ticker: {class, entry_price, quantity, exposure}}
        }
    """
    risk = _risk_cfg()
    hard_stop    = float(risk.get("hard_stop_loss", 50.0))
    max_total    = float(risk.get("max_total_exposure", 50.0))
    daily_loss   = state.get_daily_loss()
    total_exp    = state.get_total_exposure()
    by_class     = state.get_exposure_by_class()
    all_positions = state.get_all_positions()

    positions_detail = {
        ticker: {
            "contract_class": pos.contract_class,
            "side":           pos.side,
            "quantity":       pos.quantity,
            "entry_price":    pos.entry_price,
            "exposure":       round((pos.entry_price / 100) * pos.quantity, 4),
        }
        for ticker, pos in all_positions.items()
    }

    return {
        "total":              round(total_exp, 4),
        "by_class":           {k: round(v, 4) for k, v in by_class.items()},
        "headroom":           round(max(0.0, max_total - total_exp), 4),
        "hard_stop_breached": daily_loss >= hard_stop,
        "positions":          positions_detail,
    }


def check_hard_stop() -> bool:
    """
    Returns True if daily_loss >= hard_stop_loss.
    Caller should immediately halt trading when this returns True.
    Does NOT call state.halt_trading() — caller owns that decision.
    """
    risk = _risk_cfg()
    hard_stop  = float(risk.get("hard_stop_loss", 50.0))
    daily_loss = state.get_daily_loss()
    breached   = daily_loss >= hard_stop
    if breached:
        logger.warning(
            "[ExposureManager] HARD STOP: daily_loss=%.2f >= hard_stop_loss=%.2f",
            daily_loss, hard_stop,
        )
    return breached
