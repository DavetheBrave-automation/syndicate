"""
mirror.py — MIRROR Mean Reversion Agent.

Strategy: Buy contracts that have overshot. Markets overcorrect, then revert.
MIRROR only enters AFTER the move has exhausted — never mid-drop.

Rules:
  - Price must have moved 25%+ in one direction within 15 minutes
  - Current price must be at an extreme: below 15¢ or above 85¢
  - The extreme must be RECENT — happened in last 30 minutes
  - Look for price stabilization — 2+ minutes of sideways movement after drop
  - Never enter mid-drop — wait for the move to exhaust itself
  - Target reversion to 50% of the move — not full recovery
  - Exit at +15% gain — reversion plays rarely go the full distance
  - Avoid BLITZ and TIDE contracts — momentum and mean reversion conflict
  - Tennis: never fade a break of serve in first set — it often holds
  - YES side: buy when price is oversold (below 15¢) — expect bounce
  - NO side: buy when price is overbought (above 85¢) — expect pullback
"""

import logging

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.mirror")

_MIN_VOLUME       = 1_000
_OVERSOLD_THRESH  = 0.15    # YES below this = oversold
_OVERBOUGHT_THRESH = 0.85   # YES above this = overbought
_MIN_MOVE_PCT     = 25.0    # minimum % move in history required
_TARGET_GAIN      = 0.15    # 15% target on reversion
_FLAT_THRESHOLD   = 3.0     # velocity below this = stabilization

# Window for detecting the recent large move
_MOVE_WINDOW_S    = 900     # 15 min — large move must occur here
_STABLE_WINDOW_S  = 120     # 2 min — must be stable this long


def _calc_velocity_over(price_history: list, window_seconds: float) -> float:
    """% price change over last window_seconds."""
    if len(price_history) < 2:
        return 0.0
    now_ts = price_history[-1][0]
    cutoff = now_ts - window_seconds
    in_window = [(t, p) for t, p in price_history if t >= cutoff]
    if len(in_window) < 2:
        return 0.0
    oldest = in_window[0][1]
    newest = in_window[-1][1]
    if oldest <= 0:
        return 0.0
    return ((newest - oldest) / oldest) * 100.0


def _max_move_magnitude(price_history: list, window_seconds: float) -> float:
    """Return the largest % move (absolute) within the window."""
    if len(price_history) < 2:
        return 0.0
    now_ts = price_history[-1][0]
    cutoff = now_ts - window_seconds
    in_window = [p for t, p in price_history if t >= cutoff]
    if len(in_window) < 2:
        return 0.0
    max_p = max(in_window)
    min_p = min(in_window)
    if min_p <= 0:
        return 0.0
    return ((max_p - min_p) / min_p) * 100.0


class MirrorAgent(BaseAgent):
    name   = "MIRROR"
    domain = "all"

    seed_rules = [
        "Buy contracts that have overshot — markets overcorrect then revert",
        "Price must have moved 25%+ in one direction within 15 minutes",
        "Current price must be at an extreme: below 15 cents or above 85 cents",
        "The extreme must be RECENT — happened in last 30 minutes",
        "Look for price stabilization — needs 2+ minutes of sideways movement after the drop",
        "Never enter mid-drop — wait for the move to exhaust itself",
        "Target reversion to 50% of the move — not full recovery",
        "Exit at +15% gain — reversion plays rarely go the full distance",
        "Avoid BLITZ and TIDE contracts — momentum agents and mean reversion conflict",
        "Tennis: never fade a break of serve in first set — it often holds",
        "YES side: buy when price is oversold (below 15¢) — expect bounce",
        "NO side: buy when price is overbought (above 85¢) — expect pullback",
    ]

    # =========================================================================
    # should_evaluate — hot path
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        """
        Fast gate:
          - Price must be at extreme (< 15¢ or > 85¢)
          - velocity proxy suggests recent large move (market.velocity high but now lower)
          - Volume > 1000
          - Not WATCH class
        """
        if not self._base_should_evaluate(market):
            return False

        if market.volume_dollars < _MIN_VOLUME:
            return False

        # Must be at price extreme
        at_extreme = (
            market.yes_price < _OVERSOLD_THRESH or
            market.yes_price > _OVERBOUGHT_THRESH
        )
        if not at_extreme:
            return False

        return True

    # =========================================================================
    # evaluate — daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        """
        Confirm: large move happened, now stabilizing. Enter opposite direction.

        YES (oversold): price below 15¢ — expect bounce → buy YES
        NO (overbought): price above 85¢ — expect pullback → buy NO
        PROPHECY: price < 10¢ AND volume > 5000 (extreme oversold + liquid)
        HIGH_CONVICTION: clear exhaustion (velocity near zero)
        """
        history  = getattr(market, "price_history", [])
        yes_price = market.yes_price

        # Check that a large move DID happen in the last 15 minutes
        max_move = _max_move_magnitude(history, _MOVE_WINDOW_S)
        if max_move < _MIN_MOVE_PCT:
            logger.debug(
                "[MIRROR] Insufficient historical move: %s max_move=%.1f%% (min=%.1f%%)",
                market.ticker, max_move, _MIN_MOVE_PCT,
            )
            return

        # Check stabilization: recent velocity (2-min window) must be near zero
        vel_2m = abs(_calc_velocity_over(history, _STABLE_WINDOW_S))
        if vel_2m > _FLAT_THRESHOLD:
            logger.debug(
                "[MIRROR] Price not stable yet: %s vel_2m=%.1f%% (threshold=%.1f%%)",
                market.ticker, vel_2m, _FLAT_THRESHOLD,
            )
            return

        # Direction: reversion from extreme
        if yes_price < _OVERSOLD_THRESH:
            # Oversold — expect YES bounce
            side         = "yes"
            entry_price  = round(yes_price, 4)
            target_price = round(min(0.50, entry_price * (1 + _TARGET_GAIN)), 4)
            stop_price   = round(max(0.01, entry_price * 0.80), 4)
            extreme_desc = f"oversold at {yes_price*100:.1f}¢"
        else:
            # Overbought — expect NO pullback
            side         = "no"
            entry_price  = round(yes_price, 4)  # pass YES; build_signal computes NO
            target_price = round(max(0.50, yes_price * (1 - _TARGET_GAIN)), 4)
            stop_price   = round(min(0.99, yes_price * 1.20), 4)
            no_cost      = round(1.0 - yes_price, 4)
            extreme_desc = f"overbought at {yes_price*100:.1f}¢ (NO costs {no_cost:.2f})"

        # Conviction tier
        if side == "yes" and yes_price < 0.10 and market.volume_dollars > 5_000:
            conviction_tier = "PROPHECY"
        elif vel_2m <= 1.0:
            conviction_tier = "HIGH_CONVICTION"
        else:
            conviction_tier = "GLITCH"

        edge_pct = round(max_move * 0.25, 2)  # rough edge proxy from move magnitude

        reasoning = (
            f"MIRROR: {extreme_desc}. Large move={max_move:.1f}% in last 15min. "
            f"Now stabilizing (vel_2m={vel_2m:.1f}%). "
            f"{'YES: buying oversold bounce.' if side == 'yes' else 'NO: buying overbought pullback.'} "
            f"Target +{_TARGET_GAIN*100:.0f}% reversion. "
            f"Exit if momentum resumes in original direction."
        )

        signal = self.build_signal(
            market, conviction_tier, edge_pct, side,
            entry_price, target_price, stop_price, reasoning, game,
        )
        self.submit_signal(signal)
        logger.info(
            "[MIRROR] Signal: %s %s extreme=%s max_move=%.1f%% tier=%s",
            market.ticker, side.upper(), extreme_desc, max_move, conviction_tier,
        )
