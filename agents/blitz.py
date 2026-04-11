"""
blitz.py — BLITZ Velocity Momentum Trading Agent.

Detects Kalshi markets in extreme price free-fall and fades the move —
buying YES when velocity signals oversold crowd panic.

Signal logic:
  1. should_evaluate: coarse gate on market.velocity < -12.0
     (KalshiWS calls state.set_velocity() after each tick)
  2. evaluate: recompute 60s and 300s velocities from market.price_history
     - Both must be negative (sustained drop, not a single-tick spike)
     - 60s velocity magnitude drives conviction tier
  3. Side: always YES (mean reversion / fade the panic)
  4. Exit: 8 minutes OR +15% gain, whichever first

Never reaches PROPHECY — pure velocity is insufficient for highest conviction.
"""

import logging
from typing import Optional
from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.blitz")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_VOLUME        = 40_000   # higher liquidity requirement — velocity in thin markets is noise
_MAX_SPREAD        = 0.10     # wide spread during panic = dangerous fills
_MIN_PRICE         = 0.08     # skip already-crashed contracts
_MAX_PRICE         = 0.75     # only markets that have fallen meaningfully from a higher price
_VELOCITY_GATE     = -12.0    # hot-path coarse filter (market.velocity)
_MIN_60S_VELOCITY  = -15.0    # minimum 60s velocity to signal (negative = drop)
_PROFIT_TARGET_PCT = 0.15     # +15% bounce target from entry
_STOP_PCT          = 0.15     # -15% stop loss from entry


# ---------------------------------------------------------------------------
# Velocity helper
# ---------------------------------------------------------------------------

def _compute_velocity(price_history: list, window_seconds: float) -> Optional[float]:
    """
    Compute % price change over the last window_seconds using price_history.
    Returns None if fewer than 2 data points exist in the window or on any error.

    price_history: list of (ts: float, yes_price: float) tuples.
    """
    try:
        if not price_history:
            return None

        now_ts = price_history[-1][0]
        cutoff = now_ts - window_seconds
        window = [(t, p) for t, p in price_history if t >= cutoff]

        if len(window) < 2:
            return None

        oldest_price = float(window[0][1])
        newest_price = float(window[-1][1])

        if oldest_price <= 0:
            return None

        return ((newest_price - oldest_price) / oldest_price) * 100.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# BlitzAgent
# ---------------------------------------------------------------------------

class BlitzAgent(BaseAgent):
    """
    BLITZ — Velocity momentum. Fades extreme price drops in live Kalshi markets.

    Buys YES when 60s velocity < -15% AND 300s velocity < 0% (confirmed sustained
    trend, not single-tick noise). Exit target: +15% within 8 minutes.
    """

    name       = "BLITZ"
    domain     = "all"
    seed_rules = [
        "Only enter when 60s price velocity < -15% and 300s velocity also negative",
        "300s velocity positive = short-term noise spike, not a sustained drop — PASS",
        "Never enter if yes_price < 0.08 — market is already priced out",
        "Never enter if yes_price > 0.75 — need room for a meaningful fall signal",
        "Volume must exceed 40000 — velocity in illiquid markets is unreliable",
        "Spread above 0.10 signals illiquidity during panic — skip to avoid bad fills",
        "Exit in 8 minutes maximum regardless of outcome — do not hold through event",
        "Pure velocity caps conviction at HIGH_CONVICTION — never PROPHECY on price speed alone",
    ]

    # =========================================================================
    # should_evaluate — HOT PATH, <1ms, NO I/O
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        if not self._base_should_evaluate(market):
            return False

        # Coarse velocity gate — requires KalshiWS to call state.set_velocity() each tick
        if market.velocity >= _VELOCITY_GATE:
            return False

        # Price range — skip already-crashed and high-price contracts
        if market.yes_price <= _MIN_PRICE or market.yes_price > _MAX_PRICE:
            return False

        # Volume — velocity in thin markets is noise
        if market.volume_dollars < _MIN_VOLUME:
            return False

        return True

    # =========================================================================
    # evaluate — called in daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        price_history = market.price_history

        # Recompute precise velocities from raw price_history (not the cached .velocity)
        vel_60s  = _compute_velocity(price_history, 60.0)
        vel_300s = _compute_velocity(price_history, 300.0)

        if vel_60s is None or vel_300s is None:
            logger.debug("[BLITZ] Insufficient price history for ticker=%s", market.ticker)
            return

        # 300s velocity must be negative — confirms sustained drop, not a single spike
        if vel_300s >= 0.0:
            logger.debug(
                "[BLITZ] 300s velocity %.1f%% is positive — noise, PASS | ticker=%s",
                vel_300s, market.ticker,
            )
            return

        # 60s velocity must clear the threshold
        if vel_60s >= _MIN_60S_VELOCITY:
            logger.debug(
                "[BLITZ] 60s velocity %.1f%% insufficient | ticker=%s",
                vel_60s, market.ticker,
            )
            return

        # Spread check — wide spread during panic = bad fills
        if market.spread > _MAX_SPREAD:
            logger.debug(
                "[BLITZ] Spread too wide %.3f | ticker=%s", market.spread, market.ticker
            )
            return

        # Conviction driven by 60s velocity magnitude.
        # _tier_to_conviction: GLITCH→2, HIGH_CONVICTION→3.
        # Velocity alone caps at HIGH_CONVICTION — never PROPHECY.
        # -15 to -20%  → GLITCH          ($2 bet)
        # -20%+        → HIGH_CONVICTION ($3 bet)
        abs_vel = abs(vel_60s)
        if abs_vel >= 20.0:
            conviction_tier = "HIGH_CONVICTION"
        else:
            conviction_tier = "GLITCH"

        entry_price  = round(market.yes_price, 4)
        target_price = round(min(0.90, entry_price * (1.0 + _PROFIT_TARGET_PCT)), 3)
        stop_price   = round(max(0.05, entry_price * (1.0 - _STOP_PCT)),           3)

        reasoning = (
            f"BLITZ velocity drop: 60s={vel_60s:.1f}%, 300s={vel_300s:.1f}%"
            f" | entry={entry_price:.3f}"
            f" | target={target_price:.3f} (+{_PROFIT_TARGET_PCT:.0%})"
            f" | exit=8min OR +{_PROFIT_TARGET_PCT:.0%} whichever first"
        )

        signal = self.build_signal(
            market, conviction_tier, abs_vel, "yes",
            entry_price, target_price, stop_price, reasoning, game,
        )
        self.submit_signal(signal)
