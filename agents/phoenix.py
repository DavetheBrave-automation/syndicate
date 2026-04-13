"""
phoenix.py — PHOENIX Comeback Specialist Trading Agent.

Detects mispricings on Kalshi relative to historical win probability:
  - Trailing team YES: market underprices comeback → buy YES
  - Overpriced favorite NO: market overprices the leader → buy NO

Supports: tennis (ATP/WTA), baseball (MLB), basketball (NBA).

Edge:
  YES edge = (true_prob - yes_price)  × 100  [trailing team, yes_price < 0.45]
  NO  edge = (yes_price - true_prob)  × 100  [overpriced fav, yes_price > 0.55]
Minimum 12% edge required. PROPHECY at 20%+.
"""

import logging
from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.phoenix")

# ---------------------------------------------------------------------------
# Series → sport routing
# ---------------------------------------------------------------------------

_TENNIS_SERIES     = {"KXATPMATCH", "KXWTAMATCH"}
_BASEBALL_SERIES   = {"KXMLBGAME"}
_BASKETBALL_SERIES = {"KXNBAGAME"}

_ALL_SERIES = _TENNIS_SERIES | _BASEBALL_SERIES | _BASKETBALL_SERIES

# ---------------------------------------------------------------------------
# Historical comeback probability tables
# ---------------------------------------------------------------------------

# Key: (run_deficit, innings_remaining)  →  P(trailing team wins)
# Source: historical MLB run expectancy / win probability matrices.
BASEBALL_COMEBACK: dict[tuple, float] = {
    # innings_remaining = 9 (top of 1st)
    (1, 9): 0.43, (2, 9): 0.35, (3, 9): 0.27,
    (4, 9): 0.20, (5, 9): 0.14, (6, 9): 0.10,
    # innings_remaining = 6 (end of 3rd)
    (1, 6): 0.38, (2, 6): 0.28, (3, 6): 0.20,
    (4, 6): 0.13, (5, 6): 0.08, (6, 6): 0.05,
    # innings_remaining = 3 (top of 7th — hard bail below)
    (1, 3): 0.28, (2, 3): 0.16, (3, 3): 0.09,
    (4, 3): 0.05, (5, 3): 0.03, (6, 3): 0.01,
    # innings_remaining = 1 (top of 9th)
    (1, 1): 0.15, (2, 1): 0.07, (3, 1): 0.03,
    (4, 1): 0.01, (5, 1): 0.005,
}

# Key: (point_deficit, minutes_remaining)  →  P(trailing team wins)
# Source: historical NBA win probability by deficit / time.
BASKETBALL_COMEBACK: dict[tuple, float] = {
    # 48 min remaining (game start)
    (3, 48): 0.47, (5, 48): 0.40, (10, 48): 0.29, (15, 48): 0.20, (20, 48): 0.13,
    # 36 min remaining (end of 1st quarter)
    (3, 36): 0.45, (5, 36): 0.36, (10, 36): 0.24, (15, 36): 0.15, (20, 36): 0.09,
    # 24 min remaining (halftime)
    (3, 24): 0.40, (5, 24): 0.30, (10, 24): 0.18, (15, 24): 0.10, (20, 24): 0.05,
    # 12 min remaining (end of 3rd quarter)
    (3, 12): 0.33, (5, 12): 0.22, (10, 12): 0.11, (15, 12): 0.05, (20, 12): 0.02,
    # 4 min remaining
    (3,  4): 0.22, (5,  4): 0.12, (10,  4): 0.04, (15,  4): 0.01,
}

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _sets_won(set_scores: list) -> tuple:
    """
    Count (p1_sets_won, p2_sets_won) from all completed sets.
    All entries except the last are treated as completed sets.
    """
    p1, p2 = 0, 0
    if not set_scores:
        return 0, 0
    for g1, g2 in set_scores[:-1]:
        if (g1 >= 6 and g1 >= g2 + 2) or g1 == 7:
            p1 += 1
        elif (g2 >= 6 and g2 >= g1 + 2) or g2 == 7:
            p2 += 1
    return p1, p2


def _nearest_baseball_prob(deficit: int, innings_remaining: int) -> float:
    """
    Look up baseball comeback probability using nearest-neighbour matching
    on both deficit and innings_remaining dimensions.
    """
    if innings_remaining <= 0 or deficit <= 0:
        return 0.0

    deficit           = min(deficit, 6)
    innings_remaining = min(innings_remaining, 9)

    available_innings  = sorted({k[1] for k in BASEBALL_COMEBACK})
    nearest_inn        = min(available_innings, key=lambda x: abs(x - innings_remaining))

    available_deficits = sorted({k[0] for k in BASEBALL_COMEBACK if k[1] == nearest_inn})
    if not available_deficits:
        return 0.0
    nearest_def        = min(available_deficits, key=lambda x: abs(x - deficit))

    return BASEBALL_COMEBACK.get((nearest_def, nearest_inn), 0.0)


def _nearest_basketball_prob(deficit: int, minutes_remaining: float) -> float:
    """
    Look up basketball comeback probability using nearest-neighbour matching
    on both deficit and minutes_remaining dimensions.
    """
    if minutes_remaining <= 0 or deficit <= 0:
        return 0.0

    deficit           = min(deficit, 20)
    minutes_remaining = min(minutes_remaining, 48.0)

    available_minutes  = sorted({k[1] for k in BASKETBALL_COMEBACK})
    nearest_min        = min(available_minutes, key=lambda x: abs(x - minutes_remaining))

    available_deficits = sorted({k[0] for k in BASKETBALL_COMEBACK if k[1] == nearest_min})
    if not available_deficits:
        return 0.0
    nearest_def        = min(available_deficits, key=lambda x: abs(x - deficit))

    return BASKETBALL_COMEBACK.get((nearest_def, nearest_min), 0.0)


def _price_to_baseball_deficit(yes_price: float) -> int:
    """
    Estimate run deficit from Kalshi YES price for trailing team.
    Lower price → larger implied deficit.
    """
    if yes_price <= 0.10:
        return 6
    if yes_price <= 0.20:
        return 5
    if yes_price <= 0.30:
        return 4
    if yes_price <= 0.35:
        return 3
    if yes_price <= 0.42:
        return 2
    return 1


def _price_to_basketball_deficit(yes_price: float) -> int:
    """
    Estimate point deficit from Kalshi YES price for trailing team.
    """
    if yes_price <= 0.10:
        return 20
    if yes_price <= 0.15:
        return 15
    if yes_price <= 0.25:
        return 10
    if yes_price <= 0.35:
        return 7
    if yes_price <= 0.42:
        return 5
    return 3   # slightly trailing (0.42–0.45)


# ---------------------------------------------------------------------------
# PhoenixAgent
# ---------------------------------------------------------------------------

class PhoenixAgent(BaseAgent):
    """
    PHOENIX — Comeback specialist. Trades trailing sides in live markets.

    Buys YES on trailing team/player when historical comeback probability
    exceeds the Kalshi market price by ≥ 12%. PROPHECY at ≥ 20% edge.
    """

    name       = "PHOENIX"
    domain     = "all"
    seed_rules = [
        "YES side (trailing team): yes_price < 0.45 — minimum 12% edge vs true comeback prob",
        "NO side (overpriced favorite): yes_price > 0.55 — minimum 12% edge when market overprices leader",
        "Skip 0.45–0.55 zone — too close to call, edge is unreliable near 50¢",
        "Tennis YES: never trade after player goes down 0-2 sets in best-of-3",
        "Baseball YES: never enter after 7th inning — variance too low to recover",
        "Basketball YES: never enter if down 15+ points with under 4 minutes remaining",
        "Higher confidence on bigger deficits with more time remaining (deep value zone)",
        "Exit immediately if price snaps back 8%+ toward true prob",
        "Never pyramid into a losing position — one position per market only",
        "Volume must exceed 1000 for any trade (paper mode threshold)",
    ]

    # =========================================================================
    # should_evaluate — HOT PATH, <1ms, NO I/O
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        if not self._base_should_evaluate(market):
            return False

        # Only supported sport series
        if market.series_ticker not in _ALL_SERIES:
            return False

        # Tradeable range: 25¢–75¢
        # YES < 0.45  → trailing team opportunity (YES edge)
        # YES > 0.55  → overpriced favorite opportunity (NO edge)
        # 0.45–0.55   → too close to call, skip
        if market.yes_price < 0.25 or market.yes_price > 0.75:
            return False

        if 0.45 <= market.yes_price <= 0.55:
            return False

        # Volume gate
        if market.volume_dollars < 1_000:
            return False

        return True

    # =========================================================================
    # evaluate — called in daemon thread
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        series = market.series_ticker

        if series in _TENNIS_SERIES:
            self._evaluate_tennis(market, game)
        elif series in _BASEBALL_SERIES:
            self._evaluate_baseball(market)
        elif series in _BASKETBALL_SERIES:
            self._evaluate_basketball(market)

    # ── Tennis ──────────────────────────────────────────────────────────────

    def _evaluate_tennis(self, market, game) -> None:
        from connectors.tennis_ws import match_game_to_ticker  # noqa: PLC0415

        if game is None:
            game = match_game_to_ticker(market.ticker)
            if game is None:
                logger.debug("[PHOENIX] No live tennis game for ticker=%s", market.ticker)
                return

        # Bail: player down 0-2 sets in best-of-3 — comeback is effectively over
        set_scores = getattr(game, "set_scores", None) or []
        p1_sets, p2_sets = _sets_won(set_scores)
        if p1_sets == 0 and p2_sets == 2:
            logger.debug(
                "[PHOENIX] Tennis bail — player down 0-2 sets | ticker=%s", market.ticker
            )
            return

        # Use TennisWS pre-computed Markov probability (already accounts for match state)
        true_prob      = game.true_probability
        kalshi_implied = market.yes_price

        if kalshi_implied < 0.45:
            # ── Trailing player — buy YES ──────────────────────────────────────
            edge_pct = (true_prob - kalshi_implied) * 100.0
            if edge_pct < 12.0:
                logger.debug(
                    "[PHOENIX] Insufficient tennis YES edge %.1f%% | ticker=%s", edge_pct, market.ticker
                )
                return
            side         = "yes"
            entry_price  = round(kalshi_implied, 4)
            target_price = round(min(0.90, true_prob + 0.05), 3)
            stop_price   = round(max(0.05, entry_price - 0.08), 3)
            reasoning = (
                f"PHOENIX tennis trailing: Markov {true_prob:.1%} vs Kalshi {kalshi_implied:.1%}"
                f" — YES edge {edge_pct:.1f}%"
                f" | {game.player1} vs {game.player2} | sets {p1_sets}-{p2_sets}"
            )

        else:
            # ── Overpriced favorite — buy NO ───────────────────────────────────
            edge_pct = (kalshi_implied - true_prob) * 100.0
            if edge_pct < 12.0:
                logger.debug(
                    "[PHOENIX] Insufficient tennis NO edge %.1f%% | ticker=%s", edge_pct, market.ticker
                )
                return
            side         = "no"
            entry_price  = round(kalshi_implied, 4)        # YES price; build_signal derives NO cost
            no_cost      = round(1.0 - kalshi_implied, 4)
            target_price = round(max(0.05, no_cost + (kalshi_implied - true_prob) * 0.3), 3)
            stop_price   = round(min(0.95, no_cost + 0.08), 3)
            reasoning = (
                f"PHOENIX tennis overpriced fav: Kalshi {kalshi_implied:.1%} vs Markov {true_prob:.1%}"
                f" — NO edge {edge_pct:.1f}%"
                f" | {game.player1} vs {game.player2} | sets {p1_sets}-{p2_sets}"
            )

        conviction_tier = "PROPHECY" if edge_pct >= 20.0 else "HIGH_CONVICTION"
        signal = self.build_signal(
            market, conviction_tier, edge_pct, side,
            entry_price, target_price, stop_price, reasoning, game,
        )
        self.submit_signal(signal)

    # ── Baseball ─────────────────────────────────────────────────────────────

    def _evaluate_baseball(self, market) -> None:
        kalshi_implied = market.yes_price

        # Bail heuristic: wide spread → late innings, variance collapsing
        if market.spread > 0.15:
            logger.debug(
                "[PHOENIX] Baseball bail — spread %.3f suggests late innings | ticker=%s",
                market.spread, market.ticker,
            )
            return

        deficit           = _price_to_baseball_deficit(kalshi_implied)
        hours_to_settle   = market.days_to_settlement * 24.0

        # Estimate innings remaining from time to settlement.
        # MLB games run ~3 hours. Sub-0.25h → deep into 9th, bail.
        if hours_to_settle > 2.0:
            innings_remaining = 9
        elif hours_to_settle > 1.0:
            innings_remaining = 6
        elif hours_to_settle > 0.25:
            innings_remaining = 3
        else:
            innings_remaining = 1   # 9th inning territory — too late

        # Bail: past 7th inning estimate
        if innings_remaining < 3:
            logger.debug(
                "[PHOENIX] Baseball bail — estimated past 7th (innings_rem=%d) | ticker=%s",
                innings_remaining, market.ticker,
            )
            return

        true_prob = _nearest_baseball_prob(deficit, innings_remaining)

        if kalshi_implied < 0.45:
            # ── Trailing team — buy YES ────────────────────────────────────────
            edge_pct = (true_prob - kalshi_implied) * 100.0
            if edge_pct < 12.0:
                logger.debug(
                    "[PHOENIX] Insufficient baseball YES edge %.1f%% | ticker=%s", edge_pct, market.ticker
                )
                return
            side         = "yes"
            entry_price  = round(kalshi_implied, 4)
            target_price = round(min(0.90, true_prob + 0.05), 3)
            stop_price   = round(max(0.05, entry_price - 0.08), 3)
            reasoning = (
                f"PHOENIX baseball trailing: hist_prob={true_prob:.1%} vs Kalshi={kalshi_implied:.1%}"
                f" — YES edge {edge_pct:.1f}%"
                f" | deficit={deficit} runs, innings_rem={innings_remaining}"
            )

        else:
            # ── Overpriced favorite — buy NO ───────────────────────────────────
            edge_pct = (kalshi_implied - true_prob) * 100.0
            if edge_pct < 12.0:
                logger.debug(
                    "[PHOENIX] Insufficient baseball NO edge %.1f%% | ticker=%s", edge_pct, market.ticker
                )
                return
            side         = "no"
            entry_price  = round(kalshi_implied, 4)        # YES price; build_signal derives NO cost
            no_cost      = round(1.0 - kalshi_implied, 4)
            target_price = round(max(0.05, no_cost + (kalshi_implied - true_prob) * 0.3), 3)
            stop_price   = round(min(0.95, no_cost + 0.08), 3)
            reasoning = (
                f"PHOENIX baseball overpriced fav: Kalshi={kalshi_implied:.1%} vs hist_prob={true_prob:.1%}"
                f" — NO edge {edge_pct:.1f}%"
                f" | deficit={deficit} runs, innings_rem={innings_remaining}"
            )

        conviction_tier = "PROPHECY" if edge_pct >= 20.0 else "HIGH_CONVICTION"
        signal = self.build_signal(
            market, conviction_tier, edge_pct, side,
            entry_price, target_price, stop_price, reasoning,
        )
        self.submit_signal(signal)

    # ── Basketball ───────────────────────────────────────────────────────────

    def _evaluate_basketball(self, market) -> None:
        kalshi_implied    = market.yes_price
        deficit           = _price_to_basketball_deficit(kalshi_implied)
        hours_to_settle   = market.days_to_settlement * 24.0
        minutes_remaining = min(48.0, hours_to_settle * 60.0)

        # Bail: down 15+ points with under 4 minutes — statistically over
        if deficit >= 15 and minutes_remaining < 4.0:
            logger.debug(
                "[PHOENIX] Basketball bail — down %d+ pts with %.1f min left | ticker=%s",
                deficit, minutes_remaining, market.ticker,
            )
            return

        true_prob = _nearest_basketball_prob(deficit, minutes_remaining)

        if kalshi_implied < 0.45:
            # ── Trailing team — buy YES ────────────────────────────────────────
            edge_pct = (true_prob - kalshi_implied) * 100.0
            if edge_pct < 12.0:
                logger.debug(
                    "[PHOENIX] Insufficient basketball YES edge %.1f%% | ticker=%s", edge_pct, market.ticker
                )
                return
            side         = "yes"
            entry_price  = round(kalshi_implied, 4)
            target_price = round(min(0.90, true_prob + 0.05), 3)
            stop_price   = round(max(0.05, entry_price - 0.08), 3)
            reasoning = (
                f"PHOENIX basketball trailing: hist_prob={true_prob:.1%} vs Kalshi={kalshi_implied:.1%}"
                f" — YES edge {edge_pct:.1f}%"
                f" | deficit={deficit}pts, min_rem={minutes_remaining:.0f}"
            )

        else:
            # ── Overpriced favorite — buy NO ───────────────────────────────────
            edge_pct = (kalshi_implied - true_prob) * 100.0
            if edge_pct < 12.0:
                logger.debug(
                    "[PHOENIX] Insufficient basketball NO edge %.1f%% | ticker=%s", edge_pct, market.ticker
                )
                return
            side         = "no"
            entry_price  = round(kalshi_implied, 4)        # YES price; build_signal derives NO cost
            no_cost      = round(1.0 - kalshi_implied, 4)
            target_price = round(max(0.05, no_cost + (kalshi_implied - true_prob) * 0.3), 3)
            stop_price   = round(min(0.95, no_cost + 0.08), 3)
            reasoning = (
                f"PHOENIX basketball overpriced fav: Kalshi={kalshi_implied:.1%} vs hist_prob={true_prob:.1%}"
                f" — NO edge {edge_pct:.1f}%"
                f" | deficit={deficit}pts, min_rem={minutes_remaining:.0f}"
            )

        conviction_tier = "PROPHECY" if edge_pct >= 20.0 else "HIGH_CONVICTION"
        signal = self.build_signal(
            market, conviction_tier, edge_pct, side,
            entry_price, target_price, stop_price, reasoning,
        )
        self.submit_signal(signal)
