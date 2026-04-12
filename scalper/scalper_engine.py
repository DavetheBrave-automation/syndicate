"""
scalper_engine.py — Hot-path price-tick processor for The Syndicate Scalper.

Architecture:
  - on_price_update() is called from the WebSocket feed thread on every tick.
    It is the HOT PATH: no I/O, no config reloads, no Claude calls. Target <10ms.
  - _check_time_exits() runs in a separate daemon thread every 30 seconds.
    It is the ONLY place where time-based exits are evaluated.
  - _check_hard_stop() is called at the top of on_price_update and disables all
    subsequent trading if the daily hard stop has been breached.

Thread safety:
  - state.is_pending(ticker) / state.has_position(ticker) act as the atomic
    dedup guard against double-entry on the same ticker.
  - state.add_pending() is called before place_order(); state.remove_pending()
    is called in both success and failure paths.

Dependencies injected at construction (no circular imports):
  - order_manager: scalper.order_manager module
  - rule_loader:   scalper.rule_loader module

Rule loader interface expected by this engine:
  - rule_loader.get_rules(ticker: str) -> list[dict]
      Returns the list of active rules for a single ticker.
  - rule_loader.get_all_rules() -> dict[str, list[dict]]
      Returns all active rules keyed by ticker.

Rule dict schema:
  {
      "ticker":      str,    # e.g. "KXATPMATCH-26APR10-SIN"
      "class":       str,    # always "SCALP" for this engine
      "entry_price": float,  # decimal dollars (0.0–1.0)
      "target_price": float,
      "stop_price":  float,
      "max_size":    float,  # max dollars to deploy
      "expiry":      str,    # ISO-8601 UTC — rule is voided after this
      "created_by":  str,    # agent name (e.g. "ACE")
      "reasoning":   str,    # human-readable rationale
  }
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Sys-path bootstrap — must precede local imports
# ---------------------------------------------------------------------------

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SYNDICATE_ROOT not in sys.path:
    sys.path.insert(0, _SYNDICATE_ROOT)

from core.shared_state import state
import core.exposure_manager as exposure_manager

logger = logging.getLogger("syndicate.scalper")

# ---------------------------------------------------------------------------
# Config loader — mtime-cached, same pattern as Atlas brain.py
# Loaded once at __init__, re-read only in _check_time_exits (off hot path).
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
            logger.debug("[ScalperEngine] Config reloaded from %s", cfg_path)
    except Exception as exc:
        logger.error("[ScalperEngine] Config load error: %s", exc)
    return _cfg_cache or {}


def _scalper_cfg() -> dict:
    """Return the scalper sub-section with safe defaults."""
    return _load_config().get("scalper", {})


# ---------------------------------------------------------------------------
# ScalperEngine
# ---------------------------------------------------------------------------

_MIN_VOLUME_DOLLARS = 25_000.0   # hard-coded entry guard (from liquidity config)
_TIME_EXIT_INTERVAL = 30.0       # seconds between time-exit sweeps
_DEFAULT_MAX_HOLD   = 30         # minutes — fallback if config missing


class ScalperEngine:
    """
    Processes every price tick and fires entry/exit orders based on
    pre-loaded rules from rule_loader.

    Constructed once at startup; run() starts the time-exit thread.
    """

    def __init__(self, order_manager, rule_loader):
        """
        Args:
            order_manager: scalper.order_manager module (or object exposing
                           place_order() and close_position()).
            rule_loader:   scalper.rule_loader module (or object exposing
                           get_rules() and get_all_rules()).
        """
        self._order_manager = order_manager
        self._rule_loader   = rule_loader

        # Load config once at startup — used in hot path (no reload here).
        cfg = _scalper_cfg()
        self._max_hold_minutes: int = int(cfg.get("max_hold_minutes", _DEFAULT_MAX_HOLD))

        # Kill switch — set to False when hard stop is hit; never reset during session.
        self._trading_active: bool = True

        # Time-exit thread control
        self._running: bool = False
        self._exit_thread: Optional[threading.Thread] = None

        # Agent registry: agent_name → agent instance (populated via register_agents)
        self._agent_registry: dict = {}

        logger.info(
            "[ScalperEngine] Initialized. max_hold_minutes=%d",
            self._max_hold_minutes,
        )

    # -----------------------------------------------------------------------
    # Public: lifecycle
    # -----------------------------------------------------------------------

    def run(self):
        """
        Start the time-exit checker in a daemon thread.
        Call once after construction. Non-blocking.
        """
        if self._running:
            logger.warning("[ScalperEngine] run() called but already running.")
            return

        self._running = True
        self._exit_thread = threading.Thread(
            target=self._time_exit_loop,
            name="scalper-time-exits",
            daemon=True,
        )
        self._exit_thread.start()
        logger.info("[ScalperEngine] Time-exit thread started (every %.0fs).", _TIME_EXIT_INTERVAL)

    def stop(self):
        """Signal the time-exit thread to stop. Does not join."""
        self._running = False
        logger.info("[ScalperEngine] Stop signalled.")

    # -----------------------------------------------------------------------
    # Public: hot path
    # -----------------------------------------------------------------------

    def on_price_update(self, ticker: str, new_price: float, volume_dollars: float):
        """
        Called by the WebSocket feed on every price tick.

        HOT PATH — target <10ms. No I/O, no config reloads, no Claude calls.

        Args:
            ticker:         Kalshi contract ticker.
            new_price:      Current YES price (decimal, 0.0–1.0).
            volume_dollars: Session volume in dollars for this contract.
        """
        # ── Hard stop gate ──────────────────────────────────────────────────
        if not self._trading_active:
            return

        self._check_hard_stop()
        if not self._trading_active:
            return

        # ── Fetch rules for this ticker ─────────────────────────────────────
        rules = self._rule_loader.get_rules(ticker)
        if not rules:
            return

        # ── Position check (read once, avoids repeated lock acquisitions) ───
        has_position = state.has_position(ticker)
        is_pending   = state.is_pending(ticker)

        for rule in rules:
            # ── Check exit first if position is open ───────────────────────
            if has_position:
                self._evaluate_exit(ticker, new_price, rule)
                # Only one position per ticker; no need to check entry.
                continue

            # ── Skip entry if an order is already in-flight ────────────────
            if is_pending:
                continue

            # ── Evaluate entry condition ───────────────────────────────────
            self._evaluate_entry(ticker, new_price, volume_dollars, rule)

    # -----------------------------------------------------------------------
    # Internal: entry / exit evaluation (called from hot path — no I/O)
    # -----------------------------------------------------------------------

    def _evaluate_entry(
        self,
        ticker: str,
        new_price: float,
        volume_dollars: float,
        rule: dict,
    ):
        """
        Entry condition (ALL must be true):
          1. new_price <= rule["entry_price"]
          2. volume_dollars >= 25,000
          3. No existing position on ticker (checked before this call)
          4. No order currently in-flight (checked before this call)
          5. Exposure manager allows the trade
        """
        entry_price = rule.get("entry_price")
        if entry_price is None:
            return

        if new_price > entry_price:
            return

        if volume_dollars < _MIN_VOLUME_DOLLARS:
            return

        # ── Rule expiry check ──────────────────────────────────────────────
        expiry = rule.get("expiry")
        if expiry is not None:
            try:
                exp_ts = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
                if time.time() >= exp_ts:
                    logger.debug(
                        "[ScalperEngine] Rule expired for %s (expiry=%s)", ticker, expiry
                    )
                    return
            except Exception:
                pass  # malformed expiry — don't block the trade

        # ── Exposure check ─────────────────────────────────────────────────
        # Use rule max_size directly; exposure_manager enforces per-class cap.
        max_size = float(rule.get("max_size", 0.0))
        proposed_dollars = max_size

        allowed, reason = exposure_manager.check_trade(
            ticker=ticker,
            contract_class=rule.get("class", "SCALP"),
            proposed_dollars=proposed_dollars,
        )
        if not allowed:
            logger.debug(
                "[ScalperEngine] Entry blocked by exposure manager for %s: %s",
                ticker, reason,
            )
            return

        # ── Atomic dedup guard — re-check immediately before claiming slot ──
        # Reduces TOCTOU window; has_position/is_pending were read at tick start.
        if state.has_position(ticker) or state.is_pending(ticker):
            return
        state.add_pending(ticker)

        # Compute integer quantity from dollar size and current price
        quantity = max(1, int(proposed_dollars / new_price)) if new_price > 0 else 1
        rule_id  = f"{rule.get('created_by', 'unknown')}-{ticker}"

        logger.debug(
            "[ScalperEngine] ENTRY: %s @ %.4f (rule_entry=%.4f, vol=$%.0f, qty=%d) | %s",
            ticker, new_price, entry_price, volume_dollars, quantity,
            rule.get("reasoning", ""),
        )

        try:
            self._order_manager.place_order(
                ticker=ticker,
                side="yes",
                quantity=quantity,
                price=new_price,
                rule=rule,
                rule_id=rule_id,
                agent_name=rule.get("created_by", "unknown"),
                contract_class=rule.get("class", "SCALP"),
                max_size=quantity,
            )
        except Exception as exc:
            logger.error(
                "[ScalperEngine] place_order error for %s: %s", ticker, exc, exc_info=True
            )
            # Always remove pending on failure so the ticker isn't locked out.
            state.remove_pending(ticker)

    def _should_apply_stop(self, ticker: str, position) -> bool:
        """
        Returns False when the stop loss should be suppressed:
          - Hold time < 600s (10 min warmup — don't stop within first 10 minutes)
          - Tennis market in first set (completed_sets == 0)
        """
        if time.time() - position.entry_time < 600:
            return False

        try:
            from connectors.tennis_ws import match_game_to_ticker  # noqa: PLC0415
            game = match_game_to_ticker(ticker)
            if game is not None:
                set_scores     = getattr(game, "set_scores", None) or []
                completed_sets = len(set_scores) - 1 if set_scores else 0
                if completed_sets == 0:
                    return False
        except Exception:
            pass  # connector unavailable — apply stop normally

        return True

    def _evaluate_exit(self, ticker: str, new_price: float, rule: dict):
        """
        Exit conditions (ANY triggers):
          - new_price >= rule["target_price"]  → profit target hit
          - new_price <= rule["stop_price"]    → stop loss hit (gated by _should_apply_stop)

        Time-based exits are handled by _check_time_exits(), NOT here.
        """
        position = state.get_position(ticker)
        if position is None:
            return

        target_price = rule.get("target_price")
        stop_price   = rule.get("stop_price")

        reason: Optional[str] = None

        if target_price is not None and new_price >= target_price:
            reason = (
                f"profit target hit: price={new_price:.4f} >= target={target_price:.4f}"
            )
        elif stop_price is not None and new_price <= stop_price and self._should_apply_stop(ticker, position):
            reason = (
                f"stop loss hit: price={new_price:.4f} <= stop={stop_price:.4f}"
            )

        if reason is None:
            return

        logger.debug("[ScalperEngine] EXIT: %s | %s", ticker, reason)

        try:
            self._order_manager.close_position(
                position=position,
                exit_price=new_price,
                exit_reason=reason,
            )
        except Exception as exc:
            logger.error(
                "[ScalperEngine] close_position error for %s: %s", ticker, exc, exc_info=True
            )

    # -----------------------------------------------------------------------
    # Internal: hard stop check (called at top of on_price_update)
    # -----------------------------------------------------------------------

    def _check_hard_stop(self):
        """
        Calls exposure_manager.check_hard_stop().
        If the hard stop is breached, logs CRITICAL, halts shared state trading
        flag, and permanently disables this engine's _trading_active flag so
        all subsequent on_price_update calls return immediately at the top gate.
        """
        if exposure_manager.check_hard_stop():
            logger.critical(
                "[ScalperEngine] HARD STOP BREACHED — halting all trading. "
                "daily_loss >= hard_stop_loss. No further entries will be processed."
            )
            self._trading_active = False
            state.halt_trading("ScalperEngine: hard stop breached")

    # -----------------------------------------------------------------------
    # Internal: time-exit loop (runs in daemon thread, every 30s)
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Agent registry — populated by main.py / scan_engine after agents load
    # -----------------------------------------------------------------------

    def register_agents(self, agents: list) -> None:
        """
        Register agent instances so _check_agent_exits can call should_exit().
        agents: list of BaseAgent instances (ACE, AXIOM, etc.)
        """
        self._agent_registry = {a.name: a for a in agents}
        logger.info(
            "[ScalperEngine] Agent registry populated: %s",
            list(self._agent_registry.keys()),
        )

    def _get_agent_for_position(self, position):
        """Return the agent instance that created this position, or None."""
        name = getattr(position, "agent_name", None)
        if not name:
            return None
        return self._agent_registry.get(name.upper())

    def _get_game_for_ticker(self, ticker: str):
        """Try to find a live tennis game matching this ticker. Returns None if not found."""
        try:
            from connectors.tennis_ws import match_game_to_ticker  # noqa: PLC0415
            return match_game_to_ticker(ticker)
        except Exception:
            return None

    def _write_exit_trigger(self, agent, position, market, game=None) -> None:
        """
        Write triggers/{agent_name}_exit.json.
        Uses agent.build_exit_signal() for the full context payload.
        wake_syndicate.ps1 picks it up and wakes TC.
        """
        import os, json
        _SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        triggers_dir = os.path.join(_SYNDICATE_ROOT, "triggers")
        os.makedirs(triggers_dir, exist_ok=True)

        filename = f"{agent.name.lower()}_exit.json"
        path     = os.path.join(triggers_dir, filename)
        tmp_path = path + ".tmp"

        try:
            payload = agent.build_exit_signal(position, market, game)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, path)
            logger.info(
                "[ScalperEngine] Exit trigger written: %s | ticker=%s",
                filename, position.ticker,
            )
        except Exception as exc:
            logger.error(
                "[ScalperEngine] _write_exit_trigger failed for %s: %s",
                position.ticker, exc,
            )
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _check_agent_exits(self) -> None:
        """
        Called every 30s from _time_exit_loop.
        For each open position, ask its originating agent whether TC should review an exit.
        Writes triggers/{agent_name}_exit.json when threshold is crossed.
        Skips tickers that already have a pending exit trigger.
        """
        import os
        _SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        triggers_dir = os.path.join(_SYNDICATE_ROOT, "triggers")

        positions = state.get_all_positions()
        for ticker, position in positions.items():
            # Only manage exits for positions Syndicate opened.
            # Positions created before this field existed (or externally) are skipped.
            if not getattr(position, "opened_by_syndicate", False):
                continue
            agent = self._get_agent_for_position(position)
            if agent is None:
                continue

            # Skip if an exit trigger is already pending for this agent
            pending_exit = os.path.join(
                triggers_dir, f"{agent.name.lower()}_exit.json"
            )
            if os.path.exists(pending_exit):
                continue

            market = state.get_market(ticker)
            game   = self._get_game_for_ticker(ticker)

            try:
                if agent.should_exit(position, market, game):
                    logger.info(
                        "[ScalperEngine] Agent %s flagged %s for TC exit review",
                        agent.name, ticker,
                    )
                    self._write_exit_trigger(agent, position, market, game)
            except Exception as exc:
                logger.error(
                    "[ScalperEngine] _check_agent_exits error for %s/%s: %s",
                    agent.name, ticker, exc,
                )

    def _time_exit_loop(self):
        """Daemon thread body. Calls _check_time_exits() and _check_agent_exits() every 30 seconds."""
        while self._running:
            try:
                self._check_time_exits()
            except Exception as exc:
                logger.error("[ScalperEngine] _check_time_exits error: %s", exc, exc_info=True)
            try:
                self._check_agent_exits()
            except Exception as exc:
                logger.error("[ScalperEngine] _check_agent_exits error: %s", exc, exc_info=True)
            time.sleep(_TIME_EXIT_INTERVAL)

    def _check_time_exits(self):
        """
        Scan all open SCALP positions. Close any that have exceeded
        max_hold_minutes (re-read from config on each sweep so live config
        changes take effect without restart).

        This method performs I/O (config reload) and is intentionally kept
        OFF the on_price_update hot path.
        """
        cfg = _scalper_cfg()
        max_hold_minutes = int(cfg.get("max_hold_minutes", _DEFAULT_MAX_HOLD))
        max_hold_seconds = max_hold_minutes * 60
        now = time.time()

        # get_positions_by_class acquires the lock once and returns a snapshot.
        scalp_positions = state.get_positions_by_class("SCALP")

        for ticker, position in scalp_positions.items():
            hold_seconds = now - position.entry_time
            if hold_seconds >= max_hold_seconds:
                hold_min = hold_seconds / 60.0
                reason = (
                    f"max hold time exceeded: held={hold_min:.1f}m >= "
                    f"max={max_hold_minutes}m"
                )
                logger.info("[ScalperEngine] TIME EXIT: %s | %s", ticker, reason)
                market = state.get_market(ticker)
                exit_price = market.yes_price if market else (position.entry_price / 100.0)
                try:
                    self._order_manager.close_position(
                        position=position,
                        exit_price=exit_price,
                        exit_reason=reason,
                    )
                except Exception as exc:
                    logger.error(
                        "[ScalperEngine] close_position (time exit) error for %s: %s",
                        ticker, exc, exc_info=True,
                    )

    # -----------------------------------------------------------------------
    # Internal: thin config value accessor (avoids repeated _scalper_cfg() in hot path)
    # -----------------------------------------------------------------------

    @staticmethod
    def _load_config_value(dotted_key: str, default):
        """
        Read a single dotted config key without triggering a full reload.
        e.g. 'scalper.max_hold_minutes'. Uses the module-level cache.
        Not for hot-path use — only call from off-path methods.
        """
        cfg = _load_config()
        keys = dotted_key.split(".")
        val = cfg
        for k in keys:
            if not isinstance(val, dict):
                return default
            val = val.get(k, default)
        return val
