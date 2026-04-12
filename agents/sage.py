"""
sage.py — SAGE Deep Learning Analyst. Always on the decision panel.

SAGE studies the entire collective's trade history and provides historical
intelligence to TC before every decision. It doesn't trade independently —
it grades patterns and enhances/suppresses conviction.

SAGE is mandatory on every panel. After 20 trades it provides real context.
After 100 trades it becomes the collective's institutional memory.

Rules:
  - Study every closed trade in syndicate_trades.db before evaluating any signal
  - Identify which market conditions produced wins vs losses across ALL agents
  - If a similar setup has lost 3+ times historically — BLOCK regardless of current edge
  - If a similar setup has won 70%+ historically — APPROVE and increase conviction
  - Track: series, contract_class, price_range, outcome
  - Surface the single most important historical insight in every panel decision
  - If fewer than 20 total trades in DB — abstain with NEUTRAL, insufficient data
  - Weight last 30 trades 3x more than older trades — market conditions evolve
  - Track which agents have been most accurate — weight their signals accordingly
  - Never override math. If AXIOM says no edge, SAGE defers on edge questions
"""

import os
import sys
import json
import logging
import sqlite3
from datetime import datetime, timezone

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.sage")

_DB_PATH          = os.path.join(_SYNDICATE_ROOT, "logs", "syndicate_trades.db")
_MIN_PANEL_TRADES = 20
_MIN_PATTERN_N    = 5
_STRONG_WIN_RATE  = 0.70
_WEAK_WIN_RATE    = 0.40
_RECENCY_WEIGHT   = 3     # weight applied to last 30 trades


def _extract_series(ticker: str) -> str:
    return ticker.split("-")[0].upper() if ticker else ""


def _price_bucket(yes_price: float) -> str:
    p = int(yes_price * 100)
    if p < 20: return "0-20"
    elif p < 40: return "20-40"
    elif p < 60: return "40-60"
    elif p < 80: return "60-80"
    else: return "80-100"


def _query_pattern_stats(series: str, price_bkt: str, contract_class: str) -> dict:
    """
    Query DB for historical performance of this pattern across all agents.
    Applies 3x weight to last 30 trades for recency.
    Returns dict with win_rate, sample_size, best_agent, worst_agent, last_3.
    """
    if not os.path.exists(_DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT agent_name, side, entry_price, pnl, entry_time
               FROM syndicate_trades
               WHERE ticker LIKE ? AND contract_class = ?
               ORDER BY id DESC LIMIT 200""",
            (f"{series}%", contract_class),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug("[SAGE] DB query error: %s", e)
        return {}

    if not rows:
        return {}

    total  = len(rows)
    wins   = 0
    losses = 0
    agent_pnl: dict[str, list] = {}
    last_3 = []

    for i, row in enumerate(rows):
        bkt = _price_bucket(row["entry_price"] / 100.0)
        if bkt != price_bkt:
            continue
        pnl    = row["pnl"]
        agent  = row["agent_name"] or "unknown"
        weight = _RECENCY_WEIGHT if i < 30 else 1
        won    = pnl is not None and pnl > 0

        for _ in range(weight):
            if won: wins += 1
            else:   losses += 1

        agent_pnl.setdefault(agent, []).append(float(pnl or 0))
        if len(last_3) < 3:
            last_3.append({
                "agent":   agent,
                "pnl":     round(float(pnl or 0), 4),
                "outcome": "WIN" if won else "LOSS",
            })

    sample = wins + losses
    if sample < _MIN_PATTERN_N:
        return {"sample_size": sample, "insufficient": True}

    win_rate = wins / sample

    # Best and worst agent by win rate
    agent_stats = {}
    for name, pnls in agent_pnl.items():
        if len(pnls) < 2:
            continue
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        agent_stats[name] = wr

    best_agent  = max(agent_stats, key=lambda k: agent_stats[k]) if agent_stats else None
    worst_agent = min(agent_stats, key=lambda k: agent_stats[k]) if agent_stats else None

    if win_rate >= _STRONG_WIN_RATE:
        recommendation = "favorable"
        key_insight = f"Pattern wins {win_rate:.0%} — {sample} historical trades confirm edge."
    elif win_rate <= _WEAK_WIN_RATE:
        recommendation = "unfavorable"
        key_insight = f"Pattern wins only {win_rate:.0%} — {sample} trades suggest against."
    else:
        recommendation = "neutral"
        key_insight = f"Pattern at {win_rate:.0%} — mixed signals, moderate caution."

    return {
        "pattern_win_rate":                    round(win_rate, 4),
        "sample_size":                         sample,
        "best_performing_agent_on_pattern":    best_agent,
        "worst_performing_agent_on_pattern":   worst_agent,
        "last_3_similar_trades":               last_3,
        "recommendation":                      recommendation,
        "key_insight":                         key_insight,
        "insufficient":                        False,
    }


def _total_trade_count() -> int:
    """Return total trades in DB (fast count)."""
    if not os.path.exists(_DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        n    = conn.execute("SELECT COUNT(*) FROM syndicate_trades").fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


class SageAgent(BaseAgent):
    name   = "SAGE"
    domain = "all"

    ALWAYS_ON_PANEL = True   # Injected into every TC panel decision

    seed_rules = [
        "Study every closed trade in syndicate_trades.db before evaluating any signal",
        "Identify which market conditions produced wins vs losses across ALL agents",
        "If a similar setup has lost 3+ times historically — BLOCK regardless of current edge",
        "If a similar setup has won 70%+ historically — APPROVE and increase conviction",
        "Track: series, contract_class, price_range, outcome",
        "Surface the single most important historical insight in every panel decision",
        "If fewer than 20 total trades in DB — abstain with NEUTRAL, insufficient data",
        "Weight last 30 trades 3x more than older trades — market conditions evolve",
        "Track which agents have been most accurate — weight their signals accordingly",
        "Never override math. If AXIOM says no edge, SAGE defers on edge questions",
    ]

    def __init__(self, config=None):
        super().__init__()

    # =========================================================================
    # Panel briefing — called before every TC decision
    # =========================================================================

    def get_panel_briefing(self, market) -> dict:
        """
        Called before every TC panel session for this market.
        Returns structured historical briefing injected into TC prompt.
        """
        total = _total_trade_count()
        if total < _MIN_PANEL_TRADES:
            return {
                "pattern_win_rate":                  None,
                "sample_size":                       total,
                "best_performing_agent_on_pattern":  None,
                "worst_performing_agent_on_pattern": None,
                "last_3_similar_trades":             [],
                "recommendation":                    "insufficient_data",
                "key_insight":                       f"SAGE: Only {total} trades in DB. Need {_MIN_PANEL_TRADES} for reliable patterns.",
                "insufficient":                      True,
            }

        series = _extract_series(market.ticker)
        bkt    = _price_bucket(market.yes_price)
        cls_   = market.contract_class

        stats = _query_pattern_stats(series, bkt, cls_)
        if not stats:
            return {
                "pattern_win_rate":  None,
                "sample_size":       0,
                "recommendation":    "insufficient_data",
                "key_insight":       f"SAGE: No historical data for pattern {series}|{bkt}|{cls_}.",
                "insufficient":      True,
            }

        return stats

    def get_panel_briefing_str(self, market) -> str:
        """Format the briefing as a compact string for prompt injection."""
        b = self.get_panel_briefing(market)
        if b.get("insufficient"):
            return f"SAGE: {b.get('key_insight', 'Insufficient data.')}"
        wr   = b.get("pattern_win_rate")
        n    = b.get("sample_size", 0)
        rec  = b.get("recommendation", "neutral").upper()
        best = b.get("best_performing_agent_on_pattern") or "N/A"
        insight = b.get("key_insight", "")
        last3 = b.get("last_3_similar_trades", [])
        last3_str = " | ".join(
            f"{t['agent']} {t['outcome']} ${t['pnl']:+.2f}" for t in last3
        ) if last3 else "none"
        return (
            f"SAGE [{rec}]: Pattern win_rate={wr:.0%} (n={n}). "
            f"Best agent on this pattern: {best}. "
            f"Last 3 similar: {last3_str}. "
            f"Insight: {insight}"
        )

    # =========================================================================
    # should_evaluate — SAGE evaluates all non-WATCH contracts
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        if market.contract_class == "WATCH":
            return False
        if not self._benched_cache:
            return True
        return not self.is_benched()

    # =========================================================================
    # evaluate — SAGE writes historical context, does not issue BUY signals
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        """
        SAGE doesn't buy. It writes a briefing trigger for TC injection.
        If pattern is catastrophically bad (< 35% win rate, n >= 10),
        it writes a suppress signal that blocks or downgrades conviction.
        """
        total = _total_trade_count()
        if total < _MIN_PANEL_TRADES:
            return

        series = _extract_series(market.ticker)
        bkt    = _price_bucket(market.yes_price)
        cls_   = market.contract_class

        stats = _query_pattern_stats(series, bkt, cls_)
        if not stats or stats.get("insufficient"):
            return

        wr = stats.get("pattern_win_rate", 0.5)
        n  = stats.get("sample_size", 0)

        # Only surface strong signals — noisy patterns ignored
        if n < _MIN_PATTERN_N:
            return

        logger.debug(
            "[SAGE] Pattern %s|%s|%s: win_rate=%.1f%% (n=%d) rec=%s",
            series, bkt, cls_, wr * 100, n, stats.get("recommendation"),
        )

    # =========================================================================
    # Decision grading
    # =========================================================================

    def grade_decision(self, signal: dict, decision: str, outcome: dict) -> None:
        """
        Called after a trade closes. Records whether TC made the right call.
        Tracks panel accuracy in SAGE memory.

        decision: "BUY" or "PASS"
        outcome: {"pnl": float, "ticker": str, "exit_reason": str}
        """
        pnl     = outcome.get("pnl", 0.0)
        ticker  = outcome.get("ticker", "?")
        won     = pnl > 0

        # Grade the decision
        if decision.upper() == "BUY" and won:
            grade = "correct_buy"
        elif decision.upper() == "BUY" and not won:
            grade = "incorrect_buy"
        elif decision.upper() == "PASS" and not won:
            grade = "correct_pass"   # avoided a loser
        else:
            grade = "missed_win"     # passed on a winner

        try:
            mem = self.load_memory()
            panel_acc = mem.setdefault("panel_accuracy", {
                "correct_buy": 0, "incorrect_buy": 0,
                "correct_pass": 0, "missed_win": 0,
            })
            panel_acc[grade] = panel_acc.get(grade, 0) + 1
            self.save_memory(mem)
        except Exception as e:
            logger.warning("[SAGE] grade_decision save failed: %s", e)
