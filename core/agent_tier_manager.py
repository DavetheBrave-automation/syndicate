"""
core/agent_tier_manager.py — Performance-gated sizing tiers.

Queries syndicate_trades.db for each agent's win rate and trade count,
maps to a performance tier, and returns the max position size for that tier.

Tiers (highest to lowest):
  elite      — ≥30 trades, ≥75% WR → $1000 max
  proven     — ≥30 trades, ≥60% WR → $500 max
  qualified  — ≥20 trades, ≥50% WR → $250 max
  probation  — default (all new agents) → $25 max

Cache is refreshed every 5 minutes — avoids hammering DB per signal.
"""

import os
import sqlite3
import logging
import time
from datetime import datetime, timezone
from typing import Optional

_SYNDICATE_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH           = os.path.join(_SYNDICATE_ROOT, "logs", "syndicate_trades.db")
_TIER_CHANGES_LOG  = os.path.join(_SYNDICATE_ROOT, "logs", "tier_changes.log")

logger = logging.getLogger("syndicate.tier_manager")

_CACHE_TTL = 300.0  # 5 minutes
# {agent_name: (tier_name: str, max_position: float, cached_at: float)}
_cache: dict = {}

# ---------------------------------------------------------------------------
# Fallback tier definitions (used if config unavailable)
# ---------------------------------------------------------------------------

_FALLBACK_TIERS = {
    "probation": {"min_winrate": 0.00, "min_trades": 0,  "max_position": 25},
    "qualified":  {"min_winrate": 0.50, "min_trades": 20, "max_position": 250},
    "proven":     {"min_winrate": 0.60, "min_trades": 30, "max_position": 500},
    "elite":      {"min_winrate": 0.75, "min_trades": 30, "max_position": 1000},
}

_TIER_ORDER = ["elite", "proven", "qualified", "probation"]  # highest → lowest
_TIER_RANK  = {t: i for i, t in enumerate(reversed(_TIER_ORDER))}  # probation=0, elite=3


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_tier_config() -> dict:
    try:
        import yaml  # noqa: PLC0415
        cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("performance_tiers", _FALLBACK_TIERS)
    except Exception:
        return _FALLBACK_TIERS


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------

def _query_agent_stats(agent_name: str) -> tuple[int, float]:
    """
    Return (trade_count, win_rate) from syndicate_trades.db.
    Uses net_pnl for win detection where available (fee-adjusted), falls back to pnl.
    Returns (0, 0.0) on error or if DB missing.
    """
    try:
        if not os.path.exists(_DB_PATH):
            return 0, 0.0
        with sqlite3.connect(_DB_PATH, timeout=3.0) as con:
            cur = con.cursor()
            cur.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN COALESCE(net_pnl, pnl) > 0 THEN 1 ELSE 0 END) "
                "FROM syndicate_trades WHERE agent_name = ?",
                (agent_name,),
            )
            row = cur.fetchone()
            if not row or row[0] is None or row[0] == 0:
                return 0, 0.0
            trades = int(row[0])
            wins   = int(row[1] or 0)
            return trades, (wins / trades)
    except Exception as e:
        logger.warning("[TierManager] DB query failed for %s: %s", agent_name, e)
        return 0, 0.0


# ---------------------------------------------------------------------------
# Tier computation
# ---------------------------------------------------------------------------

def _compute_tier(trades: int, win_rate: float, tier_cfg: dict) -> str:
    """
    Walk tiers highest → lowest. Return the first tier where agent meets
    BOTH min_trades AND min_winrate thresholds. Always falls through to probation.
    """
    for tier_name in _TIER_ORDER:
        tc = tier_cfg.get(tier_name, {})
        if trades >= tc.get("min_trades", 0) and win_rate >= tc.get("min_winrate", 0.0):
            return tier_name
    return "probation"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_agent_tier(agent_name: str) -> str:
    """
    Return the current performance tier name for an agent.
    Uses 5-minute cache. Logs tier changes to tier_changes.log.
    """
    now = time.monotonic()
    cached = _cache.get(agent_name)
    if cached and (now - cached[2]) < _CACHE_TTL:
        return cached[0]

    tier_cfg  = _load_tier_config()
    trades, win_rate = _query_agent_stats(agent_name)
    tier_name = _compute_tier(trades, win_rate, tier_cfg)
    max_pos   = float(tier_cfg.get(tier_name, {}).get("max_position", 25))

    old_tier = cached[0] if cached else None
    _cache[agent_name] = (tier_name, max_pos, now)

    if old_tier is not None and old_tier != tier_name:
        _log_tier_change(agent_name, old_tier, tier_name, trades, win_rate)

    logger.debug(
        "[TierManager] %s: trades=%d win_rate=%.0f%% → %s (max=$%.0f)",
        agent_name, trades, win_rate * 100, tier_name, max_pos,
    )
    return tier_name


def get_max_position(agent_name: str) -> float:
    """
    Return the max position size in dollars for this agent's current tier.
    Never returns less than $25 (probation floor).
    """
    now = time.monotonic()
    cached = _cache.get(agent_name)
    if cached and (now - cached[2]) < _CACHE_TTL:
        return cached[1]
    # Force refresh via get_agent_tier which populates cache
    get_agent_tier(agent_name)
    return _cache.get(agent_name, ("probation", 25.0, 0.0))[1]


def get_all_tiers() -> list[dict]:
    """
    Return tier status for every agent that has traded.
    Used by ECHO weekly report. Format:
        [{"agent": str, "tier": str, "max_position": float, "trades": int, "win_rate": float}]
    """
    try:
        if not os.path.exists(_DB_PATH):
            return []
        with sqlite3.connect(_DB_PATH, timeout=3.0) as con:
            cur = con.cursor()
            cur.execute(
                "SELECT agent_name, COUNT(*), "
                "SUM(CASE WHEN COALESCE(net_pnl, pnl) > 0 THEN 1 ELSE 0 END) "
                "FROM syndicate_trades GROUP BY agent_name ORDER BY agent_name"
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("[TierManager] get_all_tiers DB error: %s", e)
        return []

    tier_cfg = _load_tier_config()
    results  = []
    for agent_name, trades, wins in rows:
        trades   = int(trades or 0)
        wins     = int(wins or 0)
        win_rate = wins / trades if trades else 0.0
        tier     = _compute_tier(trades, win_rate, tier_cfg)
        max_pos  = float(tier_cfg.get(tier, {}).get("max_position", 25))
        results.append({
            "agent":        agent_name,
            "tier":         tier,
            "max_position": max_pos,
            "trades":       trades,
            "win_rate":     win_rate,
        })
    return results


# ---------------------------------------------------------------------------
# Promotion progress
# ---------------------------------------------------------------------------

def get_promotion_progress(agent_name: str) -> Optional[dict]:
    """
    Return what an agent needs to reach the next tier, or None if already ELITE.

    Dict keys:
      current_tier, next_tier, trades, win_rate,
      min_trades_for_next, min_wr_for_next,
      trades_needed, wins_needed
    """
    tier_cfg      = _load_tier_config()
    trades, wr    = _query_agent_stats(agent_name)
    current_tier  = _compute_tier(trades, wr, tier_cfg)

    if current_tier == "elite":
        return None

    # _TIER_ORDER is highest→lowest; reverse to ascending
    asc = list(reversed(_TIER_ORDER))   # probation, qualified, proven, elite
    try:
        current_idx = asc.index(current_tier)
    except ValueError:
        return None
    if current_idx >= len(asc) - 1:
        return None

    next_tier = asc[current_idx + 1]
    nc = tier_cfg.get(next_tier, {})
    min_trades = int(nc.get("min_trades", 0))
    min_wr     = float(nc.get("min_winrate", 0.0))

    current_wins   = int(wr * trades)
    wins_for_next  = int(min_trades * min_wr)
    trades_needed  = max(0, min_trades - trades)
    wins_needed    = max(0, wins_for_next - current_wins)

    return {
        "current_tier":      current_tier,
        "next_tier":         next_tier,
        "trades":            trades,
        "win_rate":          wr,
        "min_trades_for_next": min_trades,
        "min_wr_for_next":   min_wr,
        "trades_needed":     trades_needed,
        "wins_needed":       wins_needed,
    }


# ---------------------------------------------------------------------------
# Tier change logging
# ---------------------------------------------------------------------------

def _log_tier_change(
    agent_name: str,
    old_tier: str,
    new_tier: str,
    trades: int,
    win_rate: float,
) -> None:
    """Append a promotion/demotion record to logs/tier_changes.log."""
    direction = "PROMOTED" if _TIER_RANK.get(new_tier, 0) > _TIER_RANK.get(old_tier, 0) else "DEMOTED"
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (
        f"{ts} | {direction} | {agent_name} | "
        f"{old_tier.upper()} → {new_tier.upper()} | "
        f"trades={trades} win_rate={win_rate:.0%}\n"
    )
    try:
        os.makedirs(os.path.dirname(_TIER_CHANGES_LOG), exist_ok=True)
        with open(_TIER_CHANGES_LOG, "a", encoding="utf-8") as f:
            f.write(line)
        logger.info("[TierManager] %s", line.strip())
    except Exception as e:
        logger.warning("[TierManager] tier_changes.log write failed: %s", e)
