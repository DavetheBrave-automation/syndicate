"""
scan_engine.py — Market scanning engine for The Syndicate.

Three scheduled daemon threads:
  HEARTBEAT   (every 5 min)  — pulls live sports markets, runs liquidity filter,
                               tracks price velocity, detects new markets, writes
                               heartbeat_latest.json for TC.
  OPPORTUNITY (every 30 min) — full catalog, classifies all markets, builds
                               prioritised opportunity report.
  STRATEGIC   (every 6 hr)   — market distribution, volume distribution, top-10
                               by volume — wakes TC for deep research.

Event triggers (called externally):
  on_market_update(ticker, yes_price, volume_dollars) — WebSocket feed tick
  on_game_live(match_id, player1, player2)            — ESPN feed game-start

All trigger files written to syndicate/triggers/.
Config reloaded from mtime on every loop iteration — live changes apply immediately.
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Path setup — must precede all local imports
# ---------------------------------------------------------------------------

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ATLAS_ROOT = os.path.join(os.path.dirname(_SYNDICATE_ROOT), "atlas")

sys.path.insert(0, _SYNDICATE_ROOT)
sys.path.insert(0, _ATLAS_ROOT)

from core.shared_state import state, MarketData       # noqa: E402
from core.liquidity_filter import check_market        # noqa: E402
from core.contract_classifier import classify_market  # noqa: E402

logger = logging.getLogger("syndicate.scanner")

# ---------------------------------------------------------------------------
# Trigger directory
# ---------------------------------------------------------------------------

_TRIGGERS_DIR = os.path.join(_SYNDICATE_ROOT, "triggers")


def _ensure_triggers_dir() -> None:
    os.makedirs(_TRIGGERS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Config loader — mtime-cached (Atlas brain.py pattern)
# ---------------------------------------------------------------------------

_cfg_cache: Optional[dict] = None
_cfg_mtime: float = 0.0
_cfg_lock = threading.Lock()


def _load_config() -> dict:
    global _cfg_cache, _cfg_mtime
    cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
    with _cfg_lock:
        try:
            mtime = os.path.getmtime(cfg_path)
            if _cfg_cache is None or mtime > _cfg_mtime:
                with open(cfg_path, encoding="utf-8") as f:
                    _cfg_cache = yaml.safe_load(f)
                _cfg_mtime = mtime
                logger.debug("[ScanEngine] Config reloaded (mtime=%.1f)", mtime)
        except Exception as e:
            logger.error("[ScanEngine] Config load error: %s", e)
        return _cfg_cache or {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _write_trigger(filename: str, payload: dict) -> None:
    """Atomically write a trigger JSON file."""
    _ensure_triggers_dir()
    path = os.path.join(_TRIGGERS_DIR, filename)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
        logger.debug("[ScanEngine] Trigger written: %s", filename)
    except Exception as e:
        logger.error("[ScanEngine] Failed to write trigger %s: %s", filename, e)


def _safe_ts_tag() -> str:
    """Return a compact UTC timestamp tag suitable for filenames."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Velocity helpers
# ---------------------------------------------------------------------------

_VELOCITY_THRESHOLD_PCT = 10.0   # 10% move triggers a velocity event
_VELOCITY_DEDUP_WINDOW  = 60.0   # suppress re-fire within 60 seconds


def _price_5min_ago(ticker: str, price_history: list) -> Optional[float]:
    """
    Return the oldest price in the last 5–10 minute window from price_history.
    price_history: list of (timestamp_float, price_float), newest last.
    Returns None if insufficient history.
    """
    now = time.time()
    cutoff_old = now - 600   # oldest we look back (10 min)
    cutoff_new = now - 300   # 5 min ago lower bound

    # Find entries that are between 5 and 10 minutes old
    candidates = [p for ts, p in price_history if cutoff_old <= ts <= cutoff_new]
    if candidates:
        return candidates[-1]   # most recent entry in that window

    # Fallback: oldest entry overall if history is shorter than 5 min
    if price_history:
        return price_history[0][1]

    return None


def _pct_change(price_now: float, price_before: float) -> float:
    """Return signed percentage change. Returns 0.0 if price_before is 0."""
    if price_before == 0:
        return 0.0
    return ((price_now - price_before) / price_before) * 100.0


# ---------------------------------------------------------------------------
# ScanEngine
# ---------------------------------------------------------------------------

class ScanEngine:
    """
    Scheduled market scanning engine.

    Three daemon threads run independent scan loops.
    External callers push WebSocket / ESPN feed data via on_market_update()
    and on_game_live().
    """

    def __init__(self) -> None:
        # ── Internal price state ──────────────────────────────────────────────
        # {ticker: [(timestamp_float, price_float), ...]} — last 10 min only
        self._price_history: dict[str, list[tuple[float, float]]] = {}
        self._price_history_lock = threading.Lock()

        # {ticker: timestamp_float} — last time a velocity trigger was fired
        # Protected by _velocity_lock to prevent double-fire TOCTOU race
        self._velocity_last_fired: dict[str, float] = {}
        self._velocity_lock = threading.Lock()

        # Tickers seen in any heartbeat scan — for new-market detection
        self._seen_tickers: set[str] = set()

        # ── Thread control ────────────────────────────────────────────────────
        self._stop_event = threading.Event()

        # ── Timestamps of last successful scan completion ─────────────────────
        self._last_heartbeat:   float = 0.0
        self._last_opportunity: float = 0.0
        self._last_strategic:   float = 0.0

        # ── Thread handles ────────────────────────────────────────────────────
        self._thread_heartbeat:   Optional[threading.Thread] = None
        self._thread_opportunity: Optional[threading.Thread] = None
        self._thread_strategic:   Optional[threading.Thread] = None

        logger.debug("[ScanEngine] Initialised.")

        # ── Agents ────────────────────────────────────────────────────────────
        self._agents: list = []
        try:
            from agents.ace import AceAgent
            from agents.axiom import AxiomAgent
            from agents.diamond import DiamondAgent
            self._agents = [AceAgent(), AxiomAgent(), DiamondAgent()]
            logger.info(
                "[ScanEngine] Agents loaded: %s",
                [a.name for a in self._agents],
            )
        except ImportError as e:
            logger.warning(
                "[ScanEngine] Agent import failed: %s — running without agents", e
            )

        # {(agent_name, ticker): last_spawn_monotonic} — prevents thread pile-up
        self._agent_spawn_ts: dict[tuple, float] = {}
        self._agent_spawn_lock = threading.Lock()

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def start(self) -> None:
        """Start all three scanner daemon threads. Returns immediately."""
        _ensure_triggers_dir()

        self._stop_event.clear()

        self._thread_heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="scan-heartbeat",
            daemon=True,
        )
        self._thread_opportunity = threading.Thread(
            target=self._opportunity_loop,
            name="scan-opportunity",
            daemon=True,
        )
        self._thread_strategic = threading.Thread(
            target=self._strategic_loop,
            name="scan-strategic",
            daemon=True,
        )

        self._thread_heartbeat.start()
        self._thread_opportunity.start()
        self._thread_strategic.start()

        logger.info(
            "[ScanEngine] Started — threads: heartbeat=%s, opportunity=%s, strategic=%s",
            self._thread_heartbeat.name,
            self._thread_opportunity.name,
            self._thread_strategic.name,
        )

    def stop(self) -> None:
        """Signal all scanner threads to stop and wait for them to exit."""
        logger.info("[ScanEngine] Stop requested.")
        self._stop_event.set()

        for thread in (
            self._thread_heartbeat,
            self._thread_opportunity,
            self._thread_strategic,
        ):
            if thread is not None and thread.is_alive():
                thread.join(timeout=10)

        logger.info("[ScanEngine] All scanner threads stopped.")

    # =========================================================================
    # Heartbeat loop  (every 5 min)
    # =========================================================================

    def _heartbeat_loop(self) -> None:
        logger.info("[Heartbeat] Thread started.")
        while not self._stop_event.is_set():
            cfg = _load_config()
            interval = float(
                cfg.get("syndicate", {}).get("scan_interval_heartbeat", 300)
            )
            try:
                self._run_heartbeat()
            except Exception as e:
                logger.error("[Heartbeat] Scan error: %s", e, exc_info=True)

            self._stop_event.wait(timeout=interval)

        logger.info("[Heartbeat] Thread exiting.")

    def _run_heartbeat(self) -> None:
        from connectors.kalshi_rest import get_sports_markets

        markets = get_sports_markets()
        if not markets:
            logger.warning("[Heartbeat] get_sports_markets returned empty list.")
            return

        now = time.time()
        n_total        = len(markets)
        n_passed       = 0
        n_velocity     = 0
        n_new          = 0
        velocity_events: list[dict] = []

        for m in markets:
            ticker         = m.get("ticker", "")
            yes_price      = float(m.get("yes_price", 0) or 0)
            volume_dollars = float(m.get("volume_dollars", 0) or 0)
            expiry         = m.get("expiry", "")
            series_ticker  = m.get("series_ticker", "")

            if not ticker:
                continue

            # ── Compute days_to_settlement from expiry ────────────────────────
            days_to_settlement = _days_from_expiry(expiry)

            # ── Spread: approximate from yes_price (mid); real data has no bid/ask
            #    The heartbeat uses the same mid-price approximation as kalshi_rest.
            #    We store spread=0.0 here — liquidity_filter will gate on it.
            spread = 0.0

            # ── Upsert into shared_state ──────────────────────────────────────
            state.upsert_market(
                ticker=ticker,
                yes_price=yes_price,
                no_bid=round(1.0 - yes_price, 4),
                volume_dollars=volume_dollars,
                spread=spread,
                days_to_settlement=days_to_settlement,
                contract_class="WATCH",    # placeholder; classifier sets real class
                series_ticker=series_ticker,
                ts=now,
            )

            # ── Retrieve updated MarketData for downstream calls ──────────────
            market_data = state.get_market(ticker)
            if market_data is None:
                continue

            # ── Liquidity filter ──────────────────────────────────────────────
            liq_result = check_market(ticker, market_data)
            if liq_result.passed:
                n_passed += 1

            # ── Velocity check (5-min window) ─────────────────────────────────
            with self._price_history_lock:
                history = self._price_history.get(ticker, [])
                price_ago = _price_5min_ago(ticker, history)

            if price_ago is not None and price_ago > 0 and yes_price > 0:
                pct = _pct_change(yes_price, price_ago)
                if abs(pct) > _VELOCITY_THRESHOLD_PCT:
                    with self._velocity_lock:
                        last_fired = self._velocity_last_fired.get(ticker, 0.0)
                        should_fire = now - last_fired >= _VELOCITY_DEDUP_WINDOW
                        if should_fire:
                            self._velocity_last_fired[ticker] = now
                    if should_fire:
                        ts_tag = _safe_ts_tag()
                        filename = f"velocity_{ticker}_{ts_tag}.json"
                        payload = {
                            "type":           "velocity",
                            "ticker":         ticker,
                            "price_now":      round(yes_price, 4),
                            "price_5min_ago": round(price_ago, 4),
                            "pct_change":     round(pct, 2),
                            "volume_dollars": volume_dollars,
                            "timestamp":      _utcnow_iso(),
                        }
                        _write_trigger(filename, payload)
                        velocity_events.append(payload)
                        n_velocity += 1
                        logger.info(
                            "[Heartbeat] Velocity event: %s pct_change=%.1f%%",
                            ticker, pct,
                        )

            # ── New market detection ──────────────────────────────────────────
            if ticker not in self._seen_tickers:
                self._seen_tickers.add(ticker)
                n_new += 1
                ts_tag = _safe_ts_tag()
                filename = f"new_market_{ticker}_{ts_tag}.json"
                payload = {
                    "type":           "new_market",
                    "ticker":         ticker,
                    "series_ticker":  series_ticker,
                    "yes_price":      round(yes_price, 4),
                    "volume_dollars": volume_dollars,
                    "expiry":         expiry,
                    "timestamp":      _utcnow_iso(),
                }
                _write_trigger(filename, payload)
                logger.info("[Heartbeat] New market detected: %s", ticker)

            # ── Update local price history (keep last 10 min) ─────────────────
            with self._price_history_lock:
                hist = self._price_history.setdefault(ticker, [])
                hist.append((now, yes_price))
                cutoff = now - 600
                self._price_history[ticker] = [
                    (ts, p) for ts, p in hist if ts >= cutoff
                ]

        # ── Write heartbeat summary ───────────────────────────────────────────
        self._last_heartbeat = now
        summary = {
            "type":             "heartbeat",
            "timestamp":        _utcnow_iso(),
            "n_markets_scanned": n_total,
            "n_passed_liquidity": n_passed,
            "n_velocity_events": n_velocity,
            "n_new_markets":     n_new,
            "velocity_events":   velocity_events,
        }
        _write_trigger("heartbeat_latest.json", summary)

        logger.info(
            "[Heartbeat] %d markets scanned, %d passed liquidity, "
            "%d velocity events, %d new markets.",
            n_total, n_passed, n_velocity, n_new,
        )

    # =========================================================================
    # Opportunity loop  (every 30 min)
    # =========================================================================

    def _opportunity_loop(self) -> None:
        logger.info("[Opportunity] Thread started.")
        while not self._stop_event.is_set():
            cfg = _load_config()
            interval = float(
                cfg.get("syndicate", {}).get("scan_interval_opportunity", 1800)
            )
            try:
                self._run_opportunity()
            except Exception as e:
                logger.error("[Opportunity] Scan error: %s", e, exc_info=True)

            self._stop_event.wait(timeout=interval)

        logger.info("[Opportunity] Thread exiting.")

    def _run_opportunity(self) -> None:
        from connectors.kalshi_rest import get_sports_markets

        markets = get_sports_markets()
        if not markets:
            logger.warning("[Opportunity] get_sports_markets returned empty list.")
            return

        now = time.time()
        now_ts = _utcnow_iso()
        horizon_24h = now + 86400   # 24 hours from now (epoch seconds)

        # Class priority order for sorting
        _CLASS_PRIORITY = {"SCALP": 0, "SWING": 1, "POSITION": 2, "WATCH": 3}

        opportunities: list[dict] = []

        for m in markets:
            ticker         = m.get("ticker", "")
            yes_price      = float(m.get("yes_price", 0) or 0)
            volume_dollars = float(m.get("volume_dollars", 0) or 0)
            expiry         = m.get("expiry", "")
            series_ticker  = m.get("series_ticker", "")
            title          = m.get("title", "")

            if not ticker:
                continue

            days_to_settlement = _days_from_expiry(expiry)

            # Build a transient MarketData for classification (no SharedState write)
            market_data = MarketData(
                ticker=ticker,
                yes_price=yes_price,
                no_bid=round(1.0 - yes_price, 4),
                volume_dollars=volume_dollars,
                spread=0.0,
                days_to_settlement=days_to_settlement,
                contract_class="WATCH",
                series_ticker=series_ticker,
                last_update=now,
            )

            profile = classify_market(market_data)
            contract_class = profile.contract_class

            # Skip WATCH — no trading interest for opportunity report
            if contract_class == "WATCH":
                continue

            # Run liquidity filter
            liq_result = check_market(ticker, market_data)
            if not liq_result.passed:
                continue

            # Upcoming: expiry within 24 hours
            expiry_epoch = _expiry_to_epoch(expiry)
            is_upcoming = (
                expiry_epoch is not None and expiry_epoch <= horizon_24h
            )

            opportunities.append({
                "ticker":             ticker,
                "title":              title,
                "series_ticker":      series_ticker,
                "contract_class":     contract_class,
                "yes_price":          round(yes_price, 4),
                "volume_dollars":     round(volume_dollars, 2),
                "days_to_settlement": round(days_to_settlement, 3),
                "expiry":             expiry,
                "max_size":           liq_result.max_size,
                "upcoming":           is_upcoming,
                "priority":           _CLASS_PRIORITY.get(contract_class, 99),
            })

        # Sort: primary by class priority (SCALP first), secondary by volume desc
        opportunities.sort(key=lambda x: (x["priority"], -x["volume_dollars"]))

        # Strip internal priority key from output
        for opp in opportunities:
            opp.pop("priority", None)

        self._last_opportunity = now
        report = {
            "type":          "opportunity_scan",
            "timestamp":     now_ts,
            "n_opportunities": len(opportunities),
            "opportunities": opportunities,
        }
        _write_trigger("opportunity_scan.json", report)

        logger.info(
            "[Opportunity] Scan complete — %d opportunities found.",
            len(opportunities),
        )

    # =========================================================================
    # Strategic loop  (every 6 hr)
    # =========================================================================

    def _strategic_loop(self) -> None:
        logger.info("[Strategic] Thread started.")
        while not self._stop_event.is_set():
            cfg = _load_config()
            interval = float(
                cfg.get("syndicate", {}).get("scan_interval_strategic", 21600)
            )
            try:
                self._run_strategic()
            except Exception as e:
                logger.error("[Strategic] Scan error: %s", e, exc_info=True)

            self._stop_event.wait(timeout=interval)

        logger.info("[Strategic] Thread exiting.")

    def _run_strategic(self) -> None:
        from connectors.kalshi_rest import get_sports_markets

        markets = get_sports_markets()
        if not markets:
            logger.warning("[Strategic] get_sports_markets returned empty list.")
            return

        now = time.time()
        now_ts = _utcnow_iso()

        # Accumulators for distribution
        class_distribution: dict[str, int]   = {}
        volume_distribution: dict[str, float] = {}
        total_volume = 0.0
        enriched: list[dict] = []

        for m in markets:
            ticker         = m.get("ticker", "")
            yes_price      = float(m.get("yes_price", 0) or 0)
            volume_dollars = float(m.get("volume_dollars", 0) or 0)
            expiry         = m.get("expiry", "")
            series_ticker  = m.get("series_ticker", "")
            title          = m.get("title", "")

            if not ticker:
                continue

            days_to_settlement = _days_from_expiry(expiry)

            market_data = MarketData(
                ticker=ticker,
                yes_price=yes_price,
                no_bid=round(1.0 - yes_price, 4),
                volume_dollars=volume_dollars,
                spread=0.0,
                days_to_settlement=days_to_settlement,
                contract_class="WATCH",
                series_ticker=series_ticker,
                last_update=now,
            )

            profile = classify_market(market_data)
            contract_class = profile.contract_class

            class_distribution[contract_class] = (
                class_distribution.get(contract_class, 0) + 1
            )
            volume_distribution[contract_class] = (
                volume_distribution.get(contract_class, 0.0) + volume_dollars
            )
            total_volume += volume_dollars

            enriched.append({
                "ticker":             ticker,
                "title":              title,
                "series_ticker":      series_ticker,
                "contract_class":     contract_class,
                "yes_price":          round(yes_price, 4),
                "volume_dollars":     round(volume_dollars, 2),
                "days_to_settlement": round(days_to_settlement, 3),
                "expiry":             expiry,
            })

        # Top 10 markets by volume
        top_10 = sorted(enriched, key=lambda x: x["volume_dollars"], reverse=True)[:10]

        # Volume distribution with percentages
        vol_dist_pct = {}
        for cls, vol in volume_distribution.items():
            vol_dist_pct[cls] = {
                "total_volume":  round(vol, 2),
                "pct_of_total":  round((vol / total_volume * 100) if total_volume > 0 else 0.0, 1),
                "market_count":  class_distribution.get(cls, 0),
            }

        self._last_strategic = now
        report = {
            "type":               "strategic_scan",
            "timestamp":          now_ts,
            "total_markets":      len(enriched),
            "total_volume":       round(total_volume, 2),
            "class_distribution": class_distribution,
            "volume_distribution": vol_dist_pct,
            "top_10_by_volume":   top_10,
            "all_markets":        enriched,
        }
        _write_trigger("strategic_scan.json", report)

        logger.info(
            "[Strategic] Scan complete — %d total markets, $%.0f total volume. "
            "Distribution: %s",
            len(enriched),
            total_volume,
            class_distribution,
        )

    # =========================================================================
    # Event triggers — called by WebSocket feed / ESPN feed
    # =========================================================================

    def on_market_update(
        self, ticker: str, yes_price: float, volume_dollars: float
    ) -> None:
        """
        Called by WebSocket feed on every price tick.

        Updates internal price history and fires velocity trigger if price
        moved > 10% vs 5-min-ago price. Not a hot path — I/O is acceptable.
        """
        now = time.time()

        # ── Update price history ──────────────────────────────────────────────
        with self._price_history_lock:
            hist = self._price_history.setdefault(ticker, [])
            hist.append((now, yes_price))
            cutoff = now - 600   # keep last 10 min
            self._price_history[ticker] = [
                (ts, p) for ts, p in hist if ts >= cutoff
            ]
            history_snapshot = list(self._price_history[ticker])

        # ── Velocity check ────────────────────────────────────────────────────
        price_ago = _price_5min_ago(ticker, history_snapshot)
        if price_ago is not None and price_ago > 0 and yes_price > 0:
            pct = _pct_change(yes_price, price_ago)
            if abs(pct) > _VELOCITY_THRESHOLD_PCT:
                with self._velocity_lock:
                    last_fired = self._velocity_last_fired.get(ticker, 0.0)
                    should_fire = now - last_fired >= _VELOCITY_DEDUP_WINDOW
                    if should_fire:
                        self._velocity_last_fired[ticker] = now
                if should_fire:
                    ts_tag = _safe_ts_tag()
                    filename = f"velocity_{ticker}_{ts_tag}.json"
                    payload = {
                        "type":           "velocity",
                        "ticker":         ticker,
                        "price_now":      round(yes_price, 4),
                        "price_5min_ago": round(price_ago, 4),
                        "pct_change":     round(pct, 2),
                        "volume_dollars": volume_dollars,
                        "timestamp":      _utcnow_iso(),
                    }
                    _write_trigger(filename, payload)
                    logger.info(
                        "[ScanEngine] on_market_update velocity: %s pct_change=%.1f%%",
                        ticker, pct,
                    )

        # ── Agent routing — hot-path gate then daemon-thread evaluate ─────────
        # Cooldown: at most one evaluate() spawn per (agent, ticker) per 10s.
        _SPAWN_COOLDOWN = 10.0
        if self._agents:
            market_data = state.get_market(ticker)
            if market_data is not None:
                now_mono = time.monotonic()
                for agent in self._agents:
                    try:
                        if not agent.should_evaluate(market_data):
                            continue
                        key = (agent.name, ticker)
                        with self._agent_spawn_lock:
                            last = self._agent_spawn_ts.get(key, 0.0)
                            if now_mono - last < _SPAWN_COOLDOWN:
                                continue
                            self._agent_spawn_ts[key] = now_mono
                        threading.Thread(
                            target=agent.evaluate,
                            args=(market_data,),
                            daemon=True,
                            name=f"agent-{agent.name}-{ticker}",
                        ).start()
                    except Exception as e:
                        logger.error(
                            "[ScanEngine] Agent %s routing error for %s: %s",
                            agent.name, ticker, e,
                        )

    def on_game_live(
        self, match_id: str, player1: str, player2: str
    ) -> None:
        """
        Called by ESPN feed when game state transitions to "in".

        Writes a game_live trigger file for TC.
        """
        ts_tag = _safe_ts_tag()
        filename = f"game_live_{match_id}_{ts_tag}.json"
        payload = {
            "type":      "game_live",
            "match_id":  match_id,
            "player1":   player1,
            "player2":   player2,
            "timestamp": _utcnow_iso(),
        }
        _write_trigger(filename, payload)

        # ── Route to all agents — each agent's should_evaluate filters its domain
        for mkt_ticker, market_data in state.get_all_markets().items():
            for agent in self._agents:
                try:
                    if agent.should_evaluate(market_data):
                        threading.Thread(
                            target=agent.evaluate,
                            args=(market_data,),
                            daemon=True,
                            name=f"agent-{agent.name}-{mkt_ticker}",
                        ).start()
                except Exception as e:
                    logger.error(
                        "[ScanEngine] on_game_live agent routing error for %s: %s",
                        mkt_ticker, e,
                    )

        logger.info(
            "[ScanEngine] on_game_live: match_id=%s %s vs %s",
            match_id, player1, player2,
        )


# ---------------------------------------------------------------------------
# Module-level helpers  (not exposed as class methods — pure functions)
# ---------------------------------------------------------------------------

def _days_from_expiry(expiry_str: str) -> float:
    """
    Parse an ISO 8601 expiry string and return days from now as a float.
    Returns 999.0 on parse failure (same convention as contract_classifier).
    """
    if not expiry_str:
        return 999.0

    from datetime import date, timedelta

    now = datetime.now(tz=timezone.utc)

    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            expiry_dt = datetime.strptime(expiry_str, fmt).replace(tzinfo=timezone.utc)
            return max((expiry_dt - now).total_seconds() / 86400.0, 0.0)
        except ValueError:
            pass

    try:
        expiry_dt = datetime.fromisoformat(expiry_str)
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        return max((expiry_dt - now).total_seconds() / 86400.0, 0.0)
    except (ValueError, TypeError):
        pass

    try:
        d = date.fromisoformat(expiry_str)
        expiry_dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
        return max((expiry_dt - now).total_seconds() / 86400.0, 0.0)
    except (ValueError, TypeError):
        pass

    logger.warning("[ScanEngine] Could not parse expiry '%s' — returning 999.0", expiry_str)
    return 999.0


def _expiry_to_epoch(expiry_str: str) -> Optional[float]:
    """
    Parse an ISO 8601 expiry string to a Unix epoch float.
    Returns None on failure.
    """
    if not expiry_str:
        return None

    from datetime import date

    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            dt = datetime.strptime(expiry_str, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass

    try:
        dt = datetime.fromisoformat(expiry_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        pass

    try:
        d = date.fromisoformat(expiry_str)
        dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        pass

    return None


# ---------------------------------------------------------------------------
# Module-level singleton (optional convenience — callers may also instantiate directly)
# ---------------------------------------------------------------------------

scan_engine = ScanEngine()
