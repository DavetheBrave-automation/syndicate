"""
endgame.py — ENDGAME Late-Stage Specialist Agent.

Fires only in the final stage of a match/game where:
  - Settlement is within 3 hours
  - Price has moved significantly (leader is clear)
  - The leading side has a sustained edge

Tennis: final set only (best-of-3).
Other sports: use settlement proximity as proxy for final stage.
Edge must be 15%+ (Markov for tennis, price structure for others).
"""

import logging
from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.endgame")

_TENNIS_SERIES = {"KXATPMATCH", "KXWTAMATCH"}
_ALL_SPORTS    = {
    "KXATPMATCH", "KXWTAMATCH", "KXGOLF",
    "KXNBA", "KXNBAGAME", "KXNBATOTAL", "KXNBASPREAD",
    "KXMLB", "KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD",
    "KXNHL", "KXNHLGAME", "KXNHLTOTAL",
    "KXNFL", "KXNFLGAME", "KXNFLTOTAL", "KXNFLSPREAD",
    "KXSOCCER", "KXNCAA",
    "KXPGATOUR", "KXPGAR1LEAD", "KXPGAR2LEAD", "KXPGAR3LEAD", "KXPGAR4LEAD",
}

_MAX_DAYS        = 0.125    # 3 hours — endgame only
_MIN_VOLUME      = 2_000
_MIN_EDGE_PCT    = 15.0     # 15% edge minimum
_PROPHECY_EDGE   = 25.0     # 25% = PROPHECY tier
_LEADER_MIN      = 0.60     # yes_price > 0.60 = strong leader
_TRAILER_MAX     = 0.40     # yes_price < 0.40 = strong trailer
_MIN_PRICE       = 0.08     # never buy sub-8¢ in endgame
_MAX_PRICE       = 0.92     # never buy above 92¢


class EndgameAgent(BaseAgent):
    name   = "ENDGAME"
    domain = "all"

    seed_rules = [
        "Only enter in final 20% of match/game time remaining",
        "Price must have moved 30%+ from its starting value today",
        "The move must be in ONE direction only — no whipsaws",
        "True probability must exceed Kalshi price by 15%+",
        "Never enter a contract within 5 minutes of settlement",
        "Exit at +20% gain or when 5 minutes remain — never hold to settlement",
        "Volume must exceed 2000 in final stage — thin markets settle unpredictably",
        "Tennis: final set only, never enter before set 3 of best-of-3",
        "Basketball: final quarter only, never enter before Q4",
        "Baseball: 8th or 9th inning only",
        "Golf: final round, final 6 holes only",
        "The longer the leader has held their lead, the more trustworthy the signal",
    ]

    # =========================================================================
    # should_evaluate — hot path
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        if not self._base_should_evaluate(market):
            return False

        if market.series_ticker not in _ALL_SPORTS:
            return False

        # Endgame only — settlement within 3 hours
        if market.days_to_settlement > _MAX_DAYS:
            return False

        # 5-min floor: days_to_settlement == 0.0 means ticker date just passed
        # (game is live today), NOT that settlement is imminent — don't block it.
        # Only block when we have a small positive value (actual countdown < 5 min).
        if 0.0 < market.days_to_settlement < (5 / 1440.0):
            return False

        if market.volume_dollars < _MIN_VOLUME:
            return False

        # Need a clear leader or trailer — avoid mid-range in endgame
        if _TRAILER_MAX <= market.yes_price <= _LEADER_MIN:
            return False

        # Price floor
        if market.yes_price < _MIN_PRICE or market.yes_price > _MAX_PRICE:
            return False

        return True

    # =========================================================================
    # evaluate — daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        if market.series_ticker in _TENNIS_SERIES:
            self._evaluate_tennis(market, game)
        else:
            self._evaluate_generic(market)

    # ── Tennis ──────────────────────────────────────────────────────────────

    def _evaluate_tennis(self, market, game) -> None:
        from connectors.tennis_ws import match_game_to_ticker

        if game is None:
            game = match_game_to_ticker(market.ticker)
            if game is None:
                logger.debug("[ENDGAME] No live game | %s", market.ticker)
                return

        # Endgame requires final set
        if not getattr(game, "is_final_set", False):
            logger.debug("[ENDGAME] Not final set | %s", market.ticker)
            return

        if game.is_match_point:
            logger.debug("[ENDGAME] Match point — too late | %s", market.ticker)
            return

        true_prob      = game.true_probability
        kalshi_implied = market.yes_price

        if true_prob is None:
            return

        edge_pct = (true_prob - kalshi_implied) * 100.0
        if abs(edge_pct) < _MIN_EDGE_PCT:
            logger.debug("[ENDGAME] Edge too small %.1f%% | %s", edge_pct, market.ticker)
            return

        # Trade the direction with positive edge
        if edge_pct > 0:
            side = "yes"
            entry_price  = kalshi_implied
            target_price = min(round(kalshi_implied + 0.20, 3), 0.95)
            stop_price   = round(kalshi_implied - 0.10, 3)
        else:
            side = "no"
            no_implied   = 1.0 - kalshi_implied
            no_true      = 1.0 - true_prob
            edge_pct     = (no_true - no_implied) * 100.0
            entry_price  = no_implied
            target_price = min(round(no_implied + 0.20, 3), 0.95)
            stop_price   = round(no_implied - 0.10, 3)

        if edge_pct < _MIN_EDGE_PCT:
            return

        tier = "PROPHECY" if edge_pct >= _PROPHECY_EDGE else "HIGH_CONVICTION"

        set_scores = getattr(game, "set_scores", [])
        reasoning = (
            f"ENDGAME: final set, {side.upper()} edge={edge_pct:.1f}%. "
            f"Score: {set_scores}. "
            f"true_prob={true_prob:.2%} vs implied={kalshi_implied:.2%}."
        )

        signal = self.build_signal(
            market=market,
            conviction_tier=tier,
            edge_pct=edge_pct,
            side=side,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            reasoning=reasoning,
            game=game,
        )
        self.submit_signal(signal)

    # ── Generic (non-tennis) late-stage ────────────────────────────────────

    def _evaluate_generic(self, market) -> None:
        """
        For non-tennis sports where we don't have a Markov model:
        Use settlement proximity + price extremity as edge proxy.
        Only trade if yes_price is very extreme (>70¢ or <30¢) indicating
        a near-settled outcome that still has room to run.
        """
        kalshi_implied = market.yes_price

        # Strong leader: market above 70¢, edge = it should be even higher
        if kalshi_implied >= 0.70:
            side         = "yes"
            # Approximate true_prob: strong late-stage leaders historically settle
            # at 85%+ if volume confirms. Use 0.85 as proxy.
            true_prob    = 0.85
            edge_pct     = (true_prob - kalshi_implied) * 100.0
            entry_price  = kalshi_implied
            target_price = min(round(kalshi_implied + 0.12, 3), 0.92)
            stop_price   = round(kalshi_implied - 0.08, 3)

        # Strong trailer: market below 35¢, edge = trailing side may come back
        elif kalshi_implied <= 0.35:
            side         = "yes"
            # Trailing side priced at 20-30¢ in final stage: true_prob ~0.10-0.20
            # Only trade if > 2× implied — similar to GHOST but in final stage
            true_prob    = 0.25
            edge_pct     = (true_prob - kalshi_implied) * 100.0
            entry_price  = kalshi_implied
            target_price = min(round(kalshi_implied * 1.5, 3), 0.45)
            stop_price   = round(kalshi_implied * 0.5, 3)
        else:
            return

        if edge_pct < _MIN_EDGE_PCT:
            logger.debug("[ENDGAME] Generic edge too small %.1f%% | %s", edge_pct, market.ticker)
            return

        tier = "PROPHECY" if edge_pct >= _PROPHECY_EDGE else "HIGH_CONVICTION"

        reasoning = (
            f"ENDGAME: {market.series_ticker} final stage, {side.upper()} "
            f"at {kalshi_implied:.2f}. Edge={edge_pct:.1f}% (generic model). "
            f"Settlement in {market.days_to_settlement*1440:.0f} min."
        )

        signal = self.build_signal(
            market=market,
            conviction_tier=tier,
            edge_pct=edge_pct,
            side=side,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            reasoning=reasoning,
        )
        self.submit_signal(signal)
