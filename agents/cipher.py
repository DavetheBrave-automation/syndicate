"""
cipher.py — CIPHER Pattern Recognition Agent.

Strategy: Trade only patterns with a proven historical win rate in syndicate_trades.db.
CIPHER improves with every trade. After 50 trades it becomes one of the most valuable agents.

Pattern definition: series_prefix + price_bucket + contract_class
Win rate calculated live from DB on every evaluate() call — never cached.

Rules:
  - Only trade patterns with 60%+ historical win rate
  - Minimum 10 historical examples required before trusting a pattern
  - Recalculate win rates from DB on every evaluate call — never cache
  - Weight recent trades 2x vs older trades — markets evolve
  - Never trade against a pattern with 65%+ win rate in opposite direction
  - After 50 trades this agent becomes the most valuable — trust the data
  - YES side: buy when pattern shows wins with YES entries
  - NO side: buy when pattern shows wins with NO entries
"""

import os
import sys
import logging
import sqlite3

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.cipher")

_DB_PATH        = os.path.join(_SYNDICATE_ROOT, "logs", "syndicate_trades.db")
_MIN_VOLUME     = 1_000
_MIN_TRADES     = 10
_MIN_WIN_RATE   = 0.60
_CONFLICT_RATE  = 0.65


def _price_bucket(yes_price: float) -> str:
    """Map yes_price to 20-cent bucket string."""
    p = int(yes_price * 100)
    if p < 20:
        return "0-20"
    elif p < 40:
        return "20-40"
    elif p < 60:
        return "40-60"
    elif p < 80:
        return "60-80"
    else:
        return "80-100"


def _extract_series(ticker: str) -> str:
    return ticker.split("-")[0].upper() if ticker else ""


def _query_pattern(series: str, price_bkt: str, contract_class: str) -> dict:
    """
    Query syndicate_trades.db for trades matching this pattern.
    Returns {yes_win_rate, no_win_rate, yes_count, no_count, total} with 2x recency weight.
    Returns empty dict if DB missing or no data.
    """
    if not os.path.exists(_DB_PATH):
        return {}

    try:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Fetch all trades for this series+class — we derive price_bucket ourselves
        rows = conn.execute(
            """SELECT side, entry_price, pnl, entry_time
               FROM syndicate_trades
               WHERE ticker LIKE ? AND contract_class = ?
               ORDER BY id DESC LIMIT 200""",
            (f"{series}%", contract_class),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug("[CIPHER] DB query error: %s", e)
        return {}

    if not rows:
        return {}

    # Filter by price bucket and apply 2x recency weight (first half of results = recent)
    yes_wins = yes_losses = no_wins = no_losses = 0
    total = len(rows)
    for i, row in enumerate(rows):
        bkt = _price_bucket(row["entry_price"] / 100.0)
        if bkt != price_bkt:
            continue
        weight = 2 if i < total // 2 else 1  # 2x for recent half
        pnl  = row["pnl"]
        side = row["side"]
        won  = pnl is not None and pnl > 0
        if side == "yes":
            if won:
                yes_wins   += weight
            else:
                yes_losses += weight
        else:
            if won:
                no_wins   += weight
            else:
                no_losses += weight

    yes_total = yes_wins + yes_losses
    no_total  = no_wins  + no_losses

    return {
        "yes_win_rate": (yes_wins / yes_total) if yes_total >= _MIN_TRADES else None,
        "no_win_rate":  (no_wins  / no_total)  if no_total  >= _MIN_TRADES else None,
        "yes_count":    yes_total,
        "no_count":     no_total,
    }


def _cipher_validation_stats() -> dict:
    """
    Query syndicate_trades.db for CIPHER's contribution to the validation phase.
    Returns counts of patterns discovered, patterns with 60%+ win rate, total trades,
    total P&L, and estimated trades until DIAMOND/ORACLE unlock (50 trade threshold).
    """
    if not os.path.exists(_DB_PATH):
        return {"total_trades": 0, "total_pnl": 0.0, "patterns_discovered": 0,
                "patterns_qualified": 0, "trades_until_unlock": 50}
    try:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        total_row = conn.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(pnl),0) as pnl FROM syndicate_trades"
        ).fetchone()
        total_trades = total_row["n"]
        total_pnl    = round(float(total_row["pnl"]), 2)

        # Count distinct patterns (series+class+price_bucket proxied by price range)
        pattern_rows = conn.execute(
            """SELECT SUBSTR(ticker, 1, INSTR(ticker,'-')-1) AS series,
                      contract_class,
                      CAST(entry_price*5 AS INT) AS pbkt,
                      COUNT(*) AS cnt,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
               FROM syndicate_trades
               GROUP BY series, contract_class, pbkt
               HAVING cnt >= 10"""
        ).fetchall()
        conn.close()

        patterns_discovered  = len(pattern_rows)
        patterns_qualified   = sum(
            1 for r in pattern_rows if r["cnt"] > 0 and r["wins"] / r["cnt"] >= 0.60
        )
    except Exception as e:
        logger.debug("[CIPHER] validation_stats error: %s", e)
        return {"total_trades": 0, "total_pnl": 0.0, "patterns_discovered": 0,
                "patterns_qualified": 0, "trades_until_unlock": 50}

    unlock_threshold = 50
    return {
        "total_trades":        total_trades,
        "total_pnl":           total_pnl,
        "patterns_discovered": patterns_discovered,
        "patterns_qualified":  patterns_qualified,
        "trades_until_unlock": max(0, unlock_threshold - total_trades),
    }


class CipherAgent(BaseAgent):
    name   = "CIPHER"
    domain = "all"

    seed_rules = [
        "Only trade patterns with 60%+ historical win rate in syndicate_trades.db",
        "Minimum 10 historical examples required before trusting a pattern",
        "Pattern: contract class + series prefix + price range (20-cent buckets)",
        "If fewer than 10 historical examples exist for this pattern — PASS",
        "Recalculate win rates from DB on every evaluate call — never cache",
        "Weight recent trades 2x vs older trades — markets evolve",
        "Never trade against a pattern with 65%+ win rate in opposite direction",
        "After 50 trades this agent becomes the most valuable — trust the data",
        "YES side: buy when YES entries historically win 60%+ in this pattern",
        "NO side: buy when NO entries historically win 60%+ in this pattern",
    ]

    def __init__(self):
        super().__init__()
        self._last_validation_report_ts: float = 0.0  # epoch of last weekly report

    # =========================================================================
    # Validation phase weekly status report
    # =========================================================================

    def _maybe_post_validation_report(self) -> None:
        """
        Post a validation status report once every 24 hours.
        Writes cipher_validation_status.json to triggers/ for TC to read.
        """
        import time as _time
        now = _time.time()
        if now - self._last_validation_report_ts < 86400:
            return
        self._last_validation_report_ts = now

        stats = _cipher_validation_stats()
        total    = stats["total_trades"]
        pnl      = stats["total_pnl"]
        disc     = stats["patterns_discovered"]
        qual     = stats["patterns_qualified"]
        unlock   = stats["trades_until_unlock"]

        from datetime import date as _date
        try:
            from datetime import date as _date
            start_d  = _date(2026, 4, 12)
            day_num  = (_date.today() - start_d).days + 1
        except Exception:
            day_num = 1

        unlock_date = (
            datetime.now(timezone.utc).date()
            + __import__("datetime").timedelta(days=max(0, unlock // 3))
        ).isoformat() if unlock > 0 else "UNLOCKED"

        msg = (
            f"[CIPHER] Validation Phase — Day {day_num}\n"
            f"Patterns discovered: {disc}\n"
            f"Patterns with 60%+ win rate: {qual}\n"
            f"Total trades: {total} | P&L: ${pnl:+.2f}\n"
            f"Trades until DIAMOND/ORACLE unlock: {unlock}\n"
            f"Estimated unlock date: {unlock_date}"
        )
        logger.info(msg)

        # Write trigger for TC
        import json as _json
        import os as _os
        _triggers = _os.path.join(_SYNDICATE_ROOT, "triggers")
        _os.makedirs(_triggers, exist_ok=True)
        _path = _os.path.join(_triggers, "cipher_validation_status.json")
        try:
            with open(_path + ".tmp", "w", encoding="utf-8") as f:
                _json.dump({
                    "type":                "cipher_validation_status",
                    "day":                 day_num,
                    "patterns_discovered": disc,
                    "patterns_qualified":  qual,
                    "total_trades":        total,
                    "total_pnl":           pnl,
                    "trades_until_unlock": unlock,
                    "unlock_date":         unlock_date,
                }, f, indent=2)
            _os.replace(_path + ".tmp", _path)
        except Exception as e:
            logger.debug("[CIPHER] validation status write failed: %s", e)

        try:
            from notifications.telegram import post as _tg
            _tg(msg)
        except Exception:
            pass

    # =========================================================================
    # should_evaluate — hot path (minimal I/O check)
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        """
        Fast gate: is there any trade history at all?
        Full pattern query happens in evaluate().
        """
        if not self._base_should_evaluate(market):
            return False

        if market.volume_dollars < _MIN_VOLUME:
            return False

        # Quick DB existence check (stat only — no query)
        if not os.path.exists(_DB_PATH):
            return False

        return True

    # =========================================================================
    # evaluate — daemon thread (DB query here)
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        """
        Build pattern signature, query DB, submit signal if pattern win rate ≥ 60%.

        YES side: buy when YES entries historically win on this pattern
        NO side:  buy when NO entries historically win on this pattern
        """
        self._maybe_post_validation_report()

        series  = _extract_series(market.ticker)
        bkt     = _price_bucket(market.yes_price)
        cls_    = market.contract_class

        stats   = _query_pattern(series, bkt, cls_)
        if not stats:
            return

        yes_wr  = stats.get("yes_win_rate")
        no_wr   = stats.get("no_win_rate")
        yes_cnt = stats.get("yes_count", 0)
        no_cnt  = stats.get("no_count", 0)

        best_side  = None
        best_wr    = 0.0
        best_count = 0

        # Pick the side with higher win rate above threshold
        if yes_wr is not None and yes_wr >= _MIN_WIN_RATE:
            best_side, best_wr, best_count = "yes", yes_wr, yes_cnt
        if no_wr is not None and no_wr >= _MIN_WIN_RATE and no_wr > best_wr:
            best_side, best_wr, best_count = "no", no_wr, no_cnt

        if best_side is None:
            logger.debug(
                "[CIPHER] No qualifying pattern: %s %s %s (yes_wr=%s no_wr=%s)",
                series, bkt, cls_, yes_wr, no_wr,
            )
            return

        # Block if opposing side has strong counter-signal
        opponent_wr = no_wr if best_side == "yes" else yes_wr
        if opponent_wr is not None and opponent_wr >= _CONFLICT_RATE:
            logger.debug(
                "[CIPHER] Conflict signal — opposing side win rate=%.1f%% for %s",
                opponent_wr * 100, market.ticker,
            )
            return

        # Conviction tier based on win rate
        if best_wr >= 0.75:
            conviction_tier = "PROPHECY"
        elif best_wr >= 0.70:
            conviction_tier = "HIGH_CONVICTION"
        elif best_wr >= 0.65:
            conviction_tier = "HIGH_CONVICTION"
        else:
            conviction_tier = "GLITCH"

        # Edge estimate: win_rate advantage over 50% baseline
        edge_pct = round((best_wr - 0.50) * 100.0 * 1.5, 2)   # scaled

        if best_side == "yes":
            entry_price  = round(market.yes_price, 4)
            target_price = round(min(0.95, entry_price * 1.15), 4)
            stop_price   = round(max(0.05, entry_price * 0.85), 4)
        else:
            no_price     = round(1.0 - market.yes_price, 4)
            entry_price  = round(market.yes_price, 4)
            target_price = round(max(0.05, market.yes_price * 0.85), 4)
            stop_price   = round(min(0.95, market.yes_price * 1.15), 4)

        reasoning = (
            f"CIPHER: Pattern {series}|{bkt}|{cls_} → {best_side.upper()} "
            f"win_rate={best_wr:.1%} ({best_count} trades, 2x recency weight). "
            f"{'YES: profits if price above strike.' if best_side == 'yes' else f'NO: profits if price below strike (NO costs {round(1.0-market.yes_price,2):.2f}).'}"
        )

        signal = self.build_signal(
            market, conviction_tier, edge_pct, best_side,
            entry_price, target_price, stop_price, reasoning, game,
        )
        self.submit_signal(signal)
        logger.info(
            "[CIPHER] Signal: %s %s win_rate=%.1f%% (n=%d) tier=%s",
            market.ticker, best_side.upper(), best_wr * 100, best_count, conviction_tier,
        )
