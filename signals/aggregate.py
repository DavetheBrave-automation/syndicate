"""
signals/aggregate.py — Combines all macro signal feeds into one snapshot.
Called by agents that need macro context (AXIOM, ORACLE, OIL, DIAMOND).

5-min in-process cache keeps heartbeat threads from hammering disk on every tick.
Each underlying source has its own disk cache (FRED 4h, FNG 1h, MacroLLM 2h).
"""
import time
import logging

logger = logging.getLogger("syndicate.signals.aggregate")

_cached_snapshot: dict = {}
_last_snapshot_ts: float = 0.0
_SNAPSHOT_CACHE_SEC = 300   # 5 min — same as heartbeat interval


def get_snapshot() -> dict:
    """
    Return combined macro signals snapshot.
    In-process cache: refreshes at most every 5 minutes.
    Each source falls back gracefully on error.
    """
    global _cached_snapshot, _last_snapshot_ts

    if time.time() - _last_snapshot_ts < _SNAPSHOT_CACHE_SEC and _cached_snapshot:
        return _cached_snapshot

    s: dict = {}

    try:
        from signals.fred import get_all as _fred
        s.update(_fred())
    except Exception as e:
        logger.warning("[Signals] FRED failed: %s", e)

    try:
        from signals.fng import get_all as _fng
        s.update(_fng())
    except Exception as e:
        logger.warning("[Signals] FNG failed: %s", e)

    try:
        from signals.macro_llm import get_all as _llm
        s.update(_llm())
    except Exception as e:
        logger.warning("[Signals] MacroLLM failed: %s", e)

    _cached_snapshot = s
    _last_snapshot_ts = time.time()

    live_keys = [k for k, v in s.items() if v not in (None, "UNKNOWN", "NO_KEY", "ERROR")]
    logger.info("[Signals] Snapshot refreshed — %d live keys: %s", len(live_keys), live_keys[:8])

    return s
