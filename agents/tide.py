"""
tide.py — TIDE Momentum Surfer Agent.

Strategy: Ride sustained directional moves already in progress.
TIDE does NOT predict reversals — it joins momentum already underway.

Rules:
  - Price must have moved 15%+ in one direction in last 10 minutes
  - Volume must be INCREASING, not decreasing (confirms real momentum)
  - Never enter in first 10 minutes of a match (no trend established)
  - Exit when momentum stalls (two consecutive flat 5-min periods)
  - Never fight the tape — if moving down, only buy NO not YES
  - Target +12% from entry — smaller wins more often
  - Max hold 20 minutes — momentum rarely lasts longer on Kalshi
  - Avoid contracts under 10¢ or over 90¢ — momentum effects weakest at extremes
  - If BLITZ already in same contract — PASS, avoid doubling momentum exposure
  - YES side: buy when you think price will go UP
  - NO side: buy when you think price will go DOWN
"""

import logging
import time

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.tide")

_MIN_VELOCITY    = 15.0    # % move required over short window
_MIN_VOLUME      = 1_000
_TARGET_GAIN     = 0.12    # 12% profit target
_MAX_DAYS        = 1.5     # don't hold if settlement > 1.5 days
_PRICE_MIN       = 0.10
_PRICE_MAX       = 0.90

# How long velocity windows cover (must align with MarketData.price_history granularity)
_SHORT_WINDOW_S  = 300    # 5 min — recent momentum
_LONG_WINDOW_S   = 600    # 10 min — trend confirmation


def _calc_velocity_over(price_history: list, window_seconds: float) -> float:
    """
    Compute % price change over the last `window_seconds` of history.
    Returns 0.0 if insufficient data.
    """
    if len(price_history) < 2:
        return 0.0
    now_ts = price_history[-1][0]
    cutoff = now_ts - window_seconds
    in_window = [(t, p) for t, p in price_history if t >= cutoff]
    if len(in_window) < 2:
        return 0.0
    oldest_price = in_window[0][1]
    newest_price = in_window[-1][1]
    if oldest_price <= 0:
        return 0.0
    return ((newest_price - oldest_price) / oldest_price) * 100.0


class TideAgent(BaseAgent):
    name                  = "TIDE"
    domain                = "all"
    EVAL_COOLDOWN_SECONDS = 300.0   # Fast-signal agent — re-evaluate every 5 min

    seed_rules = [
        "Buy contracts already moving in a direction — ride the wave not the reversal",
        "Price must have moved 15%+ in one direction in last 10 minutes",
        "Volume must be INCREASING not decreasing — confirms real momentum",
        "Never enter in first 10 minutes of a match — no trend established yet",
        "Exit when momentum stalls — two consecutive 5-min candles without progress",
        "Never fight the tape — if moving down, only buy NO not YES",
        "Target +12% from entry — smaller wins more often",
        "Max hold 20 minutes — momentum rarely lasts longer than that on Kalshi",
        "Avoid contracts under 10 cents or over 90 cents — momentum effects weakest at extremes",
        "If BLITZ already in same contract — PASS, avoid doubling momentum exposure",
        "YES side: buy when price is going UP. NO side: buy when price is going DOWN",
    ]

    # =========================================================================
    # should_evaluate — hot path
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        """
        Fast gate:
          - Price in 10-90¢ range (momentum weakens at extremes)
          - volume > 1000
          - market.velocity != 0 (proxy for non-flat movement)
          - Not WATCH class (handled by _base_should_evaluate)
          - Settlement within 1.5 days (time value matters for momentum plays)
        """
        if not self._base_should_evaluate(market):
            return False

        if market.volume_dollars < _MIN_VOLUME:
            return False

        if not (_PRICE_MIN <= market.yes_price <= _PRICE_MAX):
            return False

        if market.days_to_settlement > _MAX_DAYS:
            return False

        # Quick velocity proxy — must be non-trivial
        if abs(market.velocity) < 5.0:
            return False

        return True

    # =========================================================================
    # evaluate — daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        """
        Confirm momentum is sustained and same-direction over both 5-min and 10-min windows.
        Signal direction = direction of momentum.

        YES side: buy when price moving UP (will go higher → YES profits)
        NO side:  buy when price moving DOWN (will go lower → NO profits)
        Never buy NO above 90¢ YES, never buy YES below 10¢ YES.
        """
        history = getattr(market, "price_history", [])

        vel_5m  = _calc_velocity_over(history, _SHORT_WINDOW_S)
        vel_10m = _calc_velocity_over(history, _LONG_WINDOW_S)

        # Both windows must agree on direction (same sign)
        if vel_5m == 0.0 or vel_10m == 0.0:
            return
        if (vel_5m > 0) != (vel_10m > 0):
            # Conflicting signals — momentum is not sustained
            logger.debug(
                "[TIDE] Conflicting momentum: %s vel_5m=%.1f%% vel_10m=%.1f%%",
                market.ticker, vel_5m, vel_10m,
            )
            return

        # Momentum score = combined absolute velocity
        momentum_score = abs(vel_5m) + abs(vel_10m)

        if momentum_score < _MIN_VELOCITY:
            logger.debug(
                "[TIDE] Insufficient momentum: %s score=%.1f%% (min=%.1f%%)",
                market.ticker, momentum_score, _MIN_VELOCITY,
            )
            return

        # Direction
        going_up = vel_5m > 0

        if going_up:
            # YES side: price rising → YES will profit
            side         = "yes"
            entry_price  = round(market.yes_price, 4)
            target_price = round(min(0.95, entry_price * (1 + _TARGET_GAIN)), 4)
            stop_price   = round(max(0.05, entry_price * 0.90), 4)
        else:
            # NO side: price falling → NO will profit (NO costs 1 - yes_price)
            side         = "no"
            no_price     = round(1.0 - market.yes_price, 4)
            entry_price  = round(market.yes_price, 4)   # pass YES price; build_signal computes NO
            target_price = round(max(0.05, market.yes_price * (1 - _TARGET_GAIN)), 4)
            stop_price   = round(min(0.95, market.yes_price * 1.10), 4)

        # Conviction tier
        if momentum_score >= 40.0:
            conviction_tier = "PROPHECY"
        elif momentum_score >= 25.0:
            conviction_tier = "HIGH_CONVICTION"
        else:
            conviction_tier = "GLITCH"

        edge_pct = round(momentum_score * 0.4, 2)   # rough edge proxy

        reasoning = (
            f"TIDE: {'UP' if going_up else 'DOWN'} momentum confirmed — "
            f"vel_5m={vel_5m:+.1f}%, vel_10m={vel_10m:+.1f}%, "
            f"score={momentum_score:.1f}%. "
            f"{'YES: buying rising contract.' if going_up else f'NO: buying falling contract (NO costs {round(1.0-market.yes_price,2):.2f}).'} "
            f"Target +{_TARGET_GAIN*100:.0f}%. Exit when momentum stalls."
        )

        signal = self.build_signal(
            market, conviction_tier, edge_pct, side,
            entry_price, target_price, stop_price, reasoning, game,
        )
        self.submit_signal(signal)
        logger.info(
            "[TIDE] Signal: %s %s momentum_score=%.1f%% tier=%s",
            market.ticker, side.upper(), momentum_score, conviction_tier,
        )
