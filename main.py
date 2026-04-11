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

if _SYNDICATE_ROOT not in sys.path:
    sys.path.insert(0, _SYNDICATE_ROOT)
if _ATLAS_ROOT not in sys.path:
    sys.path.insert(0, _ATLAS_ROOT)

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
    Polls triggers/decision.json every second.
    When decision.json appears, reads it, acts on the decision, deletes it.
    Runs as a daemon thread.
    """
    logger.info("[Gate] Poll thread started.")
    while _running:
        try:
            if os.path.exists(_DECISION_PATH):
                _process_decision()
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

    # Quantity: size_dollars / current_price, floored at 1
    if current_price > 0:
        quantity = max(1, int(size_dollars / current_price))
    else:
        quantity = 1

    rule_id = f"GATE-{agent_name}-{ticker}"

    logger.info(
        "[Gate] Placing order: %s %s %dx @ %.3f | verdict=%s rule=%s",
        side.upper(), ticker, quantity, current_price, verdict, rule_id,
    )

    state.add_pending(ticker)
    try:
        order_manager.place_order(
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=current_price,
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
    pnl        = state.get_session_pnl()
    exposure   = state.get_total_exposure()
    rule_stats = rule_loader.get_stats()

    logger.info(
        "%s[Status] %s | positions=%d | session_pnl=$%.2f | exposure=$%.2f "
        "| rules=%d (active) | halted=%s",
        paper, now_str,
        len(positions), pnl, exposure,
        rule_stats.get("total_rules", 0),
        state.is_halted(),
    )


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def _shutdown(signum=None, frame=None):
    global _running
    logger.info("[Main] Shutdown signal received — stopping all components.")
    _running = False

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

    # ── 4. Connectors ─────────────────────────────────────────────────────────
    _load_connectors()

    if _kalshi_ws is not None:
        _kalshi_ws._on_tick_callback = _on_tick   # injected by Syndicate KalshiWS
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

    # ── 7. Main keep-alive loop ───────────────────────────────────────────────
    _print_status()
    logger.info("%sAll components running. Press Ctrl-C to stop.", paper_tag)

    try:
        while _running:
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()

    sys.exit(0)
