"""
ghost.py — GHOST Long-Shot Specialist Agent.

Targets contracts priced under 12 cents where the Markov model (tennis) or
market structure suggests the true probability is at least 2× the implied price.
Small fixed sizing — pure moonshot layer, never the primary signal.

Conviction: always tier 1 ($1 max bet). Exit at 3× entry or never.
Domain: tennis (ATP + WTA). Other sports require live game integration.
"""

import logging
from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.ghost")

_TENNIS_SERIES = {"KXATPMATCH", "KXWTAMATCH"}
_GHOST_MAX_PRICE   = 0.12   # only buy under 12¢
_GHOST_MIN_VOLUME  = 1_000
_GHOST_MIN_DAYS    = 0.083  # ~2 hours
_GHOST_EDGE_MULT   = 2.0    # true_prob must be >= 2× implied


class GhostAgent(BaseAgent):
    name   = "GHOST"
    domain = "all"
    _skip_base_price_gate = True  # GHOST trades sub-10¢ contracts intentionally

    seed_rules = [
        "Only buy contracts priced under 12 cents — pure long shots",
        "True probability must be at least 2x the Kalshi price",
        "Max bet $1 regardless of conviction — small sizing on moonshots",
        "Never buy a contract under 10 cents with less than 2 hours to settlement",
        "Volume must exceed 1000 — even ghosts need liquidity",
        "Only buy YES side — never short a long shot",
        "Exit at 3x entry price or better — never settle for less",
        "Never hold more than 2 Ghost positions simultaneously",
        "Sports only — no crypto long shots, too efficient",
        "If price has already moved 50%+ today — skip, party already started",
    ]

    # =========================================================================
    # should_evaluate — hot path
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        if not self._base_should_evaluate(market):
            return False

        # Tennis only until other sport game integrations exist
        if market.series_ticker not in _TENNIS_SERIES:
            return False

        # Long-shot price gate
        if market.yes_price > _GHOST_MAX_PRICE:
            return False

        # Extra-tight floor for very low prices — need time on the clock
        if market.yes_price < 0.03 and market.days_to_settlement < _GHOST_MIN_DAYS:
            return False

        if market.volume_dollars < _GHOST_MIN_VOLUME:
            return False

        return True

    # =========================================================================
    # evaluate — daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        from connectors.tennis_ws import match_game_to_ticker

        if game is None:
            game = match_game_to_ticker(market.ticker)
            if game is None:
                logger.debug("[GHOST] No live game for %s", market.ticker)
                return

        # Don't trade at match point — price will zero out instantly
        if game.is_match_point:
            logger.debug("[GHOST] Skipping match point | %s", market.ticker)
            return

        true_prob      = game.true_probability
        kalshi_implied = market.yes_price

        if true_prob is None or true_prob <= 0:
            logger.debug("[GHOST] No true_prob available | %s", market.ticker)
            return

        # Must be ≥ 2× implied for a long shot to be worth it
        if true_prob < kalshi_implied * _GHOST_EDGE_MULT:
            logger.debug(
                "[GHOST] Edge too weak | %s | implied=%.3f true=%.3f ratio=%.1f",
                market.ticker, kalshi_implied, true_prob,
                true_prob / kalshi_implied if kalshi_implied > 0 else 0,
            )
            return

        edge_pct = (true_prob - kalshi_implied) * 100.0

        # Target: 3× entry price
        target_price = min(round(kalshi_implied * 3.0, 3), 0.90)
        stop_price   = round(kalshi_implied * 0.4, 3)   # exit if it halves again

        reasoning = (
            f"GHOST: long shot at {kalshi_implied:.2f}¢ with true_prob={true_prob:.2%} "
            f"({true_prob/kalshi_implied:.1f}× implied). "
            f"Edge={edge_pct:.1f}%. Target {target_price:.2f}."
        )

        signal = self.build_signal(
            market=market,
            conviction_tier="HIGH_CONVICTION",
            edge_pct=edge_pct,
            side="yes",
            entry_price=kalshi_implied,
            target_price=target_price,
            stop_price=stop_price,
            reasoning=reasoning,
            game=game,
        )

        # Force max_size to $1 — ghost bets are always $1
        signal["signal"]["max_size_dollars"] = 1

        self.submit_signal(signal)
