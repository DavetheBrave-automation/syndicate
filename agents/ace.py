"""
ace.py — ACE Tennis Specialist Trading Agent.

Detects mispriced Kalshi ATP/WTA match-winner contracts using Markov true
probability vs. Kalshi implied price. Signals HIGH_CONVICTION and PROPHECY
edges; ignores match points, low volume, extreme prices, and volatile
late-set windows.

All connector / playbook imports are lazy (inside evaluate()) to avoid
circular import issues at module load time.
"""

import logging
from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.ace")

# ---------------------------------------------------------------------------
# Set-score helper
# ---------------------------------------------------------------------------

def _sets_won(set_scores: list) -> tuple:
    """
    Count (p1_sets, p2_sets) from a list of (p1_games, p2_games) tuples.

    A set is considered complete (won) when:
      - One player has >= 6 games AND leads by >= 2, OR
      - One player has exactly 7 games (tiebreak win).

    The final entry in set_scores is the current (in-progress) set and is
    NOT counted as a won set.
    """
    p1_sets = 0
    p2_sets = 0

    if not set_scores:
        return 0, 0

    # All entries except the last are completed sets.
    for g1, g2 in set_scores[:-1]:
        if (g1 >= 6 and g1 >= g2 + 2) or g1 == 7:
            p1_sets += 1
        elif (g2 >= 6 and g2 >= g1 + 2) or g2 == 7:
            p2_sets += 1

    return p1_sets, p2_sets


# ---------------------------------------------------------------------------
# AceAgent
# ---------------------------------------------------------------------------

class AceAgent(BaseAgent):
    """
    ACE — Tennis specialist. Trades ATP/WTA match-winner markets on Kalshi.

    Edge detection:
      - Markov true probability vs. Kalshi implied price.
      - Minimum 12% edge required for entry.
      - PROPHECY tier at >= 20% edge, HIGH_CONVICTION at >= 12%.
    """

    name       = "ACE"
    domain     = "tennis"
    seed_rules = [
        "Only buy YES when Markov true probability exceeds Kalshi price by 12%+",
        "Never enter in final 3 games of a set — price too volatile",
        "Clay surface: reduce edge threshold by 2% for baseline specialists",
        "Never buy trailing player down 2 sets in best-of-3",
        "Volume must exceed 1000 before any entry",
    ]

    # =========================================================================
    # should_evaluate — HOT PATH, <1ms, NO I/O
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        """
        Pre-filter on the hot path. No file I/O beyond the once-per-60s bench
        recheck already handled inside _base_should_evaluate.

        Returns False immediately when any gate fails.
        """
        # 1. Base checks (bench, contract_class == WATCH, volume <= 0)
        if not self._base_should_evaluate(market):
            return False

        # 2. Series must be ATP or WTA match-winner only
        if market.series_ticker not in {"KXATPMATCH", "KXWTAMATCH"}:
            return False

        # 3. Liquidity gate
        if market.volume_dollars < 1_000:
            return False

        # 4. Extreme price filter — not tradeable at edges of the curve
        if market.yes_price <= 0.05 or market.yes_price >= 0.95:
            return False

        return True

    # =========================================================================
    # evaluate — called in daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        """
        Full signal evaluation. Lazy-imports connectors/playbook here to
        prevent circular imports at module load time.
        """
        # Lazy imports — connectors and playbook only loaded on first call
        from connectors.tennis_ws import match_game_to_ticker  # noqa: PLC0415

        # 1. Resolve live game if not provided
        if game is None:
            game = match_game_to_ticker(market.ticker)
            if game is None:
                logger.debug("[ACE] No live game found for ticker=%s", market.ticker)
                return

        # 2. Never trade match points — prices are wildly volatile
        if game.is_match_point:
            logger.debug("[ACE] Skipping match point | ticker=%s", market.ticker)
            return

        # 3. Core probability values
        true_prob      = game.true_probability      # Markov model, pre-computed by TennisWS
        kalshi_implied = market.yes_price
        edge_pct       = (true_prob - kalshi_implied) * 100.0

        # 4. Minimum edge threshold (hardcoded; source of truth is seed_rules)
        MIN_EDGE = 12.0

        # 5. Rule: never buy trailing player down 2 sets in best-of-3
        #    If p1_sets == 0 and p2_sets == 2, buying YES (player 1) is forbidden.
        set_scores = getattr(game, "set_scores", None) or []
        p1_sets_won, p2_sets_won = _sets_won(set_scores)
        if p1_sets_won == 0 and p2_sets_won == 2 and true_prob > kalshi_implied:
            logger.debug(
                "[ACE] Skipping — player 1 down 0-2 sets, would buy YES | ticker=%s",
                market.ticker,
            )
            return

        # 6. Rule: never enter in final 3 games of a set.
        #    Interpreted conservatively: either player has >= 4 games in the
        #    current set (approaching 6, price is volatile).
        if set_scores:
            p1_games_cur = set_scores[-1][0]
            p2_games_cur = set_scores[-1][1]
        else:
            p1_games_cur = 0
            p2_games_cur = 0

        if max(p1_games_cur, p2_games_cur) >= 4:
            logger.debug(
                "[ACE] Skipping — final 3 games of set (games=%d-%d) | ticker=%s",
                p1_games_cur, p2_games_cur, market.ticker,
            )
            return

        # 7. Conviction tier based on edge magnitude
        if edge_pct >= 20.0:
            conviction_tier = "PROPHECY"
        elif edge_pct >= MIN_EDGE:
            conviction_tier = "HIGH_CONVICTION"
        else:
            logger.debug(
                "[ACE] Insufficient edge %.1f%% < %.1f%% | ticker=%s",
                edge_pct, MIN_EDGE, market.ticker,
            )
            return

        # 8. Side: ACE only buys YES (seed_rule 1 — true_prob > kalshi_implied guaranteed here)
        side        = "yes"
        entry_price = market.yes_price

        # 9. Target and stop prices
        target_price = round(min(0.95, true_prob + 0.05), 3)
        stop_price   = round(max(0.05, kalshi_implied - 0.10), 3)

        # 10. Human-readable reasoning
        reasoning = (
            f"Markov: {true_prob:.1%} true vs {kalshi_implied:.1%} Kalshi"
            f" — {edge_pct:.1f}% edge"
            f" | {game.player1} vs {game.player2}"
        )

        # 11. Build signal (base class assembles the full dict)
        signal = self.build_signal(
            market,
            conviction_tier,
            edge_pct,
            side,
            entry_price,
            target_price,
            stop_price,
            reasoning,
            game,
        )

        # 12. Submit — writes trigger file atomically
        self.submit_signal(signal)
