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
MIN_VOLUME      = 1_000
MAX_SPREAD      = 0.04
MAX_DAYS        = 3
PRICE_THRESHOLD = 0.30   # YES below this → extreme LOW; above (1 - this) → extreme HIGH


class AxiomAgent(BaseAgent):
    """AXIOM — consensus momentum agent for non-tennis Kalshi markets."""

    name:       str       = "AXIOM"
    domain:     str       = "prediction"

    seed_rules: list[str] = [
        "Only trade when settlement is within 3 days and volume exceeds 1000",
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

        # AXIOM price gate — YES must be in 25¢-75¢ window.
        # Outside this: contract is near-settled, entry cost is too extreme,
        # no safe round-trip. Both sides must pay 25¢-75¢ for valid edge.
        if market.yes_price > 0.75 or market.yes_price < 0.25:
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
          vol_factor   = min(1.0, volume / 10_000)    # sports-scale: $10k = full weight
          edge_pct     = displacement * time_factor * vol_factor * 100
          macro boost  = +15-20% if asset class matches MacroLLM top_opportunity_class
        """
        # Normalize yes_price to 0..1 float (safety — market always returns 0..1 but belt-and-suspenders)
        yes_price = market.yes_price
        if yes_price > 1.0:
            yes_price = yes_price / 100.0

        # Belt-and-suspenders gate inside evaluate() — should_evaluate() already checked,
        # but guard again: reject near-settled contracts where there's no room to run
        if yes_price > 0.75 or yes_price < 0.25:
            logger.debug("[AXIOM] evaluate() gate: yes_price=%.2f outside 25-75¢ — skip", yes_price)
            return

        displacement = abs(yes_price - 0.5)
        days         = market.days_to_settlement

        time_factor  = max(0.1, 1.0 - days * 0.3)
        vol_factor   = min(1.0, market.volume_dollars / 10_000)  # sports-scale ($10k = full weight)
        edge_pct     = displacement * time_factor * vol_factor * 100.0

        # ── Macro overlay — boost edge if MacroLLM flags this asset class ────
        try:
            from signals.aggregate import get_snapshot
            signals     = get_snapshot()
            top_class   = signals.get("top_opportunity_class", "")
            fng         = int(signals.get("fng_value", 50) or 50)
            market_risk = signals.get("overall_market_risk", "MEDIUM") or "MEDIUM"
            series      = market.series_ticker.upper()

            if series.startswith("KXBTC") and top_class == "crypto":
                edge_pct *= 1.15
            elif series.startswith("KXETH") and top_class == "crypto":
                edge_pct *= 1.15
            elif series.startswith("KXWTI") and top_class == "oil":
                edge_pct *= 1.20
            elif series.startswith("KXFFR") and top_class == "rates":
                edge_pct *= 1.15

            # Extreme fear = contrarian buy signal for crypto
            if fng < 20 and series.startswith(("KXBTC", "KXETH")):
                edge_pct *= 1.10

            # High market risk = avoid political contracts (too noisy)
            if market_risk == "EXTREME" and series.startswith("KXPOL"):
                edge_pct *= 0.80

        except Exception:
            pass   # signals unavailable — continue with unmodified edge

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

        # ── Direction — always buy the CHEAP UNDERDOG side (25¢-35¢ per contract) ──
        # RULE: yes_mid ≥ 0.50 → YES is expensive → buy NO at cost = 1 - yes_price
        #       yes_mid < 0.50 → YES is cheap    → buy YES at cost = yes_price
        #
        # PROOF CHECK (logged every entry):
        #   yes=0.74 → side=no,  cost=0.26 ✓  (paying 26¢ for NO)
        #   yes=0.26 → side=yes, cost=0.26 ✓  (paying 26¢ for YES)
        #
        # entry_price convention: ALWAYS stores YES price in 0..1 decimal.
        # order_manager derives NO cost as (1 - entry_price) for PNL calc.
        if yes_price > 0.50:
            side           = "no"
            our_entry_cost = round(1.0 - yes_price, 4)   # what NO actually costs
            entry_price    = round(yes_price, 4)           # store YES price (convention)
            target_price   = round(min(0.95, our_entry_cost + displacement * 0.3), 3)
            stop_price     = round(max(0.05, our_entry_cost - 0.10), 3)
        else:
            side           = "yes"
            our_entry_cost = round(yes_price, 4)           # what YES actually costs
            entry_price    = round(yes_price, 4)
            target_price   = round(min(0.95, our_entry_cost + displacement * 0.3), 3)
            stop_price     = round(max(0.05, our_entry_cost - 0.10), 3)

        logger.info(
            "[AXIOM] evaluate: ticker=%s yes_mid=%.2f side=%s cost=%.2f "
            "edge=%.1f%% conv=%s days=%.0f",
            market.ticker, yes_price, side, our_entry_cost,
            edge_pct, conviction_tier, days,
        )

        reasoning = (
            f"AXIOM {side.upper()} | yes_mid={yes_price:.2f} | "
            f"cost={our_entry_cost:.2f} | edge={edge_pct:.1f}% | "
            f"conv={conviction_tier} | HTSR=True | "
            f"displacement={displacement:.1%} | {days}d | "
            f"vol=${market.volume_dollars:,.0f}"
        )

        signal = self.build_signal(
            market, conviction_tier, edge_pct, side,
            entry_price, target_price, stop_price, reasoning, game,
        )

        # ── AXIOM exit philosophy: hold-to-resolution, NOT a scalp ──────────
        # Override the default scalper exit params so the engine holds until
        # our side reaches 85¢-90¢ (price-based) rather than exiting at +20%.
        if signal:
            sig = signal["signal"]
            sig["target_exit_pct"]   = 2.0    # PCT path disabled; price logic takes over
            sig["stop_loss_pct"]     = 0.50   # backstop: -50% (our side at ~15¢)
            sig["max_hold_minutes"]  = 4320   # 3 days — never time-stop a consensus play
            sig["hold_to_settlement"] = True

        self.submit_signal(signal)

    # =========================================================================
    # should_exit — AXIOM price-threshold override (hold-to-resolution)
    # =========================================================================

    def should_exit(self, position, market, game=None) -> bool:
        """
        AXIOM hold-to-resolution exit logic.
        Uses absolute YES price thresholds, not percentage P&L.

        Exit rules (priority order):
          1. Win lock    — our side ≥ 90¢  → lock it immediately
          2. Danger cut  — other side ≥ 90¢ → settlement risk, cut loss
          3. Profit tgt  — our side ≥ 85¢  → take the profit
          4. Stop loss   — our side ≤ 15¢  → paid ~30¢, down ~50%, cut
          5. Time stop   — underwater with < 1hr to settlement

        Deliberately does NOT flag at +20% P&L — that's a scalp, not the strategy.
        """
        if market is None:
            return False

        # market.yes_price is 0..1 float — safety normalize if somehow in cents
        yes_price = market.yes_price
        if yes_price > 1.0:
            yes_price = yes_price / 100.0

        # Compute our side and actual entry cost
        # NO holder profits as YES falls (our_side = 1 - YES rises toward 1.0)
        # YES holder profits as YES rises toward 1.0
        if position.side == "no":
            our_side   = 1.0 - yes_price
            their_side = yes_price
            our_entry  = (100 - position.entry_price) / 100.0  # actual NO cost paid
        else:
            our_side   = yes_price
            their_side = 1.0 - yes_price
            our_entry  = position.entry_price / 100.0           # actual YES cost paid

        pnl_pct = (our_side - our_entry) / our_entry if our_entry > 0 else 0.0

        logger.info(
            "[AXIOM] should_exit: side=%s yes=%.2f our_side=%.2f "
            "our_entry=%.2f pnl=%.0f%%",
            position.side, yes_price, our_side, our_entry, pnl_pct * 100,
        )

        if our_side >= 0.90:
            logger.info("[AXIOM] EXIT: win locked — our_side=%.0f%%", our_side * 100)
            return True
        if their_side >= 0.90:
            logger.info("[AXIOM] EXIT: settlement risk — their_side=%.0f%%", their_side * 100)
            return True
        if our_side >= 0.85:
            logger.info("[AXIOM] EXIT: profit target — our_side=%.0f%%", our_side * 100)
            return True
        if our_side <= 0.15:
            logger.info("[AXIOM] EXIT: stop loss — our_side=%.0f%%", our_side * 100)
            return True

        # Time stop — only if genuinely underwater ≥ 20% with <1hr to settlement
        minutes_left = market.days_to_settlement * 1440.0
        if minutes_left < 60 and pnl_pct < -0.20:
            logger.info(
                "[AXIOM] EXIT: time stop — underwater=%.0f%% minutes_left=%.0f",
                pnl_pct * 100, minutes_left,
            )
            return True

        return False  # HOLD — thesis intact
