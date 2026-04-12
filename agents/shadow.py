"""
shadow.py — SHADOW Meta-Agent.

Identifies the top-performing agent from the last 20 trades in syndicate_trades.db
and submits signals that mirror that agent's domain at reduced conviction.

If top agent is tennis-based (ACE, PHOENIX, ENDGAME): SHADOW acts on tennis markets.
If top agent is math-based (AXIOM, BLITZ): SHADOW acts on any liquid market.
If no agent has 5+ trades: defaults to AXIOM's domain (all markets).

Always reduces conviction by one level. Never leads — only echoes.
"""

import logging
import sqlite3
import os
import sys

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.shadow")

_DB_PATH = os.path.join(_SYNDICATE_ROOT, "logs", "syndicate_trades.db")
_MIN_TRADES_FOR_CONFIDENCE = 5
_TENNIS_SERIES = {"KXATPMATCH", "KXWTAMATCH"}
_TENNIS_AGENTS = {"ACE", "PHOENIX", "ENDGAME"}
_MATH_AGENTS   = {"AXIOM", "BLITZ"}


def _find_top_agent(n: int = 20) -> str:
    """
    Read the last N trades from syndicate_trades.db.
    Return the agent_name with the highest win rate (min 5 trades).
    Falls back to 'AXIOM' if insufficient data.
    """
    if not os.path.exists(_DB_PATH):
        return "AXIOM"

    try:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT agent_name, pnl FROM syndicate_trades ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug("[SHADOW] DB read error: %s", e)
        return "AXIOM"

    # Tally wins per agent
    buckets: dict[str, list[float]] = {}
    for row in rows:
        name = row["agent_name"]
        pnl  = row["pnl"]
        if name and pnl is not None:
            buckets.setdefault(name, []).append(float(pnl))

    best_agent    = "AXIOM"
    best_win_rate = 0.0
    for name, pnls in buckets.items():
        if len(pnls) < _MIN_TRADES_FOR_CONFIDENCE:
            continue
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        if win_rate > best_win_rate:
            best_win_rate = win_rate
            best_agent    = name

    logger.debug("[SHADOW] Top agent: %s (win_rate=%.1f%%)", best_agent, best_win_rate * 100)
    return best_agent


def _find_top_agent_for_series(series: str, n: int = 50) -> str:
    """
    Find the agent with the best win rate specifically on a given series prefix.
    Example: AXIOM might win 70% on KXPGATOUR but only 50% overall.
    Falls back to _find_top_agent() if insufficient series data.
    """
    if not series or not os.path.exists(_DB_PATH):
        return _find_top_agent(n)

    try:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT agent_name, pnl FROM syndicate_trades
               WHERE ticker LIKE ? ORDER BY id DESC LIMIT ?""",
            (f"{series}%", n),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug("[SHADOW] Series DB read error (%s): %s", series, e)
        return _find_top_agent(n)

    if not rows:
        return _find_top_agent(n)

    buckets: dict[str, list[float]] = {}
    for row in rows:
        name = row["agent_name"]
        pnl  = row["pnl"]
        if name and pnl is not None:
            buckets.setdefault(name, []).append(float(pnl))

    best_agent    = None
    best_win_rate = 0.0
    for name, pnls in buckets.items():
        if len(pnls) < _MIN_TRADES_FOR_CONFIDENCE:
            continue
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        if win_rate > best_win_rate:
            best_win_rate = win_rate
            best_agent    = name

    if best_agent is None:
        return _find_top_agent(n)

    logger.debug(
        "[SHADOW] Top agent for series %s: %s (win_rate=%.1f%%)",
        series, best_agent, best_win_rate * 100,
    )
    return best_agent


def _extract_series(ticker: str) -> str:
    """Extract series prefix from ticker: 'KXPGATOUR-26APR10-SIN' → 'KXPGATOUR'."""
    return ticker.split("-")[0].upper() if ticker else ""


def _reduce_tier(tier: str) -> str:
    """Step down conviction: PROPHECY → HIGH_CONVICTION → HIGH_CONVICTION (floor)."""
    if tier == "PROPHECY":
        return "HIGH_CONVICTION"
    return "HIGH_CONVICTION"


class ShadowAgent(BaseAgent):
    name                  = "SHADOW"
    domain                = "all"
    MAX_SIGNALS_PER_CYCLE = 2   # SHADOW echoes best agent — limit to 2 per cycle

    seed_rules = [
        "Copy the strategy of the agent with highest win rate in last 20 trades",
        "If no agent has 5+ trades, copy AXIOM as default",
        "Never override an agent that is currently in a position",
        "Reduce conviction by 1 level — shadow never leads, only follows",
        "If top agent is GHOST, max bet stays at $1",
        "Re-evaluate which agent to shadow every 10 trades",
        "If top agent loses 3 in a row, switch to second-best immediately",
    ]

    # =========================================================================
    # should_evaluate — hot path
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        if not self._base_should_evaluate(market):
            return False

        if market.volume_dollars < 2_000:
            return False

        # Series-specific top agent: find best agent for THIS series first
        series = _extract_series(market.ticker)
        top    = _find_top_agent_for_series(series) if series else _find_top_agent()

        if top in _TENNIS_AGENTS:
            return market.series_ticker in _TENNIS_SERIES
        # Math/velocity agents cover all liquid markets
        return True

    # =========================================================================
    # evaluate — daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        series = _extract_series(market.ticker)
        top    = _find_top_agent_for_series(series) if series else _find_top_agent()
        logger.debug("[SHADOW] Shadowing %s for %s (series=%s)", top, market.ticker, series)

        # Import and delegate to top agent's evaluate logic
        # We run it as SHADOW to avoid duplicating signals
        try:
            agent = self._instantiate_agent(top)
        except Exception as e:
            logger.debug("[SHADOW] Could not instantiate %s: %s", top, e)
            return

        if agent is None:
            return

        if not agent.should_evaluate(market, game):
            logger.debug("[SHADOW] Top agent %s would not evaluate %s", top, market.ticker)
            return

        # Intercept submit_signal to reduce conviction and re-brand as SHADOW
        original_submit = agent.submit_signal
        captured: list = []

        def _capture_signal(signal: dict) -> bool:
            captured.append(signal)
            return True

        agent.submit_signal = _capture_signal  # type: ignore[method-assign]
        try:
            agent.evaluate(market, game)
        except Exception as e:
            logger.debug("[SHADOW] Top agent %s evaluate error: %s", top, e)
            return
        finally:
            agent.submit_signal = original_submit

        if not captured:
            return

        signal = captured[0]
        sig    = signal.get("signal", {})

        # Re-brand as SHADOW at reduced conviction
        original_tier = sig.get("conviction_tier", "HIGH_CONVICTION")
        reduced_tier  = _reduce_tier(original_tier)
        sig["conviction_tier"] = reduced_tier
        sig["agent_name"]      = self.name
        sig["reasoning"] = f"[SHADOW → {top}] " + sig.get("reasoning", "")
        signal["agent"]  = self.name

        # Cap at $1 if shadowing GHOST
        if top == "GHOST":
            sig["max_size_dollars"] = 1

        self.submit_signal(signal)

    # =========================================================================
    # Internal
    # =========================================================================

    @staticmethod
    def _instantiate_agent(name: str):
        """Lazy-import and instantiate a named agent."""
        _map = {
            "ACE":     ("agents.ace",     "AceAgent"),
            "AXIOM":   ("agents.axiom",   "AxiomAgent"),
            "PHOENIX": ("agents.phoenix", "PhoenixAgent"),
            "BLITZ":   ("agents.blitz",   "BlitzAgent"),
            "GHOST":   ("agents.ghost",   "GhostAgent"),
            "ENDGAME": ("agents.endgame", "EndgameAgent"),
            "DIAMOND": ("agents.diamond", "DiamondAgent"),
        }
        entry = _map.get(name)
        if not entry:
            return None
        module_path, class_name = entry
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        return cls()
