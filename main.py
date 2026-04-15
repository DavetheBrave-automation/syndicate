"""
main.py — Entry point for The Syndicate.

Boot sequence:
  1. Logging (rotating file + console)
  2. Rule loader  — start() loads rules, launches hot-reload thread
  3. Outcome reporter — init_db() runs at import
  4. Scan engine  — start() launches 3 daemon threads (heartbeat/opportunity/strategic)
  5. Scalper engine — run() launches time-exit thread
  6. WebSocket connectors — start() launches feed threads (graceful if not installed)
  7. Intelligence gate poll thread — watches for decision.json

Shutdown:
  Ctrl-C (or SIGTERM) triggers graceful stop of all components.
"""

import os
import sys
import json
import glob
import time
import signal
import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

# ---------------------------------------------------------------------------
# Sys-path bootstrap
# ---------------------------------------------------------------------------

_SYNDICATE_ROOT = os.path.dirname(os.path.abspath(__file__))
_ATLAS_ROOT     = os.path.join(os.path.dirname(_SYNDICATE_ROOT), "atlas")

# Unconditional inserts — Python auto-adds the script dir so guards would
# skip _SYNDICATE_ROOT, leaving Atlas at index 0. Always insert to guarantee
# Syndicate shadows Atlas for any colliding module names.
sys.path.insert(0, _ATLAS_ROOT)
sys.path.insert(0, _SYNDICATE_ROOT)

# ---------------------------------------------------------------------------
# Logging setup (before any local imports that use loggers)
# ---------------------------------------------------------------------------

_LOGS_DIR = os.path.join(_SYNDICATE_ROOT, "logs")
os.makedirs(_LOGS_DIR, exist_ok=True)

_LOG_PATH = os.path.join(_LOGS_DIR, "syndicate.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[
        RotatingFileHandler(_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("syndicate.main")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        import yaml
        cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error("[Main] Config load failed: %s", e)
        return {}


def _is_paper_mode() -> bool:
    return bool(_load_config().get("syndicate", {}).get("paper_mode", True))


# ---------------------------------------------------------------------------
# PID lockfile — prevent double-start (watchdog spawns without killing old proc)
# ---------------------------------------------------------------------------

_PID_FILE = os.path.join(_SYNDICATE_ROOT, "syndicate.pid")


def _acquire_pid_lock() -> bool:
    """
    Write our PID to syndicate.pid. If a PID file already exists and that
    process is still alive, refuse to start — return False.
    """
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as f:
                old_pid = int(f.read().strip())
            # Stdlib-only liveness check: os.kill(pid, 0) raises OSError if dead
            os.kill(old_pid, 0)
            # Still alive — refuse to start
            logger.error(
                "[Main] Refusing to start — existing Syndicate process PID %d is running. "
                "Kill it first or delete syndicate.pid.",
                old_pid,
            )
            return False
        except (ValueError, ProcessLookupError, OSError):
            pass  # Stale/corrupt PID file — safe to overwrite
        except Exception:
            pass

    try:
        with open(_PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logger.warning("[Main] Could not write PID file: %s — continuing anyway.", e)
    return True


def _release_pid_lock() -> None:
    try:
        if os.path.exists(_PID_FILE):
            with open(_PID_FILE) as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(_PID_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Startup trigger cleanup
# ---------------------------------------------------------------------------

_MAX_TRIGGER_AGE_SECONDS = 300   # 5 min — transient event files older than this are stale


def _is_sweepable(fname: str) -> bool:
    """
    Only sweep transient event files. Never touch:
      - Scan/heartbeat summaries (always kept fresh)
      - *_signal.json  — agent signals waiting for TC gate (PS1 watcher reads these)
      - *_decision.json — TC decisions waiting for main.py to act on
      - pending_signal.json — panel flow signal
      - decision.json — panel flow decision
    """
    _KEEP_ALWAYS = {
        "heartbeat_latest.json", "opportunity_scan.json", "strategic_scan.json",
        "pending_signal.json", "decision.json",
    }
    if fname in _KEEP_ALWAYS:
        return False
    # Never sweep TC-facing files — PS1 watcher needs to read them
    if fname.endswith("_signal.json") or fname.endswith("_decision.json"):
        return False
    # Sweep: velocity_*, new_market_*, game_live_*, *.tmp, and unknown old files
    return True


def _cleanup_triggers() -> None:
    """
    On startup: delete transient event files older than 5 minutes.
    Never deletes signal/decision files — those belong to the TC gate.
    """
    _triggers = os.path.join(_SYNDICATE_ROOT, "triggers")
    os.makedirs(_triggers, exist_ok=True)

    now     = time.time()
    removed = 0

    for f in glob.glob(os.path.join(_triggers, "*.json")) + glob.glob(os.path.join(_triggers, "*.tmp")):
        fname = os.path.basename(f)
        if not _is_sweepable(fname):
            continue
        try:
            if now - os.path.getmtime(f) > _MAX_TRIGGER_AGE_SECONDS:
                os.remove(f)
                removed += 1
        except OSError:
            pass

    logger.info("[Startup] Triggers cleaned — %d stale files removed.", removed)


def _trigger_sweeper_loop() -> None:
    """Runtime sweeper — every 60s, purge stale transient event files."""
    _triggers = os.path.join(_SYNDICATE_ROOT, "triggers")
    while _running:
        time.sleep(60)
        if not _running:
            break
        now     = time.time()
        removed = 0
        for f in glob.glob(os.path.join(_triggers, "*.json")) + glob.glob(os.path.join(_triggers, "*.tmp")):
            fname = os.path.basename(f)
            if not _is_sweepable(fname):
                continue
            try:
                if now - os.path.getmtime(f) > _MAX_TRIGGER_AGE_SECONDS:
                    os.remove(f)
                    removed += 1
            except OSError:
                pass
        if removed:
            logger.info("[Sweeper] Purged %d stale trigger files.", removed)


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_running = True
_gate_pending: dict[str, dict] = {}   # ticker → original signal data
_gate_pending_lock = threading.Lock()

_DECISION_PATH    = os.path.join(_SYNDICATE_ROOT, "triggers", "decision.json")
_PENDING_SIG_PATH = os.path.join(_SYNDICATE_ROOT, "triggers", "pending_signal.json")

# ---------------------------------------------------------------------------
# Local imports (after sys.path is set)
# ---------------------------------------------------------------------------

from core.shared_state    import state                             # noqa: E402
from core.scan_engine     import scan_engine as _scan_engine       # noqa: E402
from scalper.rule_loader  import rule_loader                       # noqa: E402
from scalper.scalper_engine import ScalperEngine                   # noqa: E402
import scalper.order_manager as order_manager                      # noqa: E402
import core.outcome_reporter                                       # noqa: E402  (triggers init_db)

# ---------------------------------------------------------------------------
# WebSocket connector imports (graceful — connectors built in Step 11)
# ---------------------------------------------------------------------------

_kalshi_ws  = None
_tennis_ws  = None

def _load_connectors():
    global _kalshi_ws, _tennis_ws
    try:
        from connectors.kalshi_ws import KalshiWS
        cfg = _load_config()
        kalshi_cfg = cfg.get("kalshi", {})
        key_id    = kalshi_cfg.get("key_id", "")
        key_path  = kalshi_cfg.get("api_key_path", "")
        ws_url    = kalshi_cfg.get("ws_url", "wss://api.elections.kalshi.com/trade-api/ws/v2")
        if key_id and key_path:
            _kalshi_ws = KalshiWS(tickers=[], velocity_window=60.0)
            logger.info("[Main] KalshiWS connector loaded.")
        else:
            logger.warning("[Main] Kalshi key_id/key_path not configured — WebSocket disabled.")
    except ImportError:
        logger.warning("[Main] connectors.kalshi_ws not found — WebSocket disabled (paper mode only).")
    except Exception as e:
        logger.error("[Main] KalshiWS load error: %s", e)

    try:
        from connectors.tennis_ws import TennisWS
        _tennis_ws = TennisWS()
        logger.info("[Main] TennisWS connector loaded.")
    except ImportError:
        logger.warning("[Main] connectors.tennis_ws not found — tennis feed disabled.")
    except Exception as e:
        logger.error("[Main] TennisWS load error: %s", e)


# ---------------------------------------------------------------------------
# Tick dispatch — called by KalshiWS on every price update
# ---------------------------------------------------------------------------

def _on_tick(ticker: str, yes_price: float, volume_dollars: float):
    """
    Dispatch a price tick to all consumers.
    Called by KalshiWS from its daemon thread.
    """
    try:
        _scalper_engine.on_price_update(ticker, yes_price, volume_dollars)
    except Exception as e:
        logger.error("[Main] scalper_engine.on_price_update error for %s: %s", ticker, e)

    try:
        _scan_engine.on_market_update(ticker, yes_price, volume_dollars)
    except Exception as e:
        logger.error("[Main] scan_engine.on_market_update error for %s: %s", ticker, e)


def _on_game_live(match_id: str, player1: str, player2: str):
    """Dispatch a game-live event from TennisWS to scan_engine."""
    try:
        _scan_engine.on_game_live(match_id, player1, player2)
    except Exception as e:
        logger.error("[Main] scan_engine.on_game_live error: %s", e)


# ---------------------------------------------------------------------------
# Intelligence gate — submit a signal for TC review
# ---------------------------------------------------------------------------

def submit_to_gate(signal_data: dict) -> bool:
    """
    Submit a HIGH_CONVICTION or PROPHECY signal to the TC gate.

    signal_data must include:
      signal.ticker, signal.conviction_tier, signal.contract_class,
      signal.entry_price, signal.target_price, signal.stop_price,
      signal.max_size_dollars, signal.side, signal.edge_pct,
      signal.agent_name, signal.reasoning
      expires_at (ISO8601 UTC)

    Returns True if written successfully, False otherwise.
    """
    try:
        ticker = signal_data.get("signal", {}).get("ticker", "")
        if not ticker:
            logger.error("[Gate] submit_to_gate called with no ticker.")
            return False

        os.makedirs(os.path.dirname(_PENDING_SIG_PATH), exist_ok=True)
        with open(_PENDING_SIG_PATH, "w", encoding="utf-8") as f:
            json.dump(signal_data, f, indent=2)

        with _gate_pending_lock:
            _gate_pending[ticker] = signal_data

        logger.info("[Gate] Signal submitted: %s (%s)",
                    ticker, signal_data.get("signal", {}).get("conviction_tier", ""))
        return True
    except Exception as e:
        logger.error("[Gate] submit_to_gate failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Intelligence gate poll loop
# ---------------------------------------------------------------------------

def _gate_poll_loop():
    """
    Polls triggers/ for decision files every second.

    Two file patterns:
      - decision.json          — panel flow (parse_decision.py output)
      - {name}_decision.json   — agent flow (wake_syndicate.ps1 output)

    Runs as a daemon thread.
    """
    logger.info("[Gate] Poll thread started.")
    _triggers = os.path.join(_SYNDICATE_ROOT, "triggers")
    while _running:
        try:
            # Panel flow
            if os.path.exists(_DECISION_PATH):
                _process_decision()
            # Agent decision files — scan once per cycle
            for fname in os.listdir(_triggers):
                if fname.endswith("_decision.json") and fname != "decision.json":
                    _process_agent_decision(os.path.join(_triggers, fname))
        except Exception as e:
            logger.error("[Gate] Poll error: %s", e)
        time.sleep(1.0)
    logger.info("[Gate] Poll thread exiting.")


def _process_decision():
    """Read decision.json and act on the verdict."""
    try:
        with open(_DECISION_PATH, encoding="utf-8") as f:
            decision = json.load(f)
        os.remove(_DECISION_PATH)
    except Exception as e:
        logger.error("[Gate] Could not read/delete decision.json: %s", e)
        return

    ticker   = decision.get("ticker", "UNKNOWN")
    verdict  = decision.get("decision", "BLOCK")
    size     = int(decision.get("size", 0))
    edge_pct = float(decision.get("edge_pct", 0.0))

    logger.info(
        "[Gate] Decision received: ticker=%s verdict=%s size=$%d edge=%.1f%%",
        ticker, verdict, size, edge_pct,
    )

    if verdict in ("EXECUTE", "REDUCE"):
        _act_on_decision(ticker, verdict, size, decision)
    elif verdict == "DELAY":
        logger.info("[Gate] %s — DELAY: signal not re-queued in Phase 1.", ticker)
    else:  # BLOCK
        logger.info("[Gate] %s — BLOCK: discarding signal.", ticker)

    # Clear from pending queue
    with _gate_pending_lock:
        _gate_pending.pop(ticker, None)


def _process_agent_decision(path: str) -> None:
    """
    Read and act on an agent decision file ({name}_decision.json).

    TC responds with:
      { "decision": "BUY"|"PASS", "side": "yes"|"no", "bet_size": float,
        "entry_price": float, "target_exit_price": float, "reasoning": str, ...
        "ticker": str (injected by wake_syndicate.ps1) }

    Maps to _act_on_decision vocabulary:
      BUY  → EXECUTE, bet_size → size, target_exit_price → target_price
    """
    try:
        with open(path, encoding="utf-8-sig") as f:  # utf-8-sig handles BOM from PowerShell
            decision = json.load(f)
        os.remove(path)
    except Exception as e:
        logger.error("[Gate] Could not read/delete agent decision %s: %s", path, e)
        return

    # Normalise keys from TC response format to internal format
    tc_verdict = decision.get("decision", "PASS").upper()
    verdict    = "EXECUTE" if tc_verdict == "BUY" else tc_verdict  # BUY → EXECUTE

    # bet_size from TC → size expected by _act_on_decision
    if "size" not in decision:
        decision["size"] = decision.get("bet_size", 2)

    # target_exit_price → target_price
    if "target_price" not in decision:
        decision["target_price"] = decision.get("target_exit_price")

    # agent_name from ticker file name if not present
    if "agent_name" not in decision:
        fname = os.path.basename(path)
        decision["agent_name"] = fname.replace("_decision.json", "").upper()

    ticker   = decision.get("ticker", "UNKNOWN")
    size     = int(decision.get("size", 0) or 0)
    edge_pct = float(decision.get("edge_pct", decision.get("conviction", 0)) or 0)

    logger.info(
        "[Gate] Agent decision: ticker=%s verdict=%s size=$%d",
        ticker, verdict, size,
    )

    if verdict == "EXECUTE":
        _act_on_decision(ticker, verdict, size, decision)
    else:
        logger.info("[Gate] %s — %s: discarding agent signal.", ticker, verdict)

    with _gate_pending_lock:
        _gate_pending.pop(ticker, None)


def _act_on_decision(ticker: str, verdict: str, size_dollars: int, decision: dict):
    """
    Execute an order based on a TC gate decision.
    Reads order params from decision doc (populated by parse_decision.py).
    """
    if state.has_position(ticker) or state.is_pending(ticker):
        logger.info("[Gate] %s already has position/pending — skipping gate order.", ticker)
        return

    entry_price_raw = decision.get("entry_price")
    target_price    = decision.get("target_price")
    stop_price      = decision.get("stop_price")
    side            = decision.get("side", "yes")
    agent_name      = decision.get("agent_name", "TC_GATE")
    contract_class  = decision.get("contract_class", "SCALP")
    reasoning       = decision.get("reasoning", "TC gate approved")

    # Get current market price — prefer live data over signal entry_price
    market = state.get_market(ticker)
    if market:
        current_price = market.yes_price
    elif entry_price_raw is not None:
        current_price = float(entry_price_raw)
    else:
        logger.warning("[Gate] No market data for %s and no entry_price — cannot place order.", ticker)
        return

    # Build a minimal rule dict for order_manager
    rule = {
        "ticker":       ticker,
        "class":        contract_class,
        "entry_price":  current_price,
        "target_price": float(target_price) if target_price is not None else current_price * 1.20,
        "stop_price":   float(stop_price)   if stop_price   is not None else current_price * 0.85,
        "max_size":     size_dollars,
        "expiry":       "",
        "created_by":   agent_name,
        "reasoning":    reasoning,
    }

    # Determine the actual contract price for the side we're buying
    # For NO orders: contract price = 1 - yes_price (the NO market price)
    # For YES orders: contract price = yes_price
    if side.lower() == "no":
        contract_price = max(0.01, 1.0 - current_price)
    else:
        contract_price = current_price

    # Quantity: size_dollars / contract_price, floored at 1, capped at 99 (Kalshi limit)
    if contract_price > 0:
        quantity = min(99, max(1, int(size_dollars / contract_price)))
    else:
        quantity = 1

    rule_id = f"GATE-{agent_name}-{ticker}"

    logger.info(
        "[Gate] Placing order: %s %s %dx @ %.3f | verdict=%s rule=%s",
        side.upper(), ticker, quantity, contract_price, verdict, rule_id,
    )

    state.add_pending(ticker)
    try:
        order_manager.place_order(
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=contract_price,
            rule=rule,
            rule_id=rule_id,
            agent_name=agent_name,
            contract_class=contract_class,
            max_size=quantity,
        )
    except Exception as e:
        logger.error("[Gate] place_order failed for %s: %s", ticker, e)
        state.remove_pending(ticker)


# ---------------------------------------------------------------------------
# Status heartbeat (console + log)
# ---------------------------------------------------------------------------

def _status_loop():
    """Print a periodic status summary to stdout every 60 seconds."""
    while _running:
        time.sleep(60)
        if not _running:
            break
        try:
            _print_status()
        except Exception as e:
            logger.error("[Main] Status error: %s", e)


def _print_status():
    paper   = "[PAPER] " if _is_paper_mode() else ""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    positions  = state.get_all_positions()
    pnl        = state.get_daily_pnl()
    exposure   = state.get_total_exposure()
    rule_stats = rule_loader.get_stats()

    logger.info(
        "%s[Status] %s | positions=%d | session_pnl=$%.2f | exposure=$%.2f "
        "| rules=%d (active) | halted=%s",
        paper, now_str,
        len(positions), pnl, exposure,
        rule_stats.get("total_rules", 0),
        not state.trading_active(),
    )


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def _shutdown(signum=None, frame=None):
    global _running
    logger.info("[Main] Shutdown signal received — stopping all components.")
    _running = False
    _release_pid_lock()

    try:
        _scan_engine.stop()
    except Exception as e:
        logger.error("[Main] scan_engine.stop error: %s", e)

    try:
        _scalper_engine.stop()
    except Exception as e:
        logger.error("[Main] scalper_engine.stop error: %s", e)

    try:
        rule_loader.stop()
    except Exception as e:
        logger.error("[Main] rule_loader.stop error: %s", e)

    if _kalshi_ws is not None:
        try:
            _kalshi_ws.stop()
        except Exception as e:
            logger.error("[Main] kalshi_ws.stop error: %s", e)

    if _tennis_ws is not None:
        try:
            _tennis_ws.stop()
        except Exception as e:
            logger.error("[Main] tennis_ws.stop error: %s", e)

    logger.info("[Main] Shutdown complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    paper_tag = "[PAPER MODE] " if _is_paper_mode() else "[LIVE MODE] "
    logger.info("%sThe Syndicate starting up...", paper_tag)

    # ── 0a. PID lock — refuse to start if already running ────────────────────
    if not _acquire_pid_lock():
        sys.exit(1)

    # ── 0b. Startup cleanup ───────────────────────────────────────────────────
    _cleanup_triggers()

    # ── 1. Rule loader ────────────────────────────────────────────────────────
    rule_loader.start()
    logger.info("[Main] Rule loader started.")

    # ── 2. Scalper engine ─────────────────────────────────────────────────────
    import scalper.order_manager as _om
    _scalper_engine = ScalperEngine(order_manager=_om, rule_loader=rule_loader)
    _scalper_engine.run()
    logger.info("[Main] Scalper engine started.")

    # ── 3. Scan engine ────────────────────────────────────────────────────────
    _scan_engine.start()
    logger.info("[Main] Scan engine started.")

    # ── 3a. Register agents with scalper + outcome_reporter ───────────────────
    # scan_engine._agents is populated during start(); register them now.
    try:
        _agents = getattr(_scan_engine, "_agents", [])
        if _agents:
            _scalper_engine.register_agents(_agents)
            from core.outcome_reporter import outcome_reporter as _or
            _or.register_agents(_agents)
            logger.info("[Main] Agents registered with scalper and outcome_reporter.")
        else:
            logger.warning("[Main] No agents found in scan_engine — skipping registration.")
    except Exception as _reg_err:
        logger.error("[Main] Agent registration failed: %s", _reg_err)

    # ── 4. Connectors ─────────────────────────────────────────────────────────
    _load_connectors()

    if _kalshi_ws is not None:
        _kalshi_ws._on_tick_callback = _on_tick            # price tick feed
        _scan_engine._on_tickers_ready = _kalshi_ws.update_tickers  # one-shot after first heartbeat
        _kalshi_ws.start()
        logger.info("[Main] KalshiWS started.")

    if _tennis_ws is not None:
        _tennis_ws._on_game_live_callback = _on_game_live
        _tennis_ws.start()
        logger.info("[Main] TennisWS started.")

    # ── 5. Intelligence gate poll ─────────────────────────────────────────────
    _gate_thread = threading.Thread(
        target=_gate_poll_loop,
        name="syndicate-gate-poll",
        daemon=True,
    )
    _gate_thread.start()
    logger.info("[Main] Intelligence gate poll thread started.")

    # ── 6. Status heartbeat ───────────────────────────────────────────────────
    _status_thread = threading.Thread(
        target=_status_loop,
        name="syndicate-status",
        daemon=True,
    )
    _status_thread.start()

    # ── 6a. Trigger sweeper (every 60s, purge stale trigger files) ────────────
    _sweeper_thread = threading.Thread(
        target=_trigger_sweeper_loop,
        name="syndicate-trigger-sweeper",
        daemon=True,
    )
    _sweeper_thread.start()

    # ── 7. Telegram startup health check ────────────────────────────────────
    try:
        import notifications.telegram as _tg
        mode_tag = "PAPER" if _is_paper_mode() else "LIVE"
        _tg.post(
            f"Online [{mode_tag}] — scanners running, WS {'connected' if _kalshi_ws else 'disabled'}, "
            f"gate active",
            "✅",
        )
    except Exception as _tg_err:
        logger.warning("[Main] Telegram startup notification failed: %s", _tg_err)

    # ── 8. Main keep-alive loop ───────────────────────────────────────────────
    _print_status()
    logger.info("%sAll components running. Press Ctrl-C to stop.", paper_tag)

    try:
        while _running:
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()

    sys.exit(0)
