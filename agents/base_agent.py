"""
base_agent.py — Abstract base class for all Syndicate trading agents.

Agents detect trade signals and write trigger files to triggers/.
wake_syndicate.ps1 watches triggers/ and wakes TC (Claude CLI).
TC writes {name}_decision.json. main.py reads it and calls order_manager.place_order().

Agents do NOT import main.py, scan_engine.py, or any agent file.
"""

import os
import sys
import json
import time
import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Root path injection — agents/ lives one level below syndicate root
# ---------------------------------------------------------------------------

_AGENTS_DIR      = os.path.dirname(os.path.abspath(__file__))
_SYNDICATE_ROOT  = os.path.dirname(_AGENTS_DIR)

if _SYNDICATE_ROOT not in sys.path:
    sys.path.insert(0, _SYNDICATE_ROOT)

# ---------------------------------------------------------------------------
# Lazy outcome_reporter import (avoids circular issues at import time)
# ---------------------------------------------------------------------------

def _get_outcome_reporter():
    from core.outcome_reporter import outcome_reporter  # noqa: PLC0415
    return outcome_reporter

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger("syndicate.agents")

# ---------------------------------------------------------------------------
# Base-level price gate — applied to all agents unless overridden
# ---------------------------------------------------------------------------

MIN_EDGE_PCT: float = 7.0    # Minimum edge % required in build_signal

EVAL_COOLDOWN_SECONDS: float = 1800.0  # Default: 30 min between re-evaluations per ticker

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_MEMORY_DIR   = os.path.join(_SYNDICATE_ROOT, "memory")
_TRIGGERS_DIR = os.path.join(_SYNDICATE_ROOT, "triggers")

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _tier_to_conviction(tier: str) -> int:
    """Map conviction tier string to integer 1-5."""
    _MAP = {
        "GLITCH":           2,
        "HIGH_CONVICTION":  3,
        "PROPHECY":         5,
    }
    return _MAP.get(tier, 1)


def _game_to_dict(game) -> dict:
    """
    Safely serialise a TennisGame dataclass to a plain dict.
    Returns {} if game is None or serialisation fails.
    Fields: match_id, player1, player2, score_raw, true_probability,
            is_match_point, serving.
    """
    if game is None:
        return {}
    try:
        return {
            "match_id":         getattr(game, "match_id", None),
            "player1":          getattr(game, "player1", None),
            "player2":          getattr(game, "player2", None),
            "score_raw":        getattr(game, "score_raw", None),
            "true_probability": getattr(game, "true_probability", None),
            "is_match_point":   getattr(game, "is_match_point", None),
            "serving":          getattr(game, "serving", None),
        }
    except Exception as e:
        logger.warning("[BaseAgent] _game_to_dict failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------

class BaseAgent(ABC):
    """
    Abstract base for all Syndicate agents.

    Subclasses must:
      - Override class attributes: name, domain, seed_rules
      - Implement should_evaluate(market, game=None) — must call
        self._base_should_evaluate(market) first and return False if it returns False
      - Implement evaluate(market, game=None) — called in daemon thread
    """

    # ── Class-level defaults (override in subclass) ──────────────────────────
    name:       str       = "BASE"
    domain:     str       = "all"
    seed_rules: list[str] = []

    # Set True in subclasses that trade low-price contracts (e.g. GHOST)
    _skip_base_price_gate: bool = False

    # Seconds between re-evaluations of the same ticker by this agent.
    # Override to 300 in fast-signal agents (BLITZ, TIDE).
    EVAL_COOLDOWN_SECONDS: float = 1800.0

    # Max signals emitted per heartbeat cycle. None = unlimited.
    # Concurrent evaluate() threads buffer here; top N by edge_pct are flushed
    # after a short collection window. DELTA=3, SHADOW=2.
    MAX_SIGNALS_PER_CYCLE: Optional[int] = None

    # ── Memory lock (one per instance) ───────────────────────────────────────
    # _benched_cache is a plain bool — GIL makes single bool read atomic.
    # _bench_check_ts tracks monotonic time of last bench recheck.

    def __init__(self):
        self._memory_lock: threading.Lock = threading.Lock()
        self._benched_cache: bool = False
        self._bench_check_ts: float = 0.0  # monotonic time of last bench recheck
        self._eval_cooldowns: dict = {}     # cooldown_key → last eval timestamp

        # Signal cap: buffer + flush timer (only used when MAX_SIGNALS_PER_CYCLE set)
        self._cycle_buffer: list = []
        self._cycle_lock: threading.Lock = threading.Lock()
        self._cycle_flush_timer: Optional[threading.Timer] = None

        # Ensure directories exist
        os.makedirs(_MEMORY_DIR, exist_ok=True)
        os.makedirs(_TRIGGERS_DIR, exist_ok=True)

        # Bootstrap _benched_cache from disk at startup (one-time I/O is fine)
        try:
            mem = self.load_memory()
            self._benched_cache = mem.get("benched", False)
        except Exception as e:
            logger.warning("[%s] init load_memory failed: %s", self.name, e)
            self._benched_cache = False

        logger.info("[%s] initialised (domain=%s, benched=%s)",
                    self.name, self.domain, self._benched_cache)

    # =========================================================================
    # Memory
    # =========================================================================

    def _memory_path(self) -> str:
        return os.path.join(_MEMORY_DIR, f"{self.name}.json")

    def _default_memory(self) -> dict:
        return {
            "name":        self.name,
            "rules":       list(self.seed_rules),
            "lessons":     [],
            "performance": {
                "trades":    0,
                "wins":      0,
                "losses":    0,
                "total_pnl": 0.0,
            },
            "loss_streak":   0,
            "benched":       False,
            "benched_until": None,
        }

    def load_memory(self) -> dict:
        """
        Read memory/{name}.json. Thread-safe.
        Returns default dict if file missing or corrupt.
        """
        path = self._memory_path()
        with self._memory_lock:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Basic sanity check — must be a dict
                if not isinstance(data, dict):
                    raise ValueError("memory file is not a JSON object")
                return data
            except FileNotFoundError:
                return self._default_memory()
            except Exception as e:
                logger.warning("[%s] load_memory corrupt/invalid (%s) — using default", self.name, e)
                return self._default_memory()

    def save_memory(self, memory: dict) -> None:
        """
        Atomic write: write to .tmp then os.replace. Thread-safe.
        Also updates self._benched_cache.
        """
        path = self._memory_path()
        tmp_path = path + ".tmp"
        with self._memory_lock:
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(memory, f, indent=2)
                os.replace(tmp_path, path)
            except Exception as e:
                logger.error("[%s] save_memory failed: %s", self.name, e)
                # Clean up tmp if it exists
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        # Update cache AFTER releasing lock — plain bool assignment is atomic
        self._benched_cache = memory.get("benched", False)

    # =========================================================================
    # Hot path — should_evaluate
    # =========================================================================

    @abstractmethod
    def should_evaluate(self, market, game=None) -> bool:
        """
        Subclasses must implement this.
        Must call self._base_should_evaluate(market) first and return False if it returns False.
        No file I/O allowed here.
        """
        ...

    def _base_should_evaluate(self, market) -> bool:
        """
        Shared pre-checks for the hot path. NEVER does file I/O except the
        once-per-60s bench recheck.

        1. If _benched_cache is True → check if bench period expired (at most
           once per 60s via _bench_check_ts). If expired, call is_benched()
           which auto-unbenches. Re-read _benched_cache.
        2. market.contract_class not in (SCALP, SWING, POSITION) → False
        3. market.volume_dollars <= 0 → False
        4. return True
        """
        # ── Bench check ──────────────────────────────────────────────────────
        if self._benched_cache:
            now_mono = time.monotonic()
            if now_mono - self._bench_check_ts >= 60.0:
                self._bench_check_ts = now_mono
                # is_benched() reads memory and may auto-unbench
                still_benched = self.is_benched()
                # _benched_cache updated inside is_benched() via save_memory
                if still_benched:
                    return False
                # Fell through — bench expired, _benched_cache now False
            else:
                # Within 60s window — trust cache, skip I/O
                return False

        # ── Market pre-filters ───────────────────────────────────────────────
        # Allow SCALP, SWING, and POSITION. Block WATCH (no buy) and any
        # unknown class. POSITION contracts from today's matches (where Kalshi
        # sets expiry to tournament end) must be allowed through here.
        if market.contract_class not in ("SCALP", "SWING", "POSITION"):
            return False

        if market.volume_dollars <= 0:
            return False

        # ── Hard price gate ──────────────────────────────────────────────────
        # Sweet spot: YES must be priced 25¢–75¢.
        # Above 75¢ → YES too expensive, no edge.
        # Below 25¢ → buying NO costs >75¢ (= 1 - yes_price), no edge.
        # Agents that intentionally trade outside this range (e.g. GHOST)
        # set _skip_base_price_gate = True.
        if not self._skip_base_price_gate:
            if market.yes_price > 0.75 or market.yes_price < 0.25:
                return False

        # ── Re-entry lockout ─────────────────────────────────────────────────
        # Block re-entry on any ticker that was autonomously exited in the
        # last 30 minutes. Prevents buy-sell churn after pct exits.
        from core.shared_state import state as _state  # noqa: PLC0415
        lockout_ts = _state.exit_lockouts.get(market.ticker, 0.0)
        if time.time() - lockout_ts < 1800.0:  # 30-minute cooldown
            return False

        # ── Per-ticker cooldown ───────────────────────────────────────────────
        # Don't re-evaluate the same ticker within EVAL_COOLDOWN_SECONDS.
        cooldown_key = f"{self.name}:{market.ticker}"
        last_eval = self._eval_cooldowns.get(cooldown_key, 0)
        if time.time() - last_eval < self.EVAL_COOLDOWN_SECONDS:
            return False
        self._eval_cooldowns[cooldown_key] = time.time()

        return True

    # =========================================================================
    # evaluate — implemented by subclasses
    # =========================================================================

    @abstractmethod
    def evaluate(self, market, game=None) -> None:
        """
        Called in daemon thread when should_evaluate() returns True.
        Compute edge; call submit_signal() if strong enough.
        """
        ...

    # =========================================================================
    # Signal
    # =========================================================================

    def build_signal(
        self,
        market,
        conviction_tier: str,
        edge_pct: float,
        side: str,
        entry_price: float,
        target_price: float,
        stop_price: float,
        reasoning: str = "",
        game=None,
    ) -> dict:
        """
        Assemble and return a signal dict ready to write as a trigger file.

        conviction_tier: "GLITCH" | "HIGH_CONVICTION" | "PROPHECY" (or other → tier 1)
        expires_at: UTC ISO8601, 5 minutes from now.
        max_size_dollars: derived from conviction tier via get_bet_size().
        """
        # ── Edge floor — backstop against low-confidence signals ────────────
        if edge_pct < MIN_EDGE_PCT:
            logger.debug(
                "[%s] build_signal rejected | edge=%.1f%% < MIN_EDGE=%.1f%%",
                self.name, edge_pct, MIN_EDGE_PCT,
            )
            return None

        conviction_int   = _tier_to_conviction(conviction_tier)
        max_size_dollars = int(self.get_bet_size(conviction_int))

        mem            = self.load_memory()
        memory_rules   = mem.get("rules", [])
        recent_trades  = self._get_recent_trades(n=10)

        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # YES side: profits if price settles ABOVE threshold ($1 payout)
        # NO side:  profits if price settles BELOW threshold ($1 payout)
        # YES ask  ≈ 1.0 - market.no_bid (price to buy YES contracts)
        # NO price ≈ 1.0 - market.yes_price (price to buy NO contracts)
        yes_ask_est    = round(1.0 - market.no_bid, 4)
        no_price_est   = round(1.0 - market.yes_price, 4)

        if side.lower() == "yes":
            contract_cost  = entry_price  # cost per contract buying YES
            side_expl      = (
                f"YES: profits if price ABOVE strike "
                f"(entry={entry_price:.2f}, yes_ask≈{yes_ask_est:.2f})"
            )
        else:
            contract_cost  = round(1.0 - entry_price, 4) if entry_price > 0 else no_price_est
            side_expl      = (
                f"NO: profits if price BELOW strike "
                f"(NO costs≈{no_price_est:.2f}, YES currently at {market.yes_price:.2f})"
            )

        return {
            "agent": self.name,
            "signal": {
                "ticker":                    market.ticker,
                "contract_class":            market.contract_class,
                "conviction_tier":           conviction_tier,
                "edge_pct":                  edge_pct,
                "side":                      side,
                "entry_price":               entry_price,
                "yes_ask":                   yes_ask_est,
                "no_price":                  no_price_est,
                "contract_cost":             contract_cost,
                "contract_side_explanation": side_expl,
                "spread_cents":              round(market.spread * 100, 1),
                "target_price":              target_price,
                "stop_price":                stop_price,
                "max_size_dollars":          max_size_dollars,
                "agent_name":                self.name,
                "reasoning":                 reasoning,
                # ── Trading philosophy — rinse-and-repeat, never hold to settlement ──
                "target_exit_pct":   0.20,   # Exit at +20% gain
                "stop_loss_pct":     0.30,   # Exit at -30% loss
                "max_hold_minutes":  60,     # Never hold more than 60 min
                "hold_to_settlement": False, # NEVER hold to settlement
            },
            "market": {
                "ticker":             market.ticker,
                "yes_price":          market.yes_price,
                "volume_dollars":     market.volume_dollars,
                "spread":             market.spread,
                "days_to_settlement": market.days_to_settlement,
                "series_ticker":      market.series_ticker,
            },
            "game_state":    _game_to_dict(game),
            "memory_rules":  memory_rules,
            "recent_trades": recent_trades,
            "expires_at":    expires_at,
        }

    def submit_signal(self, signal: dict) -> bool:
        """
        If MAX_SIGNALS_PER_CYCLE is set, buffer the signal and schedule a flush
        (top N by edge_pct written after _CYCLE_FLUSH_DELAY seconds).
        Otherwise write immediately.
        Returns True on success / buffered, False on None or write failure.
        """
        if signal is None:
            return False

        if self.MAX_SIGNALS_PER_CYCLE is not None:
            with self._cycle_lock:
                self._cycle_buffer.append(signal)
                # Reset flush timer so all concurrent evals land before we flush
                if self._cycle_flush_timer is not None:
                    self._cycle_flush_timer.cancel()
                timer = threading.Timer(2.0, self._flush_cycle_buffer)
                timer.daemon = True
                self._cycle_flush_timer = timer
                timer.start()
            return True  # buffered — will be written by _flush_cycle_buffer

        return self._write_signal(signal)

    def _flush_cycle_buffer(self) -> None:
        """
        Drain _cycle_buffer, sort by edge_pct descending, write top MAX_SIGNALS_PER_CYCLE.
        Called from a daemon Timer thread.
        """
        with self._cycle_lock:
            candidates = self._cycle_buffer[:]
            self._cycle_buffer.clear()
            self._cycle_flush_timer = None

        if not candidates:
            return

        candidates.sort(
            key=lambda s: s.get("signal", {}).get("edge_pct", 0.0),
            reverse=True,
        )
        cap     = self.MAX_SIGNALS_PER_CYCLE or len(candidates)
        top     = candidates[:cap]
        dropped = len(candidates) - len(top)

        if dropped:
            logger.info(
                "[%s] Signal cap %d/%d — dropping %d low-edge signals",
                self.name, len(top), len(candidates), dropped,
            )

        for sig in top:
            self._write_signal(sig)

    def _write_signal(self, signal: dict) -> bool:
        """
        Write triggers/{name.lower()}_signal.json atomically and send notifications.
        """
        filename  = f"{self.name.lower()}_signal.json"
        path      = os.path.join(_TRIGGERS_DIR, filename)
        tmp_path  = path + ".tmp"

        sig = signal.get("signal", {})
        ticker          = sig.get("ticker", "?")
        conviction_tier = sig.get("conviction_tier", "?")
        edge_pct        = sig.get("edge_pct", 0.0)

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(signal, f, indent=2)
            os.replace(tmp_path, path)
            logger.info(
                "[%s] signal submitted | ticker=%s conviction=%s edge=%.2f%%",
                self.name, ticker, conviction_tier, edge_pct,
            )
            try:
                from notifications.discord import post as _discord_post
                _discord_post(
                    f"Signal: {self.name} {ticker} {signal.get('signal', {}).get('side', '?')} edge={edge_pct:.1f}%"
                )
            except Exception:
                pass
            try:
                from notifications.telegram import post as _tg_post
                _tg_post(
                    f"Signal: {self.name} {ticker} {signal.get('signal', {}).get('side', '?')}"
                )
            except Exception:
                pass
            return True
        except Exception as e:
            logger.error("[%s] _write_signal failed: %s", self.name, e)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False

    # =========================================================================
    # Exit review — called by scalper every 30s on open positions
    # =========================================================================

    # =========================================================================
    # Settlement hold check
    # =========================================================================

    def _settlement_hold_check(self, position, market) -> tuple:
        """
        Returns (should_exit: bool, reason: str).
        HARD RULE: Never hold to settlement unless WINNING with high confidence.
        """
        if market is None:
            return False, "no market data"

        minutes_remaining = market.days_to_settlement * 1440.0
        entry_dollars = position.entry_price / 100.0

        if position.side == "yes":
            current_price  = market.yes_price
            unrealized_pnl = (current_price - entry_dollars) * position.quantity
        else:
            no_entry       = (100 - position.entry_price) / 100.0
            no_current     = 1.0 - market.yes_price
            unrealized_pnl = (no_current - no_entry) * position.quantity

        entry_cost = entry_dollars * position.quantity
        pnl_pct    = unrealized_pnl / entry_cost if entry_cost > 0 else 0.0

        if minutes_remaining > 30:
            return False, "plenty of time"

        if minutes_remaining < 30 and pnl_pct < 0:
            return True, "losing with < 30 min — exit"

        if minutes_remaining < 15:
            if position.side == "yes" and market.yes_price > 0.75:
                return False, "YES clearly winning — hold"
            elif position.side == "no" and market.yes_price < 0.25:
                return False, "NO clearly winning — hold"
            else:
                return True, "not clearly winning with < 15 min — exit before settlement"

        return False, "monitoring"

    def _calc_pnl_pct(self, position, market) -> float:
        """Calculate unrealized P&L as a fraction of entry cost."""
        if market is None:
            return 0.0
        entry_dollars = position.entry_price / 100.0
        entry_cost = entry_dollars * position.quantity
        if entry_cost <= 0:
            return 0.0
        if position.side == "yes":
            unrealized_pnl = (market.yes_price - entry_dollars) * position.quantity
        else:
            no_entry       = (100 - position.entry_price) / 100.0
            no_current     = 1.0 - market.yes_price
            unrealized_pnl = (no_current - no_entry) * position.quantity
        return unrealized_pnl / entry_cost

    def _get_minutes_to_settlement(self, market) -> float:
        if market is None:
            return 9999.0
        return market.days_to_settlement * 1440.0

    def _is_first_half(self, game) -> bool:
        """Returns True if the game is in its first set/half (normal volatility — don't stop early)."""
        if game is None:
            return False
        try:
            set_scores     = getattr(game, "set_scores", None) or []
            completed_sets = len(set_scores) - 1 if set_scores else 0
            return completed_sets == 0
        except Exception:
            return False

    # =========================================================================
    # should_exit — fast pre-check for every open position (called every 30s)
    # =========================================================================

    def should_exit(self, position, market, game=None) -> bool:
        """
        Fast pre-check: should TC be woken for an exit decision?
        No file I/O. Returns True only when a threshold is crossed.

        Thresholds (OR conditions — any triggers a TC review):
          - Settlement < 5 min: always flag
          - Settlement < 10 min AND P&L < 0: losing near settlement
          - P&L >= +25%: TC decides whether to take profit
          - P&L <= -25% AND held > 10 min: TC decides stop
          - Settlement hold check triggers
        First-half losses below -40% suppressed (normal tennis volatility).
        """
        pnl_pct        = self._calc_pnl_pct(position, market)
        minutes_to_s   = self._get_minutes_to_settlement(market)
        minutes_held   = (time.time() - position.entry_time) / 60.0

        # Near settlement — always flag
        if minutes_to_s < 5:
            return True

        # Losing near settlement — must decide
        if minutes_to_s < 10 and pnl_pct < 0:
            return True

        # Profit target: up 20% — TC decides whether to take profits
        if pnl_pct >= 0.20:
            return True

        # Stop loss: down 30% after 10 min held
        if pnl_pct <= -0.30 and minutes_held > 10:
            return True

        # Settlement hold check
        should, _ = self._settlement_hold_check(position, market)
        if should:
            return True

        # NEVER flag first-half/set losses — normal volatility
        if game and self._is_first_half(game) and pnl_pct > -0.40:
            return False

        return False

    def build_exit_signal(self, position, market, game=None) -> dict:
        """Build the context dict written to {name}_exit.json for TC review."""
        import time
        entry_dollars = position.entry_price / 100.0
        current_price = market.yes_price if market else entry_dollars
        hold_seconds  = time.time() - position.entry_time

        if position.side == "yes":
            unrealized_pnl = (current_price - entry_dollars) * position.quantity
        else:
            no_entry   = (100 - position.entry_price) / 100.0
            no_current = 1.0 - current_price
            unrealized_pnl = (no_current - no_entry) * position.quantity

        mem = self.load_memory()

        def _game_summary(g) -> str:
            if g is None:
                return "no live game data"
            return str(g)

        return {
            "agent":     self.name,
            "type":      "exit_review",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=3)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "position": {
                "ticker":           position.ticker,
                "side":             position.side,
                "quantity":         position.quantity,
                "entry_price_cents": int(position.entry_price),
                "entry_price_dollars": entry_dollars,
                "hold_minutes":     round(hold_seconds / 60, 1),
            },
            "market": {
                "current_price":       round(current_price, 4),
                "unrealized_pnl":      round(unrealized_pnl, 4),
                "days_to_settlement":  market.days_to_settlement if market else 0.0,
                "minutes_to_settlement": round(
                    (market.days_to_settlement * 1440) if market else 0.0, 1
                ),
            },
            "game_state":      _game_summary(game),
            "memory_rules":    mem.get("rules", []),
        }

    # =========================================================================
    # Outcome
    # =========================================================================

    def on_outcome(self, outcome: dict) -> None:
        """
        Called by outcome_reporter registry after a trade closes.

        outcome dict must have at minimum:
          pnl (float), ticker (str), exit_reason (str)

        Steps:
          1. Load memory
          2. Update performance stats
          3. If loss_streak >= 5: bench for 4h
          4. Save memory (updates _benched_cache)
          5. Write triggers/{name.lower()}_postmortem.json atomically
        """
        pnl         = float(outcome.get("pnl", 0.0))
        ticker      = outcome.get("ticker", "?")
        exit_reason = outcome.get("exit_reason", "unknown")

        mem = self.load_memory()

        # ── Update performance stats ─────────────────────────────────────────
        perf = mem.setdefault("performance", {
            "trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0,
        })
        perf["trades"]    = perf.get("trades", 0) + 1
        perf["total_pnl"] = round(perf.get("total_pnl", 0.0) + pnl, 4)

        if pnl > 0:
            perf["wins"]         = perf.get("wins", 0) + 1
            mem["loss_streak"]   = 0
        else:
            perf["losses"]       = perf.get("losses", 0) + 1
            mem["loss_streak"]   = mem.get("loss_streak", 0) + 1

        loss_streak = mem["loss_streak"]

        # ── Auto-bench on 5 consecutive losses ───────────────────────────────
        if loss_streak >= 5 and not mem.get("benched", False):
            bench_until = (
                datetime.now(timezone.utc) + timedelta(hours=4)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            mem["benched"]       = True
            mem["benched_until"] = bench_until
            logger.warning(
                "[%s] BENCHED — loss_streak=%d, bench_until=%s",
                self.name, loss_streak, bench_until,
            )

        # ── Save memory (also updates _benched_cache) ────────────────────────
        self.save_memory(mem)

        logger.info(
            "[%s] outcome recorded | ticker=%s pnl=%.4f exit_reason=%s "
            "loss_streak=%d trades=%d",
            self.name, ticker, pnl, exit_reason, loss_streak, perf["trades"],
        )

        # ── Write postmortem trigger ─────────────────────────────────────────
        self._write_postmortem(outcome, mem, loss_streak)

    def _write_postmortem(self, outcome: dict, mem: dict, loss_streak: int) -> None:
        filename = f"{self.name.lower()}_postmortem.json"
        path     = os.path.join(_TRIGGERS_DIR, filename)
        tmp_path = path + ".tmp"

        payload = {
            "agent":        self.name,
            "outcome":      outcome,
            "memory_rules": mem.get("rules", []),
            "performance":  mem.get("performance", {}),
            "loss_streak":  loss_streak,
            "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, path)
            logger.debug("[%s] postmortem written: %s", self.name, path)
        except Exception as e:
            logger.error("[%s] _write_postmortem failed: %s", self.name, e)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _get_recent_trades(self, n: int = 10) -> list:
        """
        Fetch the last N closed trades by this agent from outcome_reporter.
        Lazy-imports outcome_reporter to avoid circular imports at module load.
        Returns [] on any error.
        """
        try:
            all_trades   = _get_outcome_reporter().get_recent_trades(50)
            agent_trades = [t for t in all_trades if t.get("agent_name") == self.name]
            return agent_trades[:n]
        except Exception as e:
            logger.warning("[%s] _get_recent_trades failed: %s", self.name, e)
            return []

    # =========================================================================
    # Sizing
    # =========================================================================

    def get_bet_size(self, conviction: int) -> float:
        """
        conviction int 1-5 → dollar bet size $1.00-$5.00, clamped.
        Tier 1 flat = $1.00.
        """
        clamped = max(1, min(5, conviction))
        return float(clamped)

    # =========================================================================
    # Bench helpers
    # =========================================================================

    def is_benched(self) -> bool:
        """
        Full check (reads memory file). Auto-unbenches if benched_until < now.
        If auto-unbenched: resets loss_streak=0, saves memory.
        Returns bool (True = currently benched).
        """
        mem = self.load_memory()

        if not mem.get("benched", False):
            self._benched_cache = False
            return False

        benched_until_str: Optional[str] = mem.get("benched_until")
        if benched_until_str is None:
            # Benched with no expiry — stay benched (_benched_cache already True)
            return True

        try:
            benched_until_dt = datetime.strptime(
                benched_until_str, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            # Malformed timestamp — stay benched to be safe
            logger.warning("[%s] malformed benched_until: %s", self.name, benched_until_str)
            return True

        if datetime.now(timezone.utc) >= benched_until_dt:
            # Bench period expired — auto-unbench
            mem["benched"]       = False
            mem["benched_until"] = None
            mem["loss_streak"]   = 0
            self.save_memory(mem)   # also sets _benched_cache = False via save_memory
            logger.info("[%s] bench expired — auto-unbenched", self.name)
            return False

        # Still within bench window (_benched_cache already True)
        return True
