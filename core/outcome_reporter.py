"""
outcome_reporter.py — Trade outcome logging, trigger file dispatch, and Discord
reporting for The Syndicate.

SQLite schema (syndicate_trades table):
  id             INTEGER PRIMARY KEY AUTOINCREMENT
  ticker         TEXT NOT NULL
  side           TEXT NOT NULL
  entry_price    INTEGER NOT NULL    -- cents integer (e.g. 45)
  exit_price     REAL                -- decimal dollars (e.g. 0.62)
  quantity       INTEGER NOT NULL
  pnl            REAL
  hold_seconds   REAL
  exit_reason    TEXT
  rule_id        TEXT
  agent_name     TEXT
  contract_class TEXT
  entry_time     TEXT NOT NULL       -- ISO8601 UTC
  exit_time      TEXT NOT NULL       -- ISO8601 UTC
  order_id       TEXT

Import note: order_manager should use `from core.outcome_reporter import outcome_reporter`.
             (scalper.outcome_reporter was incorrect — file lives in core/)
"""

import os
import sys
import json
import sqlite3
import threading
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Roots — must be set before any local imports
# ---------------------------------------------------------------------------

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ATLAS_ROOT = os.path.join(os.path.dirname(_SYNDICATE_ROOT), "atlas")

sys.path.insert(0, _SYNDICATE_ROOT)
sys.path.insert(0, _ATLAS_ROOT)

# ---------------------------------------------------------------------------
# Config (paper mode flag)
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        import yaml
        cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def _is_paper_mode() -> bool:
    try:
        return bool(_load_config().get("syndicate", {}).get("paper_mode", False))
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger("syndicate.outcomes")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DB_PATH      = os.path.join(_SYNDICATE_ROOT, "logs", "syndicate_trades.db")
_TRIGGERS_DIR = os.path.join(_SYNDICATE_ROOT, "triggers")


# ---------------------------------------------------------------------------
# OutcomeReporter
# ---------------------------------------------------------------------------

class OutcomeReporter:
    """
    Records closed trade outcomes to SQLite, writes trigger JSON files, and
    fires Discord exit notifications in a non-blocking daemon thread.

    Thread-safe: all DB operations are guarded by _db_lock.
    """

    def __init__(self):
        self._db_lock   = threading.Lock()
        self._agents: dict = {}   # agent_name → agent instance (populated via register_agents)
        self.init_db()

    # -----------------------------------------------------------------------
    # Agent registry
    # -----------------------------------------------------------------------

    def register_agents(self, agents: list) -> None:
        """Register agent instances so on_outcome() can be dispatched after each trade."""
        self._agents = {a.name: a for a in agents}
        logger.info(
            "[OutcomeReporter] Agent registry: %s",
            list(self._agents.keys()),
        )

    # -----------------------------------------------------------------------
    # DB setup
    # -----------------------------------------------------------------------

    def init_db(self):
        """Create syndicate_trades.db and schema if not present."""
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        with self._db_lock:
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS syndicate_trades (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker         TEXT    NOT NULL,
                    side           TEXT    NOT NULL,
                    entry_price    INTEGER NOT NULL,
                    exit_price     REAL,
                    quantity       INTEGER NOT NULL,
                    pnl            REAL,
                    hold_seconds   REAL,
                    exit_reason    TEXT,
                    rule_id        TEXT,
                    agent_name     TEXT,
                    contract_class TEXT,
                    entry_time     TEXT    NOT NULL,
                    exit_time      TEXT    NOT NULL,
                    order_id       TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_st_ticker ON syndicate_trades(ticker)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_st_entry_time ON syndicate_trades(entry_time)"
            )
            conn.commit()
            conn.close()
        logger.info("[OutcomeReporter] DB initialised at %s", _DB_PATH)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _get_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _utcnow_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _fire(fn, *args, **kwargs):
        """Run fn(*args, **kwargs) in a daemon thread (fire-and-forget)."""
        t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
        t.start()

    # -----------------------------------------------------------------------
    # Primary entry point
    # -----------------------------------------------------------------------

    def record_outcome(self, position, exit_price: float,
                       exit_reason: str, pnl: float, spread: float = 0.0):
        """
        Record a closed trade outcome.

        Args:
            position:    core.shared_state.Position dataclass
            exit_price:  decimal dollars (e.g. 0.62)
            exit_reason: human-readable exit label
            pnl:         dollars P&L (positive = win, negative = loss)
        """
        pfx = "[PAPER] " if _is_paper_mode() else ""

        exit_time_iso = self._utcnow_iso()

        # Calculate hold seconds from entry_time (unix timestamp)
        try:
            entry_dt = datetime.fromtimestamp(position.entry_time, tz=timezone.utc)
            exit_dt  = datetime.strptime(exit_time_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            hold_seconds = max(0.0, (exit_dt - entry_dt).total_seconds())
        except Exception:
            hold_seconds = 0.0

        entry_time_iso = datetime.fromtimestamp(
            position.entry_time, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # -- Write to DB ------------------------------------------------------
        with self._db_lock:
            try:
                conn = self._get_conn()
                conn.execute("""
                    INSERT INTO syndicate_trades
                      (ticker, side, entry_price, exit_price, quantity,
                       pnl, hold_seconds, exit_reason, rule_id, agent_name,
                       contract_class, entry_time, exit_time, order_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    position.ticker,
                    position.side,
                    int(position.entry_price),   # cents integer
                    exit_price,
                    position.quantity,
                    pnl,
                    hold_seconds,
                    exit_reason,
                    getattr(position, "rule_id", None),
                    getattr(position, "agent_name", None),
                    getattr(position, "contract_class", None),
                    entry_time_iso,
                    exit_time_iso,
                    getattr(position, "order_id", None),
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error("%s[OutcomeReporter] DB write failed: %s", pfx, e)

        result = "WIN" if pnl >= 0 else "LOSS"
        logger.info(
            "%s[OutcomeReporter] %s | %s %s %dx entry=%d¢ exit=%.3f "
            "pnl=$%.2f hold=%.0fs | %s",
            pfx, result,
            position.side.upper(), position.ticker, position.quantity,
            int(position.entry_price), exit_price, pnl, hold_seconds,
            exit_reason,
        )

        # -- Write trigger JSON -----------------------------------------------
        try:
            self._write_trigger(
                position=position,
                exit_price=exit_price,
                exit_reason=exit_reason,
                pnl=pnl,
                spread=spread,
                hold_seconds=hold_seconds,
                entry_time_iso=entry_time_iso,
                exit_time_iso=exit_time_iso,
            )
        except Exception as e:
            logger.warning("%s[OutcomeReporter] Trigger write failed: %s", pfx, e)

        # -- Dispatch on_outcome to originating agent (non-blocking) ----------
        agent_name = getattr(position, "agent_name", None)
        if agent_name and agent_name in self._agents:
            outcome_dict = {
                "pnl":          pnl,
                "ticker":       position.ticker,
                "exit_reason":  exit_reason,
                "exit_price":   exit_price,
                "hold_seconds": hold_seconds,
                "side":         position.side,
                "quantity":     position.quantity,
            }
            self._fire(self._agents[agent_name].on_outcome, outcome_dict)

        # -- Discord + Telegram notification (non-blocking) ------------------
        self._fire(
            self._post_discord_exit,
            position, exit_price, exit_reason, pnl, hold_seconds, pfx,
        )
        self._fire(
            self._post_telegram_exit,
            position, exit_price, exit_reason, pnl, hold_seconds, pfx,
        )

    # -----------------------------------------------------------------------
    # Trigger file
    # -----------------------------------------------------------------------

    def _write_trigger(self, position, exit_price: float, exit_reason: str,
                       pnl: float, hold_seconds: float,
                       entry_time_iso: str, exit_time_iso: str,
                       spread: float = 0.0):
        os.makedirs(_TRIGGERS_DIR, exist_ok=True)
        ts_tag = exit_time_iso.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
        filename = f"outcome_{position.ticker}_{ts_tag}.json"
        spread_cost = round(spread * position.quantity, 4)
        payload = {
            "type":              "outcome",
            "ticker":            position.ticker,
            "side":              position.side,
            "entry_price_cents": int(position.entry_price),
            "exit_price":        exit_price,
            "quantity":          position.quantity,
            "pnl":               round(pnl, 4),
            "spread_cost":       spread_cost,
            "true_pnl":          round(pnl, 4),
            "hold_seconds":      hold_seconds,
            "exit_reason":       exit_reason,
            "rule_id":           getattr(position, "rule_id", None),
            "agent_name":        getattr(position, "agent_name", None),
            "contract_class":    getattr(position, "contract_class", None),
            "entry_time":        entry_time_iso,
            "exit_time":         exit_time_iso,
            "order_id":          getattr(position, "order_id", None),
        }
        trigger_path = os.path.join(_TRIGGERS_DIR, filename)
        with open(trigger_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        logger.debug("[OutcomeReporter] Trigger written: %s", trigger_path)

    # -----------------------------------------------------------------------
    # Discord (runs in daemon thread via _fire)
    # -----------------------------------------------------------------------

    def _post_discord_exit(self, position, exit_price: float, exit_reason: str,
                           pnl: float, hold_seconds: float, pfx: str):
        try:
            import notifications.discord as discord
        except ImportError:
            logger.warning(
                "%s[OutcomeReporter] Discord import failed — skipping notification.", pfx
            )
            return
        try:
            discord.post_exit(
                position.ticker,
                position.side,
                position.quantity,
                int(position.entry_price),
                exit_price,
                pnl,
                hold_seconds,
                exit_reason,
            )
        except Exception as e:
            logger.warning("%s[OutcomeReporter] Discord post failed: %s", pfx, e)

    def _post_telegram_exit(self, position, exit_price: float, exit_reason: str,
                            pnl: float, hold_seconds: float, pfx: str):
        try:
            import notifications.telegram as telegram
        except ImportError:
            return
        try:
            entry_dollars = int(position.entry_price) / 100.0
            pnl_sign = "+" if pnl >= 0 else ""
            hold_str = f"{int(hold_seconds // 60)}m {int(hold_seconds % 60)}s"
            outcome = "WIN" if pnl >= 0 else "LOSS"
            agent = getattr(position, "agent_name", None) or "unknown"
            cls   = getattr(position, "contract_class", None) or ""
            emoji = "✅" if pnl >= 0 else "❌"
            telegram.post(
                f"{pfx}EXIT | {outcome} | {pnl_sign}${pnl:.2f} | {position.ticker}\n"
                f"{position.side.upper()} {position.quantity}x | "
                f"Entry: ${entry_dollars:.2f} → Exit: ${exit_price:.2f}\n"
                f"Hold: {hold_str} | Agent: {agent} | {cls}\n"
                f"Reason: {exit_reason}",
                emoji,
            )
        except Exception as e:
            logger.warning("%s[OutcomeReporter] Telegram post failed: %s", pfx, e)

    # -----------------------------------------------------------------------
    # Query helpers
    # -----------------------------------------------------------------------

    def get_recent_trades(self, n: int = 50) -> list[dict]:
        """Return last N closed trades as dicts, newest first."""
        with self._db_lock:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT * FROM syndicate_trades
                 ORDER BY id DESC
                 LIMIT ?
            """, (n,)).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    def get_today_stats(self) -> dict:
        """
        Return today's trade stats (CT = UTC-6).
        Keys: trades, wins, losses, total_pnl, win_rate, best_trade, worst_trade
        """
        ct_now    = datetime.now(timezone.utc) - timedelta(hours=6)
        today_str = ct_now.strftime("%Y-%m-%d")

        with self._db_lock:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT pnl FROM syndicate_trades
                 WHERE date(exit_time) = ?
            """, (today_str,)).fetchall()
            conn.close()

        pnls   = [r["pnl"] for r in rows if r["pnl"] is not None]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        return {
            "trades":      len(pnls),
            "wins":        len(wins),
            "losses":      len(losses),
            "total_pnl":   round(sum(pnls), 4),
            "win_rate":    round((len(wins) / len(pnls) * 100), 1) if pnls else 0.0,
            "best_trade":  max(pnls) if pnls else 0.0,
            "worst_trade": min(pnls) if pnls else 0.0,
        }

    def get_agent_stats(self, agent_name: str, n: int = 100) -> dict:
        """
        Return stats for the last N trades by a specific agent.
        Keys: agent_name, trades, wins, losses, win_rate, total_pnl,
              avg_pnl, avg_hold_seconds
        """
        with self._db_lock:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT pnl, hold_seconds FROM syndicate_trades
                 WHERE agent_name = ?
                 ORDER BY id DESC
                 LIMIT ?
            """, (agent_name, n)).fetchall()
            conn.close()

        pnls   = [r["pnl"] for r in rows if r["pnl"] is not None]
        holds  = [r["hold_seconds"] for r in rows if r["hold_seconds"] is not None]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        count  = len(pnls)
        return {
            "agent_name":       agent_name,
            "trades":           count,
            "wins":             len(wins),
            "losses":           len(losses),
            "win_rate":         round((len(wins) / count * 100), 1) if count else 0.0,
            "total_pnl":        round(sum(pnls), 4),
            "avg_pnl":          round(sum(pnls) / count, 4) if count else 0.0,
            "avg_hold_seconds": round(sum(holds) / len(holds), 1) if holds else 0.0,
        }

    def get_class_stats(self) -> dict:
        """
        Return performance breakdown by contract_class.
        Keys: {SCALP: {trades, wins, win_rate, total_pnl}, SWING: {...}, ...}
        """
        with self._db_lock:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT contract_class, pnl FROM syndicate_trades
                 WHERE contract_class IS NOT NULL
            """).fetchall()
            conn.close()

        buckets: dict[str, list[float]] = {}
        for r in rows:
            cls = r["contract_class"]
            if r["pnl"] is not None:
                buckets.setdefault(cls, []).append(r["pnl"])

        result = {}
        for cls, pnls in buckets.items():
            wins = [p for p in pnls if p > 0]
            result[cls] = {
                "trades":    len(pnls),
                "wins":      len(wins),
                "win_rate":  round((len(wins) / len(pnls) * 100), 1) if pnls else 0.0,
                "total_pnl": round(sum(pnls), 4),
            }
        return result

    def get_db_path(self) -> str:
        """Return absolute path to the SQLite database."""
        return _DB_PATH


# ---------------------------------------------------------------------------
# Module singleton — `from core.outcome_reporter import outcome_reporter`
# ---------------------------------------------------------------------------

outcome_reporter = OutcomeReporter()
