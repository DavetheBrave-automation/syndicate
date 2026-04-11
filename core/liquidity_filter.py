"""
liquidity_filter.py — Liquidity gate for The Syndicate.

Gates applied in order (fail-fast):
  1. WATCH class         → always reject (research only, no buy)
  2. days_to_settlement  → reject if > max_days_to_settlement UNLESS edge >= time_gate_override_edge
  3. volume_dollars      → reject if below min_daily_volume
  4. spread              → reject if above max_bid_ask_spread
  5. All gates passed    → return LiquidityResult(passed=True)

Note: min_expected_volatility (0.10) is in config but intentionally NOT enforced here.
Volatility is forward-looking and assessed by agents, not a hard gate at the Python layer.
"""

import os
import sys
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Root path — must come before any local imports
# ---------------------------------------------------------------------------

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from core.shared_state import MarketData                          # noqa: E402
from core.contract_classifier import classify_market, ContractProfile  # noqa: E402

logger = logging.getLogger("syndicate.liquidity")


# ---------------------------------------------------------------------------
# LiquidityResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class LiquidityResult:
    passed: bool
    rejection_reason: str          # empty string if passed
    contract_class: str            # from classifier (SCALP / SWING / POSITION / WATCH)
    max_size: float                # max dollars allowed per trade (0.0 if rejected)
    edge_override_applied: bool    # True if 30%+ edge bypassed the time gate


# ---------------------------------------------------------------------------
# Config loader — mtime-cached (same pattern as Atlas brain.py)
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
            logger.debug("[LiquidityFilter] Config reloaded (mtime=%.1f)", mtime)
    except Exception as e:
        logger.error("[LiquidityFilter] Config load error: %s", e)
    return _cfg_cache or {}


# ---------------------------------------------------------------------------
# Internal rejection log — thread-safe, capped at 500 entries
# ---------------------------------------------------------------------------

_rejection_log: deque = deque(maxlen=500)
_log_lock = threading.Lock()


def _append_rejection(ticker: str, reason: str, market: MarketData) -> None:
    """Append a rejection entry to the thread-safe deque."""
    entry = {
        "ticker":              ticker,
        "reason":              reason,
        "timestamp":           datetime.now(tz=timezone.utc).isoformat(),
        "volume":              market.volume_dollars,
        "spread":              market.spread,
        "days_to_settlement":  market.days_to_settlement,
    }
    with _log_lock:
        _rejection_log.append(entry)


def get_rejection_log(n: int = 50) -> list:
    """Return last N rejection log entries (most recent last)."""
    with _log_lock:
        entries = list(_rejection_log)
    return entries[-n:]


# ---------------------------------------------------------------------------
# Core gate function
# ---------------------------------------------------------------------------

def check_contract(market: MarketData, edge_pct: float = 0.0) -> LiquidityResult:
    """
    Run all liquidity gates against a pre-classified MarketData object.

    The caller is responsible for ensuring market.contract_class is set.
    Gates are applied in order; fails fast on first rejection.

    Returns a LiquidityResult.
    """
    cfg = _load_config()
    liq = cfg.get("liquidity", {})
    risk = cfg.get("risk", {})

    max_days:       float = float(liq.get("max_days_to_settlement", 14))
    override_edge:  float = float(liq.get("time_gate_override_edge", 30))
    min_volume:     float = float(liq.get("min_daily_volume", 25_000))
    max_spread:     float = float(liq.get("max_bid_ask_spread", 0.08))

    max_sizes = {
        "SCALP":    float(risk.get("max_per_trade_scalp",    5.00)),
        "SWING":    float(risk.get("max_per_trade_swing",    3.00)),
        "POSITION": float(risk.get("max_per_trade_position", 2.00)),
        "WATCH":    0.00,
    }

    contract_class = market.contract_class
    max_size       = max_sizes.get(contract_class, 0.00)
    ticker         = market.ticker

    # ── Gate 1: WATCH class — always reject ─────────────────────────────────
    if contract_class == "WATCH":
        reason = "contract class WATCH — research only, no buy"
        logger.info("[LiquidityFilter] REJECT %s — %s", ticker, reason)
        _append_rejection(ticker, reason, market)
        return LiquidityResult(
            passed=False,
            rejection_reason=reason,
            contract_class=contract_class,
            max_size=0.00,
            edge_override_applied=False,
        )

    # ── Gate 2: days_to_settlement — bypass allowed with sufficient edge ─────
    edge_override_applied = False
    days = market.days_to_settlement

    if days > max_days:
        if edge_pct >= override_edge:
            edge_override_applied = True
            logger.info(
                "[LiquidityFilter] %s time gate bypassed — edge %.1f%% >= %.0f%% override "
                "(days_to_settlement=%.2f)",
                ticker, edge_pct, override_edge, days,
            )
        else:
            reason = (
                f"days_to_settlement {days:.2f} exceeds max {max_days:.0f} "
                f"(edge {edge_pct:.1f}% < {override_edge:.0f}% override threshold)"
            )
            logger.info("[LiquidityFilter] REJECT %s — %s", ticker, reason)
            _append_rejection(ticker, reason, market)
            return LiquidityResult(
                passed=False,
                rejection_reason=reason,
                contract_class=contract_class,
                max_size=0.00,
                edge_override_applied=False,
            )

    # ── Gate 3: daily volume ─────────────────────────────────────────────────
    v = market.volume_dollars
    if v < min_volume:
        reason = f"volume ${v:,.0f} below min ${min_volume:,.0f}"
        logger.info("[LiquidityFilter] REJECT %s — %s", ticker, reason)
        _append_rejection(ticker, reason, market)
        return LiquidityResult(
            passed=False,
            rejection_reason=reason,
            contract_class=contract_class,
            max_size=0.00,
            edge_override_applied=edge_override_applied,
        )

    # ── Gate 4: bid-ask spread ───────────────────────────────────────────────
    s = market.spread
    if s > max_spread:
        reason = f"spread {s:.3f} exceeds max {max_spread:.3f}"
        logger.info("[LiquidityFilter] REJECT %s — %s", ticker, reason)
        _append_rejection(ticker, reason, market)
        return LiquidityResult(
            passed=False,
            rejection_reason=reason,
            contract_class=contract_class,
            max_size=0.00,
            edge_override_applied=edge_override_applied,
        )

    # ── All gates passed ─────────────────────────────────────────────────────
    logger.debug(
        "[LiquidityFilter] PASS %s — class=%s max_size=%.2f edge_override=%s",
        ticker, contract_class, max_size, edge_override_applied,
    )
    return LiquidityResult(
        passed=True,
        rejection_reason="",
        contract_class=contract_class,
        max_size=max_size,
        edge_override_applied=edge_override_applied,
    )


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------

def check_market(ticker: str, market: MarketData, edge_pct: float = 0.0) -> LiquidityResult:
    """
    Classify market first, then run all liquidity gates.
    Uses a shallow copy so the live SharedState MarketData object is never mutated.
    """
    import copy
    profile: ContractProfile = classify_market(market)

    # Work on a shallow copy — never touch the live SharedState object.
    local_market = copy.copy(market)
    local_market.contract_class = profile.contract_class

    result = check_contract(local_market, edge_pct=edge_pct)

    return LiquidityResult(
        passed=result.passed,
        rejection_reason=result.rejection_reason,
        contract_class=profile.contract_class,
        max_size=result.max_size,
        edge_override_applied=result.edge_override_applied,
    )
