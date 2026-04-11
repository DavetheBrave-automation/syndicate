"""
axiom.py — AXIOM pure math agent for Syndicate.

Strategy: Consensus Momentum Near Settlement
AXIOM trades general non-tennis Kalshi prediction markets (economic data,
sports totals, politics, etc.). It bets WITH the crowd in high-volume liquid
markets where extreme prices and imminent settlement make the consensus
statistically reliable.

Signal logic:
  - Only act when settlement is ≤ 3 days and volume > $50,000
  - Only act at price extremes: YES > 0.70 or YES < 0.30
  - Spread must be ≤ 0.04 (tight spread = liquid market)
  - Edge = displacement * time_factor * vol_factor * 100
  - PROPHECY tier: same-day settlement + ≥35% displacement + edge ≥ 15%
  - HIGH_CONVICTION tier: edge ≥ 7%

AXIOM never touches tennis markets — those belong to ACE.
"""

import logging

from agents.base_agent import BaseAgent

logger = logging.getLogger("syndicate.axiom")

# Tennis series tickers owned by ACE
_TENNIS_SERIES = {"KXATPMATCH", "KXWTAMATCH"}

MIN_EDGE        = 7.0
MIN_VOLUME      = 50_000
MAX_SPREAD      = 0.04
MAX_DAYS        = 3
PRICE_THRESHOLD = 0.30   # YES below this → extreme LOW; above (1 - this) → extreme HIGH


class AxiomAgent(BaseAgent):
    """AXIOM — consensus momentum agent for non-tennis Kalshi markets."""

    name:       str       = "AXIOM"
    domain:     str       = "prediction"
    seed_rules: list[str] = [
        "Only trade when settlement is within 3 days and volume exceeds 50000",
        "Bet WITH market consensus at extreme prices (>70% YES or <30% YES)",
        "Spread must be 0.04 or less — wide spreads erase mathematical edge",
        "PROPHECY tier reserved for same-day settlement with 35%+ displacement from 0.5",
        "Never trade tennis markets — ACE owns that domain",
    ]

    # =========================================================================
    # Hot path — should_evaluate
    # =========================================================================

    def should_evaluate(self, market, game=None) -> bool:
        """
        Fast gate — no I/O, must complete in <1ms.

        Checks (in order):
          1. BaseAgent pre-checks (bench state, contract_class, volume > 0)
          2. Skip tennis series tickers
          3. Settlement must be ≤ 3 days
          4. Volume must exceed $50,000
          5. Spread must be ≤ 0.04
          6. Price must be at an extreme (< 0.30 or > 0.70)
        """
        if not self._base_should_evaluate(market):
            return False

        if market.series_ticker in _TENNIS_SERIES:
            return False

        if market.days_to_settlement > MAX_DAYS:
            return False

        if market.volume_dollars < MIN_VOLUME:
            return False

        if market.spread > MAX_SPREAD:
            return False

        # Price in the neutral zone [0.30, 0.70] — no edge
        if PRICE_THRESHOLD <= market.yes_price <= (1.0 - PRICE_THRESHOLD):
            return False

        return True

    # =========================================================================
    # evaluate — daemon thread, full computation
    # =========================================================================

    def evaluate(self, market, game=None) -> None:
        """
        Compute math edge and submit signal if threshold is met.

        Edge formula:
          displacement = abs(yes_price - 0.5)        # 0.20 – 0.50 after filter
          time_factor  = max(0.1, 1.0 - days * 0.3)  # 1.0 → 0.7 → 0.4 → 0.1
          vol_factor   = min(1.0, volume / 100_000)   # caps at full-volume markets
          edge_pct     = displacement * time_factor * vol_factor * 100
        """
        displacement = abs(market.yes_price - 0.5)
        days         = market.days_to_settlement

        time_factor  = max(0.1, 1.0 - days * 0.3)
        vol_factor   = min(1.0, market.volume_dollars / 100_000)
        edge_pct     = displacement * time_factor * vol_factor * 100.0

        # ── Conviction tier ──────────────────────────────────────────────────
        if days == 0 and displacement >= 0.35 and edge_pct >= 15.0:
            conviction_tier = "PROPHECY"
        elif edge_pct >= MIN_EDGE:
            conviction_tier = "HIGH_CONVICTION"
        else:
            logger.debug(
                "[AXIOM] edge too low — ticker=%s edge=%.2f%% (min=%.1f%%)",
                market.ticker, edge_pct, MIN_EDGE,
            )
            return

        # ── Direction — bet WITH extreme-price consensus ─────────────────────
        if market.yes_price > 0.5:
            side         = "yes"
            entry_price  = round(market.yes_price, 4)
            target_price = round(min(0.95, market.yes_price + displacement * 0.3), 3)
            stop_price   = round(max(0.05, market.yes_price - 0.10), 3)
        else:
            side         = "no"
            entry_price  = round(1.0 - market.yes_price, 4)
            target_price = round(min(0.95, entry_price + displacement * 0.3), 3)
            stop_price   = round(max(0.05, entry_price - 0.10), 3)

        reasoning = (
            f"Consensus: yes_price={market.yes_price:.2f} "
            f"(displacement={displacement:.1%}), {days}d to settlement, "
            f"vol=${market.volume_dollars:,.0f} — math edge {edge_pct:.1f}%"
        )

        signal = self.build_signal(
            market, conviction_tier, edge_pct, side,
            entry_price, target_price, stop_price, reasoning, game,
        )
        self.submit_signal(signal)
